"""place_order.py — la ÚNICA vía autorizada para ejecutar órdenes de trading.

Ningún otro script de este proyecto debe llamar directamente a
EtoroClient.open_position_by_amount()/close_position(). Toda apertura o
cierre pasa por acá para que las reglas de negocio no-negociables se apliquen
siempre, sin excepción:

  1. NUNCA reintentar un POST de trading. Un error en un POST (HTTPError,
     ConnectionError, Timeout, EtoroUnknownOutcomeError, etc.) NO prueba que
     la orden no se haya ejecutado del lado de eToro — ver el docstring de
     EtoroClient.request(). Ante un error así, este script journalea el
     resultado como AMBIGUO (con instrucción explícita de verificar vía
     get_pnl(), cache 60s) y termina con exit 1, sin reintentar ni disparar
     una orden equivalente.
  2. Long-only: IsBuy=True y Leverage=1 son los defaults del cliente HTTP;
     este script nunca los sobreescribe (YAGNI — no exponemos shorts ni
     apalancamiento).
  3. DRY_RUN por default (fail-safe): si la env var falta o es distinta de
     "0", no se llama a la API en absoluto (ni siquiera GETs de resolución).
  4. Stop-loss real: StopLossRate es un PRECIO absoluto, no un porcentaje
     (docs/api-notes.md). Se calcula a partir del cierre de la última vela
     diaria: stop_loss_rate = round(precio * (1 - stop_loss_pct), 4). Si no
     se puede obtener el precio, no se abre la posición (exit 1).
  5. No se cierran posiciones sintéticas/pendientes ("pending-open:..." de
     snapshot.py, o "local-open:..." de este mismo script — ver regla 8): son
     órdenes pendientes o exposición todavía no confirmada, no posiciones
     reales cerrables — bloqueado con exit 2.
  6. Guard de cash: para abrir, amount > cashUsd del state bloquea con exit 2
     (aunque el % de portfolio diera OK, no hay cash real disponible).
  7. Exit codes: 0 = ejecutada u OK en dry-run | 2 = bloqueada (riesgo o
     validación) | 1 = error de ejecución (incluye resultado ambiguo).
  8. Registro de exposición intra-corrida: tras un open EXITOSO o de
     resultado AMBIGUO (asumido ejecutado, fail-closed) se reescribe
     state/positions.json atómicamente, agregando una posición sintética
     {"positionId": "local-open:<uuid4>", "symbol", "instrumentId",
     "valueUsd": amount, "pending": true} y restando amount de cashUsd.
     get_pnl() cachea 60s del lado de eToro: sin este registro, una segunda
     orden en la misma corrida (antes del próximo snapshot) leería un state
     desactualizado y podría pasar los topes de riesgo (25% posición, 35%
     cripto) muy por encima de lo real. Tras un close NO se toca el state
     (la posición sigue ahí hasta el próximo snapshot — a propósito, es
     conservador: borrarla relajaría el cálculo de drawdown).
  9. El precio de la vela usado para el stop-loss se valida: finito y > 0
     (si no, exit 1 sin abrir). Además se exige frescura: la vela no puede
     tener más de 5 días de antigüedad según su 'fromDate' (tolerancia para
     fines de semana largos); si 'fromDate' falta o no parsea, también
     exit 1 sin abrir.
  10. Frescura del state: si positions.json.updatedAt tiene más de 24h (o
      falta/no parsea), se bloquea con exit 2 — correr snapshot.py primero.
      Aplica tanto a open como a close.
  11. Al cerrar, --symbol debe coincidir (normalizado a upper) con el symbol
      real de la posición en el state — mismatch bloquea (exit 2) sin llamar
      a la API.
  12. Cualquier crash al crear el cliente HTTP (credenciales faltantes,
      etc.) se journalea como ERROR y termina en exit 1 — nunca un
      traceback pelado.
  13. Si search_instrument devuelve más de un match exacto para el mismo
      símbolo, se bloquea (exit 1) en vez de tomar el primero silenciosamente
      — una ambigüedad de instrumentId no se resuelve arbitrariamente.
  14. Si search_instrument NO devuelve ningún match exacto para el símbolo
      pedido y ese símbolo es cripto conocido (risk.CRYPTO_SEARCH_VARIANTS),
      se reintenta con las variantes de formato conocidas (p.ej. BTCUSD para
      BTC) en orden, usando la primera variante con match exacto y no
      ambiguo. Se journalea/loguea (stderr) qué variante se usó.
  15. Presupuesto de órdenes en código (WP1, auditoría pre-producción: antes
      "máximo 3 órdenes por corrida" vivía solo en PLAYBOOK.md, en prosa, sin
      nada que lo hiciera cumplir). state/.run_orders.json trackea dos
      contadores: por ETOROAGENT_RUN_ID (env var que runner.sh exporta por
      corrida) hasta MAX_ORDERS_PER_RUN, y por día calendario (hora local)
      hasta MAX_ORDERS_PER_DAY -- este último aplica SIEMPRE, incluso en
      invocaciones manuales sin ETOROAGENT_RUN_ID seteada (ahí el tope por
      corrida no se evalúa). Se chequea ANTES de abrir o cerrar (open y
      close); si cualquiera de los dos topes está agotado, exit 2 sin tocar
      el cliente HTTP. Se incrementa SOLO al ejecutar efectivamente una
      orden (dry-run, real, o de resultado AMBIGUO) -- los bloqueos (exit 2,
      sea por riesgo o por este mismo presupuesto) NUNCA consumen
      presupuesto. Escritura atómica (tmp + os.replace), mismo patrón que
      _atomic_write_json().

Toda decisión (ejecutada, bloqueada, dry-run, ambigua, error) se journalea en
state/journal.md con timestamp en hora local con offset y la razón pasada
por --reason.
"""
import argparse
import csv
import json
import math
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from etoro_api import (  # noqa: E402
    AmbiguousMatchError,
    EtoroClient,
    extract_exact_match,
)
from risk import CRYPTO_SEARCH_VARIANTS, OrderRequest, validate  # noqa: E402

STATE_DIR = Path(__file__).resolve().parent.parent / "state"
POSITIONS_FILE = "positions.json"
EQUITY_FILE = "equity.csv"
JOURNAL_FILE = "journal.md"

CANDLE_MAX_AGE_DAYS = 5
STATE_MAX_AGE_HOURS = 24

# Prefijos de positionId que identifican posiciones sintéticas/no confirmadas
# (no existen como posición real y cerrable del lado de eToro):
#   "pending-open:"  -> aperturas pendientes vistas por snapshot.py (ver su docstring).
#   "local-open:"     -> exposición registrada localmente por este script tras un
#                         open exitoso o ambiguo (ver regla 8), hasta el próximo snapshot.
SYNTHETIC_POSITION_PREFIXES = ("pending-open:", "local-open:")


# -- State / journal (I/O puro y fino) -------------------------------------


def _read_equity_rows(path: Path) -> list:
    """Lee equity.csv como lista de (fecha, valor). Ídem criterio de
    snapshot.py: fila malformada -> excepción (fail-closed, no hay valores
    inventados)."""
    rows = []
    with open(path, newline="") as f:
        reader = csv.reader(f)
        next(reader, None)  # header
        for row in reader:
            if not row:
                continue
            date, value = row[0], row[1]
            rows.append((date, float(value)))
    return rows


def load_state(state_dir: Path) -> tuple:
    """Lee state/positions.json y state/equity.csv. Fail-closed: ausentes o
    corruptos -> excepción clara. state ausente = no hubo snapshot = no
    operar (el caller de esta función lo convierte en exit 1)."""
    positions_path = state_dir / POSITIONS_FILE
    equity_path = state_dir / EQUITY_FILE

    if not positions_path.exists():
        raise FileNotFoundError(
            f"no existe {positions_path}: no hubo snapshot, no se puede operar "
            "(correr scripts/snapshot.py primero)."
        )
    try:
        with open(positions_path) as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"{positions_path} corrupto/ilegible: {exc}") from exc

    if not isinstance(state, dict) or "positions" not in state or "cashUsd" not in state:
        raise ValueError(
            f"{positions_path} con formato inesperado (falta 'positions' o 'cashUsd')."
        )

    if not equity_path.exists():
        raise FileNotFoundError(
            f"no existe {equity_path}: no hubo snapshot, no se puede operar "
            "(correr scripts/snapshot.py primero)."
        )
    try:
        equity_rows = _read_equity_rows(equity_path)
    except (ValueError, OSError) as exc:
        raise ValueError(f"{equity_path} corrupto/ilegible: {exc}") from exc

    return state, equity_rows


def journal(state_dir: Path, line: str) -> None:
    """Appendea una línea al journal con timestamp en hora LOCAL del
    sistema y offset explícito (ej: 2026-08-05 19:03 -0300), para que el
    journal quede legible sin conversión mental y consistente con los
    nombres de reports/ (que scripts/runner.sh también stampea en hora
    local — ver PLAYBOOK.md, sección Cierre de corrida). Crea state_dir y
    state_dir/journal.md si no existen — incluido el caso en que state_dir
    nunca existió (p.ej. nunca corrió snapshot.py): dejar rastro en el
    journal de un intento de orden bloqueado por falta de state vale más que
    la pureza de no tocar el filesystem. Puede lanzar OSError si ni siquiera
    esto se puede escribir (permisos, disco lleno) — el caller decide si
    eso también es fatal o si el mensaje en stderr ya alcanza."""
    state_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %z")
    with open(state_dir / JOURNAL_FILE, "a") as f:
        f.write(f"- {timestamp} {line}\n")


def _atomic_write_json(path: Path, data: dict) -> None:
    """Mismo patrón que snapshot.py: tmp + os.replace para que un crash a
    mitad de escritura nunca deje positions.json truncado/corrupto."""
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, path)


# -- Presupuesto de órdenes por corrida y por día (WP1) ---------------------
#
# Auditoría pre-producción: "máximo 3 órdenes por corrida" vivía solo en
# PLAYBOOK.md (prosa), sin nada en código que lo hiciera cumplir. Acá se
# aplica en código, con dos contadores independientes en un único archivo:
#   - "count"/"runId": por ETOROAGENT_RUN_ID (una corrida real del agente,
#     ver runner.sh). Solo se evalúa/incrementa si la env var está seteada.
#   - "dailyCount"/"date": por día calendario LOCAL (mismo criterio que
#     journal(), para quedar legible junto al resto de los timestamps).
#     Aplica siempre, con o sin ETOROAGENT_RUN_ID.
ORDER_BUDGET_FILE = ".run_orders.json"
MAX_ORDERS_PER_RUN = 3
MAX_ORDERS_PER_DAY = 6


def _today_local() -> str:
    """Fecha calendario LOCAL del sistema (no UTC) -- mismo criterio que
    journal(), para que el corte de "día" del presupuesto coincida con el
    día que ve un humano leyendo el journal/los nombres de reports/."""
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def _load_order_budget(state_dir: Path) -> dict:
    """Lee state/.run_orders.json. Fail-safe (no fail-closed): archivo
    ausente, corrupto, o con campos de tipo inesperado -> presupuesto
    "vacío" (contadores en 0), nunca una excepción -- un presupuesto
    ilegible no debe tumbar toda la corrida, a diferencia de positions.json
    (regla de negocio distinta: esto es un contador operativo, no el
    estado real del portfolio)."""
    default = {"runId": None, "count": 0, "date": None, "dailyCount": 0}
    path = state_dir / ORDER_BUDGET_FILE
    if not path.exists():
        return default
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return default
    if not isinstance(data, dict):
        return default
    count = data.get("count", 0)
    daily_count = data.get("dailyCount", 0)
    return {
        "runId": data.get("runId"),
        "count": count if isinstance(count, int) and not isinstance(count, bool) else 0,
        "date": data.get("date"),
        "dailyCount": (
            daily_count
            if isinstance(daily_count, int) and not isinstance(daily_count, bool)
            else 0
        ),
    }


def _normalize_order_budget(budget: dict, run_id, today: str) -> dict:
    """Resetea (SIN persistir -- ver _check_order_budget/_consume_order_budget)
    el contador por corrida si cambió el ETOROAGENT_RUN_ID, y el contador
    diario si cambió la fecha calendario local. Si run_id es None (invocación
    manual), el contador por corrida NO se toca -- ese caso no participa del
    tope por corrida en absoluto (ver _check_order_budget)."""
    budget = dict(budget)
    if run_id is not None and budget.get("runId") != run_id:
        budget["runId"] = run_id
        budget["count"] = 0
    if budget.get("date") != today:
        budget["date"] = today
        budget["dailyCount"] = 0
    return budget


def _save_order_budget(state_dir: Path, budget: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(state_dir / ORDER_BUDGET_FILE, budget)


def _check_order_budget(state_dir: Path) -> str:
    """Solo lectura: no incrementa ni persiste nada (eso es
    _consume_order_budget, llamado recién cuando una orden se ejecuta de
    verdad). Devuelve un mensaje de bloqueo si el presupuesto por corrida
    (con ETOROAGENT_RUN_ID seteada) o el diario (siempre) está agotado, o
    "" si hay presupuesto disponible."""
    run_id = os.environ.get("ETOROAGENT_RUN_ID") or None
    budget = _normalize_order_budget(_load_order_budget(state_dir), run_id, _today_local())

    if run_id is not None and budget["count"] >= MAX_ORDERS_PER_RUN:
        return f"tope de {MAX_ORDERS_PER_RUN} órdenes por corrida alcanzado"
    if budget["dailyCount"] >= MAX_ORDERS_PER_DAY:
        return f"presupuesto diario de {MAX_ORDERS_PER_DAY} órdenes agotado"
    return ""


def _consume_order_budget(state_dir: Path) -> None:
    """Incrementa el presupuesto tras UNA orden efectivamente ejecutada
    (dry-run, real, o de resultado AMBIGUO -- ver docstring del módulo,
    regla 15). Los bloqueos (exit 2) nunca llegan a llamar esto."""
    run_id = os.environ.get("ETOROAGENT_RUN_ID") or None
    budget = _normalize_order_budget(_load_order_budget(state_dir), run_id, _today_local())
    if run_id is not None:
        budget["count"] = budget["count"] + 1
    budget["dailyCount"] = budget["dailyCount"] + 1
    _save_order_budget(state_dir, budget)


def _register_local_exposure(
    state_dir: Path, state: dict, symbol: str, instrument_id, amount: float
) -> str:
    """Regla de negocio 8: tras un open EXITOSO o de resultado AMBIGUO
    (asumido ejecutado, fail-closed), registra la exposición localmente en
    positions.json — agrega una posición sintética y resta amount de
    cashUsd. Devuelve el positionId sintético generado.

    No toca 'updatedAt': ese campo refleja la frescura del último snapshot
    REAL contra eToro (regla 10) — un registro local no es un snapshot, y
    envejecer 'updatedAt' artificialmente ocultaría que la data real de
    eToro puede seguir sin refrescar.

    La entrada generada usa el mismo shape que las posiciones sintéticas de
    snapshot.py ({"pending": true}) para que risk.validate() y el guard de
    posiciones sintéticas en _handle_close() las traten igual sin lógica
    adicional."""
    position_id = f"local-open:{uuid.uuid4()}"
    new_state = dict(state)
    new_state["positions"] = list(state.get("positions", [])) + [
        {
            "positionId": position_id,
            "symbol": symbol,
            "instrumentId": instrument_id,
            "valueUsd": amount,
            "pending": True,
        }
    ]
    new_state["cashUsd"] = state["cashUsd"] - amount
    _atomic_write_json(state_dir / POSITIONS_FILE, new_state)
    return position_id


def _safe_register_local_exposure(
    state_dir: Path, state: dict, symbol: str, instrument_id, amount: float
) -> None:
    """Wrapper best-effort de _register_local_exposure: la orden YA fue
    enviada (exitosa o ambigua) cuando esto se llama, así que un fallo acá
    (disco lleno, permisos) no debe esconder ese resultado ni cambiar el
    exit code — solo advertir en stderr de que el próximo snapshot es más
    urgente que de costumbre, porque el state en disco quedó desactualizado."""
    try:
        _register_local_exposure(state_dir, state, symbol, instrument_id, amount)
    except Exception as exc:
        print(
            f"WARN: la orden se envió pero no se pudo registrar la exposición local en "
            f"{POSITIONS_FILE}: {exc}. Corré snapshot.py cuanto antes.",
            file=sys.stderr,
        )


def _parse_iso_datetime(raw) -> datetime:
    """Parsea una fecha ISO 8601 (tolera sufijo 'Z'). Usado tanto para
    positions.json.updatedAt (regla 10) como para candle.fromDate (regla 9)
    — misma lógica, un solo lugar. Ausente/no-string/no-parseable -> ValueError."""
    if not raw or not isinstance(raw, str):
        raise ValueError(f"fecha ausente o no parseable: {raw!r}")
    normalized = raw.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"fecha no parseable: {raw!r} ({exc})") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _check_state_freshness(state: dict) -> str:
    """Regla 10. Devuelve un mensaje de bloqueo si el state está stale (o su
    updatedAt falta/no parsea), o "" si está fresco."""
    try:
        updated_at = _parse_iso_datetime(state.get("updatedAt"))
    except ValueError as exc:
        return f"state stale: corré snapshot primero (updatedAt ausente o no parseable: {exc})"
    age = datetime.now(timezone.utc) - updated_at
    if age > timedelta(hours=STATE_MAX_AGE_HOURS):
        return (
            f"state stale: corré snapshot primero (updatedAt de hace "
            f"{age.total_seconds() / 3600:.1f}h, máx {STATE_MAX_AGE_HOURS}h)"
        )
    return ""


def make_client() -> EtoroClient:
    """Factory del cliente HTTP real. Mockeable: main() acepta un make_client
    alternativo (p.ej. lambda: MagicMock()) para tests sin red."""
    return EtoroClient()


def _is_dry_run() -> bool:
    """Fail-safe: si la env var falta o es != '0', dry-run."""
    return os.environ.get("DRY_RUN") != "0"


def _is_finite_positive(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0


# -- CLI ---------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="place_order.py")
    sub = parser.add_subparsers(dest="action", required=True)

    p_open = sub.add_parser("open", help="abrir posición")
    p_open.add_argument("--symbol", required=True)
    p_open.add_argument("--amount", required=True, type=float)
    p_open.add_argument("--stop-loss-pct", required=True, type=float, dest="stop_loss_pct")
    p_open.add_argument("--reason", default="")

    p_close = sub.add_parser("close", help="cerrar posición")
    p_close.add_argument("--position-id", required=True, dest="position_id")
    p_close.add_argument("--symbol", required=True)
    p_close.add_argument("--reason", default="")

    return parser


def _resolve_instrument_id(client, symbol: str):
    """Resuelve symbol -> internalInstrumentId vía search_instrument() +
    etoro_api.extract_exact_match() (docs/api-notes.md: el search es FUZZY,
    nunca hardcodear ids ni tomar items[0] a ciegas).

    Regla 13: si hay más de un match exacto para el símbolo ORIGINAL pedido,
    no se prueba ninguna variante — se aborta ya (AmbiguousMatchError
    re-lanzada tal cual). Una ambigüedad de instrumentId no se resuelve
    arbitrariamente ni se interpreta como "formato de símbolo distinto".

    Fallback de variantes cripto (Task 10, fix reviewer #4): si la búsqueda
    exacta del símbolo pedido no da NINGÚN match (NoExactMatchError), y el
    símbolo es una clave conocida de `risk.CRYPTO_SEARCH_VARIANTS` (eToro
    puede exponer el instrumento bajo un formato distinto, p.ej. BTCUSD en
    vez de BTC), se prueban las variantes conocidas EN ORDEN y se usa el
    primer match EXACTO y NO AMBIGUO — una variante ambigua o sin match se
    descarta (no se adivina) y se sigue probando con la siguiente."""
    try:
        match = extract_exact_match(client.search_instrument(symbol), symbol)
        return match["internalInstrumentId"]
    except AmbiguousMatchError:
        raise
    except ValueError:
        pass  # NoExactMatchError (u otro ValueError del helper): probar variantes.

    for variant in CRYPTO_SEARCH_VARIANTS.get(symbol, []):
        if variant == symbol:
            continue
        try:
            match = extract_exact_match(client.search_instrument(variant), variant)
        except ValueError:
            continue  # sin match o ambigua en esta variante: probar la siguiente.
        print(
            f"INFO: {symbol} resuelto via variante de busqueda {variant}",
            file=sys.stderr,
        )
        return match["internalInstrumentId"]

    raise ValueError(f"símbolo {symbol} no encontrado en search_instrument")


def _resolve_current_price(client, instrument_id) -> float:
    """Precio de cierre de la última vela diaria, para calcular el
    StopLossRate absoluto (docs/api-notes.md: StopLossRate es un PRECIO, no
    un %). Regla 9: valida que el precio sea finito y > 0, y que la vela no
    tenga más de CANDLE_MAX_AGE_DAYS de antigüedad según su 'fromDate' — si
    'fromDate' falta o no parsea, también se rechaza (fail-closed: sin
    fecha no se puede confirmar frescura)."""
    resp = client.get_candles(instrument_id, direction="desc", interval="OneDay", count=1)
    candle = resp["candles"][0]["candles"][0]
    price = candle.get("close")
    if not _is_finite_positive(price):
        raise ValueError(f"precio de la vela inválido (no finito o <= 0): {price!r}")

    from_date_raw = candle.get("fromDate")
    from_date = _parse_iso_datetime(from_date_raw)  # ValueError si falta/no parsea
    age = datetime.now(timezone.utc) - from_date
    if age > timedelta(days=CANDLE_MAX_AGE_DAYS):
        raise ValueError(
            f"vela desactualizada: fromDate={from_date_raw!r} (antigüedad "
            f"{age.total_seconds() / 86400:.1f} días, máx {CANDLE_MAX_AGE_DAYS})"
        )

    return float(price)


def _handle_open(args, state: dict, equity_rows: list, state_dir: Path, client_factory) -> int:
    symbol = args.symbol.strip().upper()
    amount = args.amount
    order = OrderRequest(
        action="open", symbol=symbol, amount_usd=amount, stop_loss_pct=args.stop_loss_pct
    )

    ok, msg = validate(order, state, equity_rows)
    if not ok:
        journal(
            state_dir,
            f"BLOQUEADA | open {symbol} amount={amount} | {msg} | razon={args.reason}",
        )
        print(f"BLOQUEADA: {msg}", file=sys.stderr)
        return 2

    cash_usd = state["cashUsd"]
    if amount > cash_usd:
        msg = f"monto {amount} supera el cash disponible ({cash_usd})"
        journal(
            state_dir,
            f"BLOQUEADA | open {symbol} amount={amount} | {msg} | razon={args.reason}",
        )
        print(f"BLOQUEADA: {msg}", file=sys.stderr)
        return 2

    if _is_dry_run():
        journal(
            state_dir,
            f"DRY_RUN | open {symbol} amount={amount} stop_loss_pct={args.stop_loss_pct} "
            f"| razon={args.reason}",
        )
        print(f"DRY_RUN: no se ejecuta open {symbol} amount={amount}")
        _consume_order_budget(state_dir)
        return 0

    try:
        client = client_factory()
    except Exception as exc:
        journal(
            state_dir,
            f"ERROR | open {symbol} amount={amount} | no se pudo crear el cliente eToro: "
            f"{exc} | razon={args.reason}",
        )
        print(f"ERROR: no se pudo crear el cliente eToro: {exc}", file=sys.stderr)
        return 1

    try:
        instrument_id = _resolve_instrument_id(client, symbol)
    except Exception as exc:
        journal(
            state_dir,
            f"ERROR | open {symbol} amount={amount} | no se pudo resolver instrumentId: "
            f"{exc} | razon={args.reason}",
        )
        print(f"ERROR: no se pudo resolver instrumentId para {symbol}: {exc}", file=sys.stderr)
        return 1

    try:
        price = _resolve_current_price(client, instrument_id)
    except Exception as exc:
        journal(
            state_dir,
            f"ERROR | open {symbol} amount={amount} instrument_id={instrument_id} | "
            f"no se pudo obtener un precio válido y fresco para el stop-loss: {exc} | "
            f"razon={args.reason}",
        )
        print(f"ERROR: no se pudo obtener precio para {symbol}: {exc}", file=sys.stderr)
        return 1

    stop_loss_rate = round(price * (1 - args.stop_loss_pct), 4)

    try:
        # POST de trading: NUNCA reintentar (ver docstring del módulo, regla 1).
        result = client.open_position_by_amount(
            instrument_id=instrument_id, amount_usd=amount, stop_loss_rate=stop_loss_rate
        )
    except Exception as exc:
        # Regla 8: un error acá NO prueba que la orden no se haya ejecutado
        # (regla 1) — registramos la exposición igual, fail-closed, para que
        # órdenes subsiguientes en la misma corrida no ignoren este monto.
        _safe_register_local_exposure(state_dir, state, symbol, instrument_id, amount)
        journal(
            state_dir,
            f"AMBIGUO | open {symbol} amount={amount} instrument_id={instrument_id} "
            f"stop_loss_rate={stop_loss_rate} | la orden PUDO haberse ejecutado igual "
            f"pese al error: {exc} | VERIFICAR vía get_pnl() (cache 60s) antes de asumir "
            f"fallo o de reintentar | se registró la exposición localmente (fail-closed) "
            f"| razon={args.reason}",
        )
        print(
            f"AMBIGUO: error tras enviar la orden de apertura, verificar con get_pnl() "
            f"antes de reintentar: {exc}",
            file=sys.stderr,
        )
        _consume_order_budget(state_dir)
        return 1

    _safe_register_local_exposure(state_dir, state, symbol, instrument_id, amount)
    journal(
        state_dir,
        f"ABIERTA | open {symbol} amount={amount} instrument_id={instrument_id} "
        f"stop_loss_rate={stop_loss_rate} resultado={result} | razon={args.reason}",
    )
    print(f"ABIERTA: {symbol} amount={amount} stop_loss_rate={stop_loss_rate}")
    _consume_order_budget(state_dir)
    return 0


def _handle_close(args, state: dict, state_dir: Path, client_factory) -> int:
    # BUG CRÍTICO (hallado en la primera corrida dry-run real): snapshot.py
    # persiste positionId tal cual lo devuelve la API — que puede ser un
    # ENTERO (p.ej. 3533695059) — mientras que argparse siempre entrega
    # --position-id como STRING. Comparar sin normalizar (int == str) da
    # SIEMPRE False, así que ninguna posición real era cerrable: toda salida
    # por regla (SMA200, ranking) quedaba inoperante. Normalizamos ambos
    # lados a str().strip() para la búsqueda/comparación, pero el valor que
    # se manda a la API (más abajo, `real_position_id`) es el ORIGINAL del
    # state, sin castear — para preservar el tipo que la API espera.
    position_id = str(args.position_id).strip()
    symbol = args.symbol.strip().upper()

    if position_id.startswith(SYNTHETIC_POSITION_PREFIXES):
        msg = (
            f"{position_id} es una orden pendiente/exposición registrada localmente, "
            "no una posición real cerrable"
        )
        journal(state_dir, f"BLOQUEADA | close {position_id} {symbol} | {msg} | razon={args.reason}")
        print(f"BLOQUEADA: {msg}", file=sys.stderr)
        return 2

    entry = next(
        (
            p
            for p in state.get("positions", [])
            if str(p.get("positionId")).strip() == position_id
        ),
        None,
    )
    if entry is None:
        msg = f"position-id {position_id} no existe en el state"
        journal(state_dir, f"BLOQUEADA | close {position_id} {symbol} | {msg} | razon={args.reason}")
        print(f"BLOQUEADA: {msg}", file=sys.stderr)
        return 2

    if entry.get("pending") is True:
        # Defensa en profundidad: cualquier entrada marcada pending (aunque su
        # positionId no matchee SYNTHETIC_POSITION_PREFIXES por algún motivo)
        # tampoco es una posición real cerrable.
        msg = f"{position_id} está marcada como pendiente en el state, no es cerrable"
        journal(state_dir, f"BLOQUEADA | close {position_id} {symbol} | {msg} | razon={args.reason}")
        print(f"BLOQUEADA: {msg}", file=sys.stderr)
        return 2

    entry_symbol = str(entry.get("symbol", "")).strip().upper()
    if entry_symbol != symbol:
        msg = (
            f"--symbol {symbol} no coincide con el symbol real de la posición "
            f"{position_id} en el state ({entry_symbol!r})"
        )
        journal(state_dir, f"BLOQUEADA | close {position_id} {symbol} | {msg} | razon={args.reason}")
        print(f"BLOQUEADA: {msg}", file=sys.stderr)
        return 2

    # El valor real que se manda a la API es el ORIGINAL del state (puede
    # ser int, p.ej. 3533695059), NUNCA el string normalizado de arriba —
    # ver el comentario del bug critico al inicio de esta funcion.
    real_position_id = entry["positionId"]
    instrument_id = entry["instrumentId"]

    if _is_dry_run():
        journal(
            state_dir,
            f"DRY_RUN | close {position_id} {symbol} instrument_id={instrument_id} "
            f"| razon={args.reason}",
        )
        print(f"DRY_RUN: no se ejecuta close {position_id} {symbol}")
        _consume_order_budget(state_dir)
        return 0

    try:
        client = client_factory()
    except Exception as exc:
        journal(
            state_dir,
            f"ERROR | close {position_id} {symbol} | no se pudo crear el cliente eToro: "
            f"{exc} | razon={args.reason}",
        )
        print(f"ERROR: no se pudo crear el cliente eToro: {exc}", file=sys.stderr)
        return 1

    try:
        # POST de trading: NUNCA reintentar (ver docstring del módulo, regla 1).
        result = client.close_position(position_id=real_position_id, instrument_id=instrument_id)
    except Exception as exc:
        journal(
            state_dir,
            f"AMBIGUO | close {position_id} {symbol} instrument_id={instrument_id} | "
            f"el cierre PUDO haberse ejecutado igual pese al error: {exc} | VERIFICAR vía "
            f"get_pnl() (cache 60s) antes de asumir fallo o de reintentar | razon={args.reason}",
        )
        print(
            f"AMBIGUO: error tras enviar el cierre, verificar con get_pnl() antes de "
            f"reintentar: {exc}",
            file=sys.stderr,
        )
        _consume_order_budget(state_dir)
        return 1

    journal(
        state_dir,
        f"CERRADA | close {position_id} {symbol} instrument_id={instrument_id} "
        f"resultado={result} | razon={args.reason}",
    )
    print(f"CERRADA: {position_id} {symbol}")
    _consume_order_budget(state_dir)
    return 0


def main(argv=None, state_dir: Path = None, make_client=make_client) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if state_dir is None:
        state_dir = STATE_DIR
    state_dir = Path(state_dir)

    args = _build_parser().parse_args(argv)

    try:
        state, equity_rows = load_state(state_dir)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        # Journaleamos igual aunque state_dir no exista todavía (no hubo
        # snapshot nunca): journal() crea el directorio si hace falta. Dejar
        # rastro de "se intentó operar sin snapshot" vale más que la pureza
        # de no tocar el filesystem — el intento de orden bloqueado es en sí
        # mismo información de auditoría, y state/ ya no es un directorio
        # sagrado (place_order.py también lo crea vía journal en cualquier
        # otro flujo). Si por lo que sea ni siquiera esto se puede escribir
        # (permisos, disco lleno), stderr ya quedó impreso arriba.
        try:
            journal(state_dir, f"ERROR | no se pudo cargar el state: {exc}")
        except OSError as journal_exc:
            print(f"ERROR: no se pudo journalear tampoco: {journal_exc}", file=sys.stderr)
        return 1

    # Regla 10: state stale (o updatedAt ausente/no parseable) bloquea toda
    # operación, open o close — un state viejo puede estar ocultando
    # exposición o cash reales.
    staleness_msg = _check_state_freshness(state)
    if staleness_msg:
        journal(state_dir, f"BLOQUEADA | {args.action} | {staleness_msg} | razon={args.reason}")
        print(f"BLOQUEADA: {staleness_msg}", file=sys.stderr)
        return 2

    # WP1: presupuesto de órdenes (por corrida y/o diario, ver docstring del
    # módulo, regla 15) -- se evalúa para open Y close, ANTES de despachar,
    # para que un bloqueo acá jamás llegue a tocar el cliente HTTP ni,
    # crucialmente, a consumir presupuesto él mismo.
    budget_block_msg = _check_order_budget(state_dir)
    if budget_block_msg:
        journal(
            state_dir, f"BLOQUEADA | {args.action} | {budget_block_msg} | razon={args.reason}"
        )
        print(f"BLOQUEADA: {budget_block_msg}", file=sys.stderr)
        return 2

    if args.action == "open":
        return _handle_open(args, state, equity_rows, state_dir, make_client)
    return _handle_close(args, state, state_dir, make_client)


if __name__ == "__main__":
    sys.exit(main())

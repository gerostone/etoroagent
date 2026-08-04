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
  5. No se cierran posiciones sintéticas ("pending-open:..."): son órdenes
     pendientes, no posiciones abiertas — bloqueado con exit 2.
  6. Guard de cash: para abrir, amount > cashUsd del state bloquea con exit 2
     (aunque el % de portfolio diera OK, no hay cash real disponible).
  7. Exit codes: 0 = ejecutada u OK en dry-run | 2 = bloqueada (riesgo o
     validación) | 1 = error de ejecución (incluye resultado ambiguo).

Toda decisión (ejecutada, bloqueada, dry-run, ambigua, error) se journalea en
state/journal.md con timestamp UTC y la razón pasada por --reason.
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from etoro_api import EtoroClient  # noqa: E402
from risk import OrderRequest, validate  # noqa: E402

STATE_DIR = Path(__file__).resolve().parent.parent / "state"
POSITIONS_FILE = "positions.json"
EQUITY_FILE = "equity.csv"
JOURNAL_FILE = "journal.md"


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
    """Appendea una línea al journal con timestamp UTC. Crea state_dir y
    state_dir/journal.md si no existen — incluido el caso en que state_dir
    nunca existió (p.ej. nunca corrió snapshot.py): dejar rastro en el
    journal de un intento de orden bloqueado por falta de state vale más que
    la pureza de no tocar el filesystem. Puede lanzar OSError si ni siquiera
    esto se puede escribir (permisos, disco lleno) — el caller decide si
    eso también es fatal o si el mensaje en stderr ya alcanza."""
    state_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).isoformat()
    with open(state_dir / JOURNAL_FILE, "a") as f:
        f.write(f"- {timestamp} {line}\n")


def make_client() -> EtoroClient:
    """Factory del cliente HTTP real. Mockeable: main() acepta un make_client
    alternativo (p.ej. lambda: MagicMock()) para tests sin red."""
    return EtoroClient()


def _is_dry_run() -> bool:
    """Fail-safe: si la env var falta o es != '0', dry-run."""
    return os.environ.get("DRY_RUN") != "0"


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
    """Resuelve symbol -> instrumentId vía search_instrument(). Tolera que la
    respuesta traiga los items bajo 'items' o 'instruments'. Exige match
    exacto de internalSymbolFull (docs/api-notes.md: nunca hardcodear ids)."""
    resp = client.search_instrument(symbol)
    items = (resp.get("items") if isinstance(resp, dict) else None) or (
        resp.get("instruments") if isinstance(resp, dict) else None
    ) or []
    match = next(
        (
            item
            for item in items
            if str(item.get("internalSymbolFull", "")).strip().upper() == symbol
        ),
        None,
    )
    if match is None:
        raise ValueError(f"símbolo {symbol} no encontrado en search_instrument")
    return match["instrumentId"]


def _resolve_current_price(client, instrument_id) -> float:
    """Precio de cierre de la última vela diaria, para calcular el
    StopLossRate absoluto (docs/api-notes.md: StopLossRate es un PRECIO, no
    un %)."""
    resp = client.get_candles(instrument_id, direction="desc", interval="OneDay", count=1)
    price = resp["candles"][0]["candles"][0]["close"]
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
        return 0

    client = client_factory()

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
            f"no se pudo obtener precio para el stop-loss: {exc} | razon={args.reason}",
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
        journal(
            state_dir,
            f"AMBIGUO | open {symbol} amount={amount} instrument_id={instrument_id} "
            f"stop_loss_rate={stop_loss_rate} | la orden PUDO haberse ejecutado igual "
            f"pese al error: {exc} | VERIFICAR vía get_pnl() (cache 60s) antes de asumir "
            f"fallo o de reintentar | razon={args.reason}",
        )
        print(
            f"AMBIGUO: error tras enviar la orden de apertura, verificar con get_pnl() "
            f"antes de reintentar: {exc}",
            file=sys.stderr,
        )
        return 1

    journal(
        state_dir,
        f"ABIERTA | open {symbol} amount={amount} instrument_id={instrument_id} "
        f"stop_loss_rate={stop_loss_rate} resultado={result} | razon={args.reason}",
    )
    print(f"ABIERTA: {symbol} amount={amount} stop_loss_rate={stop_loss_rate}")
    return 0


def _handle_close(args, state: dict, state_dir: Path, client_factory) -> int:
    position_id = args.position_id
    symbol = args.symbol.strip().upper()

    if position_id.startswith("pending-open:"):
        msg = f"{position_id} es una orden pendiente (apertura sintética), no una posición cerrable"
        journal(state_dir, f"BLOQUEADA | close {position_id} {symbol} | {msg} | razon={args.reason}")
        print(f"BLOQUEADA: {msg}", file=sys.stderr)
        return 2

    entry = next(
        (p for p in state.get("positions", []) if p.get("positionId") == position_id), None
    )
    if entry is None:
        msg = f"position-id {position_id} no existe en el state"
        journal(state_dir, f"BLOQUEADA | close {position_id} {symbol} | {msg} | razon={args.reason}")
        print(f"BLOQUEADA: {msg}", file=sys.stderr)
        return 2

    instrument_id = entry["instrumentId"]

    if _is_dry_run():
        journal(
            state_dir,
            f"DRY_RUN | close {position_id} {symbol} instrument_id={instrument_id} "
            f"| razon={args.reason}",
        )
        print(f"DRY_RUN: no se ejecuta close {position_id} {symbol}")
        return 0

    client = client_factory()

    try:
        # POST de trading: NUNCA reintentar (ver docstring del módulo, regla 1).
        result = client.close_position(position_id=position_id, instrument_id=instrument_id)
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
        return 1

    journal(
        state_dir,
        f"CERRADA | close {position_id} {symbol} instrument_id={instrument_id} "
        f"resultado={result} | razon={args.reason}",
    )
    print(f"CERRADA: {position_id} {symbol}")
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

    if args.action == "open":
        return _handle_open(args, state, equity_rows, state_dir, make_client)
    return _handle_close(args, state, state_dir, make_client)


if __name__ == "__main__":
    sys.exit(main())

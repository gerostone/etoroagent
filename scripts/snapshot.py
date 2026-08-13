"""Snapshot del portfolio: escribe state/positions.json y state/equity.csv.

Primer comando de toda corrida del agente. Lee el portfolio real vía
EtoroClient (get_agent_portfolios + get_pnl) y arma el state que consume
risk.py (fail-closed) y place_order.py.

HALLAZGO CRÍTICO (code review de risk.py): GET /api/v1/trading/info/real/pnl
NO devuelve símbolos, solo instrumentID. Si escribiéramos symbol=None/"",
risk.py bloquearía TODA apertura (fail-closed, por diseño). Por eso este
script resuelve instrumentId -> símbolo y FALLA RUIDOSAMENTE (stderr + exit
1, sin escribir positions.json) si algún id no se puede resolver a un
símbolo no vacío. Nunca se escribe un state con símbolos faltantes.

HALLAZGO (2do contacto con la API real, reemplaza el enfoque original de
metadata): GET /market-data/instruments?instrumentIds= NO trae ticker/símbolo
— solo instrumentDisplayDatas[] con un nombre legible ("Bitcoin"), inútil
para resolver símbolos. La única vía real es un MAPEO INVERSO: para cada
símbolo del universo operable cerrado (risk.UNIVERSE_EQUITY + los símbolos
canónicos cripto de risk.CRYPTO_SEARCH_VARIANTS), buscar con
search_instrument() + etoro_api.extract_exact_match() y armar
instrumentId -> símbolo canónico. Ver _resolve_universe_symbol_by_id().

Separación pura/IO:
  - build_state()   : pura, testeable sin red.
  - update_equity()  : pura, testeable sin red.
  - main()           : todo el I/O (red, filesystem), fino y con manejo de
                        errores fail-closed (cualquier excepción -> stderr +
                        exit 1, sin dejar archivos a medias).

Sin carga de .env acá (la hace runner.sh). Sin retries extra más allá de los
que ya maneja EtoroClient ante 429. Este script es solo lectura, pero
mantiene el principio general del proyecto de no reintentar nada de trading.

WP4/N5 (re-auditoría): main() honra la env var ETOROAGENT_STATE_DIR para
redirigir state/ (si está seteada), antes de caer al STATE_DIR real del
repo -- pensado para harnesses de test/auditoría que invocan este script
real por subprocess, no para uso normal del agente. La protección contra
que el AGENTE la use para evadir el presupuesto de órdenes o la
reconciliación (ver place_order.py) NO es este fallback -- es
scripts/risk_hook.py (WP4/N4a), que bloquea asignar
ETOROAGENT_RUN_ID/ETOROAGENT_STATE_DIR inline junto a una invocación de
este script (o place_order.py/candles.py/reconcile.py).

HALLAZGOS FAIL-OPEN adicionales (2da ronda de review), también resueltos acá:
  - ordersForOpen[mirrorID==0] (aperturas pendientes) se restaban de cashUsd
    pero no aparecían en positions[]: risk.py no veía esa exposición al
    validar una apertura nueva del mismo símbolo. Ahora entran a positions[]
    como entradas sintéticas {"pending": true, "valueUsd": amount}.
  - Clasificar cripto por igualdad exacta contra un set fijo (CRYPTO_SYMBOLS)
    deja pasar sin tope cualquier formato no listado. Ahora build_state()
    exige que todo símbolo resuelto pertenezca al universo cerrado
    risk.UNIVERSE, y risk.validate() hace lo mismo del lado de la orden.
"""
import csv
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from etoro_api import EtoroClient, extract_exact_match  # noqa: E402
from risk import CRYPTO_SEARCH_VARIANTS, UNIVERSE, UNIVERSE_EQUITY  # noqa: E402

STATE_DIR = Path(__file__).resolve().parent.parent / "state"
POSITIONS_FILE = "positions.json"
EQUITY_FILE = "equity.csv"
EQUITY_HEADER = ["date", "total"]


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _is_finite_number(x) -> bool:
    """Rechaza bool (True/False no son "1"/"0" válidos acá) y no-finitos (NaN/inf).

    Mismo criterio que risk.py — duplicado a propósito (2 líneas, sin red/estado
    compartido) para no acoplar snapshot.py a un símbolo privado de otro módulo.
    """
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _extract_pnl_value(raw) -> float:
    """unrealizedPnL puede venir como número o como {"pnL": x}. Default 0.0 si falta."""
    if raw is None:
        return 0.0
    if isinstance(raw, dict):
        val = raw.get("pnL", 0.0)
        return float(val) if val is not None else 0.0
    return float(raw)


def _position_id(pos: dict):
    """Tolera positionID (PascalCase-ish, como el resto de la API) o positionId."""
    if "positionID" in pos:
        return pos["positionID"]
    return pos.get("positionId")


def _order_id(order: dict):
    """Tolera orderID (PascalCase-ish) o orderId, igual que _position_id."""
    if "orderID" in order:
        return order["orderID"]
    return order.get("orderId")


def _normalize_instrument_id(raw):
    """Normaliza un instrumentId/instrumentID a int.

    Tolera strings numéricos ("1" -> 1). Cualquier otra cosa (no numérico,
    bool, float no parseable, etc.) -> ValueError: un id no confiable no debe
    entrar silenciosamente al mapa id->símbolo (podría no matchear nunca con
    el id real de una posición/orden, o peor, colisionar con otro).

    Se usa simétricamente en ambos lados del matching: tanto para el
    internalInstrumentId que devuelve search_instrument() como para el
    instrumentID de positions[]/ordersForOpen[] en la respuesta de get_pnl()
    — si sólo normalizáramos un lado, un id que llegara como string en el
    otro lado nunca matchearía.
    """
    if isinstance(raw, bool):
        raise ValueError(f"instrumentId inválido (bool): {raw!r}")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError:
            raise ValueError(f"instrumentId no numérico: {raw!r}")
    raise ValueError(f"instrumentId no numérico: {raw!r}")


def _normalize_instrument_id_or_none(raw):
    if raw is None:
        return None
    return _normalize_instrument_id(raw)


def _resolve_position_symbol(instrument_id, symbol_by_id: dict) -> str:
    """Resuelve instrument_id -> símbolo y valida que esté en el universo
    operable cerrado (risk.UNIVERSE).

    Un símbolo fuera del universo (formato no anticipado, instrumento nuevo,
    etc.) no debe colarse al state: risk.py clasifica exposición por
    pertenencia a sets fijos (CRYPTO_SYMBOLS) — un símbolo no reconocido no
    cuenta contra NINGÚN tope, ni de posición ni de cripto (fail-open).
    """
    symbol = symbol_by_id.get(instrument_id)
    if not symbol:
        raise ValueError(
            f"instrumentID {instrument_id!r} no se pudo resolver a un símbolo: "
            "no se puede armar el state (fail-closed, risk.py bloquearía "
            "toda apertura con símbolos faltantes)."
        )
    if symbol not in UNIVERSE:
        raise ValueError(
            f"símbolo {symbol!r} fuera del universo operable — actualizar "
            "mapeo/universo antes de continuar (fail-closed)."
        )
    return symbol


def _require_amount(item: dict, label: str) -> float:
    """Fail-closed: 'amount' ausente o no numérico/finito -> ValueError.

    Mismo criterio que con 'credit': un amount inventado (default 0.0)
    esconde exposición o cash real en vez de fallar ruidosamente.
    """
    if "amount" not in item:
        raise ValueError(f"{label} sin 'amount': no se puede calcular exposición (fail-closed).")
    amount = item["amount"]
    if not _is_finite_number(amount):
        raise ValueError(f"{label} con 'amount' no numérico/finito: {amount!r} (fail-closed).")
    return amount


def _get_client_portfolio(pnl: dict) -> dict:
    """Extrae clientPortfolio de la respuesta de get_pnl(). Fail-closed: sin
    defaults silenciosos — una respuesta sin clientPortfolio no es un portfolio
    vacío, es una respuesta que no se puede interpretar."""
    client_portfolio = pnl.get("clientPortfolio") if isinstance(pnl, dict) else None
    if not isinstance(client_portfolio, dict):
        raise ValueError(
            "respuesta de pnl sin 'clientPortfolio': no se puede armar el state "
            "(fail-closed, sin defaults silenciosos)."
        )
    return client_portfolio


def _get_credit(client_portfolio: dict) -> float:
    """Fail-closed: 'credit' ausente o no numérico/finito -> ValueError. Nunca
    un state bien formado con un cash disponible inventado (0.0 por default)."""
    if "credit" not in client_portfolio:
        raise ValueError(
            "clientPortfolio sin 'credit': no se puede calcular cash disponible "
            "(fail-closed, sin defaults silenciosos)."
        )
    credit = client_portfolio["credit"]
    if not _is_finite_number(credit):
        raise ValueError(f"'credit' no numérico/finito: {credit!r} (fail-closed).")
    return credit


def _reject_mirrors(client_portfolio: dict) -> None:
    """mirrors[] no vacío significa copy-trading activo, que este agente no
    soporta: ignorarlo silenciosamente escondería valor real del portfolio
    (posiciones espejo cuyo valor no está reflejado en positions[])."""
    mirrors = client_portfolio.get("mirrors")
    if mirrors:
        raise ValueError(
            "copy trading no soportado por este agente: clientPortfolio.mirrors "
            "no está vacío."
        )


def _qualifying_orders_for_open(orders_for_open: list) -> list:
    """Entradas de ordersForOpen con mirrorID==0 (aperturas pendientes reales,
    no copiadas), junto a su índice original en la lista y su 'amount'
    validado (fail-closed: sin defaults silenciosos).

    Índice + amount ya validados acá para que _sum_orders_for_open() y
    build_state() (que arma posiciones sintéticas a partir de estas mismas
    entradas) no dupliquen ni diverjan en el parsing de la lista.
    """
    result = []
    for index, order in enumerate(orders_for_open):
        if order.get("mirrorID", 0) != 0:
            continue
        amount = _require_amount(order, "ordersForOpen")
        result.append((index, order, amount))
    return result


def _sum_orders_for_open(orders_for_open: list) -> float:
    return sum(amount for _, _, amount in _qualifying_orders_for_open(orders_for_open))


def _sum_amounts(orders: list) -> float:
    return sum(_require_amount(o, "orders") for o in orders)


def _available_cash(client_portfolio: dict) -> float:
    """Available Cash = credit − Σ(ordersForOpen[mirrorID==0].amount) − Σ(orders.amount).

    Fórmula oficial, ver docs/api-notes.md.
    """
    credit = _get_credit(client_portfolio)
    orders_for_open = client_portfolio.get("ordersForOpen") or []
    orders = client_portfolio.get("orders") or []
    return credit - _sum_orders_for_open(orders_for_open) - _sum_amounts(orders)


def build_state(portfolio_id: str, pnl: dict, symbol_by_id: dict) -> dict:
    """Arma el state de positions.json a partir de get_pnl(). Pura, fail-closed.

    Lanza ValueError si:
      - clientPortfolio o 'credit' faltan / no son numéricos (sin defaults
        silenciosos: nunca un state bien formado con números inventados).
      - clientPortfolio.mirrors no está vacío (copy trading no soportado por
        este agente — omitirlo silenciosamente escondería valor real).
      - algún instrumentID (de una posición o de una apertura pendiente) no
        se puede resolver a un símbolo no vacío y perteneciente al universo
        operable cerrado (risk.UNIVERSE) vía symbol_by_id.
      - alguna posición no tiene positionID/positionId resoluble (una
        posición sin id no se puede cerrar más adelante).
      - falta 'amount' en una posición o en una apertura pendiente.

    valueUsd = amount + unrealizedPnL SIN clamp: puede ser negativo (p.ej. con
    apalancamiento) y así debe reflejarse en el sizing de risk.py — clampar a
    0.0 sobreestimaría el portfolio y relajaría los topes de exposición.

    Aperturas pendientes (ordersForOpen con mirrorID==0) entran a positions[]
    como entradas SINTÉTICAS ({"pending": true}, positionId "pending-open:
    <orderId o índice>", valueUsd = amount). Su monto ya fue restado de
    cashUsd (ver _available_cash) pero todavía no es una posición abierta —
    si no la reflejáramos acá, risk.py no vería esa exposición al validar una
    apertura nueva del mismo símbolo (fail-open: dejaría pasar exposición
    real por encima de los topes). orders[] (cierres pendientes) NO generan
    entradas nuevas: la posición subyacente que van a cerrar ya está en
    positions[].
    """
    client_portfolio = _get_client_portfolio(pnl)
    _reject_mirrors(client_portfolio)
    cash_usd = _available_cash(client_portfolio)
    raw_positions = client_portfolio.get("positions") or []
    orders_for_open = client_portfolio.get("ordersForOpen") or []

    positions = []
    for pos in raw_positions:
        instrument_id = _normalize_instrument_id_or_none(pos.get("instrumentID"))
        symbol = _resolve_position_symbol(instrument_id, symbol_by_id)
        position_id = _position_id(pos)
        if position_id is None:
            raise ValueError(
                f"posición del instrumentID {instrument_id!r} sin positionID/"
                "positionId resoluble: una posición sin id no se puede cerrar "
                "más adelante (fail-closed)."
            )
        amount = _require_amount(pos, f"posición {position_id!r}")
        pnl_value = _extract_pnl_value(pos.get("unrealizedPnL"))
        value_usd = amount + pnl_value  # SIN clamp — ver docstring.
        positions.append(
            {
                "positionId": position_id,
                "symbol": symbol,
                "instrumentId": instrument_id,
                "valueUsd": value_usd,
            }
        )

    for index, order, amount in _qualifying_orders_for_open(orders_for_open):
        instrument_id = _normalize_instrument_id_or_none(order.get("instrumentID"))
        symbol = _resolve_position_symbol(instrument_id, symbol_by_id)
        order_id = _order_id(order)
        position_id = f"pending-open:{order_id if order_id is not None else index}"
        positions.append(
            {
                "positionId": position_id,
                "symbol": symbol,
                "instrumentId": instrument_id,
                "valueUsd": amount,
                "pending": True,
            }
        )

    return {
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "portfolioId": portfolio_id,
        "cashUsd": cash_usd,
        "positions": positions,
    }


def compute_equity(pnl: dict) -> float:
    """Equity oficial (docs/api-notes.md): availableCash + totalInvested + unrealizedPnL.

    NO es lo mismo que cashUsd + Σ(positions.valueUsd) del state: ese cálculo
    ignora el dinero comprometido en órdenes pendientes (ordersForOpen/orders),
    que ya fue restado de availableCash pero todavía no aparece como una
    posición abierta — sin sumarlo de vuelta acá, el total caería
    artificialmente cada vez que hay órdenes en vuelo, produciendo un
    drawdown espurio que dispara el modo defensivo de risk.py.

    totalInvested = Σ(positions[].amount) + Σ(ordersForOpen[mirrorID==0].amount)
                    + Σ(orders[].amount)
    unrealizedPnL = Σ(pnl de cada posición), SIN clamp.

    Fail-closed: mismos requisitos que build_state (clientPortfolio/credit
    presentes y numéricos, mirrors vacío).
    """
    client_portfolio = _get_client_portfolio(pnl)
    _reject_mirrors(client_portfolio)
    available_cash = _available_cash(client_portfolio)

    orders_for_open = client_portfolio.get("ordersForOpen") or []
    orders = client_portfolio.get("orders") or []
    raw_positions = client_portfolio.get("positions") or []

    positions_amount_sum = sum(_require_amount(p, "posición") for p in raw_positions)
    total_invested = (
        positions_amount_sum + _sum_orders_for_open(orders_for_open) + _sum_amounts(orders)
    )
    unrealized_pnl_sum = sum(_extract_pnl_value(p.get("unrealizedPnL")) for p in raw_positions)

    return available_cash + total_invested + unrealized_pnl_sum


def update_equity(rows: list, today: str, total: float) -> list:
    """Reemplaza la fila de hoy si existe, appendea si no. Pura.

    Mantiene las filas ordenadas por fecha ascendente: drawdown_pct() en
    risk.py asume que la última fila es la más reciente (usa values[-1]) —
    si el CSV llegara desordenado (p.ej. por edición manual o por una corrida
    con reloj desincronizado), el drawdown calculado sería incorrecto.
    """
    new_rows = [(date, value) for date, value in rows if date != today]
    new_rows.append((today, total))
    new_rows.sort(key=lambda row: row[0])
    return new_rows


# Símbolos canónicos a buscar para armar el mapeo inverso id->símbolo (ver
# _resolve_universe_symbol_by_id). NO es risk.UNIVERSE completo: ese set
# incluye variantes de FORMATO cripto (BTCUSD, BTC-USD, BTCEUR, ...) que
# existen solo para CLASIFICAR una posición ya resuelta contra los topes de
# riesgo (risk.validate) — no son símbolos de búsqueda independientes.
# Buscarlas literalmente sería redundante frente a ya resolver "BTC"/"ETH"
# probando esas mismas variantes como intentos de búsqueda (ver
# risk.CRYPTO_SEARCH_VARIANTS), y podría generar falsos "instrumentos
# nuevos" si alguna variante da un match exacto distinto del canónico.
_CANONICAL_SEARCH_SYMBOLS = sorted(UNIVERSE_EQUITY) + sorted(CRYPTO_SEARCH_VARIANTS.keys())


def _resolve_universe_symbol_by_id(client: EtoroClient) -> dict:
    """Mapeo inverso instrumentId -> símbolo CANÓNICO, buscando cada símbolo
    del universo operable cerrado vía search_instrument() +
    etoro_api.extract_exact_match().

    Reemplaza la resolución por metadata (get_instruments_metadata): esa
    respuesta no trae ticker/símbolo, solo un nombre legible ("Bitcoin"),
    inútil para mapear id->símbolo.

    Para BTC/ETH prueba risk.CRYPTO_SEARCH_VARIANTS EN ORDEN y se queda con
    el primer match exacto no ambiguo, mapeándolo al símbolo CANÓNICO
    ("BTC"/"ETH"), no a la variante de búsqueda que lo encontró — así una
    posición en el instrumento devuelto bajo "BTCUSD" sigue clasificando
    como "BTC" para risk.py.

    Best-effort por símbolo: si un símbolo del universo no resuelve (sin
    match exacto, ambiguo, o ninguna variante cripto matchea), simplemente
    no entra al mapa — no aborta el snapshot. El agente no necesariamente
    tiene (ni puede tener) posiciones en cada símbolo del universo en un
    momento dado; una posición real que SÍ referencie un instrumentID no
    resuelto hace fallar build_state() (fail-closed) más adelante — eso es
    lo que importa.

    Si dos símbolos distintos del universo resuelven al MISMO instrumentId,
    ValueError inmediato: eso sí es una inconsistencia de datos real (dos
    símbolos "distintos" siendo en realidad el mismo instrumento), no un
    símbolo simplemente no resuelto.

    Se llama una sola vez por corrida de main() y el resultado se pasa a
    build_state() — "cacheado en memoria" para toda la corrida en vez de
    re-resolver por cada posición/orden pendiente individualmente.

    Nota de rate limit: hasta ~13-19 requests a search_instrument (grupo
    market-data, 120 req/60s) — dentro de la quota sin necesidad de sleeps.
    """
    symbol_by_id = {}
    for symbol in _CANONICAL_SEARCH_SYMBOLS:
        variants = CRYPTO_SEARCH_VARIANTS.get(symbol, [symbol])
        for variant in variants:
            try:
                resp = client.search_instrument(variant)
                match = extract_exact_match(resp, variant)
                instrument_id = _normalize_instrument_id(match.get("internalInstrumentId"))
            except (ValueError, TypeError):
                continue

            if instrument_id in symbol_by_id and symbol_by_id[instrument_id] != symbol:
                raise ValueError(
                    f"instrumentId {instrument_id} resuelto a dos símbolos distintos "
                    f"del universo: {symbol_by_id[instrument_id]!r} y {symbol!r} "
                    "(mapeo id->símbolo inconsistente, fail-closed)."
                )
            symbol_by_id[instrument_id] = symbol
            break
    return symbol_by_id


def _read_equity_rows(path: Path) -> list:
    if not path.exists():
        return []
    rows = []
    with open(path, newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header is not None and header != EQUITY_HEADER:
            raise ValueError(
                f"equity.csv con header inesperado: {header!r} "
                f"(se esperaba {EQUITY_HEADER}, fail-closed)."
            )
        for row in reader:
            if not row:
                continue
            date, value = row[0], row[1]
            rows.append((date, float(value)))
    return rows


def _atomic_write_json(path: Path, data: dict) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, path)


def _atomic_write_equity(path: Path, rows: list) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(EQUITY_HEADER)
        for date, value in rows:
            writer.writerow([date, value])
    os.replace(tmp_path, path)


def main():
    try:
        client = EtoroClient()

        # GET /api/v1/agent-portfolios exige scope de cuenta real:read. Un
        # token scoped a un Agent Portfolio (el caso normal de este agente)
        # no lo tiene y devuelve 403 -- pero GET /trading/info/real/pnl si
        # funciona con ese token. portfolioId es puramente informativo en
        # nuestro state (los endpoints de trading son token-scoped, no
        # dependen de portfolioId), asi que ante 403 seguimos con un
        # fallback en vez de abortar. 401 (EtoroAuthError, terminal) y
        # cualquier otro HTTPError siguen abortando como antes.
        try:
            portfolios_resp = client.get_agent_portfolios()
        except requests.exceptions.HTTPError as exc:
            status_code = getattr(exc.response, "status_code", None)
            if status_code != 403:
                raise
            portfolio_id = os.environ.get("ETORO_PORTFOLIO_ID", "token-scoped")
            print(
                "listado de portfolios no accesible con este token (403); "
                f"usando id {portfolio_id!r}"
            )
        else:
            portfolios = portfolios_resp.get("agentPortfolios") or []
            if not portfolios:
                print("no hay agent-portfolios disponibles para esta key", file=sys.stderr)
                sys.exit(1)
            portfolio_id = portfolios[0].get("agentPortfolioId")

        pnl = client.get_pnl()

        # Mapeo inverso id->símbolo sobre el universo operable cerrado (ver
        # _resolve_universe_symbol_by_id) — reemplaza la resolución por
        # metadata (get_instruments_metadata no trae ticker/símbolo).
        symbol_by_id = _resolve_universe_symbol_by_id(client)

        state = build_state(portfolio_id, pnl, symbol_by_id)

        # WP4/N5: ver docstring del módulo.
        state_dir = Path(os.environ.get("ETOROAGENT_STATE_DIR") or STATE_DIR)
        state_dir.mkdir(parents=True, exist_ok=True)
        positions_path = state_dir / POSITIONS_FILE
        equity_path = state_dir / EQUITY_FILE

        # Equity oficial para la curva histórica, NO cashUsd + Σ(valueUsd) del
        # state (ese cálculo ignora órdenes pendientes — ver compute_equity()).
        total = compute_equity(pnl)
        existing_rows = _read_equity_rows(equity_path)
        updated_rows = update_equity(existing_rows, _today_str(), total)

        _atomic_write_json(positions_path, state)
        _atomic_write_equity(equity_path, updated_rows)

        print(
            f"OK portfolio={portfolio_id} cash={state['cashUsd']:.2f} "
            f"posiciones={len(state['positions'])} total={total:.2f}"
        )
    except SystemExit:
        raise
    except Exception as exc:
        print(f"ERROR en snapshot: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

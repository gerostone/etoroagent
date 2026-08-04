"""Snapshot del portfolio: escribe state/positions.json y state/equity.csv.

Primer comando de toda corrida del agente. Lee el portfolio real vía
EtoroClient (get_agent_portfolios + get_pnl) y arma el state que consume
risk.py (fail-closed) y place_order.py.

HALLAZGO CRÍTICO (code review de risk.py): GET /api/v1/trading/info/real/pnl
NO devuelve símbolos, solo instrumentID. Si escribiéramos symbol=None/"",
risk.py bloquearía TODA apertura (fail-closed, por diseño). Por eso este
script resuelve instrumentId -> símbolo vía get_instruments_metadata() y
FALLA RUIDOSAMENTE (stderr + exit 1, sin escribir positions.json) si algún
id no se puede resolver a un símbolo no vacío. Nunca se escribe un state
con símbolos faltantes.

Separación pura/IO:
  - build_state()   : pura, testeable sin red.
  - update_equity()  : pura, testeable sin red.
  - main()           : todo el I/O (red, filesystem), fino y con manejo de
                        errores fail-closed (cualquier excepción -> stderr +
                        exit 1, sin dejar archivos a medias).

Sin carga de .env acá (la hace runner.sh). Sin retries extra más allá de los
que ya maneja EtoroClient ante 429. Este script es solo lectura, pero
mantiene el principio general del proyecto de no reintentar nada de trading.
"""
import csv
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from etoro_api import EtoroClient  # noqa: E402

STATE_DIR = Path(__file__).resolve().parent.parent / "state"
POSITIONS_FILE = "positions.json"
EQUITY_FILE = "equity.csv"
EQUITY_HEADER = ["date", "total"]

# Campos donde puede venir el símbolo en la respuesta de get_instruments_metadata,
# probados en este orden: el primero no vacío gana.
_SYMBOL_FIELDS = ("internalSymbolFull", "symbolFull", "symbol", "ticker")


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


def _sum_orders_for_open(orders_for_open: list) -> float:
    return sum(
        float(o.get("amount", 0.0) or 0.0)
        for o in orders_for_open
        if o.get("mirrorID", 0) == 0
    )


def _sum_amounts(orders: list) -> float:
    return sum(float(o.get("amount", 0.0) or 0.0) for o in orders)


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
      - algún instrumentID de una posición no se puede resolver a un símbolo
        no vacío vía symbol_by_id (risk.py bloquearía TODA apertura si
        dejáramos pasar un símbolo faltante).
      - alguna posición no tiene positionID/positionId resoluble (una
        posición sin id no se puede cerrar más adelante).

    valueUsd = amount + unrealizedPnL SIN clamp: puede ser negativo (p.ej. con
    apalancamiento) y así debe reflejarse en el sizing de risk.py — clampar a
    0.0 sobreestimaría el portfolio y relajaría los topes de exposición.
    """
    client_portfolio = _get_client_portfolio(pnl)
    _reject_mirrors(client_portfolio)
    cash_usd = _available_cash(client_portfolio)
    raw_positions = client_portfolio.get("positions") or []

    positions = []
    for pos in raw_positions:
        instrument_id = pos.get("instrumentID")
        symbol = symbol_by_id.get(instrument_id)
        if not symbol:
            raise ValueError(
                f"instrumentID {instrument_id!r} no se pudo resolver a un símbolo: "
                "no se puede armar el state (fail-closed, risk.py bloquearía "
                "toda apertura con símbolos faltantes)."
            )
        position_id = _position_id(pos)
        if position_id is None:
            raise ValueError(
                f"posición del instrumentID {instrument_id!r} sin positionID/"
                "positionId resoluble: una posición sin id no se puede cerrar "
                "más adelante (fail-closed)."
            )
        amount = float(pos.get("amount", 0.0) or 0.0)
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

    positions_amount_sum = sum(float(p.get("amount", 0.0) or 0.0) for p in raw_positions)
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


def _normalize_instrument_id(raw):
    """Normaliza el instrumentId de un item de metadata a int.

    Tolera strings numéricos ("1" -> 1). Cualquier otra cosa (no numérico,
    bool, float no parseable, etc.) -> ValueError: un id no confiable no debe
    entrar silenciosamente al mapa id->símbolo (podría no matchear nunca con
    el instrumentID real de una posición, o peor, colisionar con otro).
    """
    if isinstance(raw, bool):
        raise ValueError(f"instrumentId inválido en metadata (bool): {raw!r}")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError:
            raise ValueError(f"instrumentId no numérico en metadata: {raw!r}")
    raise ValueError(f"instrumentId no numérico en metadata: {raw!r}")


def _resolve_symbol_by_id(client: EtoroClient, instrument_ids: list) -> dict:
    """Llama a get_instruments_metadata() y arma id->símbolo.

    Para cada item busca en orden _SYMBOL_FIELDS el primer campo no vacío.
    IDs no presentes en la respuesta simplemente no entran en el dict (y
    build_state() fallará ruidosamente al no encontrarlos).

    Tolera que la respuesta sea una lista desnuda (en vez de {"items": [...]}
    ) — el chequeo isinstance(metadata, list) va ANTES de .get(...) porque
    una lista no tiene .get() (AttributeError si el orden fuera al revés).

    Si dos items de metadata traen el mismo instrumentId, se conserva el
    primero; si además difieren en símbolo, ValueError (metadata inconsistente
    no debe resolverse silenciosamente a "cualquiera de los dos").
    """
    if not instrument_ids:
        return {}
    metadata = client.get_instruments_metadata(instrument_ids)
    if isinstance(metadata, list):
        items = metadata
    else:
        items = metadata.get("items") or metadata.get("instruments") or []

    symbol_by_id = {}
    for item in items:
        raw_id = item.get("instrumentId", item.get("instrumentID"))
        if raw_id is None:
            continue
        instrument_id = _normalize_instrument_id(raw_id)

        symbol = ""
        for field in _SYMBOL_FIELDS:
            val = item.get(field)
            if val:
                symbol = val
                break
        if not symbol:
            continue

        if instrument_id in symbol_by_id:
            if symbol_by_id[instrument_id] != symbol:
                raise ValueError(
                    f"metadata inconsistente para instrumentId {instrument_id}: "
                    f"símbolos distintos ({symbol_by_id[instrument_id]!r} vs {symbol!r})."
                )
            continue  # duplicado idéntico: nos quedamos con el primero

        symbol_by_id[instrument_id] = symbol
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

        portfolios_resp = client.get_agent_portfolios()
        portfolios = portfolios_resp.get("agentPortfolios") or []
        if not portfolios:
            print("no hay agent-portfolios disponibles para esta key", file=sys.stderr)
            sys.exit(1)
        portfolio_id = portfolios[0].get("agentPortfolioId")

        pnl = client.get_pnl()
        client_portfolio = _get_client_portfolio(pnl)
        raw_positions = client_portfolio.get("positions") or []
        instrument_ids = sorted(
            {p.get("instrumentID") for p in raw_positions if p.get("instrumentID") is not None}
        )

        symbol_by_id = _resolve_symbol_by_id(client, instrument_ids)

        state = build_state(portfolio_id, pnl, symbol_by_id)

        STATE_DIR.mkdir(parents=True, exist_ok=True)
        positions_path = STATE_DIR / POSITIONS_FILE
        equity_path = STATE_DIR / EQUITY_FILE

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

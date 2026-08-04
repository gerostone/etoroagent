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
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from etoro_api import EtoroClient  # noqa: E402

STATE_DIR = Path(__file__).resolve().parent.parent / "state"
POSITIONS_FILE = "positions.json"
EQUITY_FILE = "equity.csv"

# Campos donde puede venir el símbolo en la respuesta de get_instruments_metadata,
# probados en este orden: el primero no vacío gana.
_SYMBOL_FIELDS = ("internalSymbolFull", "symbolFull", "symbol", "ticker")


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


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


def build_state(portfolio_id: str, pnl: dict, symbol_by_id: dict) -> dict:
    """Arma el state de positions.json a partir de get_pnl(). Pura, fail-closed.

    Lanza ValueError si algún instrumentID de una posición no se puede resolver
    a un símbolo no vacío vía symbol_by_id — nunca se debe escribir un state
    con símbolos faltantes (risk.py bloquearía TODA apertura en ese caso, pero
    silenciosamente en runtime en vez de fallar ruidosamente acá donde el
    problema es detectable y accionable).
    """
    client_portfolio = pnl.get("clientPortfolio", {})
    credit = float(client_portfolio.get("credit", 0.0) or 0.0)
    orders = client_portfolio.get("orders") or []
    orders_for_open = client_portfolio.get("ordersForOpen") or []
    raw_positions = client_portfolio.get("positions") or []

    orders_for_open_sum = sum(
        float(o.get("amount", 0.0) or 0.0)
        for o in orders_for_open
        if o.get("mirrorID", 0) == 0
    )
    orders_sum = sum(float(o.get("amount", 0.0) or 0.0) for o in orders)
    cash_usd = credit - orders_for_open_sum - orders_sum

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
        amount = float(pos.get("amount", 0.0) or 0.0)
        pnl_value = _extract_pnl_value(pos.get("unrealizedPnL"))
        value_usd = amount + pnl_value
        if value_usd < 0:
            value_usd = 0.0
        positions.append(
            {
                "positionId": _position_id(pos),
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


def update_equity(rows: list, today: str, total: float) -> list:
    """Reemplaza la fila de hoy si existe, appendea si no. Pura."""
    new_rows = [(date, value) for date, value in rows if date != today]
    new_rows.append((today, total))
    return new_rows


def _resolve_symbol_by_id(client: EtoroClient, instrument_ids: list) -> dict:
    """Llama a get_instruments_metadata() y arma id->símbolo.

    Para cada item busca en orden _SYMBOL_FIELDS el primer campo no vacío.
    IDs no presentes en la respuesta simplemente no entran en el dict (y
    build_state() fallará ruidosamente al no encontrarlos).
    """
    if not instrument_ids:
        return {}
    metadata = client.get_instruments_metadata(instrument_ids)
    items = metadata.get("items") or metadata.get("instruments") or []
    if isinstance(metadata, list):
        items = metadata

    symbol_by_id = {}
    for item in items:
        instrument_id = item.get("instrumentId", item.get("instrumentID"))
        if instrument_id is None:
            continue
        symbol = ""
        for field in _SYMBOL_FIELDS:
            val = item.get(field)
            if val:
                symbol = val
                break
        if symbol:
            symbol_by_id[instrument_id] = symbol
    return symbol_by_id


def _read_equity_rows(path: Path) -> list:
    if not path.exists():
        return []
    rows = []
    with open(path, newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
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
        writer.writerow(["date", "total"])
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
        client_portfolio = pnl.get("clientPortfolio", {})
        raw_positions = client_portfolio.get("positions") or []
        instrument_ids = sorted(
            {p.get("instrumentID") for p in raw_positions if p.get("instrumentID") is not None}
        )

        symbol_by_id = _resolve_symbol_by_id(client, instrument_ids)

        state = build_state(portfolio_id, pnl, symbol_by_id)

        STATE_DIR.mkdir(parents=True, exist_ok=True)
        positions_path = STATE_DIR / POSITIONS_FILE
        equity_path = STATE_DIR / EQUITY_FILE

        total = state["cashUsd"] + sum(p["valueUsd"] for p in state["positions"])
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

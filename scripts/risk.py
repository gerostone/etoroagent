"""Motor de riesgo determinista. Lógica pura: sin red ni filesystem en validate().

Límites (spec §6, perfil Moderado) — NO negociables por el agente:
  máx 25% por posición, máx 35% cripto total, stop-loss obligatorio <= 12%,
  drawdown >= 25% desde el máximo histórico => modo defensivo (solo cierres).
"""
from dataclasses import dataclass
from typing import Optional

MAX_POSITION_PCT = 0.25
MAX_CRYPTO_PCT = 0.35
MAX_STOP_LOSS_PCT = 0.12
DEFENSIVE_DRAWDOWN_PCT = 0.25
CRYPTO_SYMBOLS = {"BTC", "ETH"}


@dataclass
class OrderRequest:
    action: str  # "open" | "close"
    symbol: str
    amount_usd: float
    stop_loss_pct: Optional[float]


def portfolio_value(state: dict) -> float:
    return state.get("cashUsd", 0.0) + sum(p["valueUsd"] for p in state.get("positions", []))


def drawdown_pct(equity_rows: list) -> float:
    """equity_rows: [(fecha, valor), ...]. Drawdown del último valor vs máximo histórico."""
    if not equity_rows:
        return 0.0
    values = [v for _, v in equity_rows]
    peak = max(values)
    if peak <= 0:
        return 0.0
    return max(0.0, (peak - values[-1]) / peak)


def validate(order: OrderRequest, state: dict, equity_rows: list) -> tuple[bool, str]:
    if order.action == "close":
        return True, "cierre permitido siempre"

    if drawdown_pct(equity_rows) >= DEFENSIVE_DRAWDOWN_PCT:
        return False, (
            f"MODO DEFENSIVO: drawdown >= {DEFENSIVE_DRAWDOWN_PCT:.0%}. "
            "Solo se permiten cierres de posiciones."
        )

    if order.stop_loss_pct is None or not (0 < order.stop_loss_pct <= MAX_STOP_LOSS_PCT):
        return False, (
            f"Stop-loss obligatorio y <= {MAX_STOP_LOSS_PCT:.0%} "
            f"(recibido: {order.stop_loss_pct})."
        )

    total = portfolio_value(state)
    if total <= 0:
        return False, "Valor de portfolio desconocido o cero: no se puede dimensionar."
    if order.amount_usd <= 0:
        return False, "Monto inválido."

    current = sum(
        p["valueUsd"] for p in state.get("positions", []) if p["symbol"] == order.symbol
    )
    if (current + order.amount_usd) / total > MAX_POSITION_PCT:
        return False, (
            f"Posición resultante en {order.symbol} superaría el 25% del portfolio "
            f"({current + order.amount_usd:.2f} de {total:.2f} USD)."
        )

    if order.symbol in CRYPTO_SYMBOLS:
        crypto = sum(
            p["valueUsd"] for p in state.get("positions", []) if p["symbol"] in CRYPTO_SYMBOLS
        )
        if (crypto + order.amount_usd) / total > MAX_CRYPTO_PCT:
            return False, (
                f"Exposición cripto resultante superaría el 35% del portfolio "
                f"({crypto + order.amount_usd:.2f} de {total:.2f} USD)."
            )

    return True, "ok"

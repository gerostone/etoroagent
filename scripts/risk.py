"""Motor de riesgo determinista. Lógica pura: sin red ni filesystem en validate().

Límites (spec §6, perfil Moderado) — NO negociables por el agente:
  máx 25% por posición, máx 35% cripto total, stop-loss obligatorio <= 12%,
  drawdown >= 25% desde el máximo histórico => modo defensivo (solo cierres).

validate() es fail-closed: ante action="open" con state o equity_rows
incompletos, malformados o con valores no numéricos/no finitos, bloquea la
orden en vez de asumir defaults silenciosos. action="close" siempre se
permite, incluso si state/equity_rows están malformados.
"""
import math
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


def _is_finite_number(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)


def _norm_symbol(raw) -> str:
    if raw is None:
        return ""
    return str(raw).strip().upper()


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

    # --- Validación del state (fail-closed: sin defaults silenciosos) ---
    if not isinstance(state, dict) or "positions" not in state or "cashUsd" not in state:
        return False, "estado del portfolio incompleto o malformado"

    cash = state["cashUsd"]
    positions = state["positions"]
    if not isinstance(positions, list) or not _is_finite_number(cash):
        return False, "estado del portfolio incompleto o malformado"

    normalized_positions = []
    for p in positions:
        if not isinstance(p, dict):
            return False, "estado del portfolio incompleto o malformado"
        symbol = _norm_symbol(p.get("symbol"))
        if not symbol:
            return False, (
                "estado de posiciones inválido (símbolo no utilizable): "
                "no se puede validar"
            )
        value = p.get("valueUsd")
        if not _is_finite_number(value):
            return False, "estado del portfolio incompleto o malformado"
        normalized_positions.append({"symbol": symbol, "valueUsd": value})

    # --- Validación de la curva de equity (necesaria para evaluar drawdown) ---
    if not equity_rows:
        return False, "sin curva de equity: no se puede evaluar drawdown"

    equity_values = []
    for row in equity_rows:
        try:
            _, value = row
        except (TypeError, ValueError):
            return False, "curva de equity malformada: no se puede evaluar drawdown"
        if not _is_finite_number(value):
            return False, (
                "curva de equity con valores no finitos: no se puede evaluar drawdown"
            )
        equity_values.append(value)

    peak = max(equity_values)
    dd = max(0.0, (peak - equity_values[-1]) / peak) if peak > 0 else 0.0
    if dd >= DEFENSIVE_DRAWDOWN_PCT:
        return False, (
            f"MODO DEFENSIVO: drawdown >= {DEFENSIVE_DRAWDOWN_PCT:.0%}. "
            "Solo se permiten cierres de posiciones."
        )

    if (
        order.stop_loss_pct is None
        or not _is_finite_number(order.stop_loss_pct)
        or not (0 < order.stop_loss_pct <= MAX_STOP_LOSS_PCT)
    ):
        return False, (
            f"Stop-loss obligatorio y <= {MAX_STOP_LOSS_PCT:.0%} "
            f"(recibido: {order.stop_loss_pct})."
        )

    symbol = _norm_symbol(order.symbol)
    if not symbol:
        return False, "Símbolo de orden inválido."

    if not _is_finite_number(order.amount_usd) or order.amount_usd <= 0:
        return False, "Monto inválido."

    total = cash + sum(p["valueUsd"] for p in normalized_positions)
    if total <= 0:
        return False, "Valor de portfolio desconocido o cero: no se puede dimensionar."

    current = sum(
        p["valueUsd"] for p in normalized_positions if p["symbol"] == symbol
    )
    if (current + order.amount_usd) / total > MAX_POSITION_PCT:
        return False, (
            f"Posición resultante en {symbol} superaría el {MAX_POSITION_PCT:.0%} del portfolio "
            f"({current + order.amount_usd:.2f} de {total:.2f} USD)."
        )

    if symbol in CRYPTO_SYMBOLS:
        crypto = sum(
            p["valueUsd"] for p in normalized_positions if p["symbol"] in CRYPTO_SYMBOLS
        )
        if (crypto + order.amount_usd) / total > MAX_CRYPTO_PCT:
            return False, (
                f"Exposición cripto resultante superaría el {MAX_CRYPTO_PCT:.0%} del portfolio "
                f"({crypto + order.amount_usd:.2f} de {total:.2f} USD)."
            )

    return True, "ok"

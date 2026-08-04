"""Motor de riesgo determinista. Lógica pura: sin red ni filesystem en validate().

Límites (spec §6, perfil Moderado) — NO negociables por el agente:
  máx 25% por posición, máx 35% cripto total, stop-loss obligatorio <= 12%,
  drawdown >= 25% desde el máximo histórico => modo defensivo (solo cierres).

validate() es fail-closed: ante action="open" con state o equity_rows
incompletos, malformados o con valores no numéricos/no finitos, bloquea la
orden en vez de asumir defaults silenciosos. action="close" siempre se
permite, incluso si state/equity_rows están malformados.

portfolio_value() y drawdown_pct() son las únicas funciones que parsean/
validan state y equity_rows respectivamente: lanzan ValueError ante entrada
malformada, no finita o (para drawdown) sin un pico positivo. validate() las
usa directamente en vez de reimplementar su propio parsing, para que no haya
dos versiones divergentes de la misma lógica.
"""
import math
from dataclasses import dataclass
from typing import Optional

MAX_POSITION_PCT = 0.25
MAX_CRYPTO_PCT = 0.35
MAX_STOP_LOSS_PCT = 0.12
DEFENSIVE_DRAWDOWN_PCT = 0.25

# eToro puede devolver el símbolo de un instrumento cripto en distintos
# formatos según el endpoint/instrumento (p.ej. "BTCUSD" en vez de "BTC" —
# visto en la metadata de market-data/instruments). Si este set no cubre la
# variante devuelta, el tope de MAX_CRYPTO_PCT se evalúa contra un conjunto
# que no incluye esa posición y el límite falla ABIERTO (deja pasar
# exposición cripto sin límite). Se listan las variantes conocidas
# explícitamente en vez de un match parcial (p.ej. "BTC" in symbol), que
# podría capturar falsos positivos (tickers no-cripto que contengan "BTC").
CRYPTO_SYMBOLS = {
    "BTC", "ETH",
    "BTCUSD", "ETHUSD",
    "BTC-USD", "ETH-USD",
    "BTCEUR", "ETHEUR",
}


@dataclass
class OrderRequest:
    action: str  # "open" | "close"
    symbol: str
    amount_usd: float
    stop_loss_pct: Optional[float]


def _is_finite_number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _norm_symbol(raw) -> str:
    if raw is None:
        return ""
    return str(raw).strip().upper()


def _normalize_state(state: dict) -> tuple[float, list]:
    """Valida y normaliza state. Lanza ValueError ante entrada incompleta/malformada."""
    if not isinstance(state, dict) or "positions" not in state or "cashUsd" not in state:
        raise ValueError("estado del portfolio incompleto o malformado")

    cash = state["cashUsd"]
    positions = state["positions"]
    if not isinstance(positions, list) or not _is_finite_number(cash):
        raise ValueError("estado del portfolio incompleto o malformado")

    normalized_positions = []
    for p in positions:
        if not isinstance(p, dict):
            raise ValueError("estado del portfolio incompleto o malformado")
        symbol = _norm_symbol(p.get("symbol"))
        if not symbol:
            raise ValueError(
                "estado de posiciones inválido (símbolo no utilizable): "
                "no se puede validar"
            )
        value = p.get("valueUsd")
        if not _is_finite_number(value):
            raise ValueError("estado del portfolio incompleto o malformado")
        normalized_positions.append({"symbol": symbol, "valueUsd": value})

    return cash, normalized_positions


def portfolio_value(state: dict) -> float:
    """Valor total del portfolio (cash + posiciones). Lanza ValueError si state
    está incompleto, malformado o tiene valores no numéricos/no finitos."""
    cash, positions = _normalize_state(state)
    return cash + sum(p["valueUsd"] for p in positions)


def drawdown_pct(equity_rows: list) -> float:
    """equity_rows: [(fecha, valor), ...]. Drawdown del último valor vs máximo histórico.

    Lista vacía => 0.0 (sin datos, tratado como "sin drawdown" por esta función
    pura; validate() bloquea equity_rows vacío ANTES de llamarla). Cualquier
    fila malformada o con valor no finito, o una curva sin pico positivo
    (peak <= 0, no evaluable como drawdown), lanza ValueError.
    """
    if not equity_rows:
        return 0.0

    values = []
    for row in equity_rows:
        try:
            _, value = row
        except (TypeError, ValueError):
            raise ValueError("curva de equity malformada: no se puede evaluar drawdown")
        if not _is_finite_number(value):
            raise ValueError(
                "curva de equity con valores no finitos: no se puede evaluar drawdown"
            )
        values.append(value)

    peak = max(values)
    if peak <= 0:
        raise ValueError(
            "curva de equity sin pico positivo: no se puede evaluar drawdown"
        )
    return max(0.0, (peak - values[-1]) / peak)


def validate(order: OrderRequest, state: dict, equity_rows: list) -> tuple[bool, str]:
    if order.action == "close":
        return True, "cierre permitido siempre"

    # --- Validación del state (fail-closed: sin defaults silenciosos) ---
    try:
        cash, normalized_positions = _normalize_state(state)
    except ValueError as exc:
        return False, str(exc)

    # --- Validación de la curva de equity (necesaria para evaluar drawdown) ---
    if not equity_rows:
        return False, "sin curva de equity: no se puede evaluar drawdown"

    try:
        dd = drawdown_pct(equity_rows)
    except ValueError as exc:
        return False, str(exc)

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

    total = portfolio_value(state)  # state ya validado arriba: no debería fallar
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

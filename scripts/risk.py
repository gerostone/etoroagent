"""Motor de riesgo determinista. Lógica pura: sin red ni filesystem en validate().

Límites (spec §6, perfil Moderado) — NO negociables por el agente:
  máx 25% por posición, máx 35% cripto total, stop-loss obligatorio <= 12%,
  drawdown >= 25% desde el máximo histórico => modo defensivo (solo cierres),
  no-duplicación (no recomprar un símbolo con exposición existente),
  máx 70% de exposición agregada del portfolio (MAX_TOTAL_EXPOSURE_PCT).

  WP1 (auditoría pre-producción, hallazgo: corridas sucesivas recompraban
  símbolos ya tenidos — 3 corridas idénticas construían 59% de exposición
  real donde el agente creía 37%, porque el único freno, 25% por símbolo,
  frena tarde): la no-duplicación bloquea cualquier open sobre un símbolo
  con exposición existente (real o pending/local) SIN excepciones, antes
  del tope de 25% (que queda como defensa en profundidad). El tope de 70%
  agregado cierra el hueco de que N símbolos distintos, cada uno bajo el
  25%, sumaran una concentración total no acotada.

  WP4 (re-auditoría): la no-duplicación original comparaba símbolo por
  IGUALDAD DE STRING -- un alias distinto del mismo activo cripto (BTC en
  cartera, open BTCUSD/BTC-USD/BTCEUR/btcusd) se veía como "otro símbolo"
  y pasaba. canonical_symbol() colapsa cualquier variante con prefijo
  BTC/ETH a un único canónico; se usa en no-duplicación, en la suma de
  exposición cripto agregada, y en el chequeo de universo, para que los
  tres vean el MISMO activo sin importar el alias pedido (N2). Además, el
  filtro "valueUsd>0" de la no-duplicación original dejaba reabrir un
  símbolo cuya posición existente estaba en 0 o negativo -- ahora es la
  PRESENCIA del símbolo la que bloquea, cualquier valueUsd finito (N6).

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
# WP1 (auditoría pre-producción): tope de exposición AGREGADA del
# portfolio (suma de TODAS las posiciones, cualquier símbolo). Sin este
# tope, N símbolos distintos al 25% cada uno podían sumar una
# concentración total no acotada (75% en 3 símbolos era legal).
MAX_TOTAL_EXPOSURE_PCT = 0.70

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

# Variantes de símbolo conocidas para la BÚSQUEDA de instrumento (Task 10,
# fix reviewer #4) — distinto de CRYPTO_SYMBOLS de arriba (que clasifica
# posiciones YA resueltas para los topes de riesgo). eToro puede exponer el
# instrumento cripto bajo un formato de símbolo distinto al que pide el
# agente (p.ej. BTCUSD en vez de BTC) en `search_instrument`. candles.py y
# place_order.py usan este mapeo para reintentar la resolución de
# instrumentId con estas variantes, EN ORDEN, cuando la búsqueda exacta del
# símbolo pedido no da ningún match — ver sus `_resolve_instrument_id`.
CRYPTO_SEARCH_VARIANTS = {
    "BTC": ["BTC", "BTCUSD", "BTC-USD"],
    "ETH": ["ETH", "ETHUSD", "ETH-USD"],
}

# Universo cerrado de símbolos operables. Clasificar cripto por igualdad
# exacta contra CRYPTO_SYMBOLS (o cualquier otra pertenencia a un set fijo)
# falla ABIERTO ante un formato no listado: un símbolo desconocido no entra
# en ningún set y por lo tanto no cuenta contra NINGÚN tope (ni posición ni
# cripto). Por eso validate() rechaza cualquier símbolo que no esté en este
# universo cerrado, en vez de asumir que "no está en CRYPTO_SYMBOLS" implica
# "es equity y está permitido".
UNIVERSE_EQUITY = {
    "SPY", "QQQ", "XLK", "XLE", "XLF", "XLV", "XLI", "XLP", "XLU", "GLD", "TLT",
}
UNIVERSE = UNIVERSE_EQUITY | CRYPTO_SYMBOLS


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


def canonical_symbol(raw) -> str:
    """WP4/N2: normaliza (strip+upper, vía _norm_symbol) y además COLAPSA
    cualquier alias de símbolo cripto a un canónico único ("BTC"/"ETH"),
    para que no-duplicación, exposición cripto agregada y el chequeo de
    universo no puedan evadirse abriendo el MISMO activo bajo un alias de
    símbolo distinto (BTCUSD, BTC-USD, BTCEUR, btcusd, ...).

    Deliberadamente NO es un mapeo explícito 1:1 contra CRYPTO_SYMBOLS/
    CRYPTO_SEARCH_VARIANTS: es "empieza con BTC/ETH tras normalizar ->
    colapsa", así que también cubre variantes de formato FUTURAS que eToro
    pueda exponer y que hoy no estén listadas explícitamente en ningún
    set (mismo espíritu que el comentario sobre CRYPTO_SYMBOLS más abajo:
    una lista cerrada de variantes falla abierto ante un formato nuevo).

    Símbolos no-cripto (ninguno del universo de equities empieza con BTC/
    ETH) pasan sin cambios, ya normalizados."""
    symbol = _norm_symbol(raw)
    if symbol.startswith("BTC"):
        return "BTC"
    if symbol.startswith("ETH"):
        return "ETH"
    return symbol


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

    # WP4/N2: canonical_symbol() para que un alias cripto no listado
    # explícitamente en UNIVERSE (p.ej. una variante de formato nueva de
    # eToro) siga reconociéndose como BTC/ETH en vez de bloquearse por un
    # tecnicismo de formato -- ver docstring del módulo y de la función.
    if canonical_symbol(symbol) not in UNIVERSE:
        return False, (
            f"Símbolo {symbol} fuera del universo operable — actualizar "
            "mapeo/universo antes de continuar."
        )

    if not _is_finite_number(order.amount_usd) or order.amount_usd <= 0:
        return False, "Monto inválido."

    total = portfolio_value(state)  # state ya validado arriba: no debería fallar
    if total <= 0:
        return False, "Valor de portfolio desconocido o cero: no se puede dimensionar."

    # --- No-duplicación (WP1, WP4/N2+N6): CUALQUIER exposición existente
    # en este símbolo bloquea el open, sin excepciones. Va ANTES del tope
    # de 25% (que queda como defensa en profundidad) — mantener = no
    # recomprar; rebalancear = cerrar en esta corrida y reevaluar en la
    # siguiente, nunca sumar encima.
    #   N2: compara por canonical_symbol(), no por igualdad de string --
    #   un alias cripto distinto del MISMO activo (BTC en cartera, open
    #   BTCUSD) también bloquea.
    #   N6: la condición es la PRESENCIA del símbolo en positions --
    #   cualquier valueUsd finito (0, negativo o positivo) cuenta. El
    #   filtro "valueUsd>0" original dejaba reabrir un símbolo cuya
    #   posición existente estaba en 0 o negativo; sigue siendo LA MISMA
    #   posición, no una plaza libre.
    if any(
        canonical_symbol(p["symbol"]) == canonical_symbol(symbol)
        for p in normalized_positions
    ):
        return False, (
            "el símbolo ya tiene exposición: recompra/adición no permitida "
            "(regla de no-duplicación; mantener = no recomprar, rebalancear = "
            "cerrar y esperar la próxima corrida)"
        )

    current = sum(
        p["valueUsd"] for p in normalized_positions if p["symbol"] == symbol
    )
    if (current + order.amount_usd) / total > MAX_POSITION_PCT:
        return False, (
            f"Posición resultante en {symbol} superaría el {MAX_POSITION_PCT:.0%} del portfolio "
            f"({current + order.amount_usd:.2f} de {total:.2f} USD)."
        )

    # --- Tope de exposición agregada (WP1): suma de TODAS las posiciones
    # (cualquier símbolo) + la orden nueva, contra MAX_TOTAL_EXPOSURE_PCT.
    # Defensa contra concentración repartida en varios símbolos, cada uno
    # individualmente bajo el 25%.
    total_exposure = sum(p["valueUsd"] for p in normalized_positions) + order.amount_usd
    if total_exposure / total > MAX_TOTAL_EXPOSURE_PCT:
        return False, (
            f"Exposición total resultante superaría el {MAX_TOTAL_EXPOSURE_PCT:.0%} del portfolio "
            f"({total_exposure:.2f} de {total:.2f} USD)."
        )

    # WP4/N2: canonical_symbol() en vez de pertenencia literal a
    # CRYPTO_SYMBOLS -- así la suma agregada cubre también alias no
    # listados explícitamente (p.ej. un formato nuevo de eToro), no solo
    # las variantes ya conocidas.
    if canonical_symbol(symbol) in ("BTC", "ETH"):
        crypto = sum(
            p["valueUsd"]
            for p in normalized_positions
            if canonical_symbol(p["symbol"]) in ("BTC", "ETH")
        )
        if (crypto + order.amount_usd) / total > MAX_CRYPTO_PCT:
            return False, (
                f"Exposición cripto resultante superaría el {MAX_CRYPTO_PCT:.0%} del portfolio "
                f"({crypto + order.amount_usd:.2f} de {total:.2f} USD)."
            )

    return True, "ok"

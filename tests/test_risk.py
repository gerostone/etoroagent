import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from risk import (
    CRYPTO_SEARCH_VARIANTS,
    MAX_TOTAL_EXPOSURE_PCT,
    OrderRequest,
    canonical_symbol,
    validate,
    drawdown_pct,
    portfolio_value,
)


# --- CRYPTO_SEARCH_VARIANTS (Task 10, fix reviewer #4) ---------------------
#
# eToro puede exponer un instrumento cripto bajo un formato de símbolo
# distinto al que usa el universo/PLAYBOOK (BTCUSD en vez de BTC, etc).
# candles.py y place_order.py usan este mapeo para reintentar la búsqueda
# de instrumentId con variantes conocidas cuando la búsqueda exacta del
# símbolo pedido no da match — ver sus `_resolve_instrument_id`.


def test_crypto_search_variants_btc():
    assert CRYPTO_SEARCH_VARIANTS["BTC"] == ["BTC", "BTCUSD", "BTC-USD"]


def test_crypto_search_variants_eth():
    assert CRYPTO_SEARCH_VARIANTS["ETH"] == ["ETH", "ETHUSD", "ETH-USD"]


def test_crypto_search_variants_no_incluye_equities():
    assert "SPY" not in CRYPTO_SEARCH_VARIANTS
    assert "QQQ" not in CRYPTO_SEARCH_VARIANTS

STATE = {
    "cashUsd": 100.0,
    "positions": [
        {"symbol": "SPY", "valueUsd": 60.0},
        {"symbol": "BTC", "valueUsd": 40.0},
    ],
}  # total = 200
EQUITY_OK = [("2026-08-01", 190.0), ("2026-08-02", 200.0)]
EQUITY_DD = [("2026-08-01", 300.0), ("2026-08-02", 200.0)]  # drawdown 33%


def open_order(symbol="QQQ", amount=30.0, sl=0.12):
    return OrderRequest(action="open", symbol=symbol, amount_usd=amount, stop_loss_pct=sl)


def test_close_siempre_permitido():
    ok, _ = validate(OrderRequest("close", "SPY", 60.0, None), STATE, EQUITY_DD)
    assert ok


def test_orden_valida_pasa():
    ok, msg = validate(open_order(), STATE, EQUITY_OK)
    assert ok, msg


def test_bloquea_posicion_mayor_25pct():
    # SPY ya tiene 60 (30%); con la regla de no-duplicación (WP1) cualquier
    # recompra de un símbolo con exposición existente se bloquea ANTES de
    # llegar al tope de 25% — ver test_no_duplicacion_precede_al_tope_25pct
    # y test_bloquea_nueva_posicion_que_supera_25pct para el caso puro de
    # 25% sobre un símbolo sin posición previa.
    ok, msg = validate(open_order(symbol="SPY", amount=10.0), STATE, EQUITY_OK)
    assert not ok and "no-duplicaci" in msg.lower()


def test_bloquea_nueva_posicion_que_supera_25pct():
    ok, msg = validate(open_order(amount=60.0), STATE, EQUITY_OK)  # 60/200 = 30%
    assert not ok and "25%" in msg


def test_bloquea_cripto_sobre_35pct():
    # BTC 40 + ETH 35 = 75/200 = 37.5% → bloquear
    ok, msg = validate(open_order(symbol="ETH", amount=35.0), STATE, EQUITY_OK)
    assert not ok and "cripto" in msg.lower()


def test_bloquea_sin_stop_loss():
    ok, msg = validate(open_order(sl=None), STATE, EQUITY_OK)
    assert not ok and "stop" in msg.lower()


def test_bloquea_stop_loss_mayor_a_12pct():
    ok, msg = validate(open_order(sl=0.20), STATE, EQUITY_OK)
    assert not ok and "stop" in msg.lower()


def test_modo_defensivo_bloquea_compras():
    ok, msg = validate(open_order(), STATE, EQUITY_DD)
    assert not ok and "defensivo" in msg.lower()


def test_drawdown_pct():
    assert abs(drawdown_pct(EQUITY_DD) - (1 / 3)) < 1e-9
    assert drawdown_pct([]) == 0.0


# --- Fixes: fail-closed ante state/equity malformados, NaN y símbolos no normalizados ---


def test_symbol_none_en_state_bloquea_open():
    state = {
        "cashUsd": 100.0,
        "positions": [
            {"symbol": None, "valueUsd": 60.0},
            {"symbol": "BTC", "valueUsd": 40.0},
        ],
    }
    ok, msg = validate(open_order(), state, EQUITY_OK)
    assert not ok and "símbolo" in msg.lower()


def test_symbol_minuscula_en_state_se_normaliza_y_sigue_bloqueando():
    state = {
        "cashUsd": 100.0,
        "positions": [
            {"symbol": "spy", "valueUsd": 60.0},
            {"symbol": "btc", "valueUsd": 40.0},
        ],
    }
    # "spy" en minúscula debe normalizarse a "SPY" y seguir reconociéndose
    # como exposición existente — ahora bloqueada por no-duplicación (WP1)
    # antes de llegar al tope de 25%.
    ok, msg = validate(open_order(symbol="SPY", amount=10.0), state, EQUITY_OK)
    assert not ok and "no-duplicaci" in msg.lower()


def test_state_sin_positions_bloquea_open():
    state = {"cashUsd": 100.0}
    ok, msg = validate(open_order(), state, EQUITY_OK)
    assert not ok and "malformado" in msg.lower()


def test_state_sin_cashusd_bloquea_open():
    state = {"positions": [{"symbol": "SPY", "valueUsd": 60.0}]}
    ok, msg = validate(open_order(), state, EQUITY_OK)
    assert not ok and "malformado" in msg.lower()


def test_valueusd_no_numerico_bloquea_open():
    state = {
        "cashUsd": 100.0,
        "positions": [{"symbol": "SPY", "valueUsd": "sesenta"}],
    }
    ok, msg = validate(open_order(), state, EQUITY_OK)
    assert not ok and "malformado" in msg.lower()


def test_equity_vacio_bloquea_open():
    ok, msg = validate(open_order(), STATE, [])
    assert not ok and "equity" in msg.lower()


def test_close_permitido_incluso_con_state_y_equity_malformados():
    ok, _ = validate(OrderRequest("close", "SPY", 60.0, None), {}, [])
    assert ok


def test_bloquea_amount_nan():
    ok, _ = validate(open_order(amount=float("nan")), STATE, EQUITY_OK)
    assert not ok


def test_bloquea_amount_inf():
    ok, _ = validate(open_order(amount=float("inf")), STATE, EQUITY_OK)
    assert not ok


def test_bloquea_equity_con_nan_en_ultima_fila_pese_a_drawdown_real_alto():
    equity = [("2026-08-01", 500.0), ("2026-08-02", 100.0), ("2026-08-03", float("nan"))]
    ok, _ = validate(open_order(), STATE, equity)
    assert not ok


def test_limite_posicion_exacto_25pct_pasa():
    # Estado dedicado (en vez de STATE global, que ya tiene 50% de exposición
    # existente) para aislar el tope de 25% por posición del tope de 70%
    # agregado (WP1): 50/200 = 25% exacto sobre un símbolo nuevo, con
    # exposición previa total baja (25%) para no chocar con el 70%.
    state = {
        "cashUsd": 150.0,
        "positions": [{"symbol": "SPY", "valueUsd": 50.0}],
    }  # total = 200
    ok, msg = validate(open_order(amount=50.0), state, EQUITY_OK)  # 50/200 = 25% exacto
    assert ok, msg


def test_drawdown_exacto_25pct_bloquea():
    equity = [("2026-08-01", 400.0), ("2026-08-02", 300.0)]  # drawdown exacto 25%
    ok, msg = validate(open_order(), STATE, equity)
    assert not ok and "defensivo" in msg.lower()


def test_bloquea_total_portfolio_cero():
    state = {"cashUsd": 0.0, "positions": []}
    ok, msg = validate(open_order(), state, EQUITY_OK)
    assert not ok and "dimensionar" in msg.lower()


def test_bloquea_amount_cero_o_negativo():
    ok, msg = validate(open_order(amount=0.0), STATE, EQUITY_OK)
    assert not ok and "monto" in msg.lower()
    ok2, msg2 = validate(open_order(amount=-5.0), STATE, EQUITY_OK)
    assert not ok2 and "monto" in msg2.lower()


# --- Re-review e103e6a: peak<=0, unificación drawdown/portfolio_value, rechazo de bool ---


def test_peak_cero_bloquea_open():
    equity = [("2026-08-01", 0.0), ("2026-08-02", 0.0)]
    ok, msg = validate(open_order(), STATE, equity)
    assert not ok and "equity" in msg.lower()


def test_peak_negativo_bloquea_open():
    equity = [("2026-08-01", -100.0), ("2026-08-02", -500.0)]
    ok, msg = validate(open_order(), STATE, equity)
    assert not ok and "equity" in msg.lower()


def test_drawdown_pct_peak_no_positivo_lanza_valueerror():
    with pytest.raises(ValueError):
        drawdown_pct([("2026-08-01", 0.0), ("2026-08-02", 0.0)])
    with pytest.raises(ValueError):
        drawdown_pct([("2026-08-01", -100.0), ("2026-08-02", -500.0)])


def test_drawdown_pct_valor_no_finito_lanza_valueerror():
    with pytest.raises(ValueError):
        drawdown_pct([("2026-08-01", 100.0), ("2026-08-02", float("nan"))])


def test_drawdown_pct_fila_malformada_lanza_valueerror():
    with pytest.raises(ValueError):
        drawdown_pct([("2026-08-01",)])


def test_drawdown_pct_vacio_sigue_devolviendo_cero():
    # validate() debe bloquear equity vacío ANTES de llamar drawdown_pct (ver
    # test_equity_vacio_bloquea_open); esta función pura mantiene 0.0 para []
    # por si algún otro caller la usa directamente con una curva vacía.
    assert drawdown_pct([]) == 0.0


def test_portfolio_value_state_malformado_lanza_valueerror():
    with pytest.raises(ValueError):
        portfolio_value({})
    with pytest.raises(ValueError):
        portfolio_value({"cashUsd": 100.0})  # falta "positions"
    with pytest.raises(ValueError):
        portfolio_value({"positions": []})  # falta "cashUsd"
    with pytest.raises(ValueError):
        portfolio_value({"cashUsd": 100.0, "positions": [{"symbol": "SPY", "valueUsd": "no"}]})


def test_portfolio_value_ok():
    assert portfolio_value(STATE) == 200.0


def test_bool_en_valueusd_o_cashusd_rechazado_por_portfolio_value():
    with pytest.raises(ValueError):
        portfolio_value({"cashUsd": True, "positions": []})
    with pytest.raises(ValueError):
        portfolio_value({"cashUsd": 100.0, "positions": [{"symbol": "SPY", "valueUsd": True}]})


def test_bloquea_valueusd_bool_en_state():
    # {"valueUsd": true} no debe contar como 1.0
    state = {"cashUsd": 100.0, "positions": [{"symbol": "SPY", "valueUsd": True}]}
    ok, msg = validate(open_order(), state, EQUITY_OK)
    assert not ok and "malformado" in msg.lower()


def test_bloquea_cashusd_bool():
    state = {"cashUsd": True, "positions": []}
    ok, msg = validate(open_order(), state, EQUITY_OK)
    assert not ok and "malformado" in msg.lower()


def test_bloquea_amount_bool():
    ok, msg = validate(open_order(amount=True), STATE, EQUITY_OK)
    assert not ok


def test_bloquea_stop_loss_bool():
    ok, msg = validate(open_order(sl=True), STATE, EQUITY_OK)
    assert not ok


# --- Fix quality review Task 4: variantes de símbolo cripto + valueUsd negativo ---


def test_bloquea_cripto_btcusd_variante_sobre_35pct():
    # eToro puede devolver "BTCUSD" en vez de "BTC" — debe seguir contando
    # contra el tope de 35% cripto combinado, no pasar desapercibido.
    state = {
        "cashUsd": 100.0,
        "positions": [
            {"symbol": "BTCUSD", "valueUsd": 40.0},
            {"symbol": "SPY", "valueUsd": 60.0},
        ],
    }  # total 200
    ok, msg = validate(open_order(symbol="ETH", amount=35.0), state, EQUITY_OK)
    assert not ok and "cripto" in msg.lower()


def test_valueusd_negativo_finito_es_aceptado_por_portfolio_value():
    # valueUsd negativo (posición con pnl muy negativo, posible con
    # apalancamiento) no debe rechazarse como "malformado" — reduce el total
    # de sizing en vez de ser clampado u omitido.
    state = {"cashUsd": 100.0, "positions": [{"symbol": "SPY", "valueUsd": -50.0}]}
    assert portfolio_value(state) == 50.0


# --- Fix quality review Task 4 (2da ronda): universo cerrado de símbolos ---


def test_bloquea_simbolo_fuera_del_universo_operable():
    # Un símbolo no listado (ni equity conocido ni variante cripto conocida)
    # no debe colarse: clasificar por pertenencia a un set fijo falla abierto
    # ante cualquier formato no anticipado si no hay un universo cerrado.
    ok, msg = validate(open_order(symbol="AAPL"), STATE, EQUITY_OK)
    assert not ok and "universo" in msg.lower()


def test_permite_simbolos_del_universo_equity_y_cripto():
    ok, msg = validate(open_order(symbol="QQQ", amount=10.0), STATE, EQUITY_OK)
    assert ok, msg
    # ETH (no BTC: STATE ya tiene una posición BTC, y WP1 bloquearía esa
    # recompra por no-duplicación antes de llegar a esta validación de
    # universo) — sigue cubriendo el caso "símbolo cripto del universo".
    ok2, msg2 = validate(open_order(symbol="ETH", amount=10.0), STATE, EQUITY_OK)
    assert ok2, msg2


# --- WP1: no-duplicación de símbolos ----------------------------------------
#
# Auditoría pre-producción: corridas sucesivas recompraban símbolos ya
# tenidos porque el único freno (25% por símbolo) frena tarde — 3 corridas
# idénticas podían construir 59% de exposición real donde el agente creía
# 37%. La regla de no-duplicación bloquea CUALQUIER open sobre un símbolo
# que ya tenga exposición (real o pending/local, valueUsd>0), sin
# excepciones, ANTES del tope de 25% (que queda como defensa en
# profundidad, no como primera línea).


def test_bloquea_open_simbolo_con_posicion_real_existente():
    state = {
        "cashUsd": 1000.0,
        "positions": [{"symbol": "SPY", "valueUsd": 100.0}],
    }
    ok, msg = validate(open_order(symbol="SPY", amount=10.0), state, EQUITY_OK)
    assert not ok and "no-duplicaci" in msg.lower()


def test_bloquea_open_simbolo_con_posicion_pending_existente():
    # Exposición "pending" (aperturas pendientes del snapshot, o exposición
    # local registrada por place_order.py tras un open exitoso/ambiguo en
    # la misma corrida) cuenta igual que una posición confirmada.
    state = {
        "cashUsd": 1000.0,
        "positions": [{"symbol": "SPY", "valueUsd": 100.0, "pending": True}],
    }
    ok, msg = validate(open_order(symbol="SPY", amount=10.0), state, EQUITY_OK)
    assert not ok and "no-duplicaci" in msg.lower()


def test_no_duplicacion_precede_al_tope_25pct_en_el_mensaje():
    state = {
        "cashUsd": 1000.0,
        "positions": [{"symbol": "SPY", "valueUsd": 100.0}],
    }
    ok, msg = validate(open_order(symbol="SPY", amount=500.0), state, EQUITY_OK)
    assert not ok
    assert "no-duplicaci" in msg.lower()
    assert "25%" not in msg


def test_permite_open_simbolo_nuevo_sin_posicion_previa():
    state = {
        "cashUsd": 1000.0,
        "positions": [{"symbol": "SPY", "valueUsd": 100.0}],
    }
    ok, msg = validate(open_order(symbol="QQQ", amount=10.0), state, EQUITY_OK)
    assert ok, msg


# --- WP4/N6: re-auditoría -- valueUsd<=0 evadía la no-duplicación ---------
#
# Hallazgo: el filtro original "valueUsd>0" dejaba reabrir un símbolo cuya
# posición existente estaba en 0 o negativo. La regla correcta es la
# PRESENCIA del símbolo en positions (cualquier valueUsd FINITO, incluido
# 0 o negativo) bloquea la recompra -- una posición con pnl negativo sigue
# siendo LA MISMA posición, no una plaza libre para abrir de nuevo.


def test_bloquea_recompra_con_valueusd_cero_por_presencia():
    state = {
        "cashUsd": 1000.0,
        "positions": [{"symbol": "SPY", "valueUsd": 0.0}],
    }
    ok, msg = validate(open_order(symbol="SPY", amount=10.0), state, EQUITY_OK)
    assert not ok and "no-duplicaci" in msg.lower()


def test_bloquea_recompra_con_valueusd_negativo_por_presencia():
    state = {
        "cashUsd": 1000.0,
        "positions": [{"symbol": "SPY", "valueUsd": -50.0}],
    }
    ok, msg = validate(open_order(symbol="SPY", amount=10.0), state, EQUITY_OK)
    assert not ok and "no-duplicaci" in msg.lower()


# --- WP1: tope de exposición agregada (MAX_TOTAL_EXPOSURE_PCT = 70%) -------
#
# Sin este tope, 3 símbolos distintos al 25% cada uno (75% del portfolio)
# eran legales bajo el único tope por símbolo. positions con 70% existente
# repartido en varios símbolos (ninguno individualmente sobre 25%) más una
# apertura nueva.

_STATE_69PCT = {
    "cashUsd": 3200.0,
    "positions": [
        {"symbol": "XLE", "valueUsd": 2000.0},
        {"symbol": "XLI", "valueUsd": 2000.0},
        {"symbol": "XLP", "valueUsd": 2000.0},
        {"symbol": "XLU", "valueUsd": 800.0},
    ],
}  # total = 10000, exposición existente = 6800

_STATE_71PCT = {
    "cashUsd": 3000.0,
    "positions": [
        {"symbol": "XLE", "valueUsd": 2000.0},
        {"symbol": "XLI", "valueUsd": 2000.0},
        {"symbol": "XLP", "valueUsd": 2000.0},
        {"symbol": "XLU", "valueUsd": 1000.0},
    ],
}  # total = 10000, exposición existente = 7000


def test_tope_exposicion_total_71pct_bloquea():
    # (7000 existente + 100 nuevo) / 10000 = 71% > 70% -> bloquear.
    # QQQ es símbolo nuevo (sin no-duplicación) y 100/10000=1% no toca el
    # tope de 25% por posición.
    ok, msg = validate(open_order(symbol="QQQ", amount=100.0), _STATE_71PCT, EQUITY_OK)
    assert not ok
    assert "70%" in msg


def test_tope_exposicion_total_69pct_pasa():
    # (6800 existente + 100 nuevo) / 10000 = 69% <= 70% -> pasa.
    ok, msg = validate(open_order(symbol="QQQ", amount=100.0), _STATE_69PCT, EQUITY_OK)
    assert ok, msg


def test_tope_exposicion_total_mensaje_deriva_de_la_constante():
    ok, msg = validate(open_order(symbol="QQQ", amount=100.0), _STATE_71PCT, EQUITY_OK)
    assert not ok
    assert f"{MAX_TOTAL_EXPOSURE_PCT:.0%}" in msg


def test_max_total_exposure_pct_vale_70pct():
    assert MAX_TOTAL_EXPOSURE_PCT == 0.70


# --- WP4/N2: canonicalización de alias cripto -------------------------------
#
# Re-auditoría: eToro puede exponer BTC bajo distintos formatos de símbolo
# (BTCUSD, BTC-USD, BTCEUR, minúsculas...) y risk.py los trataba como
# activos DISTINTOS para no-duplicación y exposición cripto agregada --
# comparación literal de string, no de activo real. Escenario del auditor:
# BTC 9.83 en cartera, open BTCUSD 1000 pasaba (símbolo "distinto"), y
# repetir con BTC-USD y BTCEUR podía acumular exposición sin techo real.
# canonical_symbol() colapsa cualquier variante con prefijo BTC/ETH (tras
# normalizar) a un único canónico, para que no-duplicación, exposición
# cripto agregada, y el chequeo de universo vean el MISMO activo sin
# importar bajo qué alias se pida.


def test_canonical_symbol_colapsa_alias_btc():
    assert canonical_symbol("BTC") == "BTC"
    assert canonical_symbol("btcusd") == "BTC"
    assert canonical_symbol("BTC-USD") == "BTC"
    assert canonical_symbol("BTCEUR") == "BTC"
    assert canonical_symbol("  btc-usd  ") == "BTC"


def test_canonical_symbol_colapsa_alias_eth():
    assert canonical_symbol("ETH") == "ETH"
    assert canonical_symbol("ethusd") == "ETH"
    assert canonical_symbol("ETH-USD") == "ETH"
    assert canonical_symbol("ETHEUR") == "ETH"


def test_canonical_symbol_no_afecta_equities():
    assert canonical_symbol("spy") == "SPY"
    assert canonical_symbol("QQQ") == "QQQ"
    assert canonical_symbol("xlk") == "XLK"


def test_canonical_symbol_none_o_vacio():
    assert canonical_symbol(None) == ""
    assert canonical_symbol("") == ""


def test_bloquea_recompra_btc_via_alias_btcusd_escenario_auditor():
    # Escenario EXACTO reportado por el auditor: BTC 9.83 en cartera, open
    # BTCUSD 1000 -- antes de N2 esto pasaba (símbolo "distinto").
    state = {"cashUsd": 10000.0, "positions": [{"symbol": "BTC", "valueUsd": 9.83}]}
    ok, msg = validate(open_order(symbol="BTCUSD", amount=1000.0), state, EQUITY_OK)
    assert not ok and "no-duplicaci" in msg.lower()


def test_bloquea_recompra_btc_via_todos_los_alias_cruzados():
    alias_existentes = ["BTC", "BTCUSD", "BTC-USD", "BTCEUR"]
    alias_nuevos = ["BTC", "BTCUSD", "BTC-USD", "BTCEUR", "btcusd"]
    for existente in alias_existentes:
        state = {"cashUsd": 10000.0, "positions": [{"symbol": existente, "valueUsd": 9.83}]}
        for nuevo in alias_nuevos:
            ok, msg = validate(open_order(symbol=nuevo, amount=1000.0), state, EQUITY_OK)
            assert not ok, f"{existente} en cartera debería bloquear open de {nuevo}"
            assert "no-duplicaci" in msg.lower()


def test_permite_open_eth_con_btc_existente_alias_distinto():
    # Control negativo: BTC (cualquier alias) NO debe bloquear ETH -- son
    # activos distintos, canonical_symbol no debe fusionarlos.
    state = {"cashUsd": 10000.0, "positions": [{"symbol": "BTCUSD", "valueUsd": 9.83}]}
    ok, msg = validate(open_order(symbol="ETH", amount=10.0), state, EQUITY_OK)
    assert ok, msg


def test_universo_sigue_bloqueando_simbolo_no_cripto_desconocido():
    # Control: un símbolo desconocido que NO empieza con BTC/ETH sigue
    # bloqueado igual que antes (canonical_symbol no lo cambia).
    state = {"cashUsd": 1000.0, "positions": []}
    ok, msg = validate(open_order(symbol="AAPL"), state, EQUITY_OK)
    assert not ok and "universo" in msg.lower()


# --- WP5/N7 (re-auditoría, hallazgo CRÍTICO): canonicalización SOLO por --
# --- whitelist exacta, universo sobre el símbolo ORIGINAL --------------
#
# canonical_symbol() colapsaba por PREFIJO ("empieza con BTC/ETH ->
# colapsa"), y el chequeo de universo comparaba canonical_symbol(symbol)
# contra UNIVERSE. "BTCS" -- un activo REAL y DISTINTO, fuera del universo
# operable -- canonicalizaba igual a "BTC", pasaba el chequeo de universo y
# la orden llegaba a la API. Ahora canonical_symbol() colapsa solo alias
# CONOCIDOS (whitelist construida desde CRYPTO_SYMBOLS +
# CRYPTO_SEARCH_VARIANTS) y el chequeo de universo compara el símbolo
# ORIGINAL normalizado -- "BTCGBP"/"BTCS" (no whitelisteados) ya no
# canonicalizan a "BTC" y quedan bloqueados por universo, igual que
# cualquier otro símbolo desconocido (reemplaza los tests viejos que
# esperaban que "BTCGBP" pasara vía canonicalización por prefijo -- eso era
# la manifestación no-crítica del mismo bug que "BTCS" explota).


def test_n7_btcs_bloqueada_por_universo_cliente_no_se_llama():
    # Escenario EXACTO del hallazgo N7: "BTCS" no es una variante de BTC,
    # es un activo distinto y no está en el universo operable -- debe
    # bloquearse ANTES de resolver instrumentId o tocar el cliente HTTP.
    state = {"cashUsd": 1000.0, "positions": []}
    ok, msg = validate(open_order(symbol="BTCS", amount=10.0), state, EQUITY_OK)
    assert not ok and "universo" in msg.lower()


def test_n7_canonical_symbol_no_colapsa_btcs_por_prefijo():
    # Control directo sobre la función: "BTCS" no está en la whitelist
    # exacta -- a diferencia de la lógica de prefijo anterior, no debe
    # colapsar a "BTC".
    assert canonical_symbol("BTCS") == "BTCS"
    assert canonical_symbol("btcs") == "BTCS"
    assert canonical_symbol("ETHX") == "ETHX"


def test_n7_btcusd_sigue_canonicalizando_a_btc():
    assert canonical_symbol("BTCUSD") == "BTC"


def test_n7_btc_usd_case_insensitive_sigue_canonicalizando_a_btc():
    assert canonical_symbol("btc-usd") == "BTC"
    assert canonical_symbol("Btc-Usd") == "BTC"


def test_n7_tsla_sigue_bloqueada_fuera_del_universo():
    state = {"cashUsd": 1000.0, "positions": []}
    ok, msg = validate(open_order(symbol="TSLA", amount=10.0), state, EQUITY_OK)
    assert not ok and "universo" in msg.lower()


def test_n7_btcs_no_llega_al_cliente_mock_explota_si_se_llama():
    # Verifica el contrato completo end-to-end (validate() es lógica pura,
    # sin cliente HTTP -- este test confirma que un caller que respete su
    # resultado nunca llega a invocar la API para un símbolo bloqueado).
    class _ExplosiveClient:
        def __getattr__(self, name):
            raise AssertionError(
                f"BTCS está fuera del universo: el cliente NO debía "
                f"invocarse (se intentó acceder a {name!r})"
            )

    client = _ExplosiveClient()
    state = {"cashUsd": 1000.0, "positions": []}
    ok, msg = validate(open_order(symbol="BTCS", amount=10.0), state, EQUITY_OK)
    assert not ok and "universo" in msg.lower()
    # Si validate() hubiera dado luz verde, cualquier intento de usar el
    # cliente acá haría explotar el mock -- documentamos el contrato.
    if ok:
        client.search_instrument("BTCS")

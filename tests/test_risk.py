import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from risk import OrderRequest, validate, drawdown_pct, portfolio_value

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
    # SPY ya tiene 60 (30%); sumar cualquier monto la deja > 25% → bloquear
    ok, msg = validate(open_order(symbol="SPY", amount=10.0), STATE, EQUITY_OK)
    assert not ok and "25%" in msg


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
    # "spy" en minúscula debe normalizarse a "SPY" y seguir contando como 30% ya invertido
    ok, msg = validate(open_order(symbol="SPY", amount=10.0), state, EQUITY_OK)
    assert not ok and "25%" in msg


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
    ok, msg = validate(open_order(amount=50.0), STATE, EQUITY_OK)  # 50/200 = 25% exacto
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
    ok2, msg2 = validate(open_order(symbol="BTC", amount=10.0), STATE, EQUITY_OK)
    assert ok2, msg2

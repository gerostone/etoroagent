import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from risk import OrderRequest, validate, drawdown_pct

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

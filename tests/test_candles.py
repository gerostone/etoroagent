"""Tests de scripts/candles.py — helper de solo lectura, mockeado (sin red).

candles.py es la tercera vía autorizada (junto a place_order.py y
snapshot.py) para que el agente lea velas de un símbolo SIN recurrir a
`python -c`/heredoc con EtoroClient inline (bloqueado a propósito por
scripts/risk_hook.py).
"""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import candles  # noqa: E402


def test_symbol_resuelto_imprime_json_con_velas(capsys):
    client = MagicMock()
    client.search_instrument.return_value = {
        "items": [{"instrumentId": 42, "internalSymbolFull": "SPY"}]
    }
    client.get_candles.return_value = {
        "candles": [{"candles": [{"close": 100.0, "fromDate": "2026-08-01T00:00:00Z"}]}]
    }
    rc = candles.main(
        ["--symbol", "SPY", "--count", "210"],
        make_client=lambda: client,
    )
    assert rc == 0
    client.search_instrument.assert_called_once_with("SPY")
    client.get_candles.assert_called_once_with(42, interval="OneDay", count=210)
    # Nunca debe llamar a ningun metodo de escritura.
    client.open_position_by_amount.assert_not_called()
    client.close_position.assert_not_called()

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["symbol"] == "SPY"
    assert payload["instrumentId"] == 42
    assert payload["candles"] == client.get_candles.return_value


def test_symbol_se_normaliza_a_mayusculas(capsys):
    client = MagicMock()
    client.search_instrument.return_value = {
        "items": [{"instrumentId": 7, "internalSymbolFull": "QQQ"}]
    }
    client.get_candles.return_value = {"candles": []}
    rc = candles.main(
        ["--symbol", "qqq", "--count", "50"],
        make_client=lambda: client,
    )
    assert rc == 0
    client.search_instrument.assert_called_once_with("QQQ")


def test_interval_custom_se_pasa_a_get_candles():
    client = MagicMock()
    client.search_instrument.return_value = {
        "items": [{"instrumentId": 7, "internalSymbolFull": "QQQ"}]
    }
    client.get_candles.return_value = {"candles": []}
    rc = candles.main(
        ["--symbol", "QQQ", "--count", "10", "--interval", "OneWeek"],
        make_client=lambda: client,
    )
    assert rc == 0
    client.get_candles.assert_called_once_with(7, interval="OneWeek", count=10)


def test_simbolo_no_encontrado_falla_sin_llamar_get_candles(capsys):
    client = MagicMock()
    client.search_instrument.return_value = {"items": []}
    rc = candles.main(
        ["--symbol", "ZZZZ", "--count", "10"],
        make_client=lambda: client,
    )
    assert rc == 1
    client.get_candles.assert_not_called()
    err = capsys.readouterr().err
    assert "ZZZZ" in err


def test_simbolo_ambiguo_falla_sin_llamar_get_candles(capsys):
    client = MagicMock()
    client.search_instrument.return_value = {
        "items": [
            {"instrumentId": 1, "internalSymbolFull": "SPY"},
            {"instrumentId": 2, "internalSymbolFull": "SPY"},
        ]
    }
    rc = candles.main(
        ["--symbol", "SPY", "--count", "10"],
        make_client=lambda: client,
    )
    assert rc == 1
    client.get_candles.assert_not_called()
    err = capsys.readouterr().err
    assert "ambiguo" in err


def test_error_de_cliente_no_rompe_con_traceback(capsys):
    client = MagicMock()
    client.search_instrument.side_effect = RuntimeError("boom de red")
    rc = candles.main(
        ["--symbol", "SPY", "--count", "10"],
        make_client=lambda: client,
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "boom de red" in err


def test_faltan_argumentos_requeridos_sale_con_error(capsys):
    client = MagicMock()
    try:
        candles.main(["--symbol", "SPY"], make_client=lambda: client)
        raised = False
    except SystemExit:
        raised = True
    assert raised

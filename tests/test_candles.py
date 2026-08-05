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


def test_full_imprime_json_crudo_con_velas(capsys):
    client = MagicMock()
    client.search_instrument.return_value = {
        "items": [{"internalInstrumentId": 42, "internalSymbolFull": "SPY", "isHiddenFromClient": False}]
    }
    client.get_candles.return_value = {
        "candles": [{"candles": [{"close": 100.0, "fromDate": "2026-08-01T00:00:00Z"}]}]
    }
    rc = candles.main(
        ["--symbol", "SPY", "--count", "210", "--full"],
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


def test_symbol_se_normaliza_a_mayusculas():
    client = MagicMock()
    client.search_instrument.return_value = {
        "items": [{"internalInstrumentId": 7, "internalSymbolFull": "QQQ", "isHiddenFromClient": False}]
    }
    client.get_candles.return_value = {"candles": [{"candles": []}]}
    rc = candles.main(
        ["--symbol", "qqq", "--count", "50"],
        make_client=lambda: client,
    )
    assert rc == 0
    client.search_instrument.assert_called_once_with("QQQ")


def test_interval_custom_se_pasa_a_get_candles():
    client = MagicMock()
    client.search_instrument.return_value = {
        "items": [{"internalInstrumentId": 7, "internalSymbolFull": "QQQ", "isHiddenFromClient": False}]
    }
    client.get_candles.return_value = {"candles": [{"candles": []}]}
    rc = candles.main(
        ["--symbol", "QQQ", "--count", "10", "--interval", "OneWeek"],
        make_client=lambda: client,
    )
    assert rc == 0
    client.get_candles.assert_called_once_with(7, interval="OneWeek", count=10)


# --- Modo default (closes-only, CSV compacto en orden ascendente) ---------
#
# Task 10 (fix reviewer): el JSON crudo es ~10x mas pesado de lo que el
# agente necesita para calcular señales (solo usa close/fromDate). El modo
# default ahora imprime un header de metadata en comentario mas una linea
# "fecha,close" por vela, en orden ASCENDENTE (viejo->nuevo) — la API
# entrega en direction=desc (nuevo->viejo, default del cliente HTTP), asi
# que candles.py reordena antes de imprimir. --full devuelve el JSON crudo
# (comportamiento anterior) para casos que lo necesiten.


def _candle(close, from_date):
    return {"close": close, "fromDate": from_date}


def test_default_imprime_csv_compacto_en_orden_ascendente(capsys):
    client = MagicMock()
    client.search_instrument.return_value = {
        "items": [{"internalInstrumentId": 42, "internalSymbolFull": "SPY", "isHiddenFromClient": False}]
    }
    # La API entrega en desc (nuevo -> viejo): dia 3 (mas reciente) primero.
    client.get_candles.return_value = {
        "candles": [
            {
                "candles": [
                    _candle(103.0, "2026-08-03T00:00:00Z"),
                    _candle(102.0, "2026-08-02T00:00:00Z"),
                    _candle(101.0, "2026-08-01T00:00:00Z"),
                ]
            }
        ]
    }
    rc = candles.main(
        ["--symbol", "SPY", "--count", "3"],
        make_client=lambda: client,
    )
    assert rc == 0
    out = capsys.readouterr().out.strip("\n").split("\n")
    assert out[0] == "# symbol=SPY interval=OneDay count=3 order=asc"
    # Reordenado a ASCENDENTE: el mas viejo (dia 1) primero.
    assert out[1] == "2026-08-01T00:00:00Z,101.0"
    assert out[2] == "2026-08-02T00:00:00Z,102.0"
    assert out[3] == "2026-08-03T00:00:00Z,103.0"
    assert len(out) == 4


def test_default_no_es_full_no_llama_json_dumps_de_velas_crudas(capsys):
    # Verificacion indirecta de que el modo default no es simplemente el
    # JSON viejo: el stdout no debe ser JSON parseable como el payload
    # {"symbol":..., "instrumentId":..., "candles": {...}} de --full.
    client = MagicMock()
    client.search_instrument.return_value = {
        "items": [{"internalInstrumentId": 7, "internalSymbolFull": "QQQ", "isHiddenFromClient": False}]
    }
    client.get_candles.return_value = {
        "candles": [{"candles": [_candle(50.0, "2026-08-01T00:00:00Z")]}]
    }
    rc = candles.main(["--symbol", "QQQ", "--count", "1"], make_client=lambda: client)
    assert rc == 0
    out = capsys.readouterr().out
    try:
        json.loads(out)
        es_json = True
    except json.JSONDecodeError:
        es_json = False
    assert not es_json


def test_default_header_refleja_interval_custom_y_count_real():
    client = MagicMock()
    client.search_instrument.return_value = {
        "items": [{"internalInstrumentId": 7, "internalSymbolFull": "QQQ", "isHiddenFromClient": False}]
    }
    client.get_candles.return_value = {
        "candles": [
            {
                "candles": [
                    _candle(2.0, "2026-08-02"),
                    _candle(1.0, "2026-08-01"),
                ]
            }
        ]
    }
    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = candles.main(
            ["--symbol", "QQQ", "--count", "10", "--interval", "OneWeek"],
            make_client=lambda: client,
        )
    assert rc == 0
    header = buf.getvalue().split("\n")[0]
    assert header == "# symbol=QQQ interval=OneWeek count=2 order=asc"


def test_default_formato_inesperado_de_velas_falla_sin_traceback(capsys):
    client = MagicMock()
    client.search_instrument.return_value = {
        "items": [{"internalInstrumentId": 42, "internalSymbolFull": "SPY", "isHiddenFromClient": False}]
    }
    client.get_candles.return_value = {"algo": "inesperado"}
    rc = candles.main(["--symbol", "SPY", "--count", "10"], make_client=lambda: client)
    assert rc == 1
    err = capsys.readouterr().err
    assert "ERROR en candles" in err


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
            {"internalInstrumentId": 1, "internalSymbolFull": "SPY"},
            {"internalInstrumentId": 2, "internalSymbolFull": "SPY"},
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


# --- Fallback de variantes cripto (Task 10, fix reviewer #4) ---------------


def test_btc_sin_match_exacto_resuelve_via_variante_btcusd(capsys):
    client = MagicMock()
    client.search_instrument.side_effect = [
        {"items": []},  # "BTC" exacto: sin match
        {"items": [{"internalInstrumentId": 99, "internalSymbolFull": "BTCUSD"}]},  # variante
    ]
    client.get_candles.return_value = {
        "candles": [{"candles": [_candle(50000.0, "2026-08-01T00:00:00Z")]}]
    }
    rc = candles.main(["--symbol", "BTC", "--count", "10"], make_client=lambda: client)
    assert rc == 0
    assert client.search_instrument.call_args_list == [
        (("BTC",), {}),
        (("BTCUSD",), {}),
    ]
    client.get_candles.assert_called_once_with(99, interval="OneDay", count=10)
    assert "BTCUSD" in capsys.readouterr().err


def test_equities_sin_match_no_prueba_variantes_sin_cambios(capsys):
    # SPY no tiene entrada en CRYPTO_SEARCH_VARIANTS -> una sola llamada a
    # search_instrument, comportamiento identico al de antes de este fix.
    client = MagicMock()
    client.search_instrument.return_value = {"items": []}
    rc = candles.main(["--symbol", "SPY", "--count", "10"], make_client=lambda: client)
    assert rc == 1
    client.search_instrument.assert_called_once_with("SPY")
    client.get_candles.assert_not_called()
    assert "SPY" in capsys.readouterr().err


def test_btc_sin_ninguna_variante_con_match_falla_igual():
    client = MagicMock()
    client.search_instrument.return_value = {"items": []}  # ninguna variante matchea
    rc = candles.main(["--symbol", "BTC", "--count", "10"], make_client=lambda: client)
    assert rc == 1
    # BTC + BTCUSD + BTC-USD = 3 intentos
    assert client.search_instrument.call_count == 3
    client.get_candles.assert_not_called()


def test_fuzzy_search_btca_primero_resuelve_btc_100000():
    # Escenario real verificado con probes en vivo: buscar "BTC" devuelve
    # decenas de items no-exactos (aca simulados con 3) antes del match
    # exacto "BTC" -> instrumentId 100000. Nunca debe tomarse items[0].
    client = MagicMock()
    client.search_instrument.return_value = {
        "items": [
            {"internalSymbolFull": "BTCA", "internalInstrumentId": 55, "isHiddenFromClient": False},
            {"internalSymbolFull": "BTC", "internalInstrumentId": 100000, "isHiddenFromClient": False},
            {"internalSymbolFull": "BTCB", "internalInstrumentId": 56, "isHiddenFromClient": False},
        ]
    }
    client.get_candles.return_value = {
        "candles": [{"candles": [_candle(50000.0, "2026-08-01T00:00:00Z")]}]
    }
    rc = candles.main(["--symbol", "BTC", "--count", "10"], make_client=lambda: client)
    assert rc == 0
    client.get_candles.assert_called_once_with(100000, interval="OneDay", count=10)


def test_faltan_argumentos_requeridos_sale_con_error(capsys):
    client = MagicMock()
    try:
        candles.main(["--symbol", "SPY"], make_client=lambda: client)
        raised = False
    except SystemExit:
        raised = True
    assert raised

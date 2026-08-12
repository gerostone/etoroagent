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
    # --count=1 coincide con la unica vela mockeada: sin faltante (WP3
    # auditoria agrega validacion de cantidad en main(), ver mas abajo
    # "Validacion de cantidad de velas devueltas"), asi este test se queda
    # enfocado en el passthrough de --full sin disparar esa validacion.
    client = MagicMock()
    client.search_instrument.return_value = {
        "items": [{"internalInstrumentId": 42, "internalSymbolFull": "SPY", "isHiddenFromClient": False}]
    }
    client.get_candles.return_value = {
        "candles": [{"candles": [{"close": 100.0, "fromDate": "2026-08-01T00:00:00Z"}]}]
    }
    rc = candles.main(
        ["--symbol", "SPY", "--count", "1", "--full"],
        make_client=lambda: client,
    )
    assert rc == 0
    client.search_instrument.assert_called_once_with("SPY")
    client.get_candles.assert_called_once_with(42, interval="OneDay", count=1)
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
    # --count=10 pedido, solo 2 velas mockeadas: es exactamente el caso de
    # faltante que WP3 (auditoria) le agrega a candles.py -> el header debe
    # reflejar "requested=10" (ver tambien la seccion de tests dedicada mas
    # abajo, "Validacion de cantidad de velas devueltas").
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
    assert header == "# symbol=QQQ interval=OneWeek count=2 requested=10 order=asc"


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


# --- Validación de cantidad de velas devueltas (WP3 auditoría) -------------
#
# candles.py no validaba que la API haya devuelto la cantidad de velas
# pedida (`len(candles) < count` silencioso) — ver docs/verificacion-mom126.md
# para el detalle completo de la investigación. Estos tests cubren:
# (1) faltante moderado (M < requested pero M >= 130) -> WARNING + header
#     con "requested=<N>", sin abortar la corrida;
# (2) faltante severo (M < 130 con --count >= 130 pedido) -> exit 1
#     fail-closed, sin nada en stdout;
# (3) --count fuera de [1, 1000] o no entero -> exit 2 (error de uso, no de
#     datos).
#
# El piso de 130 solo se exige cuando el propio --count pedido ya apuntaba
# a ese piso (>=130 — el caso real siempre pide 210, ver PLAYBOOK.md
# §Señales): un pedido chico deliberado (--count < 130) no dispara el
# fail-closed, sólo el WARNING si además hay faltante frente a lo pedido.


def _candles_desc(n, start_close=100.0):
    """Genera n velas ficticias en orden DESCENDENTE (como las entrega la
    API real, direction=desc) con fechas correlativas ascendentes en el
    tiempo pero listadas de la mas reciente a la mas vieja."""
    import datetime as _dt

    base = _dt.date(2025, 1, 1)
    candles_asc = [
        _candle(start_close + i, (base + _dt.timedelta(days=i)).isoformat() + "T00:00:00Z")
        for i in range(n)
    ]
    return list(reversed(candles_asc))


def test_faltante_moderado_advierte_y_header_refleja_requested(capsys):
    # Se piden 200, la API devuelve 150 (< requested, pero >= 130: no
    # dispara el fail-closed, solo advertencia + header).
    client = MagicMock()
    client.search_instrument.return_value = {
        "items": [{"internalInstrumentId": 42, "internalSymbolFull": "GLD", "isHiddenFromClient": False}]
    }
    client.get_candles.return_value = {"candles": [{"candles": _candles_desc(150)}]}
    rc = candles.main(["--symbol", "GLD", "--count", "200"], make_client=lambda: client)
    assert rc == 0
    out = capsys.readouterr()
    header = out.out.split("\n")[0]
    assert header == "# symbol=GLD interval=OneDay count=150 requested=200 order=asc"
    assert "ADVERTENCIA: se pidieron 200 velas, la API devolvio 150" in out.err


def test_sin_faltante_header_no_incluye_requested(capsys):
    # count pedido == count devuelto: sin faltante, el header se queda en
    # el formato viejo (sin "requested=") y sin WARNING.
    client = MagicMock()
    client.search_instrument.return_value = {
        "items": [{"internalInstrumentId": 42, "internalSymbolFull": "GLD", "isHiddenFromClient": False}]
    }
    client.get_candles.return_value = {"candles": [{"candles": _candles_desc(200)}]}
    rc = candles.main(["--symbol", "GLD", "--count", "200"], make_client=lambda: client)
    assert rc == 0
    out = capsys.readouterr()
    header = out.out.split("\n")[0]
    assert header == "# symbol=GLD interval=OneDay count=200 order=asc"
    assert "ADVERTENCIA" not in out.err


def test_faltante_severo_bajo_130_con_count_grande_pedido_falla_cerrado(capsys):
    # Se piden 210 (>=130, el caso real de PLAYBOOK.md), la API devuelve
    # apenas 50 (<130): insuficiente para mom126 (necesita el indice -127)
    # -> fail-closed, exit 1, nada en stdout.
    client = MagicMock()
    client.search_instrument.return_value = {
        "items": [{"internalInstrumentId": 42, "internalSymbolFull": "GLD", "isHiddenFromClient": False}]
    }
    client.get_candles.return_value = {"candles": [{"candles": _candles_desc(50)}]}
    rc = candles.main(["--symbol", "GLD", "--count", "210"], make_client=lambda: client)
    assert rc == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert "ERROR en candles" in out.err
    assert "insuficientes" in out.err
    assert "210" in out.err
    assert "50" in out.err


def test_faltante_severo_exactamente_en_el_borde_129_falla_130_no():
    # Borde: 129 velas con --count>=130 pedido falla; 130 no.
    client = MagicMock()
    client.search_instrument.return_value = {
        "items": [{"internalInstrumentId": 42, "internalSymbolFull": "GLD", "isHiddenFromClient": False}]
    }
    client.get_candles.return_value = {"candles": [{"candles": _candles_desc(129)}]}
    rc = candles.main(["--symbol", "GLD", "--count", "200"], make_client=lambda: client)
    assert rc == 1

    client2 = MagicMock()
    client2.search_instrument.return_value = {
        "items": [{"internalInstrumentId": 42, "internalSymbolFull": "GLD", "isHiddenFromClient": False}]
    }
    client2.get_candles.return_value = {"candles": [{"candles": _candles_desc(130)}]}
    rc2 = candles.main(["--symbol", "GLD", "--count", "200"], make_client=lambda: client2)
    assert rc2 == 0


def test_count_chico_deliberado_no_dispara_piso_de_130():
    # --count < 130 pedido a propósito (p.ej. inspección puntual con
    # --full): aunque la API devuelva menos de 130, el llamador ya sabía
    # que no pedía datos para señales -> no debe fallar cerrado.
    client = MagicMock()
    client.search_instrument.return_value = {
        "items": [{"internalInstrumentId": 42, "internalSymbolFull": "GLD", "isHiddenFromClient": False}]
    }
    client.get_candles.return_value = {"candles": [{"candles": _candles_desc(5)}]}
    rc = candles.main(["--symbol", "GLD", "--count", "5"], make_client=lambda: client)
    assert rc == 0


def test_count_cero_sale_con_exit_code_2(capsys):
    client = MagicMock()
    try:
        candles.main(["--symbol", "GLD", "--count", "0"], make_client=lambda: client)
        raised = None
    except SystemExit as exc:
        raised = exc.code
    assert raised == 2
    client.search_instrument.assert_not_called()


def test_count_mayor_a_1000_sale_con_exit_code_2(capsys):
    client = MagicMock()
    try:
        candles.main(["--symbol", "GLD", "--count", "1001"], make_client=lambda: client)
        raised = None
    except SystemExit as exc:
        raised = exc.code
    assert raised == 2
    client.search_instrument.assert_not_called()


def test_count_negativo_sale_con_exit_code_2():
    client = MagicMock()
    try:
        candles.main(["--symbol", "GLD", "--count", "-5"], make_client=lambda: client)
        raised = None
    except SystemExit as exc:
        raised = exc.code
    assert raised == 2
    client.search_instrument.assert_not_called()


def test_count_no_entero_sale_con_exit_code_2():
    client = MagicMock()
    try:
        candles.main(["--symbol", "GLD", "--count", "diez"], make_client=lambda: client)
        raised = None
    except SystemExit as exc:
        raised = exc.code
    assert raised == 2
    client.search_instrument.assert_not_called()

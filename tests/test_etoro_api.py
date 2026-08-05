import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from etoro_api import (
    AmbiguousMatchError,
    EtoroAuthError,
    EtoroClient,
    EtoroUnknownOutcomeError,
    NoExactMatchError,
    extract_exact_match,
)


def make_client():
    return EtoroClient(api_key="k", user_key="u")


def fake_resp(status=200, json_body=None, headers=None, content=b"{}"):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = json_body if json_body is not None else {}
    r.headers = headers or {}
    r.content = content
    return r


def test_headers_incluyen_request_id_unico():
    c = make_client()
    h1, h2 = c._headers(), c._headers()
    assert h1["x-api-key"] == "k" and h1["x-user-key"] == "u"
    assert h1["x-request-id"] != h2["x-request-id"]


@patch("etoro_api.requests.request")
def test_401_lanza_auth_error(mock_req):
    mock_req.return_value = fake_resp(status=401)
    with pytest.raises(EtoroAuthError):
        make_client().request("GET", "/api/v1/agent-portfolios")


@patch("etoro_api.time.sleep")
@patch("etoro_api.requests.request")
def test_429_reintenta_respetando_retry_after(mock_req, mock_sleep):
    mock_req.side_effect = [
        fake_resp(status=429, headers={"Retry-After": "2"}),
        fake_resp(json_body={"ok": True}),
    ]
    assert make_client().request("GET", "/x") == {"ok": True}
    mock_sleep.assert_called_once_with(2)


@patch("etoro_api.time.sleep")
@patch("etoro_api.requests.request")
def test_429_persistente_lanza_runtime_error(mock_req, mock_sleep):
    mock_req.return_value = fake_resp(status=429, headers={})
    with pytest.raises(RuntimeError):
        make_client().request("GET", "/x")


@patch("etoro_api.requests.request")
def test_open_position_body_pascal_case_sin_opcionales(mock_req):
    mock_req.return_value = fake_resp(json_body={"orderId": "o1"})
    make_client().open_position_by_amount(instrument_id=42, amount_usd=50.0)
    body = mock_req.call_args.kwargs["json"]
    assert body == {"InstrumentID": 42, "IsBuy": True, "Leverage": 1, "Amount": 50.0}


@patch("etoro_api.requests.request")
def test_open_position_incluye_stop_loss_rate_si_se_pasa(mock_req):
    mock_req.return_value = fake_resp(json_body={"orderId": "o1"})
    make_client().open_position_by_amount(instrument_id=42, amount_usd=50.0, stop_loss_rate=99.5)
    assert mock_req.call_args.kwargs["json"]["StopLossRate"] == 99.5


@patch("etoro_api.requests.request")
def test_close_position_body(mock_req):
    mock_req.return_value = fake_resp(json_body={})
    make_client().close_position(position_id="p1", instrument_id=42)
    assert "/market-close-orders/positions/p1" in mock_req.call_args.args[1]
    assert mock_req.call_args.kwargs["json"] == {"InstrumentId": 42, "UnitsToDeduct": None}


# -- Fixes de la quality review ---------------------------------------------


def test_credenciales_faltantes_lanza_value_error(monkeypatch):
    monkeypatch.delenv("ETORO_API_KEY", raising=False)
    monkeypatch.delenv("ETORO_USER_KEY", raising=False)
    with pytest.raises(ValueError, match="ETORO_API_KEY"):
        EtoroClient(api_key=None, user_key="u")
    with pytest.raises(ValueError, match="ETORO_USER_KEY"):
        EtoroClient(api_key="k", user_key=None)


@patch("etoro_api.requests.request")
def test_500_en_open_position_no_reintenta(mock_req):
    resp = fake_resp(status=500)
    resp.raise_for_status.side_effect = requests.exceptions.HTTPError("500 server error")
    mock_req.return_value = resp
    with pytest.raises(requests.exceptions.HTTPError):
        make_client().open_position_by_amount(instrument_id=42, amount_usd=50.0)
    assert mock_req.call_count == 1


@patch("etoro_api.requests.request")
def test_connection_error_en_open_position_no_reintenta(mock_req):
    mock_req.side_effect = requests.exceptions.ConnectionError("boom")
    with pytest.raises(requests.exceptions.ConnectionError):
        make_client().open_position_by_amount(instrument_id=42, amount_usd=50.0)
    assert mock_req.call_count == 1


@patch("etoro_api.requests.request")
def test_timeout_en_open_position_no_reintenta(mock_req):
    mock_req.side_effect = requests.exceptions.Timeout("boom")
    with pytest.raises(requests.exceptions.Timeout):
        make_client().open_position_by_amount(instrument_id=42, amount_usd=50.0)
    assert mock_req.call_count == 1


@patch("etoro_api.time.sleep")
@patch("etoro_api.requests.request")
def test_retry_after_http_date_cae_a_backoff_fijo(mock_req, mock_sleep):
    mock_req.side_effect = [
        fake_resp(status=429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}),
        fake_resp(json_body={"ok": True}),
    ]
    assert make_client().request("GET", "/x") == {"ok": True}
    mock_sleep.assert_called_once_with(15)


@patch("etoro_api.requests.request")
def test_body_vacio_devuelve_dict_vacio(mock_req):
    mock_req.return_value = fake_resp(content=b"")
    assert make_client().request("GET", "/x") == {}


@patch("etoro_api.time.sleep")
@patch("etoro_api.requests.request")
def test_retry_after_gigante_se_capa_a_60(mock_req, mock_sleep):
    mock_req.side_effect = [
        fake_resp(status=429, headers={"Retry-After": "86400"}),
        fake_resp(json_body={"ok": True}),
    ]
    assert make_client().request("GET", "/x") == {"ok": True}
    mock_sleep.assert_called_once_with(60)


@patch("etoro_api.requests.request")
def test_200_con_body_no_json_lanza_unknown_outcome_error(mock_req):
    resp = fake_resp(status=200, content=b"<html>not json</html>")
    resp.json.side_effect = json.JSONDecodeError("Expecting value", "<html>not json</html>", 0)
    mock_req.return_value = resp
    with pytest.raises(EtoroUnknownOutcomeError):
        make_client().request(
            "POST", "/api/v1/trading/execution/market-open-orders/by-amount", json={}
        )


def test_get_candles_requiere_count():
    with pytest.raises(TypeError):
        make_client().get_candles(instrument_id=1)


# -- extract_exact_match (2do contacto con la API real) ---------------------
#
# Hallazgo verificado con probes en vivo: GET /market-data/search es FUZZY —
# buscar "BTC" devuelve 53 items, con "BTCA" primero y el match exacto "BTC"
# más abajo en la lista. Los campos reales de cada item son
# internalSymbolFull, internalInstrumentId (¡no "instrumentId"!),
# internalAssetClassName, isHiddenFromClient — nunca tomar items[0] a ciegas.


def test_extract_exact_match_devuelve_el_unico_match_exacto():
    resp = {
        "items": [
            {
                "internalSymbolFull": "SPY",
                "internalInstrumentId": 3000,
                "internalAssetClassName": "ETF",
                "isHiddenFromClient": False,
            }
        ]
    }
    match = extract_exact_match(resp, "SPY")
    assert match["internalInstrumentId"] == 3000


def test_extract_exact_match_es_case_insensitive():
    resp = {
        "items": [
            {"internalSymbolFull": "spy", "internalInstrumentId": 3000, "isHiddenFromClient": False}
        ]
    }
    assert extract_exact_match(resp, "SPY")["internalInstrumentId"] == 3000
    assert extract_exact_match(resp, "spy")["internalInstrumentId"] == 3000


def test_extract_exact_match_sin_match_lanza_no_exact_match_error():
    resp = {"items": [{"internalSymbolFull": "SPYX", "internalInstrumentId": 1}]}
    with pytest.raises(NoExactMatchError):
        extract_exact_match(resp, "SPY")


def test_extract_exact_match_lista_vacia_lanza_no_exact_match_error():
    with pytest.raises(NoExactMatchError):
        extract_exact_match({"items": []}, "SPY")


def test_extract_exact_match_ambiguo_lanza_ambiguous_match_error():
    resp = {
        "items": [
            {"internalSymbolFull": "SPY", "internalInstrumentId": 3000},
            {"internalSymbolFull": "SPY", "internalInstrumentId": 3001},
        ]
    }
    with pytest.raises(AmbiguousMatchError):
        extract_exact_match(resp, "SPY")


def test_extract_exact_match_filtra_ishiddenfromclient():
    resp = {
        "items": [
            {"internalSymbolFull": "SPY", "internalInstrumentId": 999, "isHiddenFromClient": True},
            {"internalSymbolFull": "SPY", "internalInstrumentId": 3000, "isHiddenFromClient": False},
        ]
    }
    # El item oculto no cuenta: queda un único match exacto visible, no ambiguo.
    match = extract_exact_match(resp, "SPY")
    assert match["internalInstrumentId"] == 3000


def test_extract_exact_match_ishiddenfromclient_ausente_no_se_filtra():
    # isHiddenFromClient ausente (no False explícito) no debe tratarse como
    # oculto — solo True excluye.
    resp = {"items": [{"internalSymbolFull": "SPY", "internalInstrumentId": 3000}]}
    assert extract_exact_match(resp, "SPY")["internalInstrumentId"] == 3000


def test_extract_exact_match_fuzzy_btca_primero_btc_mas_abajo_resuelve_100000():
    # Escenario real verificado: buscar "BTC" trae muchos items no-exactos
    # antes del match exacto (BTCA, BTCB, etc. simulados acá con 3 de 53).
    resp = {
        "items": [
            {"internalSymbolFull": "BTCA", "internalInstrumentId": 55, "isHiddenFromClient": False},
            {"internalSymbolFull": "BTCB", "internalInstrumentId": 56, "isHiddenFromClient": False},
            {"internalSymbolFull": "BTC", "internalInstrumentId": 100000, "isHiddenFromClient": False},
            {"internalSymbolFull": "BTCC", "internalInstrumentId": 57, "isHiddenFromClient": False},
        ]
    }
    match = extract_exact_match(resp, "BTC")
    assert match["internalInstrumentId"] == 100000


def test_extract_exact_match_respuesta_sin_items_lanza_no_exact_match_error():
    with pytest.raises(NoExactMatchError):
        extract_exact_match({}, "SPY")


def test_extract_exact_match_no_exact_match_error_es_value_error():
    # except ValueError genérico debe seguir capturando ambas subclases —
    # importante para callers que no necesitan distinguir el caso.
    assert issubclass(NoExactMatchError, ValueError)
    assert issubclass(AmbiguousMatchError, ValueError)

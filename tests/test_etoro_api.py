import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from etoro_api import EtoroClient, EtoroAuthError, EtoroUnknownOutcomeError


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

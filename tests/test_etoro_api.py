import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from etoro_api import EtoroClient, EtoroAuthError


def make_client():
    return EtoroClient(api_key="k", user_key="u")


def fake_resp(status=200, json_body=None, headers=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = json_body if json_body is not None else {}
    r.headers = headers or {}
    r.content = b"{}"
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

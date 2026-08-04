import csv
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import snapshot


# -- Fixtures -----------------------------------------------------------

PNL_BASIC = {
    "clientPortfolio": {
        "credit": 1000.0,
        "positions": [
            {"positionID": 1, "instrumentID": 1, "amount": 100.0, "unrealizedPnL": 5.0},
            {"positionID": 2, "instrumentID": 2, "amount": 200.0, "unrealizedPnL": {"pnL": -10.0}},
        ],
        "orders": [],
        "ordersForOpen": [],
    }
}

SYMBOL_BY_ID = {1: "SPY", 2: "BTC"}


# -- build_state ----------------------------------------------------------


def test_build_state_resuelve_simbolos_y_calcula_valueusd():
    state = snapshot.build_state("pf-1", PNL_BASIC, SYMBOL_BY_ID)
    assert state["portfolioId"] == "pf-1"
    assert state["cashUsd"] == 1000.0
    assert "updatedAt" in state
    assert "T" in state["updatedAt"]  # ISO 8601
    positions = state["positions"]
    assert len(positions) == 2
    p1 = next(p for p in positions if p["positionId"] == 1)
    p2 = next(p for p in positions if p["positionId"] == 2)
    assert p1["symbol"] == "SPY"
    assert p1["instrumentId"] == 1
    assert p1["valueUsd"] == 105.0  # 100 + 5 (float pnL)
    assert p2["symbol"] == "BTC"
    assert p2["valueUsd"] == 190.0  # 200 - 10 ({"pnL": x} form)


def test_build_state_id_no_resoluble_lanza_valueerror():
    with pytest.raises(ValueError):
        snapshot.build_state("pf-1", PNL_BASIC, {1: "SPY"})  # falta id 2


def test_build_state_simbolo_vacio_en_metadata_lanza_valueerror():
    with pytest.raises(ValueError):
        snapshot.build_state("pf-1", PNL_BASIC, {1: "SPY", 2: ""})


def test_build_state_available_cash_resta_ordersforopen_y_orders():
    pnl = {
        "clientPortfolio": {
            "credit": 1000.0,
            "positions": [],
            "orders": [{"amount": 50.0}, {"amount": 25.0}],
            "ordersForOpen": [
                {"amount": 100.0, "mirrorID": 0},
                {"amount": 999.0, "mirrorID": 7},  # debe ignorarse (mirrorID != 0)
            ],
        }
    }
    state = snapshot.build_state("pf-1", pnl, {})
    # 1000 - 100 (ordersForOpen mirrorID=0) - (50+25) (orders) = 825
    assert state["cashUsd"] == 825.0


def test_build_state_valueusd_negativo_se_clampa_a_cero():
    pnl = {
        "clientPortfolio": {
            "credit": 100.0,
            "positions": [
                {"positionID": 9, "instrumentID": 5, "amount": 10.0, "unrealizedPnL": -500.0},
            ],
            "orders": [],
            "ordersForOpen": [],
        }
    }
    state = snapshot.build_state("pf-1", pnl, {5: "SPY"})
    assert state["positions"][0]["valueUsd"] == 0.0


def test_build_state_tolera_positionid_variantes_y_listas_ausentes():
    pnl = {
        "clientPortfolio": {
            "credit": 500.0,
            "positions": [
                {"positionId": 3, "instrumentID": 1, "amount": 10.0, "unrealizedPnL": 0.0},
            ],
            # orders/ordersForOpen ausentes
        }
    }
    state = snapshot.build_state("pf-1", pnl, {1: "SPY"})
    assert state["cashUsd"] == 500.0
    assert state["positions"][0]["positionId"] == 3


# -- update_equity --------------------------------------------------------


def test_update_equity_reemplaza_fila_del_mismo_dia():
    rows = [("2026-08-01", 100.0), ("2026-08-02", 200.0)]
    result = snapshot.update_equity(rows, "2026-08-02", 250.0)
    assert result == [("2026-08-01", 100.0), ("2026-08-02", 250.0)]


def test_update_equity_appendea_dia_nuevo():
    rows = [("2026-08-01", 100.0)]
    result = snapshot.update_equity(rows, "2026-08-02", 250.0)
    assert result == [("2026-08-01", 100.0), ("2026-08-02", 250.0)]


def test_update_equity_lista_vacia():
    result = snapshot.update_equity([], "2026-08-01", 100.0)
    assert result == [("2026-08-01", 100.0)]


# -- main() -----------------------------------------------------------------


def test_main_id_no_resoluble_exit_1_y_no_escribe_positions(tmp_path, monkeypatch, capsys):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(snapshot, "STATE_DIR", state_dir)

    fake_client = MagicMock()
    fake_client.get_agent_portfolios.return_value = {
        "agentPortfolios": [{"agentPortfolioId": "pf-1"}]
    }
    fake_client.get_pnl.return_value = PNL_BASIC
    # metadata que no resuelve ningun simbolo
    fake_client.get_instruments_metadata.return_value = {"items": []}

    monkeypatch.setattr(snapshot, "EtoroClient", lambda: fake_client)

    with pytest.raises(SystemExit) as exc_info:
        snapshot.main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.err.strip() != ""
    assert not (state_dir / "positions.json").exists()


def test_main_sin_portfolios_exit_1(tmp_path, monkeypatch, capsys):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(snapshot, "STATE_DIR", state_dir)

    fake_client = MagicMock()
    fake_client.get_agent_portfolios.return_value = {"agentPortfolios": []}
    monkeypatch.setattr(snapshot, "EtoroClient", lambda: fake_client)

    with pytest.raises(SystemExit) as exc_info:
        snapshot.main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.err.strip() != ""


def test_main_ok_escribe_positions_y_equity(tmp_path, monkeypatch, capsys):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(snapshot, "STATE_DIR", state_dir)
    monkeypatch.setattr(snapshot, "_today_str", lambda: "2026-08-04")

    fake_client = MagicMock()
    fake_client.get_agent_portfolios.return_value = {
        "agentPortfolios": [{"agentPortfolioId": "pf-1"}]
    }
    fake_client.get_pnl.return_value = PNL_BASIC
    fake_client.get_instruments_metadata.return_value = {
        "items": [
            {"instrumentId": 1, "internalSymbolFull": "SPY"},
            {"instrumentId": 2, "internalSymbolFull": "BTC"},
        ]
    }
    monkeypatch.setattr(snapshot, "EtoroClient", lambda: fake_client)

    snapshot.main()

    positions_path = state_dir / "positions.json"
    equity_path = state_dir / "equity.csv"
    assert positions_path.exists()
    assert equity_path.exists()

    written = json.loads(positions_path.read_text())
    assert written["portfolioId"] == "pf-1"
    assert written["cashUsd"] == 1000.0
    assert len(written["positions"]) == 2

    with open(equity_path) as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["date", "total"]
    assert rows[1][0] == "2026-08-04"
    total = 1000.0 + 105.0 + 190.0
    assert abs(float(rows[1][1]) - total) < 1e-9

    captured = capsys.readouterr()
    assert "OK" in captured.out


def test_main_no_tmp_file_left_behind_on_success(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(snapshot, "STATE_DIR", state_dir)

    fake_client = MagicMock()
    fake_client.get_agent_portfolios.return_value = {
        "agentPortfolios": [{"agentPortfolioId": "pf-1"}]
    }
    fake_client.get_pnl.return_value = PNL_BASIC
    fake_client.get_instruments_metadata.return_value = {
        "items": [
            {"instrumentId": 1, "internalSymbolFull": "SPY"},
            {"instrumentId": 2, "internalSymbolFull": "BTC"},
        ]
    }
    monkeypatch.setattr(snapshot, "EtoroClient", lambda: fake_client)

    snapshot.main()

    tmp_files = list(state_dir.glob("*.tmp"))
    assert tmp_files == []

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import place_order
from etoro_api import EtoroUnknownOutcomeError


# -- Fixtures -------------------------------------------------------------

STATE_BASIC = {
    "updatedAt": "2026-08-04T00:00:00+00:00",
    "portfolioId": "pf-1",
    "cashUsd": 1000.0,
    "positions": [
        {"positionId": "pos-9", "symbol": "QQQ", "instrumentId": 7, "valueUsd": 50.0},
    ],
}

EQUITY_ROWS_BASIC = [("2026-08-01", 1000.0), ("2026-08-04", 1050.0)]


def write_state(tmp_path, state=None, equity_rows=None):
    """Escribe state/positions.json y state/equity.csv en tmp_path, tal como
    los dejaría snapshot.py. Devuelve el Path del directorio state."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    if state is not None:
        (state_dir / "positions.json").write_text(json.dumps(state))
    if equity_rows is not None:
        lines = ["date,total"]
        for date, value in equity_rows:
            lines.append(f"{date},{value}")
        (state_dir / "equity.csv").write_text("\n".join(lines) + "\n")
    return state_dir


def candles_resp(close):
    return {
        "candles": [
            {"candles": [{"close": close}]},
        ]
    }


# -- 1: excede 25% de posición --------------------------------------------


def test_open_excede_max_position_pct_bloquea_sin_llamar_cliente(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    state = {
        "updatedAt": "x",
        "portfolioId": "pf-1",
        "cashUsd": 80.0,
        "positions": [
            {"positionId": "pos-1", "symbol": "QQQ", "instrumentId": 7, "valueUsd": 40.0},
        ],
    }
    # total = 120 (80 cash + 40 posicion), orden de 40 -> (40+40)/120 = 66% > 25%
    write_state(tmp_path, state, [("2026-08-01", 120.0)])
    client = MagicMock()
    rc = place_order.main(
        [
            "open",
            "--symbol", "QQQ",
            "--amount", "40",
            "--stop-loss-pct", "0.10",
            "--reason", "test",
        ],
        state_dir=tmp_path / "state",
        make_client=lambda: client,
    )
    assert rc == 2
    client.search_instrument.assert_not_called()
    client.open_position_by_amount.assert_not_called()
    journal = (tmp_path / "state" / "journal.md").read_text()
    assert "BLOQUEADA" in journal


# -- 2: DRY_RUN ------------------------------------------------------------


@pytest.mark.parametrize("dry_run_value", ["1", "true", "no", None])
def test_dry_run_no_llama_cliente(tmp_path, monkeypatch, dry_run_value):
    if dry_run_value is None:
        monkeypatch.delenv("DRY_RUN", raising=False)
    else:
        monkeypatch.setenv("DRY_RUN", dry_run_value)
    write_state(tmp_path, STATE_BASIC, EQUITY_ROWS_BASIC)
    client = MagicMock()
    rc = place_order.main(
        [
            "open",
            "--symbol", "QQQ",
            "--amount", "20",
            "--stop-loss-pct", "0.10",
        ],
        state_dir=tmp_path / "state",
        make_client=lambda: client,
    )
    assert rc == 0
    client.search_instrument.assert_not_called()
    client.get_candles.assert_not_called()
    client.open_position_by_amount.assert_not_called()
    journal = (tmp_path / "state" / "journal.md").read_text()
    assert "DRY_RUN" in journal
    assert "QQQ" in journal


# -- 3: open real -----------------------------------------------------------


def test_open_real_abre_posicion_con_stop_loss_por_precio(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    write_state(tmp_path, STATE_BASIC, EQUITY_ROWS_BASIC)
    client = MagicMock()
    client.search_instrument.return_value = {
        "items": [{"instrumentId": 42, "internalSymbolFull": "QQQ"}]
    }
    client.get_candles.return_value = candles_resp(100.0)
    client.open_position_by_amount.return_value = {"positionID": 55}
    rc = place_order.main(
        [
            "open",
            "--symbol", "QQQ",
            "--amount", "20",
            "--stop-loss-pct", "0.10",
            "--reason", "test",
        ],
        state_dir=tmp_path / "state",
        make_client=lambda: client,
    )
    assert rc == 0
    client.open_position_by_amount.assert_called_once_with(
        instrument_id=42, amount_usd=20.0, stop_loss_rate=90.0
    )
    journal = (tmp_path / "state" / "journal.md").read_text()
    assert "ABIERTA" in journal


def test_open_real_tolera_key_instruments_en_search(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    write_state(tmp_path, STATE_BASIC, EQUITY_ROWS_BASIC)
    client = MagicMock()
    client.search_instrument.return_value = {
        "instruments": [{"instrumentId": 42, "internalSymbolFull": "QQQ"}]
    }
    client.get_candles.return_value = candles_resp(100.0)
    client.open_position_by_amount.return_value = {"positionID": 55}
    rc = place_order.main(
        ["open", "--symbol", "QQQ", "--amount", "20", "--stop-loss-pct", "0.10"],
        state_dir=tmp_path / "state",
        make_client=lambda: client,
    )
    assert rc == 0
    client.open_position_by_amount.assert_called_once_with(
        instrument_id=42, amount_usd=20.0, stop_loss_rate=90.0
    )


# -- 4: close real -----------------------------------------------------------


def test_close_real_cierra_posicion(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    write_state(tmp_path, STATE_BASIC, EQUITY_ROWS_BASIC)
    client = MagicMock()
    client.close_position.return_value = {"ok": True}
    rc = place_order.main(
        [
            "close",
            "--position-id", "pos-9",
            "--symbol", "QQQ",
            "--reason", "test",
        ],
        state_dir=tmp_path / "state",
        make_client=lambda: client,
    )
    assert rc == 0
    client.close_position.assert_called_once_with(position_id="pos-9", instrument_id=7)
    journal = (tmp_path / "state" / "journal.md").read_text()
    assert "CERRADA" in journal


# -- 5: close de posición sintética -----------------------------------------


def test_close_posicion_sintetica_bloquea(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    state = {
        "updatedAt": "x",
        "portfolioId": "pf-1",
        "cashUsd": 100.0,
        "positions": [
            {
                "positionId": "pending-open:3",
                "symbol": "QQQ",
                "instrumentId": 7,
                "valueUsd": 50.0,
                "pending": True,
            }
        ],
    }
    write_state(tmp_path, state, EQUITY_ROWS_BASIC)
    client = MagicMock()
    rc = place_order.main(
        ["close", "--position-id", "pending-open:3", "--symbol", "QQQ"],
        state_dir=tmp_path / "state",
        make_client=lambda: client,
    )
    assert rc == 2
    client.close_position.assert_not_called()


# -- 6: resultado ambiguo ---------------------------------------------------


def test_open_real_unknown_outcome_no_reintenta_y_marca_ambiguo(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    write_state(tmp_path, STATE_BASIC, EQUITY_ROWS_BASIC)
    client = MagicMock()
    client.search_instrument.return_value = {
        "items": [{"instrumentId": 42, "internalSymbolFull": "QQQ"}]
    }
    client.get_candles.return_value = candles_resp(100.0)
    client.open_position_by_amount.side_effect = EtoroUnknownOutcomeError("body raro")
    rc = place_order.main(
        ["open", "--symbol", "QQQ", "--amount", "20", "--stop-loss-pct", "0.10"],
        state_dir=tmp_path / "state",
        make_client=lambda: client,
    )
    assert rc == 1
    assert client.open_position_by_amount.call_count == 1
    journal = (tmp_path / "state" / "journal.md").read_text()
    assert "AMBIGUO" in journal
    assert "pnl" in journal.lower()


# -- 7: símbolo fuera del universo ------------------------------------------


def test_open_simbolo_fuera_de_universo_bloquea(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    write_state(tmp_path, STATE_BASIC, EQUITY_ROWS_BASIC)
    client = MagicMock()
    rc = place_order.main(
        ["open", "--symbol", "TSLA", "--amount", "20", "--stop-loss-pct", "0.10"],
        state_dir=tmp_path / "state",
        make_client=lambda: client,
    )
    assert rc == 2
    client.search_instrument.assert_not_called()
    client.open_position_by_amount.assert_not_called()


# -- 8: amount > cashUsd -----------------------------------------------------


def test_open_amount_mayor_a_cash_bloquea(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    state = {
        "updatedAt": "x",
        "portfolioId": "pf-1",
        "cashUsd": 10.0,
        "positions": [],
    }
    write_state(tmp_path, state, [("2026-08-01", 10.0)])
    client = MagicMock()
    rc = place_order.main(
        ["open", "--symbol", "QQQ", "--amount", "20", "--stop-loss-pct", "0.10"],
        state_dir=tmp_path / "state",
        make_client=lambda: client,
    )
    assert rc == 2
    client.search_instrument.assert_not_called()
    client.open_position_by_amount.assert_not_called()


# -- 9: close con position-id inexistente ------------------------------------


def test_close_position_id_inexistente_bloquea(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    write_state(tmp_path, STATE_BASIC, EQUITY_ROWS_BASIC)
    client = MagicMock()
    rc = place_order.main(
        ["close", "--position-id", "no-existe", "--symbol", "QQQ"],
        state_dir=tmp_path / "state",
        make_client=lambda: client,
    )
    assert rc == 2
    client.close_position.assert_not_called()


# -- 10: state ausente --------------------------------------------------------


def test_state_ausente_devuelve_rc_1(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    client = MagicMock()
    rc = place_order.main(
        ["open", "--symbol", "QQQ", "--amount", "20", "--stop-loss-pct", "0.10"],
        state_dir=tmp_path / "state",
        make_client=lambda: client,
    )
    assert rc == 1
    client.search_instrument.assert_not_called()

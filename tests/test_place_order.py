import json
import math
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import place_order
from etoro_api import EtoroUnknownOutcomeError


# -- Fixtures -------------------------------------------------------------


def _fresh_updated_at():
    """Timestamp UTC 'ahora', para que el nuevo guard de frescura del state
    (regla 10 de place_order.py: bloquea si updatedAt tiene >24h) no
    interfiera con tests que ejercitan otras reglas. Se recalcula en cada
    llamada — usar esto en vez de una constante hardcodeada evita que la
    suite se vuelva flaky con el paso del tiempo real."""
    return datetime.now(timezone.utc).isoformat()


STATE_BASIC = {
    "updatedAt": _fresh_updated_at(),
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


def candles_resp(close, from_date=None):
    """from_date default: 'ahora' (vela fresca) — así los tests existentes,
    que no ejercitan la regla de frescura de vela, no se ven afectados por
    ella. Los tests que sí la ejercitan pasan un from_date explícito."""
    if from_date is None:
        from_date = _fresh_updated_at()
    return {
        "candles": [
            {"candles": [{"close": close, "fromDate": from_date}]},
        ]
    }


# -- 1: excede 25% de posición --------------------------------------------


def test_open_excede_max_position_pct_bloquea_sin_llamar_cliente(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    state = {
        "updatedAt": _fresh_updated_at(),
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
    # SPY (no QQQ: STATE_BASIC ya tiene una posición QQQ, y WP1 bloquearía
    # esa recompra por no-duplicación antes de llegar a DRY_RUN).
    rc = place_order.main(
        [
            "open",
            "--symbol", "SPY",
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
    assert "SPY" in journal


# -- 3: open real -----------------------------------------------------------


def test_open_real_abre_posicion_con_stop_loss_por_precio(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    write_state(tmp_path, STATE_BASIC, EQUITY_ROWS_BASIC)
    client = MagicMock()
    # SPY (no QQQ: ya tiene posición en STATE_BASIC, WP1 bloquearía por no-duplicación).
    client.search_instrument.return_value = {
        "items": [{"internalInstrumentId": 42, "internalSymbolFull": "SPY", "isHiddenFromClient": False}]
    }
    client.get_candles.return_value = candles_resp(100.0)
    client.open_position_by_amount.return_value = {"positionID": 55}
    rc = place_order.main(
        [
            "open",
            "--symbol", "SPY",
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


def test_open_real_no_reconoce_key_instruments_solo_items(tmp_path, monkeypatch):
    # Verificado contra la API real: search_instrument() SOLO trae items[]
    # (nunca "instruments") — extract_exact_match() no debe tolerar esa key
    # alternativa que nunca existió en la respuesta real. Una respuesta con
    # únicamente "instruments" debe tratarse como 0 matches (sin match).
    monkeypatch.setenv("DRY_RUN", "0")
    write_state(tmp_path, STATE_BASIC, EQUITY_ROWS_BASIC)
    client = MagicMock()
    # SPY (no QQQ: ya tiene posición en STATE_BASIC, WP1 bloquearía por no-duplicación).
    client.search_instrument.return_value = {
        "instruments": [{"internalInstrumentId": 42, "internalSymbolFull": "SPY"}]
    }
    rc = place_order.main(
        ["open", "--symbol", "SPY", "--amount", "20", "--stop-loss-pct", "0.10"],
        state_dir=tmp_path / "state",
        make_client=lambda: client,
    )
    assert rc == 1
    client.get_candles.assert_not_called()
    client.open_position_by_amount.assert_not_called()


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
        "updatedAt": _fresh_updated_at(),
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
    # SPY (no QQQ: ya tiene posición en STATE_BASIC, WP1 bloquearía por no-duplicación).
    client.search_instrument.return_value = {
        "items": [{"internalInstrumentId": 42, "internalSymbolFull": "SPY", "isHiddenFromClient": False}]
    }
    client.get_candles.return_value = candles_resp(100.0)
    client.open_position_by_amount.side_effect = EtoroUnknownOutcomeError("body raro")
    rc = place_order.main(
        ["open", "--symbol", "SPY", "--amount", "20", "--stop-loss-pct", "0.10"],
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
    """Guard de cash (regla 6 del módulo): amount > cashUsd bloquea aunque
    risk.validate() por sí solo no lo haría.

    NOTA WP1: con el tope de exposición agregada (MAX_TOTAL_EXPOSURE_PCT =
    70%) ya no es posible aislar el guard de cash en un escenario donde
    risk.validate() pase Y falte cash real, como hacía este test antes de
    WP1. Es una consecuencia matemática, no un descuido: total =
    cashUsd + Σposiciones por definición de portfolio_value(); si el tope
    agregado pasa, Σposiciones + amount <= 0.70*total, luego
    cashUsd = total - Σposiciones >= 0.30*total + amount > amount siempre
    que total > 0. O sea, cashUsd > amount queda GARANTIZADO por el tope
    agregado en cualquier estado que pase risk.validate() con un símbolo
    nuevo (<=25% de total). El guard de cash sigue en el código como
    defensa en profundidad (regla 6, no se toca acá), pero en la práctica
    post-WP1 el tope agregado lo cubre antes de llegar a la orden.

    Este test verifica que ese mismo escenario del mundo real (cash
    insuficiente pese a que cada posición individual está bajo el 25%)
    sigue bloqueado — ahora por el tope agregado del 70%, en vez de por el
    guard de cash — preservando la garantía de negocio (nunca se abre una
    orden sin cash real) aunque el mecanismo concreto de bloqueo cambió."""
    monkeypatch.setenv("DRY_RUN", "0")
    state = {
        "updatedAt": _fresh_updated_at(),
        "portfolioId": "pf-1",
        "cashUsd": 50.0,
        "positions": [
            {"positionId": "p-spy", "symbol": "SPY", "instrumentId": 1, "valueUsd": 250.0},
            {"positionId": "p-xlk", "symbol": "XLK", "instrumentId": 2, "valueUsd": 250.0},
            {"positionId": "p-xle", "symbol": "XLE", "instrumentId": 3, "valueUsd": 250.0},
            {"positionId": "p-xlf", "symbol": "XLF", "instrumentId": 4, "valueUsd": 200.0},
        ],
    }
    # total = 50 (cash) + 250+250+250+200 (posiciones) = 1000
    write_state(tmp_path, state, [("2026-08-01", 1000.0)])
    client = MagicMock()
    rc = place_order.main(
        # QQQ sin posición previa: (0+100)/1000 = 10% <= 25% individual, pero
        # exposición agregada (950+100)/1000 = 105% > 70% -> bloqueado por
        # WP1 antes de llegar al guard de cash.
        ["open", "--symbol", "QQQ", "--amount", "100", "--stop-loss-pct", "0.10"],
        state_dir=tmp_path / "state",
        make_client=lambda: client,
    )
    assert rc == 2
    client.search_instrument.assert_not_called()
    client.open_position_by_amount.assert_not_called()
    journal = (tmp_path / "state" / "journal.md").read_text()
    assert "BLOQUEADA" in journal
    assert "70%" in journal


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
    state_dir = tmp_path / "state"
    assert not state_dir.exists()  # nunca corrió snapshot.py
    client = MagicMock()
    rc = place_order.main(
        ["open", "--symbol", "QQQ", "--amount", "20", "--stop-loss-pct", "0.10"],
        state_dir=state_dir,
        make_client=lambda: client,
    )
    assert rc == 1
    client.search_instrument.assert_not_called()
    # journal() crea state_dir aunque nunca haya existido (no hubo snapshot):
    # el intento de orden bloqueado queda igual como rastro de auditoría.
    journal = (state_dir / "journal.md").read_text()
    assert "ERROR" in journal
    assert "state" in journal.lower()


# -- 11: registro de exposición intra-corrida (fix quality review #1) -------


def _mock_client_for(symbol, instrument_id, close=100.0):
    client = MagicMock()
    client.search_instrument.return_value = {
        "items": [
            {
                "internalInstrumentId": instrument_id,
                "internalSymbolFull": symbol,
                "isHiddenFromClient": False,
            }
        ]
    }
    client.get_candles.return_value = candles_resp(close)
    client.open_position_by_amount.return_value = {"positionID": instrument_id}
    return client


def test_open_dos_veces_en_la_misma_corrida_excede_25_acumulado(tmp_path, monkeypatch):
    """Sin el fix, cada invocación de place_order.py relee positions.json
    desde disco sin ver lo que la corrida anterior (misma sesión del agente,
    proceso distinto) recién abrió — porque get_pnl() cachea 60s del lado de
    eToro y un re-snapshot inmediato no alcanza a reflejarlo. Dos aperturas
    de QQQ por 200 cada una, sobre un portfolio de 1000, deberían acumular
    40% y la segunda debe bloquearse por el 25%."""
    monkeypatch.setenv("DRY_RUN", "0")
    state_dir = write_state(
        tmp_path,
        {
            "updatedAt": _fresh_updated_at(),
            "portfolioId": "pf-1",
            "cashUsd": 1000.0,
            "positions": [],
        },
        [("2026-08-01", 1000.0)],
    )

    client1 = _mock_client_for("QQQ", 42)
    rc1 = place_order.main(
        ["open", "--symbol", "QQQ", "--amount", "200", "--stop-loss-pct", "0.10"],
        state_dir=state_dir,
        make_client=lambda: client1,
    )
    assert rc1 == 0

    on_disk = json.loads((state_dir / "positions.json").read_text())
    assert on_disk["cashUsd"] == 800.0
    assert len(on_disk["positions"]) == 1
    assert on_disk["positions"][0]["symbol"] == "QQQ"
    assert on_disk["positions"][0]["valueUsd"] == 200.0
    assert on_disk["positions"][0]["positionId"].startswith("local-open:")
    assert on_disk["positions"][0]["pending"] is True

    client2 = _mock_client_for("QQQ", 42)
    rc2 = place_order.main(
        ["open", "--symbol", "QQQ", "--amount", "200", "--stop-loss-pct", "0.10"],
        state_dir=state_dir,
        make_client=lambda: client2,
    )
    assert rc2 == 2
    client2.search_instrument.assert_not_called()
    client2.open_position_by_amount.assert_not_called()


def test_open_btc_luego_eth_excede_35_cripto_acumulado(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    state_dir = write_state(
        tmp_path,
        {
            "updatedAt": _fresh_updated_at(),
            "portfolioId": "pf-1",
            "cashUsd": 1000.0,
            "positions": [],
        },
        [("2026-08-01", 1000.0)],
    )

    client_btc = _mock_client_for("BTC", 1, close=50000.0)
    rc1 = place_order.main(
        ["open", "--symbol", "BTC", "--amount", "200", "--stop-loss-pct", "0.10"],
        state_dir=state_dir,
        make_client=lambda: client_btc,
    )
    assert rc1 == 0

    # BTC 200 (20%, dentro del 25% individual y del 35% cripto) ya registrado
    # localmente. ETH 200 individualmente también entraría (20%), pero
    # combinado (200+200)/1000 = 40% > 35% cripto -> debe bloquear.
    client_eth = _mock_client_for("ETH", 2, close=3000.0)
    rc2 = place_order.main(
        ["open", "--symbol", "ETH", "--amount", "200", "--stop-loss-pct", "0.10"],
        state_dir=state_dir,
        make_client=lambda: client_eth,
    )
    assert rc2 == 2
    client_eth.open_position_by_amount.assert_not_called()


def test_open_ambiguo_registra_exposicion_local(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    state_dir = write_state(
        tmp_path,
        {
            "updatedAt": _fresh_updated_at(),
            "portfolioId": "pf-1",
            "cashUsd": 1000.0,
            "positions": [],
        },
        [("2026-08-01", 1000.0)],
    )
    client = _mock_client_for("QQQ", 42)
    client.open_position_by_amount.side_effect = EtoroUnknownOutcomeError("body raro")
    rc = place_order.main(
        ["open", "--symbol", "QQQ", "--amount", "200", "--stop-loss-pct", "0.10"],
        state_dir=state_dir,
        make_client=lambda: client,
    )
    assert rc == 1
    on_disk = json.loads((state_dir / "positions.json").read_text())
    assert on_disk["cashUsd"] == 800.0
    assert len(on_disk["positions"]) == 1
    assert on_disk["positions"][0]["symbol"] == "QQQ"
    assert on_disk["positions"][0]["valueUsd"] == 200.0


def test_close_posicion_local_open_bloquea(tmp_path, monkeypatch):
    """Extensión defensiva del guard de posiciones sintéticas (regla 5):
    una posición 'local-open:...' registrada por este mismo script tras un
    open (fix #1) tampoco tiene un positionId real de eToro — cerrarla
    literalmente mandaría un id inventado a la API."""
    monkeypatch.setenv("DRY_RUN", "0")
    state = {
        "updatedAt": _fresh_updated_at(),
        "portfolioId": "pf-1",
        "cashUsd": 800.0,
        "positions": [
            {
                "positionId": "local-open:abc123",
                "symbol": "QQQ",
                "instrumentId": 42,
                "valueUsd": 200.0,
                "pending": True,
            }
        ],
    }
    write_state(tmp_path, state, [("2026-08-01", 1000.0)])
    client = MagicMock()
    rc = place_order.main(
        ["close", "--position-id", "local-open:abc123", "--symbol", "QQQ"],
        state_dir=tmp_path / "state",
        make_client=lambda: client,
    )
    assert rc == 2
    client.close_position.assert_not_called()


# -- 12: precio de vela inválido (fix quality review #2) --------------------


@pytest.mark.parametrize("bad_close", [0.0, -50.0, math.inf, math.nan])
def test_open_precio_de_vela_invalido_bloquea_sin_abrir(tmp_path, monkeypatch, bad_close):
    monkeypatch.setenv("DRY_RUN", "0")
    write_state(tmp_path, STATE_BASIC, EQUITY_ROWS_BASIC)
    client = MagicMock()
    # SPY (no QQQ: ya tiene posición en STATE_BASIC, WP1 bloquearía por no-duplicación).
    client.search_instrument.return_value = {
        "items": [{"internalInstrumentId": 42, "internalSymbolFull": "SPY", "isHiddenFromClient": False}]
    }
    client.get_candles.return_value = candles_resp(bad_close)
    rc = place_order.main(
        ["open", "--symbol", "SPY", "--amount", "20", "--stop-loss-pct", "0.10"],
        state_dir=tmp_path / "state",
        make_client=lambda: client,
    )
    assert rc == 1
    client.open_position_by_amount.assert_not_called()
    journal = (tmp_path / "state" / "journal.md").read_text()
    assert "ERROR" in journal


# -- 13: frescura de la vela (fix quality review #3) -------------------------


def test_open_vela_de_10_dias_bloquea_por_desactualizada(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    write_state(tmp_path, STATE_BASIC, EQUITY_ROWS_BASIC)
    client = MagicMock()
    # SPY (no QQQ: ya tiene posición en STATE_BASIC, WP1 bloquearía por no-duplicación).
    client.search_instrument.return_value = {
        "items": [{"internalInstrumentId": 42, "internalSymbolFull": "SPY", "isHiddenFromClient": False}]
    }
    stale_from_date = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    client.get_candles.return_value = candles_resp(100.0, from_date=stale_from_date)
    rc = place_order.main(
        ["open", "--symbol", "SPY", "--amount", "20", "--stop-loss-pct", "0.10"],
        state_dir=tmp_path / "state",
        make_client=lambda: client,
    )
    assert rc == 1
    client.open_position_by_amount.assert_not_called()
    journal = (tmp_path / "state" / "journal.md").read_text()
    assert "ERROR" in journal


def test_open_vela_sin_fromdate_bloquea(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    write_state(tmp_path, STATE_BASIC, EQUITY_ROWS_BASIC)
    client = MagicMock()
    # SPY (no QQQ: ya tiene posición en STATE_BASIC, WP1 bloquearía por no-duplicación).
    client.search_instrument.return_value = {
        "items": [{"internalInstrumentId": 42, "internalSymbolFull": "SPY", "isHiddenFromClient": False}]
    }
    client.get_candles.return_value = {"candles": [{"candles": [{"close": 100.0}]}]}
    rc = place_order.main(
        ["open", "--symbol", "SPY", "--amount", "20", "--stop-loss-pct", "0.10"],
        state_dir=tmp_path / "state",
        make_client=lambda: client,
    )
    assert rc == 1
    client.open_position_by_amount.assert_not_called()


# -- 14: close --symbol debe coincidir con el symbol real (fix #4) ----------


def test_close_symbol_no_coincide_con_state_bloquea(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    write_state(tmp_path, STATE_BASIC, EQUITY_ROWS_BASIC)  # pos-9 es QQQ
    client = MagicMock()
    rc = place_order.main(
        ["close", "--position-id", "pos-9", "--symbol", "SPY"],
        state_dir=tmp_path / "state",
        make_client=lambda: client,
    )
    assert rc == 2
    client.close_position.assert_not_called()
    journal = (tmp_path / "state" / "journal.md").read_text()
    assert "BLOQUEADA" in journal


# -- 15: crash de make_client no debe dar traceback pelado (fix #5) ---------


def test_open_make_client_crashea_journalea_error_y_rc1(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    write_state(tmp_path, STATE_BASIC, EQUITY_ROWS_BASIC)

    def make_client_que_crashea():
        raise ValueError("faltan credenciales ETORO_API_KEY/ETORO_USER_KEY")

    # SPY (no QQQ: ya tiene posición en STATE_BASIC, WP1 bloquearía por no-duplicación).
    rc = place_order.main(
        ["open", "--symbol", "SPY", "--amount", "20", "--stop-loss-pct", "0.10"],
        state_dir=tmp_path / "state",
        make_client=make_client_que_crashea,
    )
    assert rc == 1
    journal = (tmp_path / "state" / "journal.md").read_text()
    assert "ERROR" in journal
    assert "credenciales" in journal.lower()


def test_close_make_client_crashea_journalea_error_y_rc1(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    write_state(tmp_path, STATE_BASIC, EQUITY_ROWS_BASIC)

    def make_client_que_crashea():
        raise ValueError("faltan credenciales")

    rc = place_order.main(
        ["close", "--position-id", "pos-9", "--symbol", "QQQ"],
        state_dir=tmp_path / "state",
        make_client=make_client_que_crashea,
    )
    assert rc == 1
    journal = (tmp_path / "state" / "journal.md").read_text()
    assert "ERROR" in journal


# -- 16 (minor a): 2+ matches exactos en search -> exit 1, no primer match --


def test_open_multiples_matches_exactos_en_search_bloquea(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    write_state(tmp_path, STATE_BASIC, EQUITY_ROWS_BASIC)
    client = MagicMock()
    # SPY (no QQQ: ya tiene posición en STATE_BASIC, WP1 bloquearía por no-duplicación).
    client.search_instrument.return_value = {
        "items": [
            {"internalInstrumentId": 42, "internalSymbolFull": "SPY"},
            {"internalInstrumentId": 99, "internalSymbolFull": "SPY"},
        ]
    }
    rc = place_order.main(
        ["open", "--symbol", "SPY", "--amount", "20", "--stop-loss-pct", "0.10"],
        state_dir=tmp_path / "state",
        make_client=lambda: client,
    )
    assert rc == 1
    client.get_candles.assert_not_called()
    client.open_position_by_amount.assert_not_called()
    journal = (tmp_path / "state" / "journal.md").read_text()
    assert "ERROR" in journal


# -- 17 (minor b): state con updatedAt > 24h -> exit 2 "state stale" --------


def test_state_stale_mas_de_24h_bloquea(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    stale_state = dict(STATE_BASIC)
    stale_state["updatedAt"] = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    write_state(tmp_path, stale_state, EQUITY_ROWS_BASIC)
    client = MagicMock()
    rc = place_order.main(
        ["open", "--symbol", "QQQ", "--amount", "20", "--stop-loss-pct", "0.10"],
        state_dir=tmp_path / "state",
        make_client=lambda: client,
    )
    assert rc == 2
    client.search_instrument.assert_not_called()
    journal = (tmp_path / "state" / "journal.md").read_text()
    assert "stale" in journal.lower()


def test_state_stale_bloquea_tambien_close(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    stale_state = dict(STATE_BASIC)
    stale_state["updatedAt"] = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    write_state(tmp_path, stale_state, EQUITY_ROWS_BASIC)
    client = MagicMock()
    rc = place_order.main(
        ["close", "--position-id", "pos-9", "--symbol", "QQQ"],
        state_dir=tmp_path / "state",
        make_client=lambda: client,
    )
    assert rc == 2
    client.close_position.assert_not_called()
    journal = (tmp_path / "state" / "journal.md").read_text()
    assert "stale" in journal.lower()


# -- 18: fallback de variantes cripto en la resolucion de simbolo (Task 10, fix reviewer #4)


def test_open_btc_sin_match_exacto_resuelve_via_variante_btcusd(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DRY_RUN", "0")
    state_dir = write_state(
        tmp_path,
        {
            "updatedAt": _fresh_updated_at(),
            "portfolioId": "pf-1",
            "cashUsd": 1000.0,
            "positions": [],
        },
        [("2026-08-01", 1000.0)],
    )
    client = MagicMock()
    client.search_instrument.side_effect = [
        {"items": []},  # "BTC" exacto: sin match
        {"items": [{"internalInstrumentId": 99, "internalSymbolFull": "BTCUSD"}]},  # variante
    ]
    client.get_candles.return_value = candles_resp(50000.0)
    client.open_position_by_amount.return_value = {"positionID": 99}
    rc = place_order.main(
        ["open", "--symbol", "BTC", "--amount", "200", "--stop-loss-pct", "0.10"],
        state_dir=state_dir,
        make_client=lambda: client,
    )
    assert rc == 0
    assert client.search_instrument.call_args_list == [
        (("BTC",), {}),
        (("BTCUSD",), {}),
    ]
    client.open_position_by_amount.assert_called_once_with(
        instrument_id=99, amount_usd=200.0, stop_loss_rate=45000.0
    )
    assert "BTCUSD" in capsys.readouterr().err


def test_open_equity_sin_match_no_prueba_variantes_sin_cambios(tmp_path, monkeypatch):
    # SPY no tiene entrada en CRYPTO_SEARCH_VARIANTS -> comportamiento
    # identico al de antes de este fix: una sola llamada, error inmediato.
    # (SPY, no QQQ: ya tiene posición en STATE_BASIC, WP1 bloquearía por no-duplicación.)
    monkeypatch.setenv("DRY_RUN", "0")
    write_state(tmp_path, STATE_BASIC, EQUITY_ROWS_BASIC)
    client = MagicMock()
    client.search_instrument.return_value = {"items": []}
    rc = place_order.main(
        ["open", "--symbol", "SPY", "--amount", "20", "--stop-loss-pct", "0.10"],
        state_dir=tmp_path / "state",
        make_client=lambda: client,
    )
    assert rc == 1
    client.search_instrument.assert_called_once_with("SPY")
    client.open_position_by_amount.assert_not_called()


def test_open_btc_sin_ninguna_variante_con_match_falla_igual(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    state_dir = write_state(
        tmp_path,
        {
            "updatedAt": _fresh_updated_at(),
            "portfolioId": "pf-1",
            "cashUsd": 1000.0,
            "positions": [],
        },
        [("2026-08-01", 1000.0)],
    )
    client = MagicMock()
    client.search_instrument.return_value = {"items": []}  # ninguna variante matchea
    rc = place_order.main(
        ["open", "--symbol", "BTC", "--amount", "200", "--stop-loss-pct", "0.10"],
        state_dir=state_dir,
        make_client=lambda: client,
    )
    assert rc == 1
    assert client.search_instrument.call_count == 3  # BTC, BTCUSD, BTC-USD
    client.open_position_by_amount.assert_not_called()


def test_open_fuzzy_search_btca_primero_resuelve_btc_100000(tmp_path, monkeypatch):
    # Escenario real verificado con probes en vivo: buscar "BTC" devuelve
    # decenas de items no-exactos (aca simulados con 3) antes del match
    # exacto "BTC" -> instrumentId 100000. Nunca debe tomarse items[0].
    monkeypatch.setenv("DRY_RUN", "0")
    state_dir = write_state(
        tmp_path,
        {
            "updatedAt": _fresh_updated_at(),
            "portfolioId": "pf-1",
            "cashUsd": 1000.0,
            "positions": [],
        },
        [("2026-08-01", 1000.0)],
    )
    client = MagicMock()
    client.search_instrument.return_value = {
        "items": [
            {"internalSymbolFull": "BTCA", "internalInstrumentId": 55, "isHiddenFromClient": False},
            {"internalSymbolFull": "BTC", "internalInstrumentId": 100000, "isHiddenFromClient": False},
            {"internalSymbolFull": "BTCB", "internalInstrumentId": 56, "isHiddenFromClient": False},
        ]
    }
    client.get_candles.return_value = candles_resp(50000.0)
    client.open_position_by_amount.return_value = {"positionID": 100000}
    rc = place_order.main(
        ["open", "--symbol", "BTC", "--amount", "200", "--stop-loss-pct", "0.10"],
        state_dir=state_dir,
        make_client=lambda: client,
    )
    assert rc == 0
    client.open_position_by_amount.assert_called_once_with(
        instrument_id=100000, amount_usd=200.0, stop_loss_rate=45000.0
    )


# -- 18: positionId con tipo distinto entre state y CLI (bug critico) -------


def test_close_positionid_entero_en_state_matchea_con_cli_string(tmp_path, monkeypatch):
    """Bug critico hallado en la primera corrida dry-run real: snapshot.py
    persiste positionId tal cual lo devuelve la API (puede ser un ENTERO,
    p.ej. 3533695059), pero argparse siempre entrega --position-id como
    STRING. Sin normalizar la comparacion, int(3533695059) == str("3533695059")
    es SIEMPRE False -> ninguna posicion real era cerrable (ninguna salida
    por regla, SMA200 o ranking, podia ejecutarse). El id que se manda a la
    API debe ser el ORIGINAL del state (el entero), no el string casteado
    del CLI -- preservando el tipo que la API espera."""
    monkeypatch.setenv("DRY_RUN", "0")
    state = {
        "updatedAt": _fresh_updated_at(),
        "portfolioId": "pf-1",
        "cashUsd": 1000.0,
        "positions": [
            {"positionId": 3533695059, "symbol": "QQQ", "instrumentId": 7, "valueUsd": 50.0},
        ],
    }
    write_state(tmp_path, state, EQUITY_ROWS_BASIC)
    client = MagicMock()
    client.close_position.return_value = {"ok": True}
    rc = place_order.main(
        ["close", "--position-id", "3533695059", "--symbol", "QQQ"],
        state_dir=tmp_path / "state",
        make_client=lambda: client,
    )
    assert rc == 0
    # El id pasado a la API debe ser el ORIGINAL del state (int), no el
    # string "3533695059" que llego por argparse.
    client.close_position.assert_called_once_with(position_id=3533695059, instrument_id=7)
    journal = (tmp_path / "state" / "journal.md").read_text()
    assert "CERRADA" in journal


def test_close_positionid_string_en_state_sigue_matcheando_regresion(tmp_path, monkeypatch):
    """Regresion: positionId ya guardado como string en el state (caso
    preexistente) debe seguir matcheando tras el fix de normalizacion."""
    monkeypatch.setenv("DRY_RUN", "0")
    state = {
        "updatedAt": _fresh_updated_at(),
        "portfolioId": "pf-1",
        "cashUsd": 1000.0,
        "positions": [
            {"positionId": "pos-string-9", "symbol": "QQQ", "instrumentId": 7, "valueUsd": 50.0},
        ],
    }
    write_state(tmp_path, state, EQUITY_ROWS_BASIC)
    client = MagicMock()
    client.close_position.return_value = {"ok": True}
    rc = place_order.main(
        ["close", "--position-id", "pos-string-9", "--symbol", "QQQ"],
        state_dir=tmp_path / "state",
        make_client=lambda: client,
    )
    assert rc == 0
    client.close_position.assert_called_once_with(position_id="pos-string-9", instrument_id=7)


def test_close_positionid_sintetico_sigue_bloqueado_tras_el_fix(tmp_path, monkeypatch):
    """El fix de normalizacion de tipo no debe relajar el guard de
    posiciones sinteticas: 'pending-open:x' sigue exit 2 sin llamar API."""
    monkeypatch.setenv("DRY_RUN", "0")
    state = {
        "updatedAt": _fresh_updated_at(),
        "portfolioId": "pf-1",
        "cashUsd": 1000.0,
        "positions": [
            {
                "positionId": "pending-open:x",
                "symbol": "QQQ",
                "instrumentId": 7,
                "valueUsd": 50.0,
                "pending": True,
            },
        ],
    }
    write_state(tmp_path, state, EQUITY_ROWS_BASIC)
    client = MagicMock()
    rc = place_order.main(
        ["close", "--position-id", "pending-open:x", "--symbol", "QQQ"],
        state_dir=tmp_path / "state",
        make_client=lambda: client,
    )
    assert rc == 2
    client.close_position.assert_not_called()


def test_close_positionid_realmente_inexistente_sigue_exit_2_tras_el_fix(tmp_path, monkeypatch):
    """El fix de normalizacion de tipo no debe volver todo matcheable: un id
    que de verdad no existe (ni como int ni como string) sigue bloqueado."""
    monkeypatch.setenv("DRY_RUN", "0")
    state = {
        "updatedAt": _fresh_updated_at(),
        "portfolioId": "pf-1",
        "cashUsd": 1000.0,
        "positions": [
            {"positionId": 3533695059, "symbol": "QQQ", "instrumentId": 7, "valueUsd": 50.0},
        ],
    }
    write_state(tmp_path, state, EQUITY_ROWS_BASIC)
    client = MagicMock()
    rc = place_order.main(
        ["close", "--position-id", "999999999", "--symbol", "QQQ"],
        state_dir=tmp_path / "state",
        make_client=lambda: client,
    )
    assert rc == 2
    client.close_position.assert_not_called()


# -- 19: journal() timestampea en hora LOCAL con offset explicito, no UTC ---


def test_journal_usa_hora_local_con_offset_explicito(tmp_path, monkeypatch):
    """journal() debe escribir 'YYYY-MM-DD HH:MM +HHMM', hora local del
    sistema con offset — no más UTC ISO. Fija el reloj a un offset conocido
    (-03:00) para que el assert no dependa de la TZ del entorno donde
    corren los tests."""

    class _FixedNow:
        def astimezone(self):
            return datetime(2026, 8, 5, 19, 3, tzinfo=timezone(timedelta(hours=-3)))

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return _FixedNow()

    monkeypatch.setattr(place_order, "datetime", _FixedDatetime)

    place_order.journal(tmp_path / "state", "linea de prueba")

    line = (tmp_path / "state" / "journal.md").read_text().strip()
    assert line == "- 2026-08-05 19:03 -0300 linea de prueba"
    # No debe quedar en formato ISO 8601 UTC (el formato viejo, p.ej.
    # "2026-08-05T19:03:00+00:00" o con sufijo "Z").
    assert "T" not in line
    assert not re.search(r"\+00:00|Z ", line)

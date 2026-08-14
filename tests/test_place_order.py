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


# -- WP7: modo real listo por default (fixture compartido, ver tests/conftest.py) --
#
# El autouse fixture `_wp7_modo_real_listo_por_default` vive en
# tests/conftest.py (aplica a toda la suite, no solo a este archivo) --
# ver su docstring ahí para el porqué completo.


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


# -- WP1: presupuesto de órdenes por corrida y por día (state/.run_orders.json) ---
#
# Auditoría pre-producción: "el tope de 3 órdenes/corrida vive solo en
# prosa" (PLAYBOOK.md), sin nada en código que lo haga cumplir. Este bloque
# cubre el presupuesto en código: máx MAX_ORDERS_PER_RUN por
# ETOROAGENT_RUN_ID, máx MAX_ORDERS_PER_DAY por día calendario (aplica
# siempre, con o sin ETOROAGENT_RUN_ID). Los BLOQUEOS (exit 2, sea por
# riesgo o por presupuesto) no consumen presupuesto; las órdenes
# ejecutadas (dry-run u real) y las de resultado AMBIGUO sí.


def _big_state_dir(tmp_path, cash=1_000_000.0):
    """Portfolio grande y sin posiciones previas: aísla los tests de
    presupuesto de cualquier interacción con los topes de riesgo (25%,
    70% agregado, no-duplicación) al usar un símbolo distinto por orden."""
    return write_state(
        tmp_path,
        {
            "updatedAt": _fresh_updated_at(),
            "portfolioId": "pf-1",
            "cashUsd": cash,
            "positions": [],
        },
        [("2026-08-01", cash)],
    )


# Símbolos distintos del universo para no chocar con la regla de
# no-duplicación al hacer varias aperturas seguidas en los tests de abajo.
_BUDGET_SYMBOLS = ["SPY", "QQQ", "XLK", "XLE", "XLF", "XLV", "XLI", "XLP", "XLU"]


def _open_ok(state_dir, symbol, instrument_id=1, amount=10.0):
    client = _mock_client_for(symbol, instrument_id, close=100.0)
    rc = place_order.main(
        ["open", "--symbol", symbol, "--amount", str(amount), "--stop-loss-pct", "0.10"],
        state_dir=state_dir,
        make_client=lambda: client,
    )
    return rc, client


def test_cuarta_orden_mismo_run_id_bloquea_sin_llamar_cliente(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("ETOROAGENT_RUN_ID", "2026-08-12-100000-equities")
    state_dir = _big_state_dir(tmp_path)

    for i, symbol in enumerate(_BUDGET_SYMBOLS[:3]):
        rc, _ = _open_ok(state_dir, symbol, instrument_id=i)
        assert rc == 0

    client4 = MagicMock()
    rc4 = place_order.main(
        ["open", "--symbol", _BUDGET_SYMBOLS[3], "--amount", "10", "--stop-loss-pct", "0.10"],
        state_dir=state_dir,
        make_client=lambda: client4,
    )
    assert rc4 == 2
    client4.search_instrument.assert_not_called()
    client4.open_position_by_amount.assert_not_called()
    journal = (state_dir / "journal.md").read_text()
    assert "tope de 3 órdenes por corrida" in journal


def test_run_id_nuevo_resetea_presupuesto_de_corrida(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("ETOROAGENT_RUN_ID", "run-A")
    state_dir = _big_state_dir(tmp_path)

    for i, symbol in enumerate(_BUDGET_SYMBOLS[:3]):
        rc, _ = _open_ok(state_dir, symbol, instrument_id=i)
        assert rc == 0

    # Bajo el mismo run-A, una 4ta orden se bloquearía (ver test anterior).
    # Cambiar ETOROAGENT_RUN_ID a un valor nuevo (como haría runner.sh en la
    # siguiente corrida, con un STAMP distinto) debe resetear el contador.
    monkeypatch.setenv("ETOROAGENT_RUN_ID", "run-B")
    rc, client = _open_ok(state_dir, _BUDGET_SYMBOLS[3], instrument_id=3)
    assert rc == 0
    client.open_position_by_amount.assert_called_once()


def test_septima_orden_del_dia_bloquea(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    state_dir = _big_state_dir(tmp_path)

    # 6 aperturas repartidas en 2 "corridas" (run ids distintos, 3 cada
    # una) para no chocar con el tope POR CORRIDA (WP1/N4b) al ejercitar
    # el tope DIARIO global -- MAX_ORDERS_PER_DAY=6 aplica sin importar
    # cuántas corridas (o invocaciones manuales) distintas lo alcanzaron.
    monkeypatch.setenv("ETOROAGENT_RUN_ID", "run-dia-1")
    for i, symbol in enumerate(_BUDGET_SYMBOLS[:3]):
        rc, _ = _open_ok(state_dir, symbol, instrument_id=i)
        assert rc == 0
    monkeypatch.setenv("ETOROAGENT_RUN_ID", "run-dia-2")
    for i, symbol in enumerate(_BUDGET_SYMBOLS[3:6]):
        rc, _ = _open_ok(state_dir, symbol, instrument_id=i + 3)
        assert rc == 0

    monkeypatch.setenv("ETOROAGENT_RUN_ID", "run-dia-3")
    client7 = MagicMock()
    rc7 = place_order.main(
        ["open", "--symbol", _BUDGET_SYMBOLS[6], "--amount", "10", "--stop-loss-pct", "0.10"],
        state_dir=state_dir,
        make_client=lambda: client7,
    )
    assert rc7 == 2
    client7.search_instrument.assert_not_called()
    client7.open_position_by_amount.assert_not_called()
    journal = (state_dir / "journal.md").read_text()
    assert "presupuesto diario" in journal


# -- WP4/N4(b): invocación manual (sin ETOROAGENT_RUN_ID) usa un run_id ------
# -- sintético "manual-YYYY-MM-DD" -------------------------------------------
#
# Re-auditoría: rotar/desetear ETOROAGENT_RUN_ID evadía el tope por
# corrida. Antes de N4(b), "sin ETOROAGENT_RUN_ID" solo tenía el tope
# diario global (6) como freno -- hasta 6 aperturas manuales seguidas sin
# ningún throttle intermedio. Con el run_id sintético, una invocación
# manual queda acotada a MAX_ORDERS_PER_RUN (3) por día, igual que una
# corrida real -- ADEMÁS del tope diario global, que sigue aplicando.


def test_manual_sin_run_id_topea_en_3_por_dia_ademas_del_diario_global(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.delenv("ETOROAGENT_RUN_ID", raising=False)
    state_dir = _big_state_dir(tmp_path)

    for i, symbol in enumerate(_BUDGET_SYMBOLS[:3]):
        rc, client = _open_ok(state_dir, symbol, instrument_id=i)
        assert rc == 0
        client.open_position_by_amount.assert_called_once()

    # 4ta apertura manual el MISMO día -- bloqueada por el tope "por
    # corrida" del run_id sintético, aunque el diario global (6) todavía
    # tenga margen.
    client4 = MagicMock()
    rc4 = place_order.main(
        ["open", "--symbol", _BUDGET_SYMBOLS[3], "--amount", "10", "--stop-loss-pct", "0.10"],
        state_dir=state_dir,
        make_client=lambda: client4,
    )
    assert rc4 == 2
    client4.search_instrument.assert_not_called()
    journal = (state_dir / "journal.md").read_text()
    assert "tope de 3 órdenes por corrida" in journal


def test_manual_run_id_sintetico_resetea_al_dia_siguiente(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.delenv("ETOROAGENT_RUN_ID", raising=False)
    state_dir = _big_state_dir(tmp_path)

    monkeypatch.setattr(place_order, "_today_local", lambda: "2026-08-12")
    for i, symbol in enumerate(_BUDGET_SYMBOLS[:3]):
        rc, _ = _open_ok(state_dir, symbol, instrument_id=i)
        assert rc == 0

    # Día siguiente: el run_id sintético cambia ("manual-2026-08-13"),
    # mismo mecanismo de reset que un ETOROAGENT_RUN_ID real nuevo.
    monkeypatch.setattr(place_order, "_today_local", lambda: "2026-08-13")
    rc, client = _open_ok(state_dir, _BUDGET_SYMBOLS[3], instrument_id=3)
    assert rc == 0
    client.open_position_by_amount.assert_called_once()


def test_manual_y_run_id_real_no_comparten_contador_de_corrida(tmp_path, monkeypatch):
    # Una corrida real (ETOROAGENT_RUN_ID seteada) y una invocación manual
    # el mismo día son run_ids DISTINTOS ("run-real" vs
    # "manual-<hoy>") -- cada una tiene su propio tope de 3, no se pisan.
    monkeypatch.setenv("DRY_RUN", "0")
    state_dir = _big_state_dir(tmp_path)

    monkeypatch.setenv("ETOROAGENT_RUN_ID", "run-real")
    for i, symbol in enumerate(_BUDGET_SYMBOLS[:3]):
        rc, _ = _open_ok(state_dir, symbol, instrument_id=i)
        assert rc == 0

    monkeypatch.delenv("ETOROAGENT_RUN_ID", raising=False)
    rc, client = _open_ok(state_dir, _BUDGET_SYMBOLS[3], instrument_id=3)
    assert rc == 0
    client.open_position_by_amount.assert_called_once()


def test_bloqueo_por_riesgo_no_consume_presupuesto(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("ETOROAGENT_RUN_ID", "run-riesgo")
    state_dir = _big_state_dir(tmp_path)

    client_blocked = MagicMock()
    # stop-loss > 12% -> bloqueado por risk.validate(), no llega a client.
    rc_blocked = place_order.main(
        ["open", "--symbol", "SPY", "--amount", "10", "--stop-loss-pct", "0.50"],
        state_dir=state_dir,
        make_client=lambda: client_blocked,
    )
    assert rc_blocked == 2
    client_blocked.search_instrument.assert_not_called()

    # Si el bloqueo hubiera consumido presupuesto, la 3ra orden de acá abajo
    # ya estaría en la 4ta posición del contador y se bloquearía. Deben
    # pasar las 3, exactamente al tope de MAX_ORDERS_PER_RUN.
    for i, symbol in enumerate(_BUDGET_SYMBOLS[:3]):
        rc, client = _open_ok(state_dir, symbol, instrument_id=i)
        assert rc == 0, f"orden {i} ({symbol}) debería pasar: presupuesto no debía estar consumido"


# -- REGRESIÓN CRÍTICA: escenario de la auditoría (recompra en corridas sucesivas) --


def test_regresion_auditoria_recompra_en_corridas_sucesivas_queda_bloqueada(tmp_path, monkeypatch):
    """Replica el hallazgo central de la auditoría pre-producción: 3
    corridas IDÉNTICAS del agente (misma propuesta: abrir XLV 1500, XLF
    1200, XLK 1000 sobre un portfolio de ~10000) construían, antes de WP1,
    59% de exposición real acumulada, mientras el agente -- que solo veía
    el 25% por símbolo como freno -- creía estar en 37%. Con la regla de
    no-duplicación, la corrida 2 (y la 3) deben quedar TOTALMENTE
    bloqueadas: la exposición final tiene que ser 37% (3700/10000), nunca
    59%.

    El registro de exposición intra-corrida usado acá es el MISMO camino
    real que usaría place_order.py en producción
    (_register_local_exposure vía un open exitoso, regla 8 del módulo) --
    no se simula el state a mano."""
    monkeypatch.setenv("DRY_RUN", "0")
    state_dir = _big_state_dir(tmp_path, cash=10_000.0)

    ordenes = [("XLV", 1500.0, 10), ("XLF", 1200.0, 11), ("XLK", 1000.0, 12)]

    # --- Corrida 1: las 3 aperturas pasan (nada previo del mismo símbolo) ---
    monkeypatch.setenv("ETOROAGENT_RUN_ID", "2026-08-10-100000-equities")
    for symbol, amount, instrument_id in ordenes:
        rc, client = _open_ok(state_dir, symbol, instrument_id=instrument_id, amount=amount)
        assert rc == 0, f"corrida 1, {symbol}: debería abrir"
        client.open_position_by_amount.assert_called_once()

    on_disk = json.loads((state_dir / "positions.json").read_text())
    assert len(on_disk["positions"]) == 3
    assert on_disk["cashUsd"] == 10_000.0 - 1500.0 - 1200.0 - 1000.0  # 6300.0
    exposicion_tras_corrida_1 = sum(p["valueUsd"] for p in on_disk["positions"])
    assert exposicion_tras_corrida_1 == 3700.0  # 37% de 10000

    # --- Corrida 2: MISMA propuesta -- debe quedar TODA bloqueada por no-duplicación ---
    monkeypatch.setenv("ETOROAGENT_RUN_ID", "2026-08-11-100000-equities")
    for symbol, amount, instrument_id in ordenes:
        client = MagicMock()
        rc = place_order.main(
            ["open", "--symbol", symbol, "--amount", str(amount), "--stop-loss-pct", "0.10"],
            state_dir=state_dir,
            make_client=lambda: client,
        )
        assert rc == 2, f"corrida 2, {symbol}: debería bloquearse por no-duplicación"
        client.search_instrument.assert_not_called()
        client.open_position_by_amount.assert_not_called()

    on_disk_tras_corrida_2 = json.loads((state_dir / "positions.json").read_text())
    assert len(on_disk_tras_corrida_2["positions"]) == 3  # sin cambios: nada se agregó
    exposicion_tras_corrida_2 = sum(p["valueUsd"] for p in on_disk_tras_corrida_2["positions"])
    assert exposicion_tras_corrida_2 == 3700.0
    assert exposicion_tras_corrida_2 / 10_000.0 == 0.37  # nunca 59%

    journal = (state_dir / "journal.md").read_text()
    assert journal.count("no-duplicaci") >= 3

    # --- Corrida 3: mismo resultado -- confirma que no es un fluke de la corrida 2 ---
    monkeypatch.setenv("ETOROAGENT_RUN_ID", "2026-08-12-100000-equities")
    for symbol, amount, instrument_id in ordenes:
        client = MagicMock()
        rc = place_order.main(
            ["open", "--symbol", symbol, "--amount", str(amount), "--stop-loss-pct", "0.10"],
            state_dir=state_dir,
            make_client=lambda: client,
        )
        assert rc == 2, f"corrida 3, {symbol}: debería bloquearse por no-duplicación"
        client.open_position_by_amount.assert_not_called()

    on_disk_final = json.loads((state_dir / "positions.json").read_text())
    exposicion_final = sum(p["valueUsd"] for p in on_disk_final["positions"])
    assert exposicion_final == 3700.0
    assert exposicion_final / 10_000.0 == 0.37


# -- WP2: flag de reconciliación tras corrida abortada (state/.needs_reconciliation) --
#
# Auditoría pre-producción: 2 corridas emitieron órdenes y murieron (corte
# de conexión con la API de Anthropic) ANTES de journalear el razonamiento
# narrativo. scripts/runner.sh crea state/.needs_reconciliation cuando esto
# pasa (claude exit != 0) -- ver tests/test_runner.py. Acá se cubre el lado
# de place_order.py: mientras el flag exista, las APERTURAS se bloquean
# (exit 2, sin tocar el cliente HTTP); los CIERRES siguen permitidos
# siempre -- reducir riesgo nunca debe esperar a una reconciliación.


def test_open_con_flag_de_reconciliacion_bloquea_sin_llamar_cliente(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    state_dir = _big_state_dir(tmp_path)
    (state_dir / ".needs_reconciliation").write_text(
        json.dumps({"reason": "corrida equities abortada (claude exit 1)"})
    )

    client = MagicMock()
    rc = place_order.main(
        ["open", "--symbol", "QQQ", "--amount", "10", "--stop-loss-pct", "0.10"],
        state_dir=state_dir,
        make_client=lambda: client,
    )

    assert rc == 2
    client.search_instrument.assert_not_called()
    client.open_position_by_amount.assert_not_called()
    journal = (state_dir / "journal.md").read_text()
    assert "BLOQUEADA" in journal
    assert "reconciliaci" in journal
    assert "RECONCILIACION" in journal
    assert "state/.needs_reconciliation" in journal


def test_close_con_flag_de_reconciliacion_permitido(tmp_path, monkeypatch):
    """Los cierres son la dirección fail-safe (reducir riesgo): deben seguir
    funcionando aunque haya una reconciliación pendiente."""
    monkeypatch.setenv("DRY_RUN", "0")
    state = {
        "updatedAt": _fresh_updated_at(),
        "portfolioId": "pf-1",
        "cashUsd": 1000.0,
        "positions": [
            {"positionId": "pos-9", "symbol": "QQQ", "instrumentId": 7, "valueUsd": 50.0},
        ],
    }
    state_dir = write_state(tmp_path, state, EQUITY_ROWS_BASIC)
    (state_dir / ".needs_reconciliation").write_text(
        json.dumps({"reason": "corrida crypto abortada (claude exit 1)"})
    )

    client = MagicMock()
    client.close_position.return_value = {"ok": True}
    rc = place_order.main(
        ["close", "--position-id", "pos-9", "--symbol", "QQQ"],
        state_dir=state_dir,
        make_client=lambda: client,
    )

    assert rc == 0
    client.close_position.assert_called_once()
    journal = (state_dir / "journal.md").read_text()
    assert "CERRADA" in journal


def test_borrar_flag_de_reconciliacion_rehabilita_aperturas(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    state_dir = _big_state_dir(tmp_path)
    flag_path = state_dir / ".needs_reconciliation"
    flag_path.write_text(json.dumps({"reason": "corrida equities abortada (claude exit 1)"}))

    rc_blocked = place_order.main(
        ["open", "--symbol", "QQQ", "--amount", "10", "--stop-loss-pct", "0.10"],
        state_dir=state_dir,
        make_client=lambda: MagicMock(),
    )
    assert rc_blocked == 2

    # El operador reconcilia y borra el flag (PLAYBOOK.md §Reconciliación
    # tras corrida abortada, paso 4) -- la próxima apertura debe pasar.
    flag_path.unlink()
    rc, client = _open_ok(state_dir, "QQQ", instrument_id=7)
    assert rc == 0


# -- WP4/N1 CRÍTICO: los CIERRES nunca esperan presupuesto de órdenes -------
#
# Re-auditoría: el presupuesto de órdenes (WP1) evaluaba y consumía tanto
# para open como para close. Reducir riesgo (cerrar una posición) NUNCA
# debe esperar a que haya "presupuesto" disponible -- mismo principio
# fail-safe que ya rige la reconciliación (WP2: los cierres siguen
# permitidos con el flag puesto). Fix: el presupuesto (por corrida y
# diario) aplica SOLO a aperturas; los cierres ni lo chequean ni lo
# consumen.


def _closable_state_dir(tmp_path, cash=1_000_000.0):
    """Como _big_state_dir, pero con una posición REAL cerrable (GLD,
    símbolo distinto de los que usan los tests de presupuesto en
    _BUDGET_SYMBOLS para no interferir con no-duplicación)."""
    return write_state(
        tmp_path,
        {
            "updatedAt": _fresh_updated_at(),
            "portfolioId": "pf-1",
            "cashUsd": cash,
            "positions": [
                {"positionId": "pos-close", "symbol": "GLD", "instrumentId": 99, "valueUsd": 50.0},
            ],
        },
        [("2026-08-01", cash)],
    )


def test_cierre_pasa_con_presupuesto_de_corrida_agotado(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("ETOROAGENT_RUN_ID", "run-close-test")
    state_dir = _closable_state_dir(tmp_path)

    # Agotar el presupuesto de la corrida (3 aperturas).
    for i, symbol in enumerate(_BUDGET_SYMBOLS[:3]):
        rc, _ = _open_ok(state_dir, symbol, instrument_id=i)
        assert rc == 0

    # Confirmamos que una 4ta APERTURA sí se bloquea (control del escenario).
    rc_open_blocked = place_order.main(
        ["open", "--symbol", _BUDGET_SYMBOLS[3], "--amount", "10", "--stop-loss-pct", "0.10"],
        state_dir=state_dir,
        make_client=lambda: MagicMock(),
    )
    assert rc_open_blocked == 2

    # Un CIERRE, en cambio, debe pasar igual -- nunca debe esperar presupuesto.
    client_close = MagicMock()
    client_close.close_position.return_value = {"ok": True}
    rc_close = place_order.main(
        ["close", "--position-id", "pos-close", "--symbol", "GLD"],
        state_dir=state_dir,
        make_client=lambda: client_close,
    )
    assert rc_close == 0
    client_close.close_position.assert_called_once()
    journal = (state_dir / "journal.md").read_text()
    assert "CERRADA" in journal

    # El cierre tampoco debe haber consumido presupuesto (los contadores
    # quedan exactamente como los dejaron las 3 aperturas anteriores).
    budget = json.loads((state_dir / ".run_orders.json").read_text())
    assert budget["count"] == 3
    assert budget["dailyCount"] == 3


def test_cierre_pasa_con_presupuesto_diario_agotado(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.delenv("ETOROAGENT_RUN_ID", raising=False)
    state_dir = _closable_state_dir(tmp_path)

    # Agotar el presupuesto diario global (6 aperturas, repartidas en 2
    # corridas para no chocar con el tope por corrida).
    monkeypatch.setenv("ETOROAGENT_RUN_ID", "run-dia-a")
    for i, symbol in enumerate(_BUDGET_SYMBOLS[:3]):
        rc, _ = _open_ok(state_dir, symbol, instrument_id=i)
        assert rc == 0
    monkeypatch.setenv("ETOROAGENT_RUN_ID", "run-dia-b")
    for i, symbol in enumerate(_BUDGET_SYMBOLS[3:6]):
        rc, _ = _open_ok(state_dir, symbol, instrument_id=i + 3)
        assert rc == 0

    # Control: una 7ma apertura (corrida nueva, presupuesto de corrida
    # fresco) se bloquea igual por el tope DIARIO.
    monkeypatch.setenv("ETOROAGENT_RUN_ID", "run-dia-c")
    rc_open_blocked = place_order.main(
        ["open", "--symbol", _BUDGET_SYMBOLS[6], "--amount", "10", "--stop-loss-pct", "0.10"],
        state_dir=state_dir,
        make_client=lambda: MagicMock(),
    )
    assert rc_open_blocked == 2

    client_close = MagicMock()
    client_close.close_position.return_value = {"ok": True}
    rc_close = place_order.main(
        ["close", "--position-id", "pos-close", "--symbol", "GLD"],
        state_dir=state_dir,
        make_client=lambda: client_close,
    )
    assert rc_close == 0
    client_close.close_position.assert_called_once()


# -- WP4/N3(a): presupuesto corrupto/ilegible -> fail-closed para aperturas --
#
# Re-auditoría: state/.run_orders.json ilegible (JSON inválido, o campos
# con tipo inesperado) hacía que _load_order_budget() devolviera contadores
# en 0 -- presupuesto "lleno" de nuevo (fail-OPEN). Fix: un archivo
# PRESENTE pero corrupto se trata como presupuesto AGOTADO para aperturas
# (exit 2, requiere intervención manual). Un archivo AUSENTE (nunca se
# corrió una orden aún) sigue siendo el caso normal, contadores en 0.


def test_presupuesto_corrupto_bloquea_apertura_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    state_dir = _big_state_dir(tmp_path)
    (state_dir / ".run_orders.json").write_text("{esto no es json")

    client = MagicMock()
    rc = place_order.main(
        ["open", "--symbol", "SPY", "--amount", "10", "--stop-loss-pct", "0.10"],
        state_dir=state_dir,
        make_client=lambda: client,
    )
    assert rc == 2
    client.search_instrument.assert_not_called()
    client.open_position_by_amount.assert_not_called()
    journal = (state_dir / "journal.md").read_text()
    assert "presupuesto ilegible" in journal
    # El archivo corrupto no se toca/sobreescribe silenciosamente.
    assert (state_dir / ".run_orders.json").read_text() == "{esto no es json"


def test_presupuesto_con_tipo_inesperado_en_count_bloquea_apertura(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    state_dir = _big_state_dir(tmp_path)
    (state_dir / ".run_orders.json").write_text(
        json.dumps({"runId": "x", "count": "tres", "date": None, "dailyCount": 0})
    )

    client = MagicMock()
    rc = place_order.main(
        ["open", "--symbol", "SPY", "--amount", "10", "--stop-loss-pct", "0.10"],
        state_dir=state_dir,
        make_client=lambda: client,
    )
    assert rc == 2
    client.search_instrument.assert_not_called()
    journal = (state_dir / "journal.md").read_text()
    assert "presupuesto ilegible" in journal


def test_presupuesto_no_es_un_objeto_json_bloquea_apertura(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    state_dir = _big_state_dir(tmp_path)
    (state_dir / ".run_orders.json").write_text(json.dumps([1, 2, 3]))

    client = MagicMock()
    rc = place_order.main(
        ["open", "--symbol", "SPY", "--amount", "10", "--stop-loss-pct", "0.10"],
        state_dir=state_dir,
        make_client=lambda: client,
    )
    assert rc == 2
    journal = (state_dir / "journal.md").read_text()
    assert "presupuesto ilegible" in journal


def test_presupuesto_corrupto_no_bloquea_cierre(tmp_path, monkeypatch):
    # post-N1: los cierres ni siquiera chequean presupuesto -- pasan aunque
    # el archivo esté corrupto.
    monkeypatch.setenv("DRY_RUN", "0")
    state_dir = _closable_state_dir(tmp_path)
    (state_dir / ".run_orders.json").write_text("{esto no es json")

    client = MagicMock()
    client.close_position.return_value = {"ok": True}
    rc = place_order.main(
        ["close", "--position-id", "pos-close", "--symbol", "GLD"],
        state_dir=state_dir,
        make_client=lambda: client,
    )
    assert rc == 0
    client.close_position.assert_called_once()


def test_presupuesto_ausente_no_es_corrupcion_primera_orden_pasa(tmp_path, monkeypatch):
    # Control: archivo AUSENTE (nunca se corrió una orden aún) es el caso
    # normal -- no debe tratarse como corrupción.
    monkeypatch.setenv("DRY_RUN", "0")
    state_dir = _big_state_dir(tmp_path)
    assert not (state_dir / ".run_orders.json").exists()

    rc, client = _open_ok(state_dir, "SPY", instrument_id=1)
    assert rc == 0
    client.open_position_by_amount.assert_called_once()


# -- WP4/N5: ETOROAGENT_STATE_DIR redirige state/ cuando no hay kwarg -------
#
# Pensado para harnesses de test/auditoría que invocan el script real por
# subprocess (no pueden pasar el kwarg state_dir= de Python). La
# protección contra que el AGENTE la use para evadir presupuesto/
# reconciliación es el hook (N4a), no este fallback.


def test_state_dir_por_env_cuando_kwarg_ausente(tmp_path, monkeypatch):
    # DRY_RUN por default (no seteado, fail-safe): no hace falta un client
    # mockeado, y no toca el state/ real del repo -- ETOROAGENT_STATE_DIR
    # debe resolverse igual sin el kwarg state_dir=.
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.delenv("ETOROAGENT_RUN_ID", raising=False)
    env_state_dir = write_state(tmp_path, STATE_BASIC, EQUITY_ROWS_BASIC)
    monkeypatch.setenv("ETOROAGENT_STATE_DIR", str(env_state_dir))

    # Sin pasar state_dir= -- debe resolver vía ETOROAGENT_STATE_DIR, no el
    # STATE_DIR real del repo.
    rc = place_order.main(["open", "--symbol", "SPY", "--amount", "10", "--stop-loss-pct", "0.10"])

    assert rc == 0
    journal = (env_state_dir / "journal.md").read_text()
    assert "DRY_RUN" in journal


def test_state_dir_kwarg_explicito_tiene_prioridad_sobre_env(tmp_path, monkeypatch):
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.delenv("ETOROAGENT_RUN_ID", raising=False)
    kwarg_dir = write_state(tmp_path / "a", STATE_BASIC, EQUITY_ROWS_BASIC)
    other_dir = tmp_path / "b" / "state"
    monkeypatch.setenv("ETOROAGENT_STATE_DIR", str(other_dir))

    rc = place_order.main(
        ["open", "--symbol", "SPY", "--amount", "10", "--stop-loss-pct", "0.10"],
        state_dir=kwarg_dir,
    )

    assert rc == 0
    assert (kwarg_dir / "journal.md").exists()
    assert not other_dir.exists()


def test_sin_env_ni_kwarg_usa_state_dir_default_del_modulo(tmp_path, monkeypatch):
    # Sin ETOROAGENT_STATE_DIR seteada y sin kwarg -- debe caer al
    # STATE_DIR real del módulo (place_order.STATE_DIR), no a otra ruta.
    # Monkeypatcheamos STATE_DIR a un tmp_path para no depender del
    # filesystem real ni ensuciar el state/ real del repo.
    monkeypatch.delenv("ETOROAGENT_STATE_DIR", raising=False)
    monkeypatch.delenv("ETOROAGENT_RUN_ID", raising=False)
    monkeypatch.delenv("DRY_RUN", raising=False)
    fallback_dir = write_state(tmp_path, STATE_BASIC, EQUITY_ROWS_BASIC)
    monkeypatch.setattr(place_order, "STATE_DIR", fallback_dir)

    rc = place_order.main(["open", "--symbol", "SPY", "--amount", "10", "--stop-loss-pct", "0.10"])

    assert rc == 0
    assert (fallback_dir / "journal.md").exists()


# ============================================================================
# WP7: el candado final para habilitar DRY_RUN=0 desatendido.
# ============================================================================
#
# Piezas 1 (reconstrucción de estado desde la API en modo real), 2 (sombra
# de integridad fuera del repo) y 3 (autorización de corridas reales).
# Todos los tests de esta sección marcados @pytest.mark.wp7_real optan
# afuera del stub por default de _reconstruct_state_from_api/_load_shadow
# (ver tests/conftest.py) para ejercitar la lógica REAL.


def _client_factory_no_debe_invocarse():
    raise AssertionError(
        "make_client no debía invocarse: el bloqueo tenía que ocurrir antes "
        "de tocar el cliente HTTP"
    )


def _wp7_search_instrument_side_effect(symbol_to_instrument: dict):
    def _side_effect(query_symbol):
        if query_symbol in symbol_to_instrument:
            return {
                "items": [
                    {
                        "internalInstrumentId": symbol_to_instrument[query_symbol],
                        "internalSymbolFull": query_symbol,
                        "isHiddenFromClient": False,
                    }
                ]
            }
        return {"items": []}

    return _side_effect


def _wp7_pnl_response(cash: float, positions: list) -> dict:
    """positions: lista de (symbol_ignorado_aca, instrument_id, amount) --
    el símbolo real lo resuelve build_state() vía symbol_by_id, no hace
    falta acá (se conserva en la tupla solo para legibilidad del test)."""
    return {
        "clientPortfolio": {
            "credit": cash,
            "positions": [
                {
                    "positionID": f"pos-{i}",
                    "instrumentID": instrument_id,
                    "amount": amount,
                    "unrealizedPnL": 0,
                }
                for i, (_symbol, instrument_id, amount) in enumerate(positions)
            ],
            "ordersForOpen": [],
            "orders": [],
            "mirrors": [],
        }
    }


def _wp7_client(cash: float, positions: list, symbol_to_instrument: dict, price: float = 100.0):
    """Cliente mockeado completo para tests @wp7_real: get_pnl() +
    search_instrument() (para la resolución de universo que hace
    snapshot._resolve_universe_symbol_by_id) + get_candles() +
    open_position_by_amount()."""
    client = MagicMock()
    client.search_instrument.side_effect = _wp7_search_instrument_side_effect(symbol_to_instrument)
    client.get_pnl.return_value = _wp7_pnl_response(cash, positions)
    client.get_candles.return_value = candles_resp(price)
    client.open_position_by_amount.return_value = {"positionID": "new-1"}
    return client


def _write_shadow(shadow_dir: Path, equity_rows: list, budget: dict) -> None:
    shadow_dir.mkdir(parents=True, exist_ok=True)
    lines = ["date,total"] + [f"{date},{value}" for date, value in equity_rows]
    (shadow_dir / place_order.EQUITY_SHADOW_FILE).write_text("\n".join(lines) + "\n")
    (shadow_dir / place_order.RUN_ORDERS_SHADOW_FILE).write_text(json.dumps(budget))


# -- Pieza 1: reconstrucción de estado desde la API en vivo -----------------


def _stub_shadow_neutral(monkeypatch):
    """@pytest.mark.wp7_real opta afuera de AMBOS stubs por default
    (reconstrucción Y sombra, ver tests/conftest.py) -- los tests de esta
    sub-sección ejercitan SOLO la pieza 1 (reconstrucción real) y no les
    interesa la pieza 2 (sombra), así que reponen el stub neutro de
    _load_shadow acá para no toparse con el gate de sombra ausente."""
    monkeypatch.setattr(
        place_order,
        "_load_shadow",
        lambda shadow_dir: {"equity_rows": [], "budget": None},
    )


@pytest.mark.wp7_real
def test_wp7_reconstruccion_usa_estado_vivo_no_el_archivo(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    _stub_shadow_neutral(monkeypatch)
    # El ARCHIVO dice casi sin cash y con una posición QQQ existente (que
    # bloquearía por no-duplicación) -- pero la API en vivo dice que el
    # portfolio real está vacío y con cash de sobra. La reconstrucción
    # debe validar contra la API, no contra el archivo desactualizado.
    stale_state = {
        "updatedAt": _fresh_updated_at(),
        "portfolioId": "pf-1",
        "cashUsd": 1.0,
        "positions": [{"positionId": "pos-9", "symbol": "QQQ", "instrumentId": 7, "valueUsd": 999.0}],
    }
    state_dir = write_state(tmp_path, stale_state, EQUITY_ROWS_BASIC)
    client = _wp7_client(cash=1000.0, positions=[], symbol_to_instrument={"SPY": 1})

    rc = place_order.main(
        ["open", "--symbol", "SPY", "--amount", "20", "--stop-loss-pct", "0.10"],
        state_dir=state_dir,
        make_client=lambda: client,
    )

    assert rc == 0
    client.open_position_by_amount.assert_called_once()


@pytest.mark.wp7_real
def test_wp7_reconstruccion_bloquea_no_dup_con_posicion_real_solo_visible_en_api(
    tmp_path, monkeypatch
):
    # Inverso del anterior: el ARCHIVO no tiene ninguna posición, pero la
    # API en vivo sí tiene QQQ -- debe bloquear por no-duplicación igual,
    # porque valida contra la API, no contra el archivo.
    monkeypatch.setenv("DRY_RUN", "0")
    _stub_shadow_neutral(monkeypatch)
    state_dir = write_state(tmp_path, STATE_BASIC_SIN_POSICIONES(), EQUITY_ROWS_BASIC)
    client = _wp7_client(cash=1000.0, positions=[("QQQ", 7, 200.0)], symbol_to_instrument={"QQQ": 7})

    rc = place_order.main(
        ["open", "--symbol", "QQQ", "--amount", "20", "--stop-loss-pct", "0.10"],
        state_dir=state_dir,
        make_client=lambda: client,
    )

    assert rc == 2
    journal = (state_dir / "journal.md").read_text()
    assert "no-duplicaci" in journal.lower()
    client.open_position_by_amount.assert_not_called()


@pytest.mark.wp7_real
def test_wp7_get_pnl_falla_exit1_sin_operar(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    _stub_shadow_neutral(monkeypatch)
    state_dir = write_state(tmp_path, STATE_BASIC, EQUITY_ROWS_BASIC)
    client = MagicMock()
    client.get_pnl.side_effect = RuntimeError("API caída")

    rc = place_order.main(
        ["open", "--symbol", "SPY", "--amount", "20", "--stop-loss-pct", "0.10"],
        state_dir=state_dir,
        make_client=lambda: client,
    )

    assert rc == 1
    client.open_position_by_amount.assert_not_called()
    journal = (state_dir / "journal.md").read_text()
    assert "ERROR" in journal
    assert "reconstruir" in journal.lower()


@pytest.mark.wp7_real
def test_wp7_instrumentid_no_resoluble_exit1_sin_operar(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    _stub_shadow_neutral(monkeypatch)
    state_dir = write_state(tmp_path, STATE_BASIC, EQUITY_ROWS_BASIC)
    client = MagicMock()
    client.search_instrument.return_value = {"items": []}  # nada resuelve nunca
    client.get_pnl.return_value = {
        "clientPortfolio": {
            "credit": 1000.0,
            "positions": [
                {"positionID": "p1", "instrumentID": 999999, "amount": 10.0, "unrealizedPnL": 0}
            ],
            "ordersForOpen": [],
            "orders": [],
            "mirrors": [],
        }
    }

    rc = place_order.main(
        ["open", "--symbol", "SPY", "--amount", "20", "--stop-loss-pct", "0.10"],
        state_dir=state_dir,
        make_client=lambda: client,
    )

    assert rc == 1
    client.open_position_by_amount.assert_not_called()
    journal = (state_dir / "journal.md").read_text()
    assert "ERROR" in journal


@pytest.mark.wp7_real
def test_wp7_merge_local_open_reciente_bloquea_recompra_por_no_dup(tmp_path, monkeypatch):
    # get_pnl() cachea 60s del lado de eToro: puede no reflejar todavía
    # una apertura de ESTA misma corrida. La entrada local-open:* reciente
    # debe mergearse encima del estado reconstruido y seguir bloqueando
    # la recompra por no-duplicación, aunque la API "en vivo" diga que el
    # portfolio está vacío.
    monkeypatch.setenv("DRY_RUN", "0")
    _stub_shadow_neutral(monkeypatch)
    state = {
        "updatedAt": _fresh_updated_at(),
        "portfolioId": "pf-1",
        "cashUsd": 500.0,
        "positions": [
            {
                "positionId": "local-open:abc",
                "symbol": "QQQ",
                "instrumentId": 7,
                "valueUsd": 200.0,
                "pending": True,
                "localOpenAt": _fresh_updated_at(),
            }
        ],
    }
    state_dir = write_state(tmp_path, state, EQUITY_ROWS_BASIC)
    client = _wp7_client(cash=1000.0, positions=[], symbol_to_instrument={"QQQ": 7})

    rc = place_order.main(
        ["open", "--symbol", "QQQ", "--amount", "50", "--stop-loss-pct", "0.10"],
        state_dir=state_dir,
        make_client=lambda: client,
    )

    assert rc == 2
    journal = (state_dir / "journal.md").read_text()
    assert "no-duplicaci" in journal.lower()
    client.open_position_by_amount.assert_not_called()


@pytest.mark.wp7_real
def test_wp7_merge_local_open_viejo_fuera_de_ventana_no_se_mergea(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    _stub_shadow_neutral(monkeypatch)
    old_ts = (
        datetime.now(timezone.utc)
        - timedelta(minutes=place_order.LOCAL_OPEN_MERGE_WINDOW_MINUTES + 5)
    ).isoformat()
    state = {
        "updatedAt": _fresh_updated_at(),
        "portfolioId": "pf-1",
        "cashUsd": 500.0,
        "positions": [
            {
                "positionId": "local-open:old",
                "symbol": "QQQ",
                "instrumentId": 7,
                "valueUsd": 200.0,
                "pending": True,
                "localOpenAt": old_ts,
            }
        ],
    }
    state_dir = write_state(tmp_path, state, EQUITY_ROWS_BASIC)
    client = _wp7_client(cash=1000.0, positions=[], symbol_to_instrument={"QQQ": 7})

    rc = place_order.main(
        ["open", "--symbol", "QQQ", "--amount", "50", "--stop-loss-pct", "0.10"],
        state_dir=state_dir,
        make_client=lambda: client,
    )

    assert rc == 0
    client.open_position_by_amount.assert_called_once()


# -- Pieza 2: sombra de integridad fuera del repo ----------------------------


@pytest.mark.wp7_real
def test_wp7_sombra_ausente_bloquea_open_sin_llamar_cliente(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    state_dir = write_state(tmp_path, STATE_BASIC, EQUITY_ROWS_BASIC)
    shadow_dir = tmp_path / "no-existe-sombra"

    rc = place_order.main(
        ["open", "--symbol", "SPY", "--amount", "20", "--stop-loss-pct", "0.10"],
        state_dir=state_dir,
        shadow_dir=shadow_dir,
        make_client=_client_factory_no_debe_invocarse,
    )

    assert rc == 2
    journal = (state_dir / "journal.md").read_text()
    assert "sombra" in journal.lower()


@pytest.mark.wp7_real
def test_wp7_sombra_corrupta_bloquea_open(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    state_dir = write_state(tmp_path, STATE_BASIC, EQUITY_ROWS_BASIC)
    shadow_dir = tmp_path / "sombra"
    shadow_dir.mkdir()
    (shadow_dir / place_order.EQUITY_SHADOW_FILE).write_text("date,total\n2026-08-01,1000.0\n")
    (shadow_dir / place_order.RUN_ORDERS_SHADOW_FILE).write_text("{esto no es json")

    rc = place_order.main(
        ["open", "--symbol", "SPY", "--amount", "20", "--stop-loss-pct", "0.10"],
        state_dir=state_dir,
        shadow_dir=shadow_dir,
        make_client=_client_factory_no_debe_invocarse,
    )

    assert rc == 2
    journal = (state_dir / "journal.md").read_text()
    assert "sombra" in journal.lower()


@pytest.mark.wp7_real
def test_wp7_close_inmune_a_sombra_ausente(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    state_dir = write_state(tmp_path, STATE_BASIC, EQUITY_ROWS_BASIC)
    shadow_dir = tmp_path / "no-existe-sombra"
    client = MagicMock()
    client.close_position.return_value = {"ok": True}

    rc = place_order.main(
        ["close", "--position-id", "pos-9", "--symbol", "QQQ"],
        state_dir=state_dir,
        shadow_dir=shadow_dir,
        make_client=lambda: client,
    )

    assert rc == 0
    client.close_position.assert_called_once()


@pytest.mark.wp7_real
def test_wp7_sombra_equity_peak_detecta_drawdown_pese_a_archivo_truncado(tmp_path, monkeypatch):
    # Archivo de equity truncado/sustituido: una sola fila reciente y
    # baja, sin historia -- ningún drawdown visible si solo se mirara el
    # archivo. La sombra retiene el pico real (1000): (1000-700)/1000 =
    # 30% >= 25% -> modo defensivo, bloquea la apertura.
    monkeypatch.setenv("DRY_RUN", "0")
    truncated_equity = [("2026-08-13", 700.0)]
    state_dir = write_state(tmp_path, STATE_BASIC, truncated_equity)
    shadow_dir = tmp_path / "sombra"
    _write_shadow(
        shadow_dir,
        equity_rows=[("2026-08-01", 1000.0), ("2026-08-05", 950.0)],
        budget={"runId": None, "count": 0, "date": None, "dailyCount": 0},
    )
    client = _wp7_client(cash=1000.0, positions=[], symbol_to_instrument={"SPY": 1})

    rc = place_order.main(
        ["open", "--symbol", "SPY", "--amount", "20", "--stop-loss-pct", "0.10"],
        state_dir=state_dir,
        shadow_dir=shadow_dir,
        make_client=lambda: client,
    )

    assert rc == 2
    journal = (state_dir / "journal.md").read_text()
    assert "defensivo" in journal.lower()
    client.open_position_by_amount.assert_not_called()


@pytest.mark.wp7_real
def test_wp7_sombra_presupuesto_detecta_reset_del_archivo(tmp_path, monkeypatch):
    # state/.run_orders.json fue "reseteado" (borrado/sustituido, nunca
    # escrito en este tmp_path): el archivo por sí solo reportaría
    # presupuesto fresco (0/0). La sombra retiene el conteo real (ya
    # agotado, MISMO runId/fecha) -- el presupuesto EFECTIVO sigue
    # agotado.
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("ETOROAGENT_RUN_ID", "run-x")
    state_dir = write_state(tmp_path, STATE_BASIC, EQUITY_ROWS_BASIC)
    shadow_dir = tmp_path / "sombra"
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    _write_shadow(
        shadow_dir,
        equity_rows=[("2026-08-01", 1000.0)],
        budget={
            "runId": "run-x",
            "count": place_order.MAX_ORDERS_PER_RUN,
            "date": today,
            "dailyCount": place_order.MAX_ORDERS_PER_RUN,
        },
    )

    rc = place_order.main(
        ["open", "--symbol", "SPY", "--amount", "20", "--stop-loss-pct", "0.10"],
        state_dir=state_dir,
        shadow_dir=shadow_dir,
        make_client=_client_factory_no_debe_invocarse,
    )

    assert rc == 2
    journal = (state_dir / "journal.md").read_text()
    assert "tope" in journal.lower() or "presupuesto" in journal.lower()


@pytest.mark.wp7_real
def test_wp7_sombra_no_eleva_presupuesto_de_contexto_distinto(tmp_path, monkeypatch):
    # Control negativo: si la sombra pertenece a un runId/fecha DISTINTO
    # del actual, su conteo no debe pisar el presupuesto fresco de la
    # corrida/día actual -- un contexto nuevo legítimamente arranca en 0.
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("ETOROAGENT_RUN_ID", "run-nuevo")
    state_dir = write_state(tmp_path, STATE_BASIC_SIN_POSICIONES(), EQUITY_ROWS_BASIC)
    shadow_dir = tmp_path / "sombra"
    _write_shadow(
        shadow_dir,
        equity_rows=[("2026-08-01", 1000.0)],
        budget={
            "runId": "run-viejo",
            "count": place_order.MAX_ORDERS_PER_RUN,
            "date": "2020-01-01",
            "dailyCount": place_order.MAX_ORDERS_PER_RUN,
        },
    )
    client = _wp7_client(cash=1000.0, positions=[], symbol_to_instrument={"SPY": 1})

    rc = place_order.main(
        ["open", "--symbol", "SPY", "--amount", "20", "--stop-loss-pct", "0.10"],
        state_dir=state_dir,
        shadow_dir=shadow_dir,
        make_client=lambda: client,
    )

    assert rc == 0
    client.open_position_by_amount.assert_called_once()


# -- Pieza 3: autorización de corridas reales --------------------------------


def test_wp7_authorized_run_ausente_bloquea_open_sin_llamar_cliente(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.delenv("ETOROAGENT_AUTHORIZED_RUN", raising=False)
    state_dir = write_state(tmp_path, STATE_BASIC, EQUITY_ROWS_BASIC)

    rc = place_order.main(
        ["open", "--symbol", "SPY", "--amount", "20", "--stop-loss-pct", "0.10"],
        state_dir=state_dir,
        make_client=_client_factory_no_debe_invocarse,
    )

    assert rc == 2
    journal = (state_dir / "journal.md").read_text()
    assert "autoriza" in journal.lower()


def test_wp7_authorized_run_distinto_de_1_bloquea_open(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("ETOROAGENT_AUTHORIZED_RUN", "true")  # cualquier cosa != "1"
    state_dir = write_state(tmp_path, STATE_BASIC, EQUITY_ROWS_BASIC)

    rc = place_order.main(
        ["open", "--symbol", "SPY", "--amount", "20", "--stop-loss-pct", "0.10"],
        state_dir=state_dir,
        make_client=_client_factory_no_debe_invocarse,
    )

    assert rc == 2


def test_wp7_authorized_run_ausente_close_pasa(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.delenv("ETOROAGENT_AUTHORIZED_RUN", raising=False)
    state_dir = write_state(tmp_path, STATE_BASIC, EQUITY_ROWS_BASIC)
    client = MagicMock()
    client.close_position.return_value = {"ok": True}

    rc = place_order.main(
        ["close", "--position-id", "pos-9", "--symbol", "QQQ"],
        state_dir=state_dir,
        make_client=lambda: client,
    )

    assert rc == 0
    client.close_position.assert_called_once()


def test_wp7_authorized_run_presente_habilita_open(tmp_path, monkeypatch):
    # Control positivo explícito (más allá de que el autouse fixture ya
    # lo setee) -- confirma que "1" efectivamente abre la puerta, no solo
    # que algo-no-vacío lo hace.
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("ETOROAGENT_AUTHORIZED_RUN", "1")
    state_dir = write_state(tmp_path, STATE_BASIC, EQUITY_ROWS_BASIC)
    client = _mock_client_for("SPY", 1)

    rc = place_order.main(
        ["open", "--symbol", "SPY", "--amount", "20", "--stop-loss-pct", "0.10"],
        state_dir=state_dir,
        make_client=lambda: client,
    )

    assert rc == 0
    client.open_position_by_amount.assert_called_once()


def STATE_BASIC_SIN_POSICIONES():
    return {
        "updatedAt": _fresh_updated_at(),
        "portfolioId": "pf-1",
        "cashUsd": 1000.0,
        "positions": [],
    }

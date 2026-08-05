import csv
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import snapshot
from etoro_api import EtoroAuthError
from risk import OrderRequest, validate, portfolio_value


def _http_error(status_code):
    """Simula el requests.exceptions.HTTPError que lanza EtoroClient.request()
    (vía resp.raise_for_status()) para un status_code dado."""
    resp = MagicMock()
    resp.status_code = status_code
    return requests.exceptions.HTTPError(response=resp)


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
                {"amount": 100.0, "mirrorID": 0, "instrumentID": 9},
                {"amount": 999.0, "mirrorID": 7},  # debe ignorarse (mirrorID != 0)
            ],
        }
    }
    state = snapshot.build_state("pf-1", pnl, {9: "SPY"})
    # 1000 - 100 (ordersForOpen mirrorID=0) - (50+25) (orders) = 825
    assert state["cashUsd"] == 825.0
    # La orden pendiente (mirrorID=0) entra a positions[] como sintética;
    # la ignorada (mirrorID=7) no genera ninguna entrada.
    assert len(state["positions"]) == 1
    assert state["positions"][0]["symbol"] == "SPY"
    assert state["positions"][0]["valueUsd"] == 100.0
    assert state["positions"][0]["pending"] is True


def test_build_state_valueusd_negativo_no_se_clampa_y_reduce_total_de_sizing():
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
    assert state["positions"][0]["valueUsd"] == -490.0
    # Integración con risk.py: el total de sizing refleja el pnl negativo real,
    # no un valor clampado que sobreestime el portfolio.
    assert portfolio_value(state) == 100.0 + (-490.0)


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


# -- build_state: aperturas pendientes cuentan como exposición (2da ronda, fix #1) --


def test_build_state_ordenes_pendientes_generan_posicion_sintetica():
    pnl = {
        "clientPortfolio": {
            "credit": 10000.0,
            "positions": [],
            "orders": [],
            "ordersForOpen": [
                {"amount": 2400.0, "mirrorID": 0, "instrumentID": 3},
            ],
        }
    }
    state = snapshot.build_state("pf-1", pnl, {3: "BTC"})
    assert state["cashUsd"] == 7600.0  # 10000 - 2400
    assert len(state["positions"]) == 1
    pending = state["positions"][0]
    assert pending["symbol"] == "BTC"
    assert pending["instrumentId"] == 3
    assert pending["valueUsd"] == 2400.0
    assert pending["pending"] is True
    assert str(pending["positionId"]).startswith("pending-open:")


def test_build_state_orden_pendiente_ignorada_por_mirrorid_no_genera_posicion():
    pnl = {
        "clientPortfolio": {
            "credit": 1000.0,
            "positions": [],
            "orders": [],
            "ordersForOpen": [
                {"amount": 500.0, "mirrorID": 7, "instrumentID": 3},  # copiada: se ignora
            ],
        }
    }
    state = snapshot.build_state("pf-1", pnl, {3: "BTC"})
    assert state["positions"] == []
    assert state["cashUsd"] == 1000.0


def test_build_state_cierre_pendiente_no_genera_posicion_sintetica_duplicada():
    # orders[] (cierres pendientes de posiciones YA abiertas) no debe agregar
    # una entrada nueva a positions[] — la posición subyacente ya está ahí.
    pnl = {
        "clientPortfolio": {
            "credit": 500.0,
            "positions": [
                {"positionID": 1, "instrumentID": 1, "amount": 100.0, "unrealizedPnL": 0.0},
            ],
            "orders": [
                {"amount": 100.0, "instrumentID": 1},
            ],
            "ordersForOpen": [],
        }
    }
    state = snapshot.build_state("pf-1", pnl, {1: "SPY"})
    assert len(state["positions"]) == 1


def test_ordenes_pendientes_visibles_para_risk_bloquean_exposicion_excesiva():
    # Escenario reportado en el review: equity 10000, orden BTC pendiente de
    # apertura por 2400. Antes de este fix, risk.py no veía esa exposición y
    # permitía abrir 1800 USD más de BTC (exposición real resultante 42%,
    # muy por encima del tope del 25% por posición).
    pnl = {
        "clientPortfolio": {
            "credit": 10000.0,
            "positions": [],
            "orders": [],
            "ordersForOpen": [
                {"amount": 2400.0, "mirrorID": 0, "instrumentID": 3},
            ],
        }
    }
    state = snapshot.build_state("pf-1", pnl, {3: "BTC"})
    order = OrderRequest(action="open", symbol="BTC", amount_usd=1800.0, stop_loss_pct=0.05)
    equity_rows = [("2026-08-01", 9500.0), ("2026-08-02", 10000.0)]
    ok, msg = validate(order, state, equity_rows)
    assert not ok
    assert "25%" in msg


# -- build_state: universo cerrado de símbolos (2da ronda, fix #2) ----------


def test_build_state_simbolo_fuera_del_universo_lanza_valueerror():
    pnl = {
        "clientPortfolio": {
            "credit": 100.0,
            "positions": [
                {"positionID": 1, "instrumentID": 1, "amount": 10.0, "unrealizedPnL": 0.0},
            ],
            "orders": [],
            "ordersForOpen": [],
        }
    }
    with pytest.raises(ValueError, match="universo"):
        snapshot.build_state("pf-1", pnl, {1: "AAPL"})  # AAPL no está en risk.UNIVERSE


def test_build_state_orden_pendiente_simbolo_fuera_del_universo_lanza_valueerror():
    pnl = {
        "clientPortfolio": {
            "credit": 100.0,
            "positions": [],
            "orders": [],
            "ordersForOpen": [{"amount": 50.0, "mirrorID": 0, "instrumentID": 2}],
        }
    }
    with pytest.raises(ValueError, match="universo"):
        snapshot.build_state("pf-1", pnl, {2: "TSLA"})


# -- build_state: normalización de instrumentID en positions/ordersForOpen (fix #3) --


def test_build_state_instrumentid_string_numerico_en_posicion_se_normaliza():
    pnl = {
        "clientPortfolio": {
            "credit": 500.0,
            "positions": [
                {"positionID": 1, "instrumentID": "1", "amount": 10.0, "unrealizedPnL": 0.0},
            ],
            "orders": [],
            "ordersForOpen": [],
        }
    }
    state = snapshot.build_state("pf-1", pnl, {1: "SPY"})  # symbol_by_id con int 1
    assert state["positions"][0]["symbol"] == "SPY"
    assert state["positions"][0]["instrumentId"] == 1


def test_build_state_instrumentid_string_numerico_en_orden_pendiente_se_normaliza():
    pnl = {
        "clientPortfolio": {
            "credit": 500.0,
            "positions": [],
            "orders": [],
            "ordersForOpen": [{"amount": 50.0, "mirrorID": 0, "instrumentID": "3"}],
        }
    }
    state = snapshot.build_state("pf-1", pnl, {3: "BTC"})
    assert state["positions"][0]["symbol"] == "BTC"
    assert state["positions"][0]["instrumentId"] == 3


def test_build_state_instrumentid_no_numerico_en_posicion_lanza_valueerror():
    pnl = {
        "clientPortfolio": {
            "credit": 500.0,
            "positions": [
                {"positionID": 1, "instrumentID": "no-es-un-id", "amount": 10.0, "unrealizedPnL": 0.0},
            ],
            "orders": [],
            "ordersForOpen": [],
        }
    }
    with pytest.raises(ValueError):
        snapshot.build_state("pf-1", pnl, {1: "SPY"})


# -- build_state: amount requerido, sin default silencioso (fix #4) ---------


def test_build_state_amount_ausente_en_posicion_lanza_valueerror():
    pnl = {
        "clientPortfolio": {
            "credit": 100.0,
            "positions": [
                {"positionID": 1, "instrumentID": 1, "unrealizedPnL": 0.0},  # sin amount
            ],
            "orders": [],
            "ordersForOpen": [],
        }
    }
    with pytest.raises(ValueError):
        snapshot.build_state("pf-1", pnl, {1: "SPY"})


def test_build_state_amount_ausente_en_ordersforopen_lanza_valueerror():
    pnl = {
        "clientPortfolio": {
            "credit": 100.0,
            "positions": [],
            "orders": [],
            "ordersForOpen": [{"mirrorID": 0, "instrumentID": 1}],  # sin amount
        }
    }
    with pytest.raises(ValueError):
        snapshot.build_state("pf-1", pnl, {1: "SPY"})


def test_build_state_amount_ausente_en_orders_lanza_valueerror():
    pnl = {
        "clientPortfolio": {
            "credit": 100.0,
            "positions": [],
            "orders": [{}],  # sin amount
            "ordersForOpen": [],
        }
    }
    with pytest.raises(ValueError):
        snapshot.build_state("pf-1", pnl, {})


# -- build_state: fail-closed sin defaults silenciosos (fix #2) -------------


def test_build_state_sin_clientportfolio_lanza_valueerror():
    with pytest.raises(ValueError):
        snapshot.build_state("pf-1", {}, {})


def test_build_state_clientportfolio_sin_credit_lanza_valueerror():
    pnl = {"clientPortfolio": {"positions": []}}
    with pytest.raises(ValueError):
        snapshot.build_state("pf-1", pnl, {})


def test_build_state_credit_no_numerico_lanza_valueerror():
    pnl = {"clientPortfolio": {"credit": "un montón", "positions": []}}
    with pytest.raises(ValueError):
        snapshot.build_state("pf-1", pnl, {})


def test_build_state_credit_bool_lanza_valueerror():
    pnl = {"clientPortfolio": {"credit": True, "positions": []}}
    with pytest.raises(ValueError):
        snapshot.build_state("pf-1", pnl, {})


def test_build_state_credit_none_lanza_valueerror():
    pnl = {"clientPortfolio": {"credit": None, "positions": []}}
    with pytest.raises(ValueError):
        snapshot.build_state("pf-1", pnl, {})


# -- build_state: positionId ausente (fix #7) --------------------------------


def test_build_state_positionid_ausente_lanza_valueerror():
    pnl = {
        "clientPortfolio": {
            "credit": 100.0,
            "positions": [
                {"instrumentID": 1, "amount": 10.0, "unrealizedPnL": 0.0},  # sin positionID/Id
            ],
            "orders": [],
            "ordersForOpen": [],
        }
    }
    with pytest.raises(ValueError):
        snapshot.build_state("pf-1", pnl, {1: "SPY"})


def test_build_state_positionid_null_lanza_valueerror():
    pnl = {
        "clientPortfolio": {
            "credit": 100.0,
            "positions": [
                {"positionID": None, "instrumentID": 1, "amount": 10.0, "unrealizedPnL": 0.0},
            ],
            "orders": [],
            "ordersForOpen": [],
        }
    }
    with pytest.raises(ValueError):
        snapshot.build_state("pf-1", pnl, {1: "SPY"})


# -- build_state: mirrors no soportado (fix #8) ------------------------------


def test_build_state_con_mirrors_poblado_lanza_valueerror():
    pnl = {
        "clientPortfolio": {
            "credit": 100.0,
            "positions": [],
            "orders": [],
            "ordersForOpen": [],
            "mirrors": [{"mirrorID": 42}],
        }
    }
    with pytest.raises(ValueError, match="copy trading"):
        snapshot.build_state("pf-1", pnl, {})


def test_build_state_mirrors_vacio_o_ausente_no_falla():
    pnl_sin_mirrors_key = {
        "clientPortfolio": {"credit": 100.0, "positions": [], "orders": [], "ordersForOpen": []}
    }
    pnl_mirrors_vacio = {
        "clientPortfolio": {
            "credit": 100.0, "positions": [], "orders": [], "ordersForOpen": [], "mirrors": [],
        }
    }
    snapshot.build_state("pf-1", pnl_sin_mirrors_key, {})
    snapshot.build_state("pf-1", pnl_mirrors_vacio, {})


# -- compute_equity: fórmula oficial (fix #1) --------------------------------


def test_compute_equity_formula_oficial_con_ordenes_pendientes():
    # Portfolio de 10000 con órdenes pendientes de ambos tipos: la equity real
    # sigue siendo 10000, no debe caer solo porque hay órdenes en vuelo.
    pnl = {
        "clientPortfolio": {
            "credit": 10000.0,
            "positions": [],
            "orders": [{"amount": 1500.0}],
            "ordersForOpen": [{"amount": 2500.0, "mirrorID": 0}],
        }
    }
    assert snapshot.compute_equity(pnl) == 10000.0


def test_compute_equity_incluye_posiciones_abiertas_y_pnl_sin_clamp():
    pnl = {
        "clientPortfolio": {
            "credit": 700.0,
            "positions": [
                {"positionID": 1, "instrumentID": 1, "amount": 100.0, "unrealizedPnL": -30.0},
            ],
            "orders": [],
            "ordersForOpen": [],
        }
    }
    # availableCash=700, totalInvested=100, unrealizedPnL=-30 -> 700+100-30=770
    assert snapshot.compute_equity(pnl) == 770.0


def test_compute_equity_sin_clientportfolio_lanza_valueerror():
    with pytest.raises(ValueError):
        snapshot.compute_equity({})


def test_compute_equity_sin_credit_lanza_valueerror():
    with pytest.raises(ValueError):
        snapshot.compute_equity({"clientPortfolio": {}})


def test_compute_equity_con_mirrors_poblado_lanza_valueerror():
    pnl = {"clientPortfolio": {"credit": 100.0, "mirrors": [{"mirrorID": 1}]}}
    with pytest.raises(ValueError, match="copy trading"):
        snapshot.compute_equity(pnl)


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


def test_update_equity_ordena_filas_desordenadas_ascendente():
    rows = [("2026-08-03", 300.0), ("2026-08-01", 100.0)]
    result = snapshot.update_equity(rows, "2026-08-02", 200.0)
    assert result == [
        ("2026-08-01", 100.0),
        ("2026-08-02", 200.0),
        ("2026-08-03", 300.0),
    ]


# -- _resolve_symbol_by_id (fix #3) ------------------------------------------


def _fake_client_with_metadata(metadata):
    client = MagicMock()
    client.get_instruments_metadata.return_value = metadata
    return client


def test_resolve_symbol_by_id_con_dict_items():
    client = _fake_client_with_metadata(
        {"items": [{"instrumentId": 1, "internalSymbolFull": "SPY"}]}
    )
    assert snapshot._resolve_symbol_by_id(client, [1]) == {1: "SPY"}


def test_resolve_symbol_by_id_con_lista_desnuda_no_rompe():
    # Antes de este fix, metadata.get(...) sobre una lista lanzaba AttributeError
    # antes de llegar al isinstance(metadata, list) — rama muerta.
    client = _fake_client_with_metadata(
        [{"instrumentId": 1, "internalSymbolFull": "SPY"}]
    )
    assert snapshot._resolve_symbol_by_id(client, [1]) == {1: "SPY"}


def test_resolve_symbol_by_id_instrumentid_string_numerico_tolerado():
    client = _fake_client_with_metadata(
        {"items": [{"instrumentId": "1", "internalSymbolFull": "SPY"}]}
    )
    assert snapshot._resolve_symbol_by_id(client, [1]) == {1: "SPY"}


def test_resolve_symbol_by_id_instrumentid_no_numerico_lanza_valueerror():
    client = _fake_client_with_metadata(
        {"items": [{"instrumentId": "no-es-un-id", "internalSymbolFull": "SPY"}]}
    )
    with pytest.raises(ValueError):
        snapshot._resolve_symbol_by_id(client, [1])


def test_resolve_symbol_by_id_duplicado_mismo_simbolo_se_queda_con_primero():
    client = _fake_client_with_metadata(
        {
            "items": [
                {"instrumentId": 1, "internalSymbolFull": "SPY"},
                {"instrumentId": 1, "internalSymbolFull": "SPY"},
            ]
        }
    )
    assert snapshot._resolve_symbol_by_id(client, [1]) == {1: "SPY"}


def test_resolve_symbol_by_id_duplicado_simbolo_distinto_lanza_valueerror():
    client = _fake_client_with_metadata(
        {
            "items": [
                {"instrumentId": 1, "internalSymbolFull": "SPY"},
                {"instrumentId": 1, "internalSymbolFull": "QQQ"},
            ]
        }
    )
    with pytest.raises(ValueError):
        snapshot._resolve_symbol_by_id(client, [1])


# -- _read_equity_rows: header inesperado (minor) ----------------------------


def test_read_equity_rows_header_inesperado_lanza_valueerror(tmp_path):
    path = tmp_path / "equity.csv"
    path.write_text("fecha,valor\n2026-08-01,100.0\n")
    with pytest.raises(ValueError):
        snapshot._read_equity_rows(path)


def test_read_equity_rows_header_correcto_ok(tmp_path):
    path = tmp_path / "equity.csv"
    path.write_text("date,total\n2026-08-01,100.0\n")
    assert snapshot._read_equity_rows(path) == [("2026-08-01", 100.0)]


def test_read_equity_rows_archivo_inexistente_devuelve_vacio(tmp_path):
    path = tmp_path / "no-existe.csv"
    assert snapshot._read_equity_rows(path) == []


# -- Integración cripto: variante BTCUSD cuenta contra el tope (fix #4) ------


def test_integracion_build_state_btcusd_bloquea_cripto_combinado():
    pnl = {
        "clientPortfolio": {
            "credit": 150.0,
            "positions": [
                {"positionID": 1, "instrumentID": 1, "amount": 50.0, "unrealizedPnL": 0.0},
            ],
            "orders": [],
            "ordersForOpen": [],
        }
    }
    state = snapshot.build_state("pf-1", pnl, {1: "BTCUSD"})
    # cashUsd=150, posición BTCUSD valueUsd=50 -> total=200
    order = OrderRequest(action="open", symbol="ETH", amount_usd=25.0, stop_loss_pct=0.05)
    equity_rows = [("2026-08-01", 190.0), ("2026-08-02", 200.0)]
    ok, msg = validate(order, state, equity_rows)
    assert not ok
    assert "cripto" in msg.lower()


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


# -- main(): 403 en listado de portfolios (token scoped al Agent Portfolio) --
#
# Hallazgo real contra la API: GET /api/v1/agent-portfolios exige scope de
# cuenta real:read, que un token scoped a un Agent Portfolio no tiene (403).
# GET /trading/info/real/pnl sí funciona con ese token. portfolioId es solo
# informativo en nuestro state (los endpoints de trading son token-scoped,
# no dependen de portfolioId) — por eso ante 403 podemos seguir con un
# fallback en vez de abortar.


def test_main_403_en_listado_portfolios_usa_fallback_token_scoped(
    tmp_path, monkeypatch, capsys
):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(snapshot, "STATE_DIR", state_dir)
    monkeypatch.delenv("ETORO_PORTFOLIO_ID", raising=False)

    fake_client = MagicMock()
    fake_client.get_agent_portfolios.side_effect = _http_error(403)
    fake_client.get_pnl.return_value = PNL_BASIC
    fake_client.get_instruments_metadata.return_value = {
        "items": [
            {"instrumentId": 1, "internalSymbolFull": "SPY"},
            {"instrumentId": 2, "internalSymbolFull": "BTC"},
        ]
    }
    monkeypatch.setattr(snapshot, "EtoroClient", lambda: fake_client)

    snapshot.main()  # no debe abortar: ni SystemExit ni ninguna excepción

    written = json.loads((state_dir / "positions.json").read_text())
    assert written["portfolioId"] == "token-scoped"

    captured = capsys.readouterr()
    assert "403" in captured.out
    assert "token-scoped" in captured.out


def test_main_403_en_listado_portfolios_usa_etoro_portfolio_id_del_env(
    tmp_path, monkeypatch
):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(snapshot, "STATE_DIR", state_dir)
    monkeypatch.setenv("ETORO_PORTFOLIO_ID", "pf-real-123")

    fake_client = MagicMock()
    fake_client.get_agent_portfolios.side_effect = _http_error(403)
    fake_client.get_pnl.return_value = PNL_BASIC
    fake_client.get_instruments_metadata.return_value = {
        "items": [
            {"instrumentId": 1, "internalSymbolFull": "SPY"},
            {"instrumentId": 2, "internalSymbolFull": "BTC"},
        ]
    }
    monkeypatch.setattr(snapshot, "EtoroClient", lambda: fake_client)

    snapshot.main()

    written = json.loads((state_dir / "positions.json").read_text())
    assert written["portfolioId"] == "pf-real-123"


def test_main_401_en_listado_portfolios_sigue_abortando(tmp_path, monkeypatch, capsys):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(snapshot, "STATE_DIR", state_dir)

    fake_client = MagicMock()
    fake_client.get_agent_portfolios.side_effect = _http_error(401)
    monkeypatch.setattr(snapshot, "EtoroClient", lambda: fake_client)

    with pytest.raises(SystemExit) as exc_info:
        snapshot.main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.err.strip() != ""
    assert not (state_dir / "positions.json").exists()


def test_main_etoroautherror_en_listado_portfolios_sigue_abortando(tmp_path, monkeypatch):
    # El caso real de un 401: EtoroClient.request() lo convierte en
    # EtoroAuthError (terminal) antes de que llegue a ser un HTTPError.
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(snapshot, "STATE_DIR", state_dir)

    fake_client = MagicMock()
    fake_client.get_agent_portfolios.side_effect = EtoroAuthError("credenciales inválidas")
    monkeypatch.setattr(snapshot, "EtoroClient", lambda: fake_client)

    with pytest.raises(SystemExit) as exc_info:
        snapshot.main()

    assert exc_info.value.code == 1
    assert not (state_dir / "positions.json").exists()


def test_main_500_en_listado_portfolios_sigue_abortando(tmp_path, monkeypatch):
    # Cualquier HTTPError que no sea 403 sigue abortando como antes.
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(snapshot, "STATE_DIR", state_dir)

    fake_client = MagicMock()
    fake_client.get_agent_portfolios.side_effect = _http_error(500)
    monkeypatch.setattr(snapshot, "EtoroClient", lambda: fake_client)

    with pytest.raises(SystemExit) as exc_info:
        snapshot.main()

    assert exc_info.value.code == 1
    assert not (state_dir / "positions.json").exists()


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
    # Sin órdenes pendientes en este fixture, equity oficial == cash + sum(valueUsd)
    total = 1000.0 + 105.0 + 190.0
    assert abs(float(rows[1][1]) - total) < 1e-9

    captured = capsys.readouterr()
    assert "OK" in captured.out


def test_main_equity_csv_usa_formula_oficial_con_ordenes_pendientes(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(snapshot, "STATE_DIR", state_dir)
    monkeypatch.setattr(snapshot, "_today_str", lambda: "2026-08-04")

    pnl = {
        "clientPortfolio": {
            "credit": 10000.0,
            "positions": [],
            "orders": [{"amount": 1500.0}],
            "ordersForOpen": [{"amount": 2500.0, "mirrorID": 0, "instrumentID": 7}],
        }
    }
    fake_client = MagicMock()
    fake_client.get_agent_portfolios.return_value = {
        "agentPortfolios": [{"agentPortfolioId": "pf-1"}]
    }
    fake_client.get_pnl.return_value = pnl
    fake_client.get_instruments_metadata.return_value = {
        "items": [{"instrumentId": 7, "internalSymbolFull": "SPY"}]
    }
    monkeypatch.setattr(snapshot, "EtoroClient", lambda: fake_client)

    snapshot.main()

    equity_path = state_dir / "equity.csv"
    with open(equity_path) as f:
        rows = list(csv.reader(f))
    assert rows[1][0] == "2026-08-04"
    assert abs(float(rows[1][1]) - 10000.0) < 1e-9


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

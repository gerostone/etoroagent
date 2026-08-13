"""WP5: suite de invariantes de seguridad.

A diferencia de tests/test_risk.py, tests/test_place_order.py, etc. (que
verifican comportamiento puntual de una función/rama de código), este
archivo verifica PROPIEDADES independientes de la implementación -- deben
seguir siendo verdad sin importar cómo se reorganice el código interno de
risk.py/place_order.py, mientras el CONTRATO de seguridad se mantenga.
Cada invariante documenta, en su docstring, la garantía que protege y por
qué existe (qué se rompería si dejara de cumplirse).

Ejercita el camino REAL (place_order.main() end-to-end con un cliente HTTP
mockeado, no solo risk.validate() aislado) siempre que la propiedad
dependa de la orquestación entre risk.py, el presupuesto de órdenes, el
flag de reconciliación y el registro de exposición local -- para que estos
tests no puedan quedar "verdes" por una refactorización que rompa cómo esas
piezas se combinan en producción.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import place_order  # noqa: E402
from risk import OrderRequest, validate  # noqa: E402


# -- Fixtures / helpers -----------------------------------------------------
#
# Duplicados a propósito respecto de tests/test_place_order.py (mismo
# criterio que snapshot.py documenta para su propio _is_finite_number: son
# unas pocas líneas sin red/estado compartido, no vale la pena acoplar este
# archivo a otro archivo de test).


def _fresh_updated_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_state(tmp_path, state=None, equity_rows=None):
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
    if from_date is None:
        from_date = _fresh_updated_at()
    return {"candles": [{"candles": [{"close": close, "fromDate": from_date}]}]}


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
    client.close_position.return_value = {"status": "closed"}
    return client


class _ExplosiveClient:
    """Cualquier acceso a un atributo/método explota -- prueba en firme de
    que el caller nunca llegó a tocar el cliente HTTP. Usado en los
    invariantes donde la propiedad exigida es exactamente "esto no debe
    poder llegar a la API", no solo "el resultado esperado es tal"."""

    def __getattr__(self, name):
        raise AssertionError(
            f"el cliente HTTP NO debía invocarse -- se accedió a {name!r}"
        )


def _base_state(cash=1000.0, positions=None):
    return {
        "updatedAt": _fresh_updated_at(),
        "portfolioId": "pf-1",
        "cashUsd": cash,
        "positions": positions or [],
    }


def _base_equity(total=1000.0):
    return [("2026-08-01", total)]


# ============================================================================
# Invariante 1: un close NUNCA es bloqueado por presupuesto agotado, flag de
# reconciliación, no-duplicación, tope agregado, o presupuesto corrupto.
# ============================================================================
#
# Garantía: reducir riesgo (cerrar una posición) no debe poder quedar
# rehén de ningún guard pensado para APERTURAS. place_order.main() aplica
# el chequeo de presupuesto y de reconciliación SOLO en la rama
# action=="open" (ver su main()), y _handle_close() no invoca
# risk.validate() en absoluto -- así que no-duplicación y el tope agregado
# (ambos dentro de validate()) tampoco pueden interponerse. Este test
# ejercita los cinco estados adversos A LA VEZ sobre un state que, si
# fuera tratado como una apertura, dispararía tanto no-duplicación (ya hay
# una posición en el símbolo) como el tope agregado (~99% de exposición ya
# existente) -- y confirma que aun así el cierre se ejecuta.


def _presupuesto_agotado_por_corrida():
    return json.dumps(
        {"runId": "cualquier-run", "count": place_order.MAX_ORDERS_PER_RUN, "date": None, "dailyCount": 0}
    )


def _presupuesto_agotado_por_dia():
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    return json.dumps(
        {"runId": None, "count": 0, "date": today, "dailyCount": place_order.MAX_ORDERS_PER_DAY}
    )


def _presupuesto_corrupto():
    return "{esto no es json valido en absoluto"


@pytest.mark.parametrize(
    "budget_contents",
    [
        _presupuesto_agotado_por_corrida(),
        _presupuesto_agotado_por_dia(),
        _presupuesto_corrupto(),
    ],
    ids=["presupuesto_por_corrida_agotado", "presupuesto_diario_agotado", "presupuesto_corrupto"],
)
def test_close_nunca_bloqueado_por_guards_de_apertura(tmp_path, monkeypatch, budget_contents):
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.delenv("ETOROAGENT_RUN_ID", raising=False)

    # Estado que, tratado como una APERTURA sobre el mismo símbolo, sería
    # bloqueado tanto por no-duplicación (posición existente en XLV) como
    # por el tope agregado de 70% (9000/9100 ≈ 98.9% ya expuesto).
    state = _base_state(
        cash=100.0,
        positions=[{"positionId": "pos-1", "symbol": "XLV", "instrumentId": 5, "valueUsd": 9000.0}],
    )
    state_dir = write_state(tmp_path, state, _base_equity(total=9100.0))

    (state_dir / place_order.NEEDS_RECONCILIATION_FILE).write_text(
        json.dumps({"reason": "corrida abortada (test)", "log": "reports/x.log", "at": "2026-08-01 00:00 -0300"})
    )
    (state_dir / place_order.ORDER_BUDGET_FILE).write_text(budget_contents)

    client = _mock_client_for("XLV", 5)
    rc = place_order.main(
        ["close", "--position-id", "pos-1", "--symbol", "XLV", "--reason", "riesgo"],
        state_dir=state_dir,
        make_client=lambda: client,
    )

    assert rc == 0
    client.close_position.assert_called_once()
    journal = (state_dir / "journal.md").read_text()
    assert "CERRADA" in journal
    assert "BLOQUEADA" not in journal


# ============================================================================
# Invariante 2: ningún open sin stop-loss VÁLIDO llega al cliente HTTP.
# ============================================================================
#
# Garantía: risk.validate() exige 0 < stop_loss_pct <= 12% (finito). Si un
# valor inválido lograra colarse hasta _resolve_instrument_id/
# _resolve_current_price/open_position_by_amount, una orden real quedaría
# sin protección de stop-loss (o con un valor sin sentido). El mock
# explosivo prueba, no solo infiere, que el cliente nunca se toca.


@pytest.mark.parametrize(
    "stop_loss_arg",
    ["0", "-0.01", "0.13", "nan", "inf", "-inf"],
    ids=["cero", "negativo", "mayor_12pct", "nan", "inf", "menos_inf"],
)
def test_open_stop_loss_invalido_nunca_llega_al_cliente(tmp_path, monkeypatch, stop_loss_arg):
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.delenv("ETOROAGENT_RUN_ID", raising=False)
    state_dir = write_state(tmp_path, _base_state(), _base_equity())

    # "--stop-loss-pct=<valor>" (un solo token) en vez de dos argv
    # separados: con valores negativos ("-0.01", "-inf"), argparse
    # interpretaría el segundo token como un flag nuevo si no van pegados
    # con "=".
    rc = place_order.main(
        ["open", "--symbol", "SPY", "--amount", "10", f"--stop-loss-pct={stop_loss_arg}"],
        state_dir=state_dir,
        make_client=lambda: _ExplosiveClient(),
    )

    assert rc == 2
    journal = (state_dir / "journal.md").read_text()
    assert "BLOQUEADA" in journal
    assert "stop" in journal.lower()


def test_validate_stop_loss_ausente_bloquea_directamente():
    # None no es expresable como argumento de argparse (type=float, CLI
    # siempre manda algún valor) -- se cubre directo sobre risk.validate().
    ok, msg = validate(
        OrderRequest("open", "SPY", 10.0, None), _base_state(cash=1000.0), _base_equity()
    )
    assert not ok and "stop" in msg.lower()


# ============================================================================
# Invariante 3: ningún símbolo fuera de UNIVERSE llega al cliente HTTP.
# ============================================================================
#
# Garantía: el universo operable cerrado (risk.UNIVERSE) es la única
# fuente de verdad de qué se puede operar. "BTCS" (N7: activo real y
# distinto que casi colapsaba por prefijo a "BTC"), "TSLA" (equity fuera
# del universo), "BTCUSDT" (formato Binance-style, no está en la
# whitelist de alias cripto conocidos) y símbolo vacío/solo-espacios deben
# bloquearse ANTES de resolver instrumentId.


@pytest.mark.parametrize(
    "symbol",
    ["BTCS", "TSLA", "BTCUSDT", "", "   "],
    ids=["btcs_prefijo_falso_positivo", "tsla_equity_fuera_universo", "btcusdt_formato_no_whitelisteado", "vacio", "solo_espacios"],
)
def test_open_fuera_de_universo_nunca_llega_al_cliente(tmp_path, monkeypatch, symbol):
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.delenv("ETOROAGENT_RUN_ID", raising=False)
    state_dir = write_state(tmp_path, _base_state(), _base_equity())

    rc = place_order.main(
        ["open", "--symbol", symbol, "--amount", "10", "--stop-loss-pct", "0.10"],
        state_dir=state_dir,
        make_client=lambda: _ExplosiveClient(),
    )

    assert rc == 2
    journal = (state_dir / "journal.md").read_text()
    assert "BLOQUEADA" in journal


# ============================================================================
# Invariante 4: guards corruptos/ilegibles -> aperturas bloqueadas, cierres
# pasan.
# ============================================================================
#
# Garantía: state/.run_orders.json (el único de los dos archivos de
# control que place_order.py efectivamente PARSEA) ilegible/corrupto debe
# tratarse fail-closed para aperturas (WP4/N3a: nunca reiniciar contadores
# en 0 silenciosamente) -- pero sin afectar en absoluto a los cierres, que
# ni siquiera lo leen.


def test_open_bloqueada_por_presupuesto_corrupto_o_ilegible(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.delenv("ETOROAGENT_RUN_ID", raising=False)
    state_dir = write_state(tmp_path, _base_state(), _base_equity())
    (state_dir / place_order.ORDER_BUDGET_FILE).write_text("{esto no es json ilegible")

    rc = place_order.main(
        ["open", "--symbol", "SPY", "--amount", "10", "--stop-loss-pct", "0.10"],
        state_dir=state_dir,
        make_client=lambda: _ExplosiveClient(),
    )

    assert rc == 2
    journal = (state_dir / "journal.md").read_text().lower()
    assert "presupuesto" in journal and "ilegible" in journal


def test_close_pasa_pese_a_presupuesto_corrupto_o_ilegible(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.delenv("ETOROAGENT_RUN_ID", raising=False)
    state = _base_state(
        cash=100.0,
        positions=[{"positionId": "pos-1", "symbol": "SPY", "instrumentId": 1, "valueUsd": 50.0}],
    )
    state_dir = write_state(tmp_path, state, _base_equity(total=150.0))
    (state_dir / place_order.ORDER_BUDGET_FILE).write_text("{esto no es json ilegible")

    client = _mock_client_for("SPY", 1)
    rc = place_order.main(
        ["close", "--position-id", "pos-1", "--symbol", "SPY"],
        state_dir=state_dir,
        make_client=lambda: client,
    )

    assert rc == 0
    client.close_position.assert_called_once()


# ============================================================================
# Invariante 5: replay de 3 corridas idénticas -> exposición final EXACTA
# 37%, corridas 2 y 3 bloqueadas.
# ============================================================================
#
# Escenario original del auditor (WP1): sin no-duplicación, 3 corridas
# idénticas construían 59% de exposición real donde el agente creía 37%
# (el único freno, 25% por símbolo, frena tarde). Ejercita el camino REAL
# (place_order.main() con DRY_RUN=0 y un cliente mockeado, registrando
# exposición local tal como lo hace la producción -- no simula el
# resultado escribiendo el state a mano) con un ETOROAGENT_RUN_ID DISTINTO
# por corrida (como haría runner.sh en corridas reales sucesivas), para
# que el freno que realmente actúa en las corridas 2 y 3 sea la
# no-duplicación -- no un presupuesto de órdenes que no llegó a resetear.


def test_replay_tres_corridas_identicas_exposicion_final_37pct(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    state_dir = write_state(tmp_path, _base_state(cash=10000.0, positions=[]), _base_equity(total=10000.0))

    ordenes = [("XLV", 1500.0, 10), ("XLF", 1200.0, 11), ("XLK", 1000.0, 12)]

    # -- Corrida 1: las tres aperturas deben ejecutarse (dentro de todos
    # los topes: 15%/12%/10% por posición, 37% agregado < 70%).
    monkeypatch.setenv("ETOROAGENT_RUN_ID", "run-1")
    for symbol, amount, instrument_id in ordenes:
        client = _mock_client_for(symbol, instrument_id)
        rc = place_order.main(
            ["open", "--symbol", symbol, "--amount", str(amount), "--stop-loss-pct", "0.10"],
            state_dir=state_dir,
            make_client=lambda client=client: client,
        )
        assert rc == 0, f"corrida 1, {symbol} debía abrirse"
        client.open_position_by_amount.assert_called_once()

    on_disk = json.loads((state_dir / "positions.json").read_text())
    assert on_disk["cashUsd"] == 10000.0 - (1500.0 + 1200.0 + 1000.0)
    assert len(on_disk["positions"]) == 3
    exposicion_tras_corrida_1 = sum(p["valueUsd"] for p in on_disk["positions"])
    assert exposicion_tras_corrida_1 == 3700.0
    assert exposicion_tras_corrida_1 / 10000.0 == pytest.approx(0.37)

    # -- Corridas 2 y 3: MISMAS órdenes, run_id NUEVO en cada una (budget
    # fresco) -- deben bloquearse igual, por no-duplicación.
    for run_label in ("run-2", "run-3"):
        monkeypatch.setenv("ETOROAGENT_RUN_ID", run_label)
        for symbol, amount, instrument_id in ordenes:
            client = _ExplosiveClient()
            rc = place_order.main(
                ["open", "--symbol", symbol, "--amount", str(amount), "--stop-loss-pct", "0.10"],
                state_dir=state_dir,
                make_client=lambda client=client: client,
            )
            assert rc == 2, f"{run_label}, {symbol} debía bloquearse"

        journal = (state_dir / "journal.md").read_text()
        assert journal.count("no-duplicaci") >= 3 or "no-duplicaci" in journal.lower()

    # -- Exposición final: EXACTAMENTE 37%, sin cambios respecto de la
    # corrida 1 (ninguna de las corridas 2/3 debe haber tocado el state).
    final = json.loads((state_dir / "positions.json").read_text())
    exposicion_final = sum(p["valueUsd"] for p in final["positions"])
    assert exposicion_final == 3700.0
    assert exposicion_final / 10000.0 == pytest.approx(0.37)
    assert len(final["positions"]) == 3


# ============================================================================
# Invariante 6: cualquier mezcla de alias cripto (BTC+BTCUSD+BTC-USD+
# BTCEUR) no puede superar el 35% de exposición combinada.
# ============================================================================
#
# Garantía: canonical_symbol() (N2, corregido por N7) colapsa estos cuatro
# alias a un único canónico "BTC" para la suma de exposición cripto
# agregada -- sin importar CUÁL de los cuatro (o qué combinación/mayúsculas)
# tenga cada posición individual, la suma total nunca debe poder superar
# MAX_CRYPTO_PCT. Se abre ETH (activo cripto DISTINTO) en vez de más BTC
# para aislar el tope de 35% de la no-duplicación (que bloquearía
# cualquier alias de BTC nuevo apenas hay UNA posición BTC existente, sin
# siquiera llegar a evaluar el tope agregado).

_MEZCLAS_ALIAS_BTC = [
    pytest.param([("BTC", 300.0)], id="solo_btc"),
    pytest.param([("BTCUSD", 300.0)], id="solo_btcusd"),
    pytest.param([("BTC-USD", 150.0), ("BTCEUR", 150.0)], id="btc_usd_mas_btceur"),
    pytest.param([("btc", 100.0), ("btcusd", 100.0), ("btc-usd", 100.0)], id="minuscula_tres_alias"),
    pytest.param(
        [("BTC", 75.0), ("BTCUSD", 75.0), ("BTC-USD", 75.0), ("BTCEUR", 75.0)],
        id="los_cuatro_alias_mezclados",
    ),
]


@pytest.mark.parametrize("posiciones_btc", _MEZCLAS_ALIAS_BTC)
def test_exposicion_cripto_combinada_no_supera_35pct_sin_importar_el_alias(posiciones_btc):
    total_btc_existente = sum(v for _, v in posiciones_btc)
    assert total_btc_existente == 300.0  # fijo por diseño del caso de prueba
    state = _base_state(
        cash=1000.0 - total_btc_existente,
        positions=[{"symbol": alias, "valueUsd": valor} for alias, valor in posiciones_btc],
    )

    # 300 (BTC, cualquier mezcla de alias) + 50 (ETH nuevo) = 350/1000 = 35% exacto -> pasa.
    ok, msg = validate(OrderRequest("open", "ETH", 50.0, 0.10), state, _base_equity())
    assert ok, msg

    # 300 + 51 = 351/1000 = 35.1% > 35% -> bloquea, sin importar la mezcla de alias.
    ok2, msg2 = validate(OrderRequest("open", "ETH", 51.0, 0.10), state, _base_equity())
    assert not ok2 and "cripto" in msg2.lower()


# ============================================================================
# Invariante 7: DRY_RUN ausente, "1", "true", "2" -> cero llamadas al
# cliente; solo "0" (exacto) habilita.
# ============================================================================
#
# Garantía: _is_dry_run() es fail-safe por diseño (cualquier cosa != "0"
# es dry-run) -- ningún valor "parecido a verdadero" (incluido un entero
# arbitrario como "2", que un caller descuidado podría escribir pensando
# en "modo 2") debe habilitar tráfico real hacia la API.


@pytest.mark.parametrize("dry_run_value", [None, "1", "true", "2"], ids=["ausente", "uno", "true_texto", "dos"])
def test_dry_run_no_exacto_a_0_nunca_llama_al_cliente(tmp_path, monkeypatch, dry_run_value):
    if dry_run_value is None:
        monkeypatch.delenv("DRY_RUN", raising=False)
    else:
        monkeypatch.setenv("DRY_RUN", dry_run_value)
    monkeypatch.delenv("ETOROAGENT_RUN_ID", raising=False)
    state_dir = write_state(tmp_path, _base_state(), _base_equity())

    rc = place_order.main(
        ["open", "--symbol", "SPY", "--amount", "10", "--stop-loss-pct", "0.10"],
        state_dir=state_dir,
        make_client=lambda: _ExplosiveClient(),
    )

    assert rc == 0
    journal = (state_dir / "journal.md").read_text()
    assert "DRY_RUN" in journal


def test_dry_run_exactamente_0_habilita_la_llamada_real(tmp_path, monkeypatch):
    # Control positivo: sin este caso, un fix que rompiera _is_dry_run()
    # bloqueando SIEMPRE (incluso con "0") pasaría todos los tests de
    # arriba igual -- hay que probar también que "0" efectivamente abre
    # la puerta.
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.delenv("ETOROAGENT_RUN_ID", raising=False)
    state_dir = write_state(tmp_path, _base_state(), _base_equity())
    client = _mock_client_for("SPY", 1)
    rc = place_order.main(
        ["open", "--symbol", "SPY", "--amount", "10", "--stop-loss-pct", "0.10"],
        state_dir=state_dir,
        make_client=lambda: client,
    )
    assert rc == 0
    client.open_position_by_amount.assert_called_once()

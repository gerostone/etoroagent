"""Configuración compartida de pytest para la suite del proyecto."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import place_order  # noqa: E402


def pytest_configure(config):
    # WP7: marca los tests que ejercitan la reconstrucción REAL de estado
    # desde la API (place_order._reconstruct_state_from_api) y/o la sombra
    # de integridad REAL (place_order._load_shadow), en vez del stub
    # permisivo que `_wp7_modo_real_listo_por_default` (más abajo) aplica
    # por default al resto de la suite.
    config.addinivalue_line(
        "markers",
        "wp7_real: opta fuera del stub por default de _reconstruct_state_from_api/_load_shadow",
    )


# -- WP7: modo real listo por default ----------------------------------------
#
# Desde WP7, TODA apertura en modo real (DRY_RUN=="0") exige además: (a)
# ETOROAGENT_AUTHORIZED_RUN=="1" en el entorno, (b) una sombra de
# integridad legible (place_order._load_shadow), y (c) reconstruye el
# estado que valida risk.validate() desde la API en vivo
# (place_order._reconstruct_state_from_api), no desde el archivo. La
# inmensa mayoría de los tests de la suite (escritos ANTES de WP7, en
# tests/test_place_order.py y tests/test_invariants.py) ejercitan otra
# cosa (topes de riesgo, presupuesto, reconciliación, manejo de errores
# del cliente, DRY_RUN, etc.) contra un `state` file-shaped que ellos
# mismos arman -- no la reconstrucción real ni la sombra real. En vez de
# tocar cada uno de esos tests para satisfacer los tres candados nuevos,
# este fixture autouse (aplica a TODA la suite, no solo a un archivo) los
# deja "listos" por default:
#   - autoriza la corrida siempre (mismo espíritu que asumir un runner.sh
#     real detrás);
#   - stubea _reconstruct_state_from_api a un pass-through del `state`
#     que el propio test ya arma (así el escenario de riesgo que el test
#     describe se sigue validando exactamente igual que antes de WP7,
#     sin necesitar mocks de get_pnl()/search_instrument());
#   - stubea _load_shadow a una sombra vacía/neutra (sin equity_rows, sin
#     budget) que no cambia el comportamiento del presupuesto ni del
#     drawdown respecto de antes de WP7.
# Los tests marcados @pytest.mark.wp7_real (piezas 1 y 2 de WP7, en
# tests/test_place_order.py y tests/test_invariants.py) optan afuera de
# los dos stubs y ejercitan la lógica REAL, con sus propios mocks de
# get_pnl/search_instrument y su propia sombra en disco. Los tests
# dedicados a la pieza 3 (autorización) desautorizan explícitamente con
# su propio monkeypatch DESPUÉS de este fixture (mismo monkeypatch,
# function-scoped: la última asignación en el cuerpo del test gana).
@pytest.fixture(autouse=True)
def _wp7_modo_real_listo_por_default(request, monkeypatch):
    monkeypatch.setenv("ETOROAGENT_AUTHORIZED_RUN", "1")
    if "wp7_real" not in request.keywords:
        monkeypatch.setattr(
            place_order,
            "_reconstruct_state_from_api",
            lambda client, file_state: file_state,
        )
        monkeypatch.setattr(
            place_order,
            "_load_shadow",
            lambda shadow_dir: {"equity_rows": [], "budget": None},
        )

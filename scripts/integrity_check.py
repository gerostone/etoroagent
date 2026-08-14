"""integrity_check.py -- WP8: compara el estado del proyecto (state/)
contra la sombra de integridad fuera del repo (WP7, scripts/shadow_sync.py)
y DETECTA divergencias que la sola PREVENCIÓN (place_order.py, MAX
archivo-vs-sombra en modo real) no puede cerrar del todo.

Uso: .venv/bin/python scripts/integrity_check.py [--state-dir DIR] [--shadow-dir DIR]

Riesgo residual aceptado en prevención (WP7): un compuesto de DOS `cp -r`
coordinados -- uno sobre state/, otro sobre la sombra fuera del repo --
que reescriben ambos lados de forma consistente evade el chequeo MAX de
place_order.py (que solo ve una sombra ya "de acuerdo" con el archivo
falseado). Este script convierte ese residual en DETECCIÓN: se corre
DESPUÉS de cada sincronización real de la sombra (scripts/shadow_sync.py,
invocado por runner.sh) y compara ambos lados de forma independiente --
un atacante necesitaría descubrir Y tocar el directorio sombra (fuera del
repo, no obvio) con un valor EXACTAMENTE consistente con lo que acaba de
falsear en state/, además de ganarle la carrera a la sincronización que
runner.sh ya corrió ANTES de este check. Cualquier divergencia real
(tamper parcial, bug, corrupción, o simplemente sustituir solo un lado)
se detecta acá.

Dos comparaciones, mismo criterio de "mismo contexto" que shadow_sync.py:

  Pico de equity: si el pico de state/equity.csv es MENOR al pico de la
    sombra (más allá de una tolerancia relativa chica, para no disparar
    por ruido de redondeo de punto flotante) -> DIVERGENCIA. Bajo
    operación normal esto NUNCA debería pasar (la sombra solo puede
    quedarse IGUAL o POR DEBAJO del archivo, nunca por encima, porque se
    construye copiando desde él) -- que ocurra es evidencia de que
    state/equity.csv fue sustituido/truncado después de la última
    sincronización exitosa.

  Presupuesto de órdenes: si, para el MISMO contexto (mismo runId y misma
    fecha en ambos lados), los contadores de state/.run_orders.json son
    MENORES a los de la sombra -> DIVERGENCIA (mismo razonamiento: bajo
    operación normal el archivo real nunca debería quedar por debajo de
    lo que la sombra ya sincronizó del mismo contexto). Un contexto
    (runId/fecha) distinto no es comparable -- no es divergencia, es
    simplemente una corrida o un día nuevo que la sombra todavía no vio.

  Ausencia/corrupción asimétrica (un lado legible y presente, el otro
    ausente o ilegible) -> DIVERGENCIA, fail-closed: no se puede
    verificar integridad si falta o no se puede leer una de las dos
    fuentes que se están comparando. Ambos lados ausentes (proyecto
    virgen, nunca hubo una corrida real) -> íntegro, nada que comparar
    todavía.

Exit codes: 0 = íntegro (sin divergencias) | 3 = divergencia detectada
(el detalle de cada una se imprime a stdout, una por línea) | 1 = error
propio de este script (nunca una divergencia -- ver main()). Un exit 1
NO debe frenar la operación (scripts/runner.sh solo journalea un WARN):
un chequeo roto no es evidencia de tamper, y la PREVENCIÓN (WP7) sigue
activa de todos modos.
"""
import argparse
import json
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shadow_sync import (  # noqa: E402
    EQUITY_SHADOW_FILE,
    ORDER_BUDGET_FILE,
    RUN_ORDERS_SHADOW_FILE,
    _as_int,
    _load_json_dict_or_none,
    _read_csv_rows,
    default_shadow_dir,
)
from shadow_sync import EQUITY_FILE  # noqa: E402

STATE_DIR = Path(__file__).resolve().parent.parent / "state"

# Tolerancia relativa para el pico de equity -- absorbe ruido de redondeo
# de punto flotante entre lo que escribió snapshot.py/place_order.py del
# lado del archivo y lo que copió shadow_sync.py del lado de la sombra
# (misma fuente, distinto momento de escritura), sin abrir una ventana
# real de manipulación (0.1% de una cuenta de trading real es
# insignificante frente a los topes de riesgo, que son de puntos
# porcentuales enteros).
EQUITY_PEAK_TOLERANCE = 0.001


def _peak(rows: list):
    """Pico (máximo) de una lista de (fecha, valor) -- None si no hay
    ningún valor finito utilizable."""
    values = [v for _, v in rows if isinstance(v, (int, float)) and math.isfinite(v)]
    return max(values) if values else None


def check_equity(state_dir: Path, shadow_dir: Path):
    """Devuelve un mensaje de divergencia, o None si el pico de equity es
    íntegro (o ambos lados están ausentes -- proyecto virgen)."""
    state_path = state_dir / EQUITY_FILE
    shadow_path = shadow_dir / EQUITY_SHADOW_FILE
    state_exists = state_path.exists()
    shadow_exists = shadow_path.exists()

    if not state_exists and not shadow_exists:
        return None

    if state_exists != shadow_exists:
        ausente = "state/equity.csv" if not state_exists else "la sombra de equity"
        return f"equity: {ausente} está ausente mientras el otro lado existe (fail-closed)"

    state_rows = _read_csv_rows(state_path)
    shadow_rows = _read_csv_rows(shadow_path)
    state_peak = _peak(state_rows)
    shadow_peak = _peak(shadow_rows)

    if state_peak is None or shadow_peak is None:
        return "equity: uno de los dos lados no tiene ningún valor utilizable (ilegible/vacío)"

    threshold = shadow_peak * (1 - EQUITY_PEAK_TOLERANCE)
    if state_peak < threshold:
        return (
            f"pico de equity: state={state_peak!r} < sombra={shadow_peak!r} "
            f"(más allá de la tolerancia de {EQUITY_PEAK_TOLERANCE:.2%})"
        )
    return None


def check_budget(state_dir: Path, shadow_dir: Path):
    """Devuelve un mensaje de divergencia, o None si el presupuesto es
    íntegro (o no comparable: contexto distinto, o ambos lados ausentes)."""
    state_path = state_dir / ORDER_BUDGET_FILE
    shadow_path = shadow_dir / RUN_ORDERS_SHADOW_FILE
    state_exists = state_path.exists()
    shadow_exists = shadow_path.exists()

    if not state_exists and not shadow_exists:
        return None

    if state_exists != shadow_exists:
        ausente = "state/.run_orders.json" if not state_exists else "la sombra de presupuesto"
        return f"presupuesto: {ausente} está ausente mientras el otro lado existe (fail-closed)"

    state_budget = _load_json_dict_or_none(state_path)
    shadow_budget = _load_json_dict_or_none(shadow_path)
    if state_budget is None or shadow_budget is None:
        return "presupuesto: uno de los dos lados es ilegible/corrupto"

    same_context = (
        state_budget.get("runId") == shadow_budget.get("runId")
        and state_budget.get("date") == shadow_budget.get("date")
    )
    if not same_context:
        return None  # contexto distinto: no comparable, no es divergencia

    fc = _as_int(state_budget.get("count", 0))
    fd = _as_int(state_budget.get("dailyCount", 0))
    sc = _as_int(shadow_budget.get("count", 0))
    sd = _as_int(shadow_budget.get("dailyCount", 0))
    if fc is None or fd is None or sc is None or sd is None:
        return "presupuesto: tipos inesperados en alguno de los dos lados"

    if fc < sc or fd < sd:
        return (
            f"presupuesto: state count={fc}/dailyCount={fd} < "
            f"sombra count={sc}/dailyCount={sd} (mismo runId/fecha)"
        )
    return None


def check(state_dir: Path, shadow_dir: Path) -> list:
    """Lista de mensajes de divergencia (vacía si todo íntegro)."""
    divergencias = []
    for resultado in (check_equity(state_dir, shadow_dir), check_budget(state_dir, shadow_dir)):
        if resultado:
            divergencias.append(resultado)
    return divergencias


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="integrity_check.py")
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--shadow-dir", default=None)
    return parser


def main(argv=None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    args = _build_parser().parse_args(argv)

    state_dir = Path(args.state_dir) if args.state_dir else (
        Path(os.environ.get("ETOROAGENT_STATE_DIR")) if os.environ.get("ETOROAGENT_STATE_DIR") else STATE_DIR
    )
    shadow_dir = Path(args.shadow_dir) if args.shadow_dir else default_shadow_dir()

    try:
        divergencias = check(state_dir, shadow_dir)
    except Exception as exc:
        print(f"ERROR: no se pudo verificar la integridad: {exc}", file=sys.stderr)
        return 1

    if divergencias:
        print("DIVERGENCIA: archivo vs. sombra de integridad no coinciden:")
        for d in divergencias:
            print(f"  - {d}")
        return 3

    print("OK: integridad verificada, sin divergencias entre archivo y sombra.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

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

WP8b (sign-off del auditor sobre WP8): las dos comparaciones de arriba
comparan ARCHIVO vs SOMBRA -- pero un compuesto de DOS sustituciones
coordinadas puede envenenar AMBOS lados EN SINCRONÍA al mismo valor más
bajo, y esas dos comparaciones no ven ninguna divergencia (archivo y
sombra siguen "de acuerdo" entre sí, solo que los dos mienten igual).
Tercera comparación, --expected-peak: el pico de equity es MONÓTONO NO
DECRECIENTE bajo operación legítima -- nunca debería bajar. runner.sh lee
el pico combinado (max de archivo y sombra) ANTES de lanzar `claude` y lo
retiene EN UNA VARIABLE DE SHELL (memoria del proceso del runner -- a
diferencia de un archivo, ningún `cp -r` puede envenenar una variable de
un proceso en ejecución). Después de la corrida, se le pasa ese valor a
--expected-peak: si el pico combinado ACTUAL (max de archivo y sombra,
ya con la sustitución/envenenamiento aplicado si lo hubo) es menor al
valor retenido en memoria (más allá de la tolerancia), es DIVERGENCIA --
sin importar que archivo y sombra sigan de acuerdo entre sí. El cálculo
vive acá (testeable en aislamiento); el runner solo transporta el número
(ver --print-peak, que expone la mitad "leer el pico combinado" de este
mismo cálculo para que el runner no tenga que parsear CSVs/JSON él
mismo).

Exit codes: 0 = íntegro (sin divergencias) | 3 = divergencia detectada
(el detalle de cada una se imprime a stdout, una por línea) | 1 = error
propio de este script (nunca una divergencia real detectada -- ver
main()). WP8b: a diferencia de la versión original de este script, un
exit 1 SÍ frena la operación en runner.sh (mismo camino que un exit 3) --
un verificador de integridad que no puede leerse a sí mismo con
confianza no puede certificar que el sistema esté íntegro; journalear
solo un WARN y seguir operando sería fail-open exactamente en el
componente que existe para ser fail-closed.
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


def combined_peak(state_dir: Path, shadow_dir: Path):
    """Pico combinado (max) entre state/equity.csv y la sombra de
    equity -- lo que el runner retiene EN MEMORIA (variable de shell)
    antes de lanzar `claude`, y contra lo que se vuelve a calcular
    después de la corrida para la comparación de monotonía (WP8b, ver
    check_peak_monotonicity). None si ninguno de los dos lados tiene
    ningún valor utilizable (proyecto virgen)."""
    state_path = state_dir / EQUITY_FILE
    shadow_path = shadow_dir / EQUITY_SHADOW_FILE
    state_rows = _read_csv_rows(state_path) if state_path.exists() else []
    shadow_rows = _read_csv_rows(shadow_path) if shadow_path.exists() else []
    peaks = [p for p in (_peak(state_rows), _peak(shadow_rows)) if p is not None]
    return max(peaks) if peaks else None


def check_peak_monotonicity(
    state_dir: Path, shadow_dir: Path, expected_peak, tolerance: float = EQUITY_PEAK_TOLERANCE
):
    """WP8b: el pico combinado (archivo y sombra) nunca debería bajar
    respecto de expected_peak -- el valor que el runner leyó y retuvo EN
    MEMORIA antes de lanzar `claude`, inmune a cualquier sustitución de
    archivos ocurrida DURANTE la corrida (a diferencia de check_equity,
    que solo compara archivo contra sombra entre sí y no ve nada si
    ambos fueron envenenados al mismo valor, en sincronía).

    expected_peak es None cuando el caller no lo provee (runner.sh en su
    primera corrida jamás, sin historial todavía; o cualquier invocación
    manual/de test que no pasa --expected-peak) -- en ese caso esta
    comparación no aplica, devuelve None (no es una divergencia, es
    'no verificable con esta pieza')."""
    if expected_peak is None:
        return None

    current = combined_peak(state_dir, shadow_dir)
    if current is None:
        return (
            f"monotonía de pico: no hay ningún valor de equity utilizable "
            f"(se esperaba un pico >= {expected_peak!r}, retenido en memoria "
            f"antes de la corrida)"
        )

    threshold = expected_peak * (1 - tolerance)
    if current < threshold:
        return (
            f"monotonía de pico: pico actual (max archivo/sombra) = {current!r} "
            f"< pico retenido en memoria del runner antes de la corrida = "
            f"{expected_peak!r} (más allá de la tolerancia de {tolerance:.2%}) "
            f"-- el compuesto de dos fuentes envenenadas en sincronía no "
            f"engaña a la memoria del proceso"
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


def check(state_dir: Path, shadow_dir: Path, expected_peak=None) -> list:
    """Lista de mensajes de divergencia (vacía si todo íntegro)."""
    divergencias = []
    for resultado in (
        check_equity(state_dir, shadow_dir),
        check_budget(state_dir, shadow_dir),
        check_peak_monotonicity(state_dir, shadow_dir, expected_peak),
    ):
        if resultado:
            divergencias.append(resultado)
    return divergencias


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="integrity_check.py")
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--shadow-dir", default=None)
    # WP8b: pico retenido en memoria del runner ANTES de la corrida, para
    # la comparación de monotonía (ver check_peak_monotonicity).
    parser.add_argument("--expected-peak", type=float, default=None)
    # WP8b: modo de consulta -- imprime el pico combinado actual (o nada
    # si no hay ninguno todavía) y sale 0, sin correr ninguna comparación.
    # El runner lo usa ANTES de lanzar `claude` para capturar el valor
    # que después le pasa de vuelta como --expected-peak.
    parser.add_argument("--print-peak", action="store_true")
    return parser


def main(argv=None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    args = _build_parser().parse_args(argv)

    state_dir = Path(args.state_dir) if args.state_dir else (
        Path(os.environ.get("ETOROAGENT_STATE_DIR")) if os.environ.get("ETOROAGENT_STATE_DIR") else STATE_DIR
    )
    shadow_dir = Path(args.shadow_dir) if args.shadow_dir else default_shadow_dir()

    if args.print_peak:
        # WP8b: modo de consulta pura -- nunca falla (ver docstring de
        # --print-peak): el runner captura esto ANTES de lanzar `claude`
        # y lo retiene en una variable de shell, así que este comando NO
        # puede abortar el runner (set -euo pipefail) por una lectura que
        # a lo sumo debería devolver "sin dato todavía", nunca un error.
        try:
            peak = combined_peak(state_dir, shadow_dir)
        except Exception:
            peak = None
        print(peak if peak is not None else "")
        return 0

    try:
        divergencias = check(state_dir, shadow_dir, expected_peak=args.expected_peak)
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

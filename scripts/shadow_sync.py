"""shadow_sync.py -- sincroniza la sombra de integridad (WP7) fuera del
repo hacia $HOME/Library/Application Support/etoroagent, con merge
append-only que nunca sincroniza hacia abajo.

Uso: .venv/bin/python scripts/shadow_sync.py [--state-dir DIR] [--shadow-dir DIR]

Invocado por scripts/runner.sh ANTES de lanzar `claude` y DESPUÉS de que
termina (haya operado o no) -- nunca por el agente. La sombra es la
defensa contra la clase residual de ataque que el default-deny por
mención del hook (scripts/risk_hook.py, WP6/N11) no puede cerrar: una
sustitución de TODO el directorio state/ (rm -rf state/, cp -r otro-dir/.
state/) que nunca menciona el nombre de ningún archivo individual. La
sombra vive fuera de state/ (y del repo), en un directorio que esas dos
operaciones nunca tocan.

Dos archivos, dos algoritmos de merge:

  equity-shadow.csv: cada fila (fecha, total) de state/equity.csv cuya
    fecha NO esté ya en la sombra se agrega. Una fecha YA presente en la
    sombra NUNCA se sobreescribe -- el valor registrado para una fecha,
    una vez en la sombra, es inmutable (evita que una sustitución de
    state/equity.csv "borre" un pico histórico bajándole el valor a una
    fecha ya vista). scripts/place_order.py usa el pico de esta sombra
    como piso del pico usado para el drawdown en modo real (ver
    _augment_equity_rows_with_shadow_floor).

  run-orders-shadow.json: se adopta el contenido de
    state/.run_orders.json tal cual, EXCEPTO cuando el runId/date de
    AMBOS archivos coincide (mismo contexto) Y el archivo actual reporta
    contadores MENORES a los ya guardados en la sombra -- en ese caso no
    se sincroniza (probable sustitución/reset del archivo real, mantener
    el conteo más alto ya visto). Un runId/date DISTINTO entre archivo y
    sombra es un contexto nuevo legítimo (nueva corrida, nuevo día): se
    adopta directamente, sin comparar contadores de contextos distintos.
    scripts/place_order.py usa el MAX contador a contador entre archivo y
    sombra como presupuesto efectivo en modo real (ver
    _check_order_budget).

Ambas escrituras son atómicas (tmp + os.replace), mismo patrón que el
resto del proyecto. Cualquier archivo de ORIGEN (state/equity.csv,
state/.run_orders.json) ausente o corrupto se salta silenciosamente (no
hay nada que sincronizar todavía, o el archivo de estado está roto -- no
hace falta propagar esa corrupción a la sombra); un archivo de SOMBRA
existente pero corrupto se trata como ausente (se sobreescribe con el
contenido del archivo real, fail-safe hacia adelante -- la sombra ya
perdió su valor de piso si está corrupta).
"""
import argparse
import csv
import json
import os
import sys
from pathlib import Path

STATE_DIR = Path(__file__).resolve().parent.parent / "state"
EQUITY_FILE = "equity.csv"
ORDER_BUDGET_FILE = ".run_orders.json"

EQUITY_SHADOW_FILE = "equity-shadow.csv"
RUN_ORDERS_SHADOW_FILE = "run-orders-shadow.json"
EQUITY_HEADER = ["date", "total"]


def default_shadow_dir() -> Path:
    """$HOME/Library/Application Support/etoroagent -- Path.home() respeta
    la env var HOME (vía os.path.expanduser), así los tests de
    integración pueden redirigir HOME a un tmp_path sin tocar el
    directorio real del operador."""
    return Path.home() / "Library" / "Application Support" / "etoroagent"


def _read_csv_rows(path: Path) -> list:
    if not path.exists():
        return []
    rows = []
    with open(path, newline="") as f:
        reader = csv.reader(f)
        next(reader, None)  # header
        for row in reader:
            if not row:
                continue
            date, value = row[0], row[1]
            try:
                rows.append((date, float(value)))
            except (TypeError, ValueError):
                continue  # fila malformada: se ignora, no aborta el sync
    return rows


def _atomic_write_csv(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(EQUITY_HEADER)
        for date, value in rows:
            writer.writerow([date, value])
    os.replace(tmp_path, path)


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, path)


def sync_equity(state_equity_path: Path, shadow_equity_path: Path) -> None:
    """Merge append-only: agrega a la sombra las fechas de
    state_equity_path que todavía no están en ella. Nunca sobreescribe
    una fecha ya presente en la sombra."""
    if not state_equity_path.exists():
        return

    state_rows = _read_csv_rows(state_equity_path)
    if not state_rows:
        return

    shadow_rows = _read_csv_rows(shadow_equity_path)
    shadow_dates = {date for date, _ in shadow_rows}
    new_rows = [(date, value) for date, value in state_rows if date not in shadow_dates]

    if not new_rows:
        return

    merged = shadow_rows + new_rows
    merged.sort(key=lambda row: row[0])
    _atomic_write_csv(shadow_equity_path, merged)


def _as_int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _load_json_dict_or_none(path: Path):
    if not path.exists():
        return None
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def sync_budget(state_budget_path: Path, shadow_budget_path: Path) -> None:
    """Adopta el presupuesto del archivo real, salvo que ambos compartan
    el MISMO contexto (runId y date) y el archivo actual esté POR DEBAJO
    de la sombra en algún contador -- en ese caso no se sincroniza
    (probable sustitución/reset del archivo real)."""
    file_budget = _load_json_dict_or_none(state_budget_path)
    if file_budget is None:
        return  # archivo de estado ausente/corrupto: nada que sincronizar

    shadow_budget = _load_json_dict_or_none(shadow_budget_path)
    if shadow_budget is None:
        _atomic_write_json(shadow_budget_path, file_budget)
        return

    same_context = (
        file_budget.get("runId") == shadow_budget.get("runId")
        and file_budget.get("date") == shadow_budget.get("date")
    )
    if not same_context:
        _atomic_write_json(shadow_budget_path, file_budget)
        return

    fc = _as_int(file_budget.get("count", 0))
    fd = _as_int(file_budget.get("dailyCount", 0))
    if fc is None or fd is None:
        return  # archivo con tipos inesperados: no sincronizar

    sc = _as_int(shadow_budget.get("count", 0))
    sd = _as_int(shadow_budget.get("dailyCount", 0))
    if sc is None or sd is None or (fc >= sc and fd >= sd):
        _atomic_write_json(shadow_budget_path, file_budget)
    # si no: el archivo está por debajo de la sombra en el MISMO contexto
    # -- no se sincroniza, la sombra retiene el conteo más alto ya visto.


def sync(state_dir: Path, shadow_dir: Path) -> None:
    sync_equity(state_dir / EQUITY_FILE, shadow_dir / EQUITY_SHADOW_FILE)
    sync_budget(state_dir / ORDER_BUDGET_FILE, shadow_dir / RUN_ORDERS_SHADOW_FILE)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shadow_sync.py")
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
        sync(state_dir, shadow_dir)
    except OSError as exc:
        print(f"ERROR: no se pudo sincronizar la sombra de integridad: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

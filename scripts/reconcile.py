"""reconcile.py -- cierra el protocolo de reconciliación tras una corrida
abortada (ver PLAYBOOK.md §Reconciliación tras corrida abortada, WP2/WP4).

Uso: .venv/bin/python scripts/reconcile.py --done

Antes de este script, el borrado de state/.needs_reconciliation dependía
del honor system: PLAYBOOK.md (paso 4 del protocolo) instruía al agente a
borrarlo "a mano" tras journalear una entrada RECONCILIACION (paso 3), pero
nada verificaba en código que esa entrada REALMENTE existiera antes del
borrado -- nada impedía `rm state/.needs_reconciliation` (o cualquier otra
escritura de shell sobre el archivo) sin haber reconciliado nada de
verdad. Tras WP4/N3(c), scripts/risk_hook.py bloquea esas escrituras de
shell directas sobre el flag, dejando `reconcile.py --done` como la única
vía autorizada para borrarlo.

`--done` exige, ANTES de borrar el flag:
  1. Que state/.needs_reconciliation exista y tenga un campo "at" parseable
     (hora local con offset explícito, mismo formato que runner.sh lo
     escribe -- ver su docstring).
  2. Que state/journal.md contenga al menos una línea con el FORMATO real
     de una entrada de journal, "- <timestamp> RECONCILIACION | ..." (no
     alcanza con que la palabra "RECONCILIACION" aparezca en cualquier otro
     lugar del journal -- el mensaje de bloqueo que place_order.py escribe
     mientras el flag está puesto menciona la palabra como PROSA dentro de
     una línea BLOQUEADA, "journaleá RECONCILIACION y eliminá
     state/.needs_reconciliation", lo cual NO cuenta como la entrada real),
     con timestamp ESTRICTAMENTE POSTERIOR al "at" del flag.

Si ambas condiciones se cumplen, borra el flag y termina con éxito (exit
0). Si el flag no existe, no hay nada que hacer (exit 0, idempotente). Si
falta cualquiera de las dos condiciones, o el flag/journal están
corruptos/ilegibles, NO toca el flag y termina con exit 1, explicando qué
falta.

place_order.py no cambia: sigue bloqueando aperturas mientras el flag
exista, sin importar cómo se haya borrado -- este script es simplemente la
única vía recomendada (y, tras N3c, la única practicable por Bash) para
borrarlo con la verificación de por medio.
"""
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

STATE_DIR = Path(__file__).resolve().parent.parent / "state"
NEEDS_RECONCILIATION_FILE = ".needs_reconciliation"
JOURNAL_FILE = "journal.md"

# Mismo formato que journal() en place_order.py y el campo "at" que
# runner.sh escribe en el flag: "YYYY-MM-DD HH:MM +HHMM" (hora local, con
# offset explícito, nunca ISO/UTC).
_TIMESTAMP_FMT = "%Y-%m-%d %H:%M %z"

# Ancla el FORMATO real de una entrada de journal -- "- <timestamp>
# RECONCILIACION | ...", con RECONCILIACION INMEDIATAMENTE después del
# timestamp y seguido de "|". Exigir esto (no un substring en cualquier
# posición de la línea) es lo que evita el falso positivo del mensaje de
# bloqueo de place_order.py, que menciona "RECONCILIACION" como PROSA en
# medio de una línea BLOQUEADA (ahí NO está seguida de espacio+"|" -- ver
# docstring del módulo).
_RECONCILIACION_ENTRY_RE = re.compile(
    r"^-\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s+[+-]\d{4})\s+RECONCILIACION\s*\|",
    re.MULTILINE,
)


def _parse_ts(raw: str) -> datetime:
    return datetime.strptime(raw.strip(), _TIMESTAMP_FMT)


def _find_reconciliation_entry_after(journal_text: str, cutoff: datetime):
    """Devuelve el datetime de la entrada RECONCILIACION más reciente
    posterior (estrictamente) a `cutoff`, o None si no hay ninguna."""
    latest = None
    for m in _RECONCILIACION_ENTRY_RE.finditer(journal_text):
        try:
            ts = _parse_ts(m.group(1))
        except ValueError:
            continue
        if ts > cutoff and (latest is None or ts > latest):
            latest = ts
    return latest


def cmd_done(state_dir: Path) -> int:
    flag_path = state_dir / NEEDS_RECONCILIATION_FILE
    if not flag_path.exists():
        print("OK: no hay reconciliación pendiente (state/.needs_reconciliation no existe).")
        return 0

    try:
        with open(flag_path) as f:
            flag_data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"ERROR: {flag_path} existe pero es ilegible/corrupto ({exc}) -- "
            "no se borra automáticamente, revisión manual requerida.",
            file=sys.stderr,
        )
        return 1

    if not isinstance(flag_data, dict):
        print(
            f"ERROR: {flag_path} no es un objeto JSON válido: {flag_data!r} -- "
            "no se borra automáticamente.",
            file=sys.stderr,
        )
        return 1

    at_raw = flag_data.get("at")
    if not at_raw:
        print(
            f"ERROR: {flag_path} no tiene campo 'at' (hora del aborto) -- "
            "no se puede verificar si la reconciliación es posterior.",
            file=sys.stderr,
        )
        return 1

    try:
        cutoff = _parse_ts(at_raw)
    except (ValueError, TypeError) as exc:
        print(
            f"ERROR: campo 'at' de {flag_path} no parseable ({at_raw!r}): {exc}",
            file=sys.stderr,
        )
        return 1

    journal_path = state_dir / JOURNAL_FILE
    journal_text = journal_path.read_text() if journal_path.exists() else ""
    entry_ts = _find_reconciliation_entry_after(journal_text, cutoff)
    if entry_ts is None:
        print(
            "ERROR: no se encontró una entrada 'RECONCILIACION' en "
            f"{journal_path} posterior a {at_raw} (formato exigido: "
            "'- <timestamp> RECONCILIACION | ...' -- mencionar la palabra "
            "en otro contexto, p.ej. dentro de un mensaje BLOQUEADA, no "
            "cuenta). Journaleá la reconciliación (PLAYBOOK.md "
            "§Reconciliación tras corrida abortada, paso 3) antes de "
            "correr este comando.",
            file=sys.stderr,
        )
        return 1

    flag_path.unlink()
    print(f"OK: reconciliación verificada (journal {entry_ts}) -- {flag_path} eliminado.")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reconcile.py")
    parser.add_argument(
        "--done",
        action="store_true",
        required=True,
        help="verifica que la reconciliación quedó journaleada y borra el flag",
    )
    return parser


def main(argv=None, state_dir: Path = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if state_dir is None:
        state_dir = STATE_DIR
    state_dir = Path(state_dir)

    _build_parser().parse_args(argv)  # solo soporta --done por ahora
    return cmd_done(state_dir)


if __name__ == "__main__":
    sys.exit(main())

"""Tests de scripts/runner.sh (WP2, auditoría pre-producción).

runner.sh siempre hace `cd "$(dirname "$0")/.."` -- opera sobre el
repo REAL (state/, reports/, .env), no sobre un tmp_path aislado como los
tests de place_order.py. No hay una forma de inyectarle un cwd distinto sin
reescribir el script. Por eso estos tests corren el script real vía
subprocess, pero:
  - hacen backup/restore de state/journal.md y state/.needs_reconciliation
    (los únicos archivos que estos escenarios tocan) para no ensuciar el
    journal real ni dejar un flag de reconciliación colgado;
  - usan CLAUDE_BIN=/usr/bin/false para el escenario de corrida abortada,
    así nunca se dispara una llamada real a la API de Anthropic;
  - usan MODE=crypto (nunca pasa por market_open.py, que depende de la
    hora real) para que el escenario de "claude falla" sea determinístico
    cualquier día/hora que corra la suite;
  - limpian cualquier reports/*.log y state/.runner.lock que hayan creado.

.env debe existir con ETORO_API_KEY/ETORO_USER_KEY (ya lo requiere el resto
del proyecto para operar) -- estos tests no leen su contenido, solo
necesitan que runner.sh pueda sourcearlo sin abortar antes de llegar al
escenario bajo prueba.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "scripts" / "runner.sh"
JOURNAL = ROOT / "state" / "journal.md"
NEEDS_RECONCILIATION = ROOT / "state" / ".needs_reconciliation"
LOCK_DIR = ROOT / "state" / ".runner.lock"

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" and not RUNNER.exists(),
    reason="runner.sh no encontrado",
)


def _backup(path: Path):
    return path.read_bytes() if path.exists() else None


def _restore(path: Path, data):
    if data is None:
        if path.exists():
            path.unlink()
    else:
        path.write_bytes(data)


@pytest.fixture
def real_state_backup():
    """Backup/restore de los archivos reales que estos tests tocan --
    mismo criterio que el resto de la suite usa tmp_path, pero acá no es
    posible aislar: runner.sh siempre opera sobre el repo real."""
    journal_data = _backup(JOURNAL)
    recon_data = _backup(NEEDS_RECONCILIATION)
    yield
    _restore(JOURNAL, journal_data)
    _restore(NEEDS_RECONCILIATION, recon_data)
    # Por si un test dejó el lock tomado tras un fallo inesperado.
    pidfile = LOCK_DIR / "pid"
    if pidfile.exists():
        pidfile.unlink()
    if LOCK_DIR.exists():
        try:
            LOCK_DIR.rmdir()
        except OSError:
            pass


def _run_runner(mode: str, extra_env: dict, timeout: float = 90.0):
    env = os.environ.copy()
    env.update(extra_env)
    return subprocess.run(
        ["bash", str(RUNNER), mode],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _cleanup_report_log(stdout: str) -> None:
    """runner.sh imprime 'corrida <modo> terminada: reports/<stamp>-<modo>.log'
    al final -- si el archivo quedó, lo borramos para no acumular basura de
    test en reports/ (el contenido es solo la salida de /usr/bin/false)."""
    match = re.search(r"reports/[\w.-]+\.log", stdout)
    if match:
        log_path = ROOT / match.group(0)
        if log_path.exists():
            log_path.unlink()


def test_claude_fallido_journalea_abortada_y_crea_flag_de_reconciliacion(
    real_state_backup,
):
    result = _run_runner(
        "crypto",
        {"CLAUDE_BIN": "/usr/bin/false", "DRY_RUN": "1"},
    )
    try:
        # runner.sh captura el fallo de claude internamente (rc del propio
        # script sigue siendo 0: journalea ABORTADA y termina, no propaga
        # el exit code de claude).
        assert result.returncode == 0, result.stdout + result.stderr

        journal_text = JOURNAL.read_text()
        assert "ABORTADA" in journal_text
        assert "corrida crypto abortada (claude exit 1)" in journal_text

        assert NEEDS_RECONCILIATION.exists()
        data = json.loads(NEEDS_RECONCILIATION.read_text())
        assert data["reason"] == "corrida crypto abortada (claude exit 1)"
        assert re.fullmatch(r"reports/[\w.-]+-crypto\.log", data["log"])
        assert data["at"]  # timestamp local no vacío
        # Mismo formato de timestamp que journal() en place_order.py: hora
        # local con offset explícito (ej: 2026-08-12 20:15 -0300), no ISO.
        assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2} [+-]\d{4}$", data["at"])
    finally:
        _cleanup_report_log(result.stdout)
        if NEEDS_RECONCILIATION.exists():
            NEEDS_RECONCILIATION.unlink()


def test_lock_ocupado_journalea_skip(real_state_backup):
    LOCK_DIR.mkdir(parents=True)
    (LOCK_DIR / "pid").write_text(str(os.getpid()))  # pid vivo: es este mismo proceso

    result = _run_runner("crypto", {})

    assert result.returncode == 0, result.stdout + result.stderr
    assert "corrida en curso" in result.stdout

    journal_text = JOURNAL.read_text()
    assert "SKIP" in journal_text
    assert "corrida crypto no corrió" in journal_text
    assert "lock ocupado" in journal_text

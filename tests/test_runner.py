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
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "scripts" / "runner.sh"
JOURNAL = ROOT / "state" / "journal.md"
NEEDS_RECONCILIATION = ROOT / "state" / ".needs_reconciliation"
LOCK_DIR = ROOT / "state" / ".runner.lock"
EQUITY_CSV = ROOT / "state" / "equity.csv"
RUN_ORDERS_JSON = ROOT / "state" / ".run_orders.json"

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
    real_state_backup, tmp_path
):
    # WP7: runner.sh ahora sincroniza la sombra de integridad (fuera del
    # repo, bajo $HOME) antes/después de cada corrida -- redirigimos HOME
    # a un tmp_path para no tocar el directorio real del operador durante
    # el test.
    result = _run_runner(
        "crypto",
        {"CLAUDE_BIN": "/usr/bin/false", "DRY_RUN": "1", "HOME": str(tmp_path / "home")},
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


# --- WP7: autorización de corridas reales + sombra de integridad -----------
#
# runner.sh es la única vía sancionada para corridas reales (DRY_RUN=0
# desatendido): exporta ETOROAGENT_AUTHORIZED_RUN=1 (place_order.py lo
# exige para toda apertura en modo real) y sincroniza la sombra de
# integridad fuera del repo (scripts/shadow_sync.py) antes de lanzar
# `claude` y después de que termina. HOME se redirige a un tmp_path en
# todos estos tests para no tocar el directorio real del operador.


def _fake_claude_que_captura_env(path: Path, var_name: str, marker_path: Path, exit_code: int = 0):
    """Escribe un script ejecutable que, en vez de invocar la API de
    Anthropic, vuelca el valor de `var_name` a `marker_path` y termina con
    `exit_code` -- permite verificar qué exportó runner.sh sin depender
    del CLI real de Claude."""
    path.write_text(
        "#!/bin/bash\n"
        f'echo "${{{var_name}:-AUSENTE}}" > "{marker_path}"\n'
        f"exit {exit_code}\n"
    )
    path.chmod(0o755)


def test_authorized_run_exportado_en_el_entorno_de_claude(real_state_backup, tmp_path):
    marker = tmp_path / "authorized_marker.txt"
    fake_claude = tmp_path / "fake_claude.sh"
    _fake_claude_que_captura_env(fake_claude, "ETOROAGENT_AUTHORIZED_RUN", marker, exit_code=0)

    result = _run_runner(
        "crypto",
        {"CLAUDE_BIN": str(fake_claude), "DRY_RUN": "1", "HOME": str(tmp_path / "home")},
    )

    try:
        assert result.returncode == 0, result.stdout + result.stderr
        assert marker.exists()
        assert marker.read_text().strip() == "1"
    finally:
        _cleanup_report_log(result.stdout)


def test_sombra_sincronizada_antes_y_despues_de_la_corrida(real_state_backup, tmp_path):
    # El fake claude no toca la sombra -- si aparece poblada después de la
    # corrida, fue el sync PRE (antes de lanzarlo) el que la creó (el sync
    # POST, si el PRE ya sincronizó todo lo disponible, puede no agregar
    # filas nuevas -- lo que importa es que el mecanismo corrió, no que
    # cada llamada individual haya escrito algo).
    home_dir = tmp_path / "home"
    marker = tmp_path / "marker.txt"
    fake_claude = tmp_path / "fake_claude.sh"
    _fake_claude_que_captura_env(fake_claude, "ETOROAGENT_RUN_ID", marker, exit_code=0)

    result = _run_runner(
        "crypto",
        {"CLAUDE_BIN": str(fake_claude), "DRY_RUN": "1", "HOME": str(home_dir)},
    )

    try:
        assert result.returncode == 0, result.stdout + result.stderr
        shadow_dir = home_dir / "Library" / "Application Support" / "etoroagent"
        # El repo real tiene state/equity.csv y state/.run_orders.json
        # (corridas reales previas, ver tests/test_shadow_sync.py para la
        # lógica de sync en aislamiento) -- sync_shadow() debe reflejarlos
        # en la sombra bajo el HOME redirigido.
        if (ROOT / "state" / "equity.csv").exists():
            assert (shadow_dir / "equity-shadow.csv").exists()
        if (ROOT / "state" / ".run_orders.json").exists():
            assert (shadow_dir / "run-orders-shadow.json").exists()
    finally:
        _cleanup_report_log(result.stdout)


def test_sombra_sincronizada_incluso_si_claude_aborta(real_state_backup, tmp_path):
    home_dir = tmp_path / "home"
    result = _run_runner(
        "crypto",
        {"CLAUDE_BIN": "/usr/bin/false", "DRY_RUN": "1", "HOME": str(home_dir)},
    )
    try:
        assert result.returncode == 0, result.stdout + result.stderr
        shadow_dir = home_dir / "Library" / "Application Support" / "etoroagent"
        if (ROOT / "state" / "equity.csv").exists():
            assert (shadow_dir / "equity-shadow.csv").exists()
    finally:
        _cleanup_report_log(result.stdout)
        if NEEDS_RECONCILIATION.exists():
            NEEDS_RECONCILIATION.unlink()


# --- WP8: detección de divergencia + kill-switch automático ----------------
#
# El compuesto de dos sustituciones coordinadas (una sobre state/, otra
# sobre la sombra) queda como riesgo residual en la PREVENCIÓN de WP7 --
# scripts/integrity_check.py lo convierte en DETECCIÓN: runner.sh lo
# corre después de la sincronización post-corrida y, ante una
# divergencia, journalea, crea el flag de reconciliación (bloquea
# aperturas por el mecanismo ya existente), notifica, y descarga ambos
# jobs de launchd (kill-switch, detachado). El kill-switch invoca
# `launchctl` real -- estos tests lo mockean con un stub en PATH que solo
# registra la invocación, para no tocar launchd real de la máquina.
#
# Simula la divergencia con un CLAUDE_BIN falso que sobrescribe
# state/equity.csv (el archivo real del repo, sin forma de aislarlo --
# mismo criterio que el resto de este archivo) con una única fila de
# valor bajo DESPUÉS de que el sync PRE-corrida ya capturó el pico
# histórico real en la sombra (bajo HOME redirigido) -- reproduce
# exactamente el patrón "el archivo real terminó por debajo de lo que la
# sombra ya vio". state/equity.csv y state/.run_orders.json se
# respaldan/restauran explícitamente, además de journal.md y el flag.


def _fake_launchctl_stub(bin_dir: Path, marker_path: Path) -> None:
    """Escribe un `launchctl` ejecutable en bin_dir que solo registra sus
    argumentos en marker_path y sale 0 -- para que el kill-switch de
    runner.sh no invoque el launchctl real de la máquina durante el test."""
    launchctl = bin_dir / "launchctl"
    launchctl.write_text(
        "#!/bin/bash\n"
        f'echo "launchctl $*" >> "{marker_path}"\n'
        "exit 0\n"
    )
    launchctl.chmod(0o755)


def test_divergencia_de_integridad_journalea_flag_y_dispara_killswitch(tmp_path):
    equity_backup = _backup(EQUITY_CSV)
    budget_backup = _backup(RUN_ORDERS_JSON)
    journal_backup = _backup(JOURNAL)
    recon_backup = _backup(NEEDS_RECONCILIATION)
    result = None

    try:
        home_dir = tmp_path / "home"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        marker = tmp_path / "launchctl_calls.txt"
        _fake_launchctl_stub(bin_dir, marker)

        # Sobrescribe state/equity.csv (el archivo REAL) con una única
        # fila de valor bajo, DESPUÉS de que runner.sh ya haya corrido el
        # sync PRE-corrida (que captura el pico histórico real en la
        # sombra, bajo HOME redirigido) -- simula el archivo real
        # quedando por debajo de lo que la sombra ya vio.
        fake_claude = tmp_path / "fake_claude.sh"
        fake_claude.write_text(
            "#!/bin/bash\n"
            "mkdir -p state\n"
            "printf 'date,total\\n2099-01-01,1.0\\n' > state/equity.csv\n"
            "exit 0\n"
        )
        fake_claude.chmod(0o755)

        result = _run_runner(
            "crypto",
            {
                "CLAUDE_BIN": str(fake_claude),
                "DRY_RUN": "1",
                "HOME": str(home_dir),
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
            },
        )

        assert result.returncode == 0, result.stdout + result.stderr

        journal_text = JOURNAL.read_text()
        assert "INTEGRIDAD" in journal_text
        assert "divergencia" in journal_text.lower()

        assert NEEDS_RECONCILIATION.exists()
        flag_data = json.loads(NEEDS_RECONCILIATION.read_text())
        assert "integridad" in flag_data["reason"].lower()
        assert flag_data["at"]

        # El kill-switch corre detachado (nohup ... sleep 2 ... &) -- se
        # espera a que termine para verificar que invocó el stub.
        deadline = time.time() + 10
        while time.time() < deadline and not marker.exists():
            time.sleep(0.5)

        assert marker.exists(), "el kill-switch no invocó el launchctl stub a tiempo"
        calls = marker.read_text()
        assert "unload" in calls
        assert "com.etoroagent.equities.plist" in calls
        assert "com.etoroagent.crypto.plist" in calls
    finally:
        _restore(EQUITY_CSV, equity_backup)
        _restore(RUN_ORDERS_JSON, budget_backup)
        _restore(JOURNAL, journal_backup)
        _restore(NEEDS_RECONCILIATION, recon_backup)
        if result is not None:
            _cleanup_report_log(result.stdout)
        for stray_log in (ROOT / "reports").glob("*-crypto-integrity.log"):
            stray_log.unlink()
        pidfile = LOCK_DIR / "pid"
        if pidfile.exists():
            pidfile.unlink()
        if LOCK_DIR.exists():
            try:
                LOCK_DIR.rmdir()
            except OSError:
                pass


def test_compuesto_de_dos_sustituciones_coordinadas_es_detectado_por_monotonia(tmp_path):
    # WP8b: el vector exacto que evadía check_equity -- el atacante
    # descubre Y toca AMBOS lados (state/equity.csv real Y la sombra bajo
    # HOME redirigido, que el fake claude simula tocar como si conociera
    # su ubicación) al MISMO valor bajo, en sincronía. check_equity por
    # sí solo (archivo vs sombra) NO vería nada -- ambos "de acuerdo" en
    # el valor envenenado. Solo el pico retenido en memoria del runner
    # ANTES de la corrida (inmune a cualquier archivo, real o sombra)
    # detecta que el pico bajó.
    equity_backup = _backup(EQUITY_CSV)
    budget_backup = _backup(RUN_ORDERS_JSON)
    journal_backup = _backup(JOURNAL)
    recon_backup = _backup(NEEDS_RECONCILIATION)
    result = None

    try:
        home_dir = tmp_path / "home"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        marker = tmp_path / "launchctl_calls.txt"
        _fake_launchctl_stub(bin_dir, marker)

        # El fake claude sobrescribe AMBOS lados -- el archivo real Y la
        # sombra (bajo HOME redirigido) -- al MISMO valor bajo, DESPUÉS
        # de que el sync PRE-corrida ya sincronizó el pico histórico real
        # (y el runner ya lo retuvo en su variable de shell, invisible
        # para este script).
        fake_claude = tmp_path / "fake_claude.sh"
        fake_claude.write_text(
            "#!/bin/bash\n"
            "mkdir -p state\n"
            "printf 'date,total\\n2099-01-01,1.0\\n' > state/equity.csv\n"
            'SHADOW_DIR="$HOME/Library/Application Support/etoroagent"\n'
            'mkdir -p "$SHADOW_DIR"\n'
            'printf \'date,total\\n2099-01-01,1.0\\n\' > "$SHADOW_DIR/equity-shadow.csv"\n'
            "exit 0\n"
        )
        fake_claude.chmod(0o755)

        result = _run_runner(
            "crypto",
            {
                "CLAUDE_BIN": str(fake_claude),
                "DRY_RUN": "1",
                "HOME": str(home_dir),
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
            },
        )

        assert result.returncode == 0, result.stdout + result.stderr

        # Control: si SOLO comparáramos archivo vs sombra post-corrida,
        # ambos muestran 1.0 -- "de acuerdo" entre sí, ninguna
        # divergencia visible por esa vía. La detección tiene que venir
        # de la monotonía contra la memoria del runner.
        shadow_csv = home_dir / "Library" / "Application Support" / "etoroagent" / "equity-shadow.csv"
        assert shadow_csv.exists()

        journal_text = JOURNAL.read_text()
        assert "INTEGRIDAD" in journal_text
        assert "divergencia" in journal_text.lower()

        assert NEEDS_RECONCILIATION.exists()
        flag_data = json.loads(NEEDS_RECONCILIATION.read_text())
        assert "integridad" in flag_data["reason"].lower()

        deadline = time.time() + 10
        while time.time() < deadline and not marker.exists():
            time.sleep(0.5)
        assert marker.exists(), "el kill-switch no invocó el launchctl stub a tiempo"
    finally:
        _restore(EQUITY_CSV, equity_backup)
        _restore(RUN_ORDERS_JSON, budget_backup)
        _restore(JOURNAL, journal_backup)
        _restore(NEEDS_RECONCILIATION, recon_backup)
        if result is not None:
            _cleanup_report_log(result.stdout)
        for stray_log in (ROOT / "reports").glob("*-crypto-integrity.log"):
            stray_log.unlink()
        pidfile = LOCK_DIR / "pid"
        if pidfile.exists():
            pidfile.unlink()
        if LOCK_DIR.exists():
            try:
                LOCK_DIR.rmdir()
            except OSError:
                pass


def test_verificador_ilegible_rc1_ejecuta_el_mismo_camino_de_freno(tmp_path):
    # WP8b, fix 2: un fallo interno del propio verificador (rc=1) ya no
    # se journalea como un WARN inocuo -- frena por el MISMO camino que
    # una divergencia real (rc=3). Simula "archivo corrupto irrecuperable"
    # con permiso 000 sobre el archivo REAL de equity: ni
    # scripts/shadow_sync.py (best-effort, sigue de largo con un WARN,
    # sin relación con este fix) ni scripts/integrity_check.py pueden
    # leerlo -- pero integrity_check.py ahora sí debe frenar la corrida.
    equity_backup = _backup(EQUITY_CSV)
    budget_backup = _backup(RUN_ORDERS_JSON)
    journal_backup = _backup(JOURNAL)
    recon_backup = _backup(NEEDS_RECONCILIATION)
    original_mode = EQUITY_CSV.stat().st_mode if EQUITY_CSV.exists() else None
    result = None

    try:
        assert EQUITY_CSV.exists(), "se necesita un state/equity.csv real para este test"

        home_dir = tmp_path / "home"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        marker = tmp_path / "launchctl_calls.txt"
        _fake_launchctl_stub(bin_dir, marker)

        # Claude exitoso y sin efecto (no toca equity.csv) -- aísla este
        # test del camino de ABORTADA (WP2), que también escribe el flag
        # de reconciliación y confundiría la aserción de abajo.
        fake_claude = tmp_path / "fake_claude.sh"
        fake_claude.write_text("#!/bin/bash\nexit 0\n")
        fake_claude.chmod(0o755)

        # Pre-siembra la sombra (bajo HOME redirigido) para que exista
        # ANTES de correr runner.sh -- si dependiera del sync PRE-corrida,
        # ese sync también fallaría al intentar leer el archivo real ya
        # corrupto (permiso 000, ver abajo) y la sombra quedaría ausente,
        # lo que haría que check_equity viera una AUSENCIA ASIMÉTRICA
        # (divergencia real, rc=3) en vez de llegar a leer el contenido
        # del archivo corrupto (rc=1, lo que este test quiere ejercitar).
        shadow_dir = home_dir / "Library" / "Application Support" / "etoroagent"
        shadow_dir.mkdir(parents=True)
        (shadow_dir / "equity-shadow.csv").write_text("date,total\n2026-08-01,1000.0\n")
        (shadow_dir / "run-orders-shadow.json").write_text(
            '{"runId": null, "count": 0, "date": null, "dailyCount": 0}'
        )

        EQUITY_CSV.chmod(0o000)

        result = _run_runner(
            "crypto",
            {
                "CLAUDE_BIN": str(fake_claude),
                "DRY_RUN": "1",
                "HOME": str(home_dir),
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
            },
        )

        assert result.returncode == 0, result.stdout + result.stderr

        journal_text = JOURNAL.read_text()
        assert "INTEGRIDAD" in journal_text
        assert "ilegible" in journal_text.lower() or "error interno" in journal_text.lower()

        assert NEEDS_RECONCILIATION.exists()
        flag_data = json.loads(NEEDS_RECONCILIATION.read_text())
        assert "ilegible" in flag_data["reason"].lower()

        deadline = time.time() + 10
        while time.time() < deadline and not marker.exists():
            time.sleep(0.5)
        assert marker.exists(), "el kill-switch no invocó el launchctl stub a tiempo"
    finally:
        if original_mode is not None:
            EQUITY_CSV.chmod(original_mode)
        _restore(EQUITY_CSV, equity_backup)
        _restore(RUN_ORDERS_JSON, budget_backup)
        _restore(JOURNAL, journal_backup)
        _restore(NEEDS_RECONCILIATION, recon_backup)
        if result is not None:
            _cleanup_report_log(result.stdout)
        for stray_log in (ROOT / "reports").glob("*-crypto-integrity.log"):
            stray_log.unlink()
        pidfile = LOCK_DIR / "pid"
        if pidfile.exists():
            pidfile.unlink()
        if LOCK_DIR.exists():
            try:
                LOCK_DIR.rmdir()
            except OSError:
                pass


def test_integridad_intacta_no_journalea_ni_dispara_killswitch(real_state_backup, tmp_path):
    # Control negativo: una corrida normal (sin divergencia) no debe
    # journalear INTEGRIDAD ni tocar el flag de reconciliación.
    home_dir = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "launchctl_calls.txt"
    _fake_launchctl_stub(bin_dir, marker)

    result = _run_runner(
        "crypto",
        {
            "CLAUDE_BIN": "/usr/bin/false",
            "DRY_RUN": "1",
            "HOME": str(home_dir),
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        },
    )
    try:
        assert result.returncode == 0, result.stdout + result.stderr
        journal_text = JOURNAL.read_text()
        assert "INTEGRIDAD" not in journal_text
        time.sleep(3)
        assert not marker.exists(), "el kill-switch no debía dispararse sin divergencia"
    finally:
        _cleanup_report_log(result.stdout)
        for stray_log in (ROOT / "reports").glob("*-crypto-integrity.log"):
            stray_log.unlink()
        if NEEDS_RECONCILIATION.exists():
            NEEDS_RECONCILIATION.unlink()

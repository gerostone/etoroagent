"""Tests de scripts/shadow_sync.py (WP7/pieza 2).

Sincroniza state/equity.csv y state/.run_orders.json hacia una sombra
fuera del repo, con merge append-only (equity) y "solo hacia arriba, en
el mismo contexto" (presupuesto) -- ver el docstring del módulo para el
algoritmo exacto. Invocado por scripts/runner.sh antes/después de cada
corrida real, nunca por el agente.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import shadow_sync  # noqa: E402


def _write_csv(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["date,total"] + [f"{date},{value}" for date, value in rows]
    path.write_text("\n".join(lines) + "\n")


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if data is None:
        path.write_text("{esto no es json")
    else:
        path.write_text(json.dumps(data))


# -- sync_equity --------------------------------------------------------


def test_sync_equity_agrega_fechas_nuevas(tmp_path):
    state_csv = tmp_path / "state" / "equity.csv"
    shadow_csv = tmp_path / "shadow" / "equity-shadow.csv"
    _write_csv(state_csv, [("2026-08-01", 1000.0), ("2026-08-02", 1050.0)])

    shadow_sync.sync_equity(state_csv, shadow_csv)

    rows = shadow_sync._read_csv_rows(shadow_csv)
    assert rows == [("2026-08-01", 1000.0), ("2026-08-02", 1050.0)]


def test_sync_equity_no_sobreescribe_fecha_existente(tmp_path):
    state_csv = tmp_path / "state" / "equity.csv"
    shadow_csv = tmp_path / "shadow" / "equity-shadow.csv"
    # La sombra ya tiene 2026-08-01 con el pico REAL (1200) -- el archivo
    # de state fue truncado/sustituido y ahora reporta un valor MENOR
    # (800) para esa misma fecha. El valor de la sombra debe persistir.
    _write_csv(shadow_csv, [("2026-08-01", 1200.0)])
    _write_csv(state_csv, [("2026-08-01", 800.0), ("2026-08-02", 850.0)])

    shadow_sync.sync_equity(state_csv, shadow_csv)

    rows = dict(shadow_sync._read_csv_rows(shadow_csv))
    assert rows["2026-08-01"] == 1200.0  # preservado, no bajado a 800
    assert rows["2026-08-02"] == 850.0  # fecha nueva, se agrega


def test_sync_equity_state_ausente_no_hace_nada(tmp_path):
    state_csv = tmp_path / "state" / "equity.csv"
    shadow_csv = tmp_path / "shadow" / "equity-shadow.csv"
    _write_csv(shadow_csv, [("2026-08-01", 1000.0)])

    shadow_sync.sync_equity(state_csv, shadow_csv)

    rows = shadow_sync._read_csv_rows(shadow_csv)
    assert rows == [("2026-08-01", 1000.0)]


def test_sync_equity_shadow_ausente_se_crea_desde_cero(tmp_path):
    state_csv = tmp_path / "state" / "equity.csv"
    shadow_csv = tmp_path / "shadow" / "equity-shadow.csv"
    _write_csv(state_csv, [("2026-08-01", 1000.0)])
    assert not shadow_csv.exists()

    shadow_sync.sync_equity(state_csv, shadow_csv)

    assert shadow_csv.exists()
    assert shadow_sync._read_csv_rows(shadow_csv) == [("2026-08-01", 1000.0)]


def test_sync_equity_filas_quedan_ordenadas_por_fecha(tmp_path):
    state_csv = tmp_path / "state" / "equity.csv"
    shadow_csv = tmp_path / "shadow" / "equity-shadow.csv"
    _write_csv(shadow_csv, [("2026-08-03", 1100.0)])
    _write_csv(state_csv, [("2026-08-01", 900.0), ("2026-08-03", 1100.0)])

    shadow_sync.sync_equity(state_csv, shadow_csv)

    rows = shadow_sync._read_csv_rows(shadow_csv)
    assert [d for d, _ in rows] == ["2026-08-01", "2026-08-03"]


# -- sync_budget ----------------------------------------------------------


def test_sync_budget_adopta_archivo_si_sombra_ausente(tmp_path):
    state_json = tmp_path / "state" / ".run_orders.json"
    shadow_json = tmp_path / "shadow" / "run-orders-shadow.json"
    _write_json(state_json, {"runId": "run-1", "count": 2, "date": "2026-08-13", "dailyCount": 2})

    shadow_sync.sync_budget(state_json, shadow_json)

    assert json.loads(shadow_json.read_text()) == {
        "runId": "run-1", "count": 2, "date": "2026-08-13", "dailyCount": 2
    }


def test_sync_budget_mismo_contexto_archivo_mayor_o_igual_sincroniza(tmp_path):
    state_json = tmp_path / "state" / ".run_orders.json"
    shadow_json = tmp_path / "shadow" / "run-orders-shadow.json"
    _write_json(shadow_json, {"runId": "run-1", "count": 2, "date": "2026-08-13", "dailyCount": 2})
    _write_json(state_json, {"runId": "run-1", "count": 3, "date": "2026-08-13", "dailyCount": 3})

    shadow_sync.sync_budget(state_json, shadow_json)

    assert json.loads(shadow_json.read_text())["count"] == 3


def test_sync_budget_mismo_contexto_archivo_menor_no_sincroniza(tmp_path):
    # Escenario del ataque: el archivo real fue borrado/sustituido y ahora
    # reporta contadores más bajos que la sombra, PERO con el MISMO
    # runId/date (el atacante no cambió de contexto, solo intentó
    # "resetear" los contadores). La sombra debe retener el conteo real.
    state_json = tmp_path / "state" / ".run_orders.json"
    shadow_json = tmp_path / "shadow" / "run-orders-shadow.json"
    _write_json(shadow_json, {"runId": "run-1", "count": 3, "date": "2026-08-13", "dailyCount": 6})
    _write_json(state_json, {"runId": "run-1", "count": 0, "date": "2026-08-13", "dailyCount": 0})

    shadow_sync.sync_budget(state_json, shadow_json)

    shadow = json.loads(shadow_json.read_text())
    assert shadow["count"] == 3
    assert shadow["dailyCount"] == 6


def test_sync_budget_contexto_distinto_run_id_adopta_directo(tmp_path):
    # runId nuevo (nueva corrida real) -- contexto legítimamente distinto,
    # se adopta el conteo fresco del archivo sin comparar contra la
    # sombra de la corrida anterior.
    state_json = tmp_path / "state" / ".run_orders.json"
    shadow_json = tmp_path / "shadow" / "run-orders-shadow.json"
    _write_json(shadow_json, {"runId": "run-viejo", "count": 3, "date": "2026-08-13", "dailyCount": 3})
    _write_json(state_json, {"runId": "run-nuevo", "count": 0, "date": "2026-08-13", "dailyCount": 3})

    shadow_sync.sync_budget(state_json, shadow_json)

    assert json.loads(shadow_json.read_text())["count"] == 0


def test_sync_budget_contexto_distinto_date_adopta_directo(tmp_path):
    # Día calendario nuevo -- ídem, contexto distinto.
    state_json = tmp_path / "state" / ".run_orders.json"
    shadow_json = tmp_path / "shadow" / "run-orders-shadow.json"
    _write_json(shadow_json, {"runId": None, "count": 0, "date": "2026-08-12", "dailyCount": 6})
    _write_json(state_json, {"runId": None, "count": 0, "date": "2026-08-13", "dailyCount": 0})

    shadow_sync.sync_budget(state_json, shadow_json)

    assert json.loads(shadow_json.read_text())["dailyCount"] == 0


def test_sync_budget_archivo_ausente_no_hace_nada(tmp_path):
    state_json = tmp_path / "state" / ".run_orders.json"
    shadow_json = tmp_path / "shadow" / "run-orders-shadow.json"
    _write_json(shadow_json, {"runId": "run-1", "count": 3, "date": "2026-08-13", "dailyCount": 3})
    assert not state_json.exists()

    shadow_sync.sync_budget(state_json, shadow_json)

    assert json.loads(shadow_json.read_text())["count"] == 3


def test_sync_budget_archivo_corrupto_no_hace_nada(tmp_path):
    state_json = tmp_path / "state" / ".run_orders.json"
    shadow_json = tmp_path / "shadow" / "run-orders-shadow.json"
    _write_json(shadow_json, {"runId": "run-1", "count": 3, "date": "2026-08-13", "dailyCount": 3})
    _write_json(state_json, None)  # corrupto

    shadow_sync.sync_budget(state_json, shadow_json)

    assert json.loads(shadow_json.read_text())["count"] == 3


def test_sync_budget_sombra_corrupta_se_trata_como_ausente(tmp_path):
    state_json = tmp_path / "state" / ".run_orders.json"
    shadow_json = tmp_path / "shadow" / "run-orders-shadow.json"
    _write_json(shadow_json, None)  # corrupta
    _write_json(state_json, {"runId": "run-1", "count": 2, "date": "2026-08-13", "dailyCount": 2})

    shadow_sync.sync_budget(state_json, shadow_json)

    assert json.loads(shadow_json.read_text())["count"] == 2


# -- default_shadow_dir / main() CLI -----------------------------------


def test_default_shadow_dir_respeta_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert shadow_sync.default_shadow_dir() == tmp_path / "Library" / "Application Support" / "etoroagent"


def test_main_sincroniza_ambos_archivos_con_flags_explicitos(tmp_path):
    state_dir = tmp_path / "state"
    shadow_dir = tmp_path / "shadow"
    _write_csv(state_dir / "equity.csv", [("2026-08-01", 1000.0)])
    _write_json(
        state_dir / ".run_orders.json",
        {"runId": "run-1", "count": 1, "date": "2026-08-13", "dailyCount": 1},
    )

    rc = shadow_sync.main(["--state-dir", str(state_dir), "--shadow-dir", str(shadow_dir)])

    assert rc == 0
    assert (shadow_dir / "equity-shadow.csv").exists()
    assert (shadow_dir / "run-orders-shadow.json").exists()


def test_main_sin_archivos_de_estado_no_falla(tmp_path):
    state_dir = tmp_path / "state"
    shadow_dir = tmp_path / "shadow"
    state_dir.mkdir()

    rc = shadow_sync.main(["--state-dir", str(state_dir), "--shadow-dir", str(shadow_dir)])

    assert rc == 0

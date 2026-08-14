"""Tests de scripts/integrity_check.py (WP8).

Compara state/ contra la sombra de integridad fuera del repo (WP7,
scripts/shadow_sync.py) y detecta divergencias que la prevención en
tiempo real de place_order.py no puede cerrar del todo (el compuesto de
dos `cp -r` coordinados, uno sobre state/ y otro sobre la sombra).
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import integrity_check  # noqa: E402


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


# -- check_equity -----------------------------------------------------------


def test_check_equity_integro_sin_divergencia(tmp_path):
    state_dir = tmp_path / "state"
    shadow_dir = tmp_path / "shadow"
    _write_csv(state_dir / "equity.csv", [("2026-08-01", 1000.0), ("2026-08-02", 1050.0)])
    _write_csv(shadow_dir / "equity-shadow.csv", [("2026-08-01", 1000.0)])

    assert integrity_check.check_equity(state_dir, shadow_dir) is None


def test_check_equity_pico_rebajado_es_divergencia(tmp_path):
    state_dir = tmp_path / "state"
    shadow_dir = tmp_path / "shadow"
    # El archivo real fue sustituido/truncado: ya no tiene el pico
    # historico que la sombra retiene.
    _write_csv(state_dir / "equity.csv", [("2026-08-13", 700.0)])
    _write_csv(shadow_dir / "equity-shadow.csv", [("2026-08-01", 1000.0), ("2026-08-05", 950.0)])

    msg = integrity_check.check_equity(state_dir, shadow_dir)

    assert msg is not None
    assert "pico" in msg.lower() or "equity" in msg.lower()


def test_check_equity_sombra_ausente_archivo_presente_es_divergencia(tmp_path):
    state_dir = tmp_path / "state"
    shadow_dir = tmp_path / "shadow"
    _write_csv(state_dir / "equity.csv", [("2026-08-01", 1000.0)])
    assert not (shadow_dir / "equity-shadow.csv").exists()

    msg = integrity_check.check_equity(state_dir, shadow_dir)

    assert msg is not None


def test_check_equity_archivo_ausente_sombra_presente_es_divergencia(tmp_path):
    state_dir = tmp_path / "state"
    shadow_dir = tmp_path / "shadow"
    _write_csv(shadow_dir / "equity-shadow.csv", [("2026-08-01", 1000.0)])
    assert not (state_dir / "equity.csv").exists()

    msg = integrity_check.check_equity(state_dir, shadow_dir)

    assert msg is not None


def test_check_equity_ambos_ausentes_proyecto_virgen_no_es_divergencia(tmp_path):
    state_dir = tmp_path / "state"
    shadow_dir = tmp_path / "shadow"

    assert integrity_check.check_equity(state_dir, shadow_dir) is None


def test_check_equity_tolerancia_de_redondeo_no_dispara_falso_positivo(tmp_path):
    state_dir = tmp_path / "state"
    shadow_dir = tmp_path / "shadow"
    # Diferencia de punto flotante minúscula (0.005%), muy por debajo de
    # la tolerancia (0.1%) -- no debe dispararse como divergencia.
    _write_csv(state_dir / "equity.csv", [("2026-08-01", 999.95)])
    _write_csv(shadow_dir / "equity-shadow.csv", [("2026-08-01", 1000.0)])

    assert integrity_check.check_equity(state_dir, shadow_dir) is None


def test_check_equity_justo_debajo_de_la_tolerancia_si_dispara(tmp_path):
    state_dir = tmp_path / "state"
    shadow_dir = tmp_path / "shadow"
    # 1% de diferencia -- muy por encima de la tolerancia de 0.1%.
    _write_csv(state_dir / "equity.csv", [("2026-08-01", 990.0)])
    _write_csv(shadow_dir / "equity-shadow.csv", [("2026-08-01", 1000.0)])

    assert integrity_check.check_equity(state_dir, shadow_dir) is not None


# -- check_budget -------------------------------------------------------


def test_check_budget_integro_sin_divergencia(tmp_path):
    state_dir = tmp_path / "state"
    shadow_dir = tmp_path / "shadow"
    _write_json(
        state_dir / ".run_orders.json",
        {"runId": "run-1", "count": 3, "date": "2026-08-13", "dailyCount": 3},
    )
    _write_json(
        shadow_dir / "run-orders-shadow.json",
        {"runId": "run-1", "count": 3, "date": "2026-08-13", "dailyCount": 3},
    )

    assert integrity_check.check_budget(state_dir, shadow_dir) is None


def test_check_budget_contadores_rebajados_mismo_contexto_es_divergencia(tmp_path):
    state_dir = tmp_path / "state"
    shadow_dir = tmp_path / "shadow"
    _write_json(
        state_dir / ".run_orders.json",
        {"runId": "run-1", "count": 0, "date": "2026-08-13", "dailyCount": 0},
    )
    _write_json(
        shadow_dir / "run-orders-shadow.json",
        {"runId": "run-1", "count": 3, "date": "2026-08-13", "dailyCount": 6},
    )

    msg = integrity_check.check_budget(state_dir, shadow_dir)

    assert msg is not None
    assert "presupuesto" in msg.lower()


def test_check_budget_contexto_distinto_no_es_divergencia(tmp_path):
    state_dir = tmp_path / "state"
    shadow_dir = tmp_path / "shadow"
    _write_json(
        state_dir / ".run_orders.json",
        {"runId": "run-nuevo", "count": 0, "date": "2026-08-14", "dailyCount": 0},
    )
    _write_json(
        shadow_dir / "run-orders-shadow.json",
        {"runId": "run-viejo", "count": 3, "date": "2026-08-13", "dailyCount": 3},
    )

    assert integrity_check.check_budget(state_dir, shadow_dir) is None


def test_check_budget_sombra_ausente_archivo_presente_es_divergencia(tmp_path):
    state_dir = tmp_path / "state"
    shadow_dir = tmp_path / "shadow"
    _write_json(
        state_dir / ".run_orders.json",
        {"runId": "run-1", "count": 1, "date": "2026-08-13", "dailyCount": 1},
    )

    assert integrity_check.check_budget(state_dir, shadow_dir) is not None


def test_check_budget_ambos_ausentes_proyecto_virgen_no_es_divergencia(tmp_path):
    state_dir = tmp_path / "state"
    shadow_dir = tmp_path / "shadow"

    assert integrity_check.check_budget(state_dir, shadow_dir) is None


def test_check_budget_archivo_corrupto_con_sombra_presente_es_divergencia(tmp_path):
    state_dir = tmp_path / "state"
    shadow_dir = tmp_path / "shadow"
    _write_json(state_dir / ".run_orders.json", None)  # corrupto
    _write_json(
        shadow_dir / "run-orders-shadow.json",
        {"runId": "run-1", "count": 1, "date": "2026-08-13", "dailyCount": 1},
    )

    assert integrity_check.check_budget(state_dir, shadow_dir) is not None


# -- check() / main() ---------------------------------------------------


def test_check_proyecto_virgen_ambos_lados_ausentes_lista_vacia(tmp_path):
    state_dir = tmp_path / "state"
    shadow_dir = tmp_path / "shadow"

    assert integrity_check.check(state_dir, shadow_dir) == []


def test_check_acumula_ambas_divergencias(tmp_path):
    state_dir = tmp_path / "state"
    shadow_dir = tmp_path / "shadow"
    _write_csv(state_dir / "equity.csv", [("2026-08-13", 700.0)])
    _write_csv(shadow_dir / "equity-shadow.csv", [("2026-08-01", 1000.0)])
    _write_json(
        state_dir / ".run_orders.json",
        {"runId": "run-1", "count": 0, "date": "2026-08-13", "dailyCount": 0},
    )
    _write_json(
        shadow_dir / "run-orders-shadow.json",
        {"runId": "run-1", "count": 3, "date": "2026-08-13", "dailyCount": 3},
    )

    divergencias = integrity_check.check(state_dir, shadow_dir)

    assert len(divergencias) == 2


def test_main_integro_devuelve_0(tmp_path):
    state_dir = tmp_path / "state"
    shadow_dir = tmp_path / "shadow"
    _write_csv(state_dir / "equity.csv", [("2026-08-01", 1000.0)])
    _write_csv(shadow_dir / "equity-shadow.csv", [("2026-08-01", 1000.0)])

    rc = integrity_check.main(["--state-dir", str(state_dir), "--shadow-dir", str(shadow_dir)])

    assert rc == 0


def test_main_divergencia_devuelve_3_e_imprime_detalle(tmp_path, capsys):
    state_dir = tmp_path / "state"
    shadow_dir = tmp_path / "shadow"
    _write_csv(state_dir / "equity.csv", [("2026-08-13", 700.0)])
    _write_csv(shadow_dir / "equity-shadow.csv", [("2026-08-01", 1000.0)])

    rc = integrity_check.main(["--state-dir", str(state_dir), "--shadow-dir", str(shadow_dir)])

    assert rc == 3
    out = capsys.readouterr().out
    assert "DIVERGENCIA" in out
    assert "700" in out and "1000" in out


def test_main_proyecto_virgen_devuelve_0(tmp_path):
    state_dir = tmp_path / "state"
    shadow_dir = tmp_path / "shadow"

    rc = integrity_check.main(["--state-dir", str(state_dir), "--shadow-dir", str(shadow_dir)])

    assert rc == 0


def test_main_honra_etoroagent_state_dir(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    shadow_dir = tmp_path / "shadow"
    _write_csv(state_dir / "equity.csv", [("2026-08-01", 1000.0)])
    _write_csv(shadow_dir / "equity-shadow.csv", [("2026-08-01", 1000.0)])
    monkeypatch.setenv("ETOROAGENT_STATE_DIR", str(state_dir))

    rc = integrity_check.main(["--shadow-dir", str(shadow_dir)])

    assert rc == 0


def test_main_honra_home_para_la_sombra_por_default(tmp_path, monkeypatch):
    home_dir = tmp_path / "home"
    state_dir = tmp_path / "state"
    shadow_dir = home_dir / "Library" / "Application Support" / "etoroagent"
    _write_csv(state_dir / "equity.csv", [("2026-08-01", 1000.0)])
    _write_csv(shadow_dir / "equity-shadow.csv", [("2026-08-01", 1000.0)])
    monkeypatch.setenv("HOME", str(home_dir))

    rc = integrity_check.main(["--state-dir", str(state_dir)])

    assert rc == 0

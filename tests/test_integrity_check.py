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


# --- WP8b: monotonía del pico retenido en memoria (--expected-peak) -------
#
# El compuesto de DOS sustituciones coordinadas (una sobre state/, otra
# sobre la sombra) puede envenenar AMBOS lados en sincronía al mismo
# valor más bajo -- check_equity/check_budget no ven nada porque archivo
# y sombra siguen "de acuerdo" entre sí. check_peak_monotonicity compara
# contra un valor que NINGÚN archivo puede envenenar: el pico que el
# runner leyó y retuvo en una variable de shell (memoria del proceso)
# ANTES de lanzar `claude`.


def test_combined_peak_max_entre_archivo_y_sombra(tmp_path):
    state_dir = tmp_path / "state"
    shadow_dir = tmp_path / "shadow"
    _write_csv(state_dir / "equity.csv", [("2026-08-01", 900.0)])
    _write_csv(shadow_dir / "equity-shadow.csv", [("2026-08-01", 1000.0)])

    assert integrity_check.combined_peak(state_dir, shadow_dir) == 1000.0


def test_combined_peak_ambos_ausentes_es_none(tmp_path):
    state_dir = tmp_path / "state"
    shadow_dir = tmp_path / "shadow"

    assert integrity_check.combined_peak(state_dir, shadow_dir) is None


def test_check_peak_monotonicity_sin_expected_peak_no_aplica(tmp_path):
    state_dir = tmp_path / "state"
    shadow_dir = tmp_path / "shadow"
    _write_csv(state_dir / "equity.csv", [("2026-08-01", 100.0)])
    _write_csv(shadow_dir / "equity-shadow.csv", [("2026-08-01", 1000.0)])

    assert integrity_check.check_peak_monotonicity(state_dir, shadow_dir, None) is None


def test_check_peak_monotonicity_integro_cuando_el_pico_se_mantiene(tmp_path):
    state_dir = tmp_path / "state"
    shadow_dir = tmp_path / "shadow"
    _write_csv(state_dir / "equity.csv", [("2026-08-01", 10000.0)])
    _write_csv(shadow_dir / "equity-shadow.csv", [("2026-08-01", 10000.0)])

    assert integrity_check.check_peak_monotonicity(state_dir, shadow_dir, 10000.0) is None


def test_check_peak_monotonicity_ambos_lados_envenenados_en_sincronia_es_divergencia(tmp_path):
    # El vector EXACTO del auditor: pico pre-corrida 10000, retenido en
    # memoria; ambos archivos (state/equity.csv Y la sombra) fueron
    # sustituidos en sincronía a un pico de 8000. check_equity NO ve
    # nada (8000 == 8000, ambos "de acuerdo") -- solo la comparación
    # contra la memoria lo detecta.
    state_dir = tmp_path / "state"
    shadow_dir = tmp_path / "shadow"
    _write_csv(state_dir / "equity.csv", [("2026-08-14", 8000.0)])
    _write_csv(shadow_dir / "equity-shadow.csv", [("2026-08-01", 8000.0)])

    # Control: check_equity, comparando SOLO archivo vs sombra, no ve la
    # divergencia -- confirma que el compuesto realmente evade esa pieza.
    assert integrity_check.check_equity(state_dir, shadow_dir) is None

    msg = integrity_check.check_peak_monotonicity(state_dir, shadow_dir, 10000.0)

    assert msg is not None
    assert "monoton" in msg.lower()


def test_check_peak_monotonicity_crecimiento_legitimo_no_es_divergencia(tmp_path):
    # El pico crece legítimamente entre corridas (10000 -> 10050) -- no
    # debe dispararse.
    state_dir = tmp_path / "state"
    shadow_dir = tmp_path / "shadow"
    _write_csv(state_dir / "equity.csv", [("2026-08-14", 10050.0)])
    _write_csv(shadow_dir / "equity-shadow.csv", [("2026-08-01", 10000.0)])

    assert integrity_check.check_peak_monotonicity(state_dir, shadow_dir, 10000.0) is None


def test_check_peak_monotonicity_tolerancia_de_redondeo_no_dispara(tmp_path):
    state_dir = tmp_path / "state"
    shadow_dir = tmp_path / "shadow"
    # 0.02% de diferencia -- muy por debajo de la tolerancia (0.1%).
    _write_csv(state_dir / "equity.csv", [("2026-08-14", 9998.0)])
    _write_csv(shadow_dir / "equity-shadow.csv", [("2026-08-01", 9998.0)])

    assert integrity_check.check_peak_monotonicity(state_dir, shadow_dir, 10000.0) is None


def test_check_peak_monotonicity_justo_debajo_de_la_tolerancia_si_dispara(tmp_path):
    state_dir = tmp_path / "state"
    shadow_dir = tmp_path / "shadow"
    _write_csv(state_dir / "equity.csv", [("2026-08-14", 9900.0)])
    _write_csv(shadow_dir / "equity-shadow.csv", [("2026-08-01", 9900.0)])

    assert integrity_check.check_peak_monotonicity(state_dir, shadow_dir, 10000.0) is not None


def test_check_peak_monotonicity_sin_ningun_valor_utilizable_es_divergencia(tmp_path):
    # Se esperaba un pico (memoria del runner no vacía) pero ninguno de
    # los dos lados tiene ahora ningún valor utilizable -- fail-closed.
    state_dir = tmp_path / "state"
    shadow_dir = tmp_path / "shadow"

    msg = integrity_check.check_peak_monotonicity(state_dir, shadow_dir, 10000.0)

    assert msg is not None


def test_check_incluye_monotonicidad_cuando_se_pasa_expected_peak(tmp_path):
    state_dir = tmp_path / "state"
    shadow_dir = tmp_path / "shadow"
    _write_csv(state_dir / "equity.csv", [("2026-08-14", 8000.0)])
    _write_csv(shadow_dir / "equity-shadow.csv", [("2026-08-01", 8000.0)])

    divergencias = integrity_check.check(state_dir, shadow_dir, expected_peak=10000.0)

    assert len(divergencias) == 1
    assert "monoton" in divergencias[0].lower()


def test_check_sin_expected_peak_no_agrega_divergencia_de_monotonicidad(tmp_path):
    state_dir = tmp_path / "state"
    shadow_dir = tmp_path / "shadow"
    _write_csv(state_dir / "equity.csv", [("2026-08-14", 8000.0)])
    _write_csv(shadow_dir / "equity-shadow.csv", [("2026-08-01", 8000.0)])

    assert integrity_check.check(state_dir, shadow_dir) == []


def test_main_expected_peak_bajo_dispara_divergencia_rc3(tmp_path):
    state_dir = tmp_path / "state"
    shadow_dir = tmp_path / "shadow"
    _write_csv(state_dir / "equity.csv", [("2026-08-14", 8000.0)])
    _write_csv(shadow_dir / "equity-shadow.csv", [("2026-08-01", 8000.0)])

    rc = integrity_check.main(
        [
            "--state-dir", str(state_dir),
            "--shadow-dir", str(shadow_dir),
            "--expected-peak", "10000",
        ]
    )

    assert rc == 3


def test_main_expected_peak_creciente_rc0(tmp_path):
    state_dir = tmp_path / "state"
    shadow_dir = tmp_path / "shadow"
    _write_csv(state_dir / "equity.csv", [("2026-08-14", 10050.0)])
    _write_csv(shadow_dir / "equity-shadow.csv", [("2026-08-01", 10000.0)])

    rc = integrity_check.main(
        [
            "--state-dir", str(state_dir),
            "--shadow-dir", str(shadow_dir),
            "--expected-peak", "10000",
        ]
    )

    assert rc == 0


def test_main_print_peak_imprime_el_pico_combinado(tmp_path, capsys):
    state_dir = tmp_path / "state"
    shadow_dir = tmp_path / "shadow"
    _write_csv(state_dir / "equity.csv", [("2026-08-01", 900.0)])
    _write_csv(shadow_dir / "equity-shadow.csv", [("2026-08-01", 1000.0)])

    rc = integrity_check.main(
        ["--state-dir", str(state_dir), "--shadow-dir", str(shadow_dir), "--print-peak"]
    )

    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == "1000.0"


def test_main_print_peak_sin_datos_imprime_vacio_y_rc0(tmp_path, capsys):
    state_dir = tmp_path / "state"
    shadow_dir = tmp_path / "shadow"

    rc = integrity_check.main(
        ["--state-dir", str(state_dir), "--shadow-dir", str(shadow_dir), "--print-peak"]
    )

    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == ""


def test_main_print_peak_nunca_falla_ante_datos_corruptos(tmp_path, capsys):
    state_dir = tmp_path / "state"
    shadow_dir = tmp_path / "shadow"
    state_dir.mkdir(parents=True)
    (state_dir / "equity.csv").write_text("esto no es un csv valido de verdad\n\x00\x01")

    rc = integrity_check.main(
        ["--state-dir", str(state_dir), "--shadow-dir", str(shadow_dir), "--print-peak"]
    )

    assert rc == 0


# --- WP8b, fix 2: rc=1 (verificador ilegible) -------------------------


def test_main_rc1_cuando_el_archivo_es_irrecuperable(tmp_path):
    # Ambos lados deben EXISTIR simétricamente para que check_equity
    # llegue a leer el CONTENIDO del archivo (si solo uno existiera, se
    # detecta como ausencia asimétrica -- una divergencia real, rc=3 --
    # antes de intentar leer nada). Con permiso 000 sobre el archivo de
    # equity, la lectura de su contenido lanza PermissionError, no
    # capturada dentro de check() -- exit 1, nunca una divergencia real.
    state_dir = tmp_path / "state"
    shadow_dir = tmp_path / "shadow"
    _write_csv(state_dir / "equity.csv", [("2026-08-01", 1000.0)])
    _write_csv(shadow_dir / "equity-shadow.csv", [("2026-08-01", 1000.0)])
    equity_path = state_dir / "equity.csv"
    equity_path.chmod(0o000)

    try:
        rc = integrity_check.main(["--state-dir", str(state_dir), "--shadow-dir", str(shadow_dir)])
        assert rc == 1
    finally:
        equity_path.chmod(0o644)

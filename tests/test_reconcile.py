"""Tests de scripts/reconcile.py (WP4/N3b, re-auditoría).

Antes de este script, borrar state/.needs_reconciliation dependía del
honor system: PLAYBOOK.md (paso 4 del protocolo) instruía al agente a
borrarlo "a mano" tras journalear una entrada RECONCILIACION, pero nada
verificaba que esa entrada REALMENTE existiera antes del borrado -- nada
impedía `rm state/.needs_reconciliation` sin haber reconciliado nada (y,
tras WP4/N3c, risk_hook.py bloquea justamente ese `rm` por Bash, dejando
`reconcile.py --done` como la única vía).

`--done` exige, ANTES de borrar el flag:
  1. Que el flag exista y tenga un campo "at" parseable.
  2. Que state/journal.md tenga una línea con el FORMATO real de una
     entrada de journal "- <timestamp> RECONCILIACION | ..." (no alcanza
     con que la palabra aparezca en cualquier lado -- el mensaje de
     bloqueo de place_order.py la menciona como PROSA dentro de una línea
     BLOQUEADA, lo cual NO cuenta) con timestamp POSTERIOR al "at".
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import reconcile  # noqa: E402


def _write_flag(state_dir: Path, at: str, reason: str = "corrida equities abortada (claude exit 1)"):
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / ".needs_reconciliation").write_text(
        json.dumps({"reason": reason, "log": "reports/x.log", "at": at})
    )


def _journal_line(ts: str, kind: str, rest: str = "detalle") -> str:
    return f"- {ts} {kind} | {rest}\n"


# -- Caso feliz --------------------------------------------------------------


def test_done_borra_flag_si_hay_entrada_reconciliacion_posterior(tmp_path):
    state_dir = tmp_path / "state"
    _write_flag(state_dir, "2026-08-12 20:15 -0300")
    (state_dir / "journal.md").write_text(
        _journal_line("2026-08-12 20:15 -0300", "ABORTADA", "corrida abortada")
        + _journal_line("2026-08-12 20:20 -0300", "RECONCILIACION", "sin discrepancias")
    )

    rc = reconcile.main(["--done"], state_dir=state_dir)

    assert rc == 0
    assert not (state_dir / ".needs_reconciliation").exists()


def test_done_sin_flag_no_hace_nada_exit_0(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    rc = reconcile.main(["--done"], state_dir=state_dir)
    assert rc == 0


# -- Bloqueos: no se borra el flag sin evidencia real ------------------------


def test_done_sin_entrada_reconciliacion_no_borra_flag(tmp_path, capsys):
    state_dir = tmp_path / "state"
    _write_flag(state_dir, "2026-08-12 20:15 -0300")
    (state_dir / "journal.md").write_text(
        _journal_line("2026-08-12 20:15 -0300", "ABORTADA", "corrida abortada")
    )

    rc = reconcile.main(["--done"], state_dir=state_dir)

    assert rc == 1
    assert (state_dir / ".needs_reconciliation").exists()
    assert "RECONCILIACION" in capsys.readouterr().err


def test_done_falso_positivo_mensaje_de_bloqueo_no_cuenta(tmp_path):
    """El mensaje BLOQUEADA de place_order.py menciona la palabra
    "RECONCILIACION" como PROSA (ver su docstring) -- una entrada así NO
    debe contar como la entrada RECONCILIACION real que exige el
    protocolo. Sin esto, reconcile.py --done podría borrar el flag sin que
    nadie haya reconciliado nada de verdad."""
    state_dir = tmp_path / "state"
    _write_flag(state_dir, "2026-08-12 20:15 -0300")
    mensaje_bloqueo = (
        "reconciliación pendiente tras corrida abortada: revisá posiciones vs "
        "journal, journaleá RECONCILIACION y eliminá state/.needs_reconciliation"
    )
    (state_dir / "journal.md").write_text(
        _journal_line("2026-08-12 20:15 -0300", "ABORTADA", "corrida abortada")
        + _journal_line("2026-08-12 20:30 -0300", "BLOQUEADA", mensaje_bloqueo)
    )

    rc = reconcile.main(["--done"], state_dir=state_dir)

    assert rc == 1
    assert (state_dir / ".needs_reconciliation").exists()


def test_done_entrada_reconciliacion_anterior_al_flag_no_alcanza(tmp_path):
    # La entrada RECONCILIACION existe pero es de ANTES del aborto que
    # generó el flag -- no prueba que ESTA reconciliación se haya hecho.
    state_dir = tmp_path / "state"
    _write_flag(state_dir, "2026-08-12 20:15 -0300")
    (state_dir / "journal.md").write_text(
        _journal_line("2026-08-11 10:00 -0300", "RECONCILIACION", "reconciliación vieja")
        + _journal_line("2026-08-12 20:15 -0300", "ABORTADA", "corrida abortada")
    )

    rc = reconcile.main(["--done"], state_dir=state_dir)

    assert rc == 1
    assert (state_dir / ".needs_reconciliation").exists()


def test_done_entrada_reconciliacion_exactamente_en_el_mismo_timestamp_no_alcanza(tmp_path):
    # Estrictamente POSTERIOR, no >=: un timestamp igual al del aborto no
    # prueba que la reconciliación haya pasado DESPUÉS del aborto.
    state_dir = tmp_path / "state"
    _write_flag(state_dir, "2026-08-12 20:15 -0300")
    (state_dir / "journal.md").write_text(
        _journal_line("2026-08-12 20:15 -0300", "RECONCILIACION", "misma hora")
    )

    rc = reconcile.main(["--done"], state_dir=state_dir)

    assert rc == 1
    assert (state_dir / ".needs_reconciliation").exists()


def test_done_flag_corrupto_no_borra(tmp_path, capsys):
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / ".needs_reconciliation").write_text("{esto no es json")

    rc = reconcile.main(["--done"], state_dir=state_dir)

    assert rc == 1
    assert (state_dir / ".needs_reconciliation").exists()
    assert "ilegible" in capsys.readouterr().err.lower()


def test_done_flag_sin_campo_at_no_borra(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / ".needs_reconciliation").write_text(json.dumps({"reason": "x"}))

    rc = reconcile.main(["--done"], state_dir=state_dir)

    assert rc == 1
    assert (state_dir / ".needs_reconciliation").exists()


def test_done_sin_journal_no_borra(tmp_path):
    # journal.md ni siquiera existe todavía -- no puede haber una entrada
    # RECONCILIACION real.
    state_dir = tmp_path / "state"
    _write_flag(state_dir, "2026-08-12 20:15 -0300")
    assert not (state_dir / "journal.md").exists()

    rc = reconcile.main(["--done"], state_dir=state_dir)

    assert rc == 1
    assert (state_dir / ".needs_reconciliation").exists()


def test_done_usa_la_entrada_reconciliacion_mas_reciente(tmp_path):
    # Si hay varias entradas RECONCILIACION posteriores al flag, alcanza
    # con que exista al menos una -- no hace falta que sea justo la última
    # línea del journal.
    state_dir = tmp_path / "state"
    _write_flag(state_dir, "2026-08-12 20:15 -0300")
    (state_dir / "journal.md").write_text(
        _journal_line("2026-08-12 20:20 -0300", "RECONCILIACION", "primera")
        + _journal_line("2026-08-12 20:25 -0300", "DRY_RUN", "open QQQ")
    )

    rc = reconcile.main(["--done"], state_dir=state_dir)

    assert rc == 0
    assert not (state_dir / ".needs_reconciliation").exists()

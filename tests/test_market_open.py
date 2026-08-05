import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import market_open


NY = ZoneInfo("America/New_York")


def _ny(*args) -> datetime:
    return datetime(*args, tzinfo=NY)


def test_miercoles_mediodia_abierto():
    assert market_open.is_market_open(_ny(2026, 8, 5, 12, 0)) is True


def test_sabado_cerrado():
    assert market_open.is_market_open(_ny(2026, 8, 8, 12, 0)) is False


def test_miercoles_antes_de_apertura_cerrado():
    assert market_open.is_market_open(_ny(2026, 8, 5, 8, 0)) is False


def test_miercoles_despues_de_cierre_cerrado():
    assert market_open.is_market_open(_ny(2026, 8, 5, 16, 30)) is False


def test_borde_apertura_9_30_abierto():
    assert market_open.is_market_open(_ny(2026, 8, 5, 9, 30)) is True


def test_borde_cierre_16_00_abierto():
    assert market_open.is_market_open(_ny(2026, 8, 5, 16, 0)) is True

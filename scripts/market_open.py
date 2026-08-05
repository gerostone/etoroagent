#!/usr/bin/env python3
"""Exit 0 si NYSE está abierto ahora (aprox: lun-vie 9:30-16:00 ET, sin feriados).

Limitación asumida (spec §8): no valida feriados de NYSE; en feriado la corrida
igual termina sin operar porque los datos no cambian y el playbook manda no operar
ante señales sin novedad. Stdlib-only (corre con python3 del sistema).
"""
import sys
from datetime import datetime
from zoneinfo import ZoneInfo


def is_market_open(now=None) -> bool:
    now = now or datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= minutes <= 16 * 60


def main() -> int:
    if is_market_open():
        print("mercado abierto")
        return 0
    print("mercado cerrado: fin de semana o fuera de horario NYSE")
    return 1


if __name__ == "__main__":
    sys.exit(main())

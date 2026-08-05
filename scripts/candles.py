#!/usr/bin/env python3
"""candles.py — helper de SOLO LECTURA autorizado para consultar velas.

Existe para que el agente pueda leer precios/velas de un símbolo sin
recurrir a `python -c`/heredoc con `EtoroClient` inline — eso lo bloquea a
propósito `scripts/risk_hook.py` (ver su docstring). En vez de eso, esta es
la tercera vía autorizada junto a `scripts/place_order.py` (escritura) y
`scripts/snapshot.py` (estado del portfolio): resuelve un símbolo con
`EtoroClient.search_instrument` (con fallback a variantes de símbolo para
cripto — ver `risk.CRYPTO_SEARCH_VARIANTS`) y descarga sus velas con
`EtoroClient.get_candles`, sin llamar nunca a
`open_position_by_amount`/`close_position`.

Uso:
    .venv/bin/python scripts/candles.py --symbol SPY --count 210 \
        [--interval OneDay] [--full]

Por DEFAULT (sin --full) imprime a stdout un formato CSV compacto, en
orden ASCENDENTE (viejo→nuevo) — la API entrega las velas en
direction=desc (nuevo→viejo, default de `EtoroClient.get_candles`), así
que este script las reordena antes de imprimir. Formato:

    # symbol=SPY interval=OneDay count=210 order=asc
    2026-01-02T00:00:00Z,410.23
    2026-01-03T00:00:00Z,412.10
    ...

La primera línea es un comentario de metadata (empieza con "#"); cada línea
siguiente es "<fromDate>,<close>" de una vela, de la más vieja a la más
reciente. Este formato es ~10x más liviano que el JSON crudo (descarta
open/high/low/volume y la estructura anidada de la respuesta) — pensado
para que el agente lo lea directo sin parsear JSON para calcular señales
(ver PLAYBOOK.md §Señales: **siempre verificar el header `order=asc`**
antes de indexar la lista de closes).

Con `--full` imprime el JSON crudo de siempre:
    {"symbol": "SPY", "instrumentId": 123, "candles": {...respuesta cruda...}}

Fail-closed: cualquier error (símbolo no encontrado, ambiguo, formato de
velas inesperado, error HTTP, etc.) escribe a stderr y termina con exit 1,
sin imprimir nada a stdout.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from etoro_api import (  # noqa: E402
    AmbiguousMatchError,
    EtoroClient,
    extract_exact_match,
)
from risk import CRYPTO_SEARCH_VARIANTS  # noqa: E402


def make_client() -> EtoroClient:
    """Factory del cliente HTTP real. Mockeable: main() acepta un make_client
    alternativo (p.ej. lambda: MagicMock()) para tests sin red."""
    return EtoroClient()


def _resolve_instrument_id(client, symbol: str):
    """Mismo criterio que place_order.py::_resolve_instrument_id: match
    exacto vía etoro_api.extract_exact_match(), fail-closed ante 0 o >1
    matches (una ambigüedad no se resuelve arbitrariamente al primero).

    Fallback de variantes cripto (Task 10, fix reviewer): si la búsqueda
    exacta del símbolo pedido no da NINGÚN match, y el símbolo es una clave
    conocida de `risk.CRYPTO_SEARCH_VARIANTS` (eToro puede exponer el
    instrumento bajo un formato distinto, p.ej. BTCUSD en vez de BTC), se
    prueban las variantes conocidas EN ORDEN y se usa el primer match
    EXACTO y NO AMBIGUO — una variante ambigua o sin match se descarta (no
    se adivina) y se sigue probando con la siguiente. Un símbolo ambiguo en
    la búsqueda ORIGINAL no dispara el fallback: sigue siendo un error
    inmediato (la ambigüedad no es un problema de "formato de símbolo
    distinto", así que no tiene sentido probar variantes para resolverla).
    """
    try:
        match = extract_exact_match(client.search_instrument(symbol), symbol)
        return match["internalInstrumentId"]
    except AmbiguousMatchError:
        raise
    except ValueError:
        pass  # NoExactMatchError (u otro ValueError del helper): probar variantes.

    for variant in CRYPTO_SEARCH_VARIANTS.get(symbol, []):
        if variant == symbol:
            continue
        try:
            match = extract_exact_match(client.search_instrument(variant), variant)
        except ValueError:
            continue  # sin match o ambigua en esta variante: probar la siguiente.
        print(
            f"INFO: {symbol} resuelto via variante de busqueda {variant}",
            file=sys.stderr,
        )
        return match["internalInstrumentId"]

    raise ValueError(f"símbolo {symbol} no encontrado en search_instrument")


def _extract_candle_list(candles_resp) -> list:
    """Extrae la lista plana de velas del JSON crudo de get_candles (ver
    docs/api-notes.md: resp['candles'][0]['candles']). Fail-closed ante
    shape inesperado — nunca asume una lista vacía como default silencioso."""
    try:
        return candles_resp["candles"][0]["candles"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"formato de velas inesperado: {exc}") from exc


def _closes_ascending_csv(symbol: str, interval: str, candles_resp) -> str:
    """Formato default (ver docstring del módulo): header de metadata en
    comentario + una línea "fromDate,close" por vela, en orden ASCENDENTE.

    La API entrega en direction=desc (nuevo->viejo — default de
    EtoroClient.get_candles, que este script no sobreescribe), así que acá
    se invierte la lista antes de imprimir. El orden ascendente es el que
    necesitan los cálculos de PLAYBOOK.md (momentum N-velas-atrás, SMA):
    con la lista ascendente, el índice -1 es "hoy" y el índice -(N+1) es
    "hace N velas"."""
    candle_list = _extract_candle_list(candles_resp)
    ascending = list(reversed(candle_list))
    lines = [f"# symbol={symbol} interval={interval} count={len(ascending)} order=asc"]
    for candle in ascending:
        lines.append(f"{candle.get('fromDate')},{candle.get('close')}")
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="candles.py")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--count", required=True, type=int)
    parser.add_argument("--interval", default="OneDay")
    parser.add_argument(
        "--full",
        action="store_true",
        help="imprime el JSON crudo de la API en vez del CSV compacto (default)",
    )
    return parser


def main(argv=None, make_client=make_client) -> int:
    if argv is None:
        argv = sys.argv[1:]
    args = _build_parser().parse_args(argv)
    symbol = args.symbol.strip().upper()

    try:
        client = make_client()
        instrument_id = _resolve_instrument_id(client, symbol)
        candles_resp = client.get_candles(
            instrument_id, interval=args.interval, count=args.count
        )
        if args.full:
            print(
                json.dumps(
                    {"symbol": symbol, "instrumentId": instrument_id, "candles": candles_resp}
                )
            )
        else:
            print(_closes_ascending_csv(symbol, args.interval, candles_resp))
        return 0
    except Exception as exc:
        print(f"ERROR en candles: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

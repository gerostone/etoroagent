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

Validación de cantidad de velas (WP3 auditoría, ver
docs/verificacion-mom126.md): si la API devuelve menos velas de las
pedidas, se imprime un WARNING a stderr con ambas cantidades y, en el modo
default, el header del CSV agrega `requested=<N>` para dejar constancia.
Si además ese faltante deja menos de MIN_CANDLES_FOR_SIGNALS (130) velas
disponibles Y se habían pedido --count >= 130 (el caso real siempre pide
210 — ver PLAYBOOK.md §Señales, mom126 necesita el índice -127), la
corrida falla cerrado (exit 1, sin stdout): sin datos suficientes no hay
señal confiable. Pedidos deliberadamente chicos (--count < 130, p.ej. para
inspeccionar unas pocas velas con --full) no disparan ese piso — ahí el
llamador ya sabe que no está pidiendo datos para señales.

`--count` fuera de [1, 1000] o no entero es un error de uso, no de datos:
exit code 2 (vía argparse), no el exit 1 de fail-closed.
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


# Piso de velas para que mom126 (PLAYBOOK.md §Señales) sea confiable: la
# fórmula usa el índice -127 (127 velas atrás), así que hacen falta al
# menos 130 velas de margen. Solo aplica cuando --count pedido ya apuntaba
# a ese piso (>=130, el caso real: PLAYBOOK.md siempre pide --count 210) —
# pedidos deliberadamente chicos no lo disparan (ver docstring del módulo,
# y docs/verificacion-mom126.md para el detalle de la investigación WP3).
MIN_CANDLES_FOR_SIGNALS = 130


def _count_type(value: str) -> int:
    """Validador de --count para argparse: entero en [1, 1000]. Cualquier
    otro valor es un error de USO (símbolo/flags mal pasados), no un
    fail-closed de datos -> argparse lo convierte en exit code 2
    (ArgumentTypeError), distinto del exit 1 de fail-closed por datos
    insuficientes/formato inesperado."""
    try:
        count = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--count debe ser un entero, se recibio: {value!r}"
        )
    if not (1 <= count <= 1000):
        raise argparse.ArgumentTypeError(
            f"--count debe estar entre 1 y 1000, se recibio: {count}"
        )
    return count


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


def _closes_ascending_csv(
    symbol: str, interval: str, candle_list: list, requested_count: int
) -> str:
    """Formato default (ver docstring del módulo): header de metadata en
    comentario + una línea "fromDate,close" por vela, en orden ASCENDENTE.

    `candle_list` ya viene extraído por el caller (ver _extract_candle_list
    en main()) en direction=desc (nuevo->viejo — default de
    EtoroClient.get_candles, que este script no sobreescribe), así que acá
    se invierte la lista antes de imprimir. El orden ascendente es el que
    necesitan los cálculos de PLAYBOOK.md (momentum N-velas-atrás, SMA):
    con la lista ascendente, el índice -1 es "hoy" y el índice -(N+1) es
    "hace N velas".

    Si `len(candle_list) < requested_count` (la API devolvió menos velas
    de las pedidas — main() ya emitió el WARNING a stderr), el header deja
    constancia agregando `requested=<requested_count>` (WP3 auditoría, ver
    docs/verificacion-mom126.md)."""
    ascending = list(reversed(candle_list))
    actual_count = len(ascending)
    if actual_count < requested_count:
        header = (
            f"# symbol={symbol} interval={interval} count={actual_count} "
            f"requested={requested_count} order=asc"
        )
    else:
        header = f"# symbol={symbol} interval={interval} count={actual_count} order=asc"
    lines = [header]
    for candle in ascending:
        lines.append(f"{candle.get('fromDate')},{candle.get('close')}")
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="candles.py")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--count", required=True, type=_count_type)
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
        candle_list = _extract_candle_list(candles_resp)
        actual_count = len(candle_list)
        if actual_count < args.count:
            print(
                f"ADVERTENCIA: se pidieron {args.count} velas, la API devolvio {actual_count}",
                file=sys.stderr,
            )
        if args.count >= MIN_CANDLES_FOR_SIGNALS and actual_count < MIN_CANDLES_FOR_SIGNALS:
            raise ValueError(
                "datos insuficientes para señales (mom126 usa el indice -127, "
                f"piso de {MIN_CANDLES_FOR_SIGNALS} velas): se pidieron "
                f"{args.count}, la API devolvio apenas {actual_count}"
            )
        if args.full:
            print(
                json.dumps(
                    {"symbol": symbol, "instrumentId": instrument_id, "candles": candles_resp}
                )
            )
        else:
            print(_closes_ascending_csv(symbol, args.interval, candle_list, args.count))
        return 0
    except Exception as exc:
        print(f"ERROR en candles: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

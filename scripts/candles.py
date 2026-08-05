"""candles.py — helper de SOLO LECTURA autorizado para consultar velas.

Existe para que el agente pueda leer precios/velas de un símbolo sin
recurrir a `python -c`/heredoc con `EtoroClient` inline — eso lo bloquea a
propósito `scripts/risk_hook.py` (ver su docstring). En vez de eso, esta es
la tercera vía autorizada junto a `scripts/place_order.py` (escritura) y
`scripts/snapshot.py` (estado del portfolio): resuelve un símbolo con
`EtoroClient.search_instrument` y descarga sus velas con
`EtoroClient.get_candles`, sin llamar nunca a
`open_position_by_amount`/`close_position`.

Uso:
    .venv/bin/python scripts/candles.py --symbol SPY --count 210 \
        [--interval OneDay]

Imprime a stdout un único JSON:
    {"symbol": "SPY", "instrumentId": 123, "candles": {...respuesta cruda...}}

Fail-closed: cualquier error (símbolo no encontrado, ambiguo, error HTTP,
etc.) escribe a stderr y termina con exit 1, sin imprimir nada a stdout.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from etoro_api import EtoroClient  # noqa: E402


def make_client() -> EtoroClient:
    """Factory del cliente HTTP real. Mockeable: main() acepta un make_client
    alternativo (p.ej. lambda: MagicMock()) para tests sin red."""
    return EtoroClient()


def _resolve_instrument_id(client, symbol: str):
    """Mismo criterio que place_order.py::_resolve_instrument_id: match
    exacto de internalSymbolFull, fail-closed ante 0 o >1 matches (una
    ambiguedad de metadata no se resuelve arbitrariamente al primero)."""
    resp = client.search_instrument(symbol)
    items = (resp.get("items") if isinstance(resp, dict) else None) or (
        resp.get("instruments") if isinstance(resp, dict) else None
    ) or []
    matches = [
        item
        for item in items
        if str(item.get("internalSymbolFull", "")).strip().upper() == symbol
    ]
    if not matches:
        raise ValueError(f"símbolo {symbol} no encontrado en search_instrument")
    if len(matches) > 1:
        raise ValueError(
            f"{len(matches)} matches exactos para {symbol} en search_instrument "
            f"(ambiguo, no se toma el primero silenciosamente): {matches}"
        )
    return matches[0]["instrumentId"]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="candles.py")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--count", required=True, type=int)
    parser.add_argument("--interval", default="OneDay")
    return parser


def main(argv=None, make_client=make_client) -> int:
    if argv is None:
        argv = sys.argv[1:]
    args = _build_parser().parse_args(argv)
    symbol = args.symbol.strip().upper()

    try:
        client = make_client()
        instrument_id = _resolve_instrument_id(client, symbol)
        candles = client.get_candles(
            instrument_id, interval=args.interval, count=args.count
        )
        print(
            json.dumps(
                {"symbol": symbol, "instrumentId": instrument_id, "candles": candles}
            )
        )
        return 0
    except Exception as exc:
        print(f"ERROR en candles: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

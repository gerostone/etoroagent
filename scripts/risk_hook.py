#!/usr/bin/env python3
"""Hook PreToolUse (Bash): bloquea escrituras directas a la API de eToro.

Defensa en profundidad: el agente de trading es Claude Code headless y
podria, por error o por seguir instrucciones inyectadas, intentar ejecutar
una orden por fuera de `scripts/place_order.py` (que valida los limites de
riesgo de `scripts/risk.py`). Este hook intercepta cada tool call Bash y
bloquea cualquier via alternativa: curl/wget/httpie contra los endpoints de
escritura de la API, o uso inline de `EtoroClient` desde `python -c`/heredoc.

Complementa (no reemplaza) las reglas de PLAYBOOK.md. Bypass residual
aceptado (no cerrado por este hook): escribir un script .py nuevo a disco y
ejecutarlo. Se mitiga con PLAYBOOK.md (que exige que `scripts/place_order.py`
sea la unica via de escritura, con su propia validacion interna de riesgo)
y con revision humana del journal — no hay forma de cerrar ese bypass desde
un hook que solo inspecciona el texto del comando Bash.

Matching robusto contra ofuscacion de shell: antes de correr los patrones,
generamos una copia NORMALIZADA del comando (sin comillas simples/dobles ni
backslashes) porque bash concatena literales quoted adyacentes sin
separador al sacar las comillas (p.ej. "trad""ing" -> trading). Sin esto,
alguien podria partir cualquier substring vigilado (dominio, endpoint,
EtoroClient) en pedazos quoted y evadir el matching por texto plano. Todos
los patrones corren tanto sobre el comando crudo como sobre el normalizado;
basta que cualquiera de los dos matchee para bloquear.

Ademas, ante la API de eToro, un metodo HTTP no-literal (variable de shell
como -X "$METHOD", o cualquier valor que no sea exactamente GET/HEAD)
se trata como escritura y se bloquea fail-closed: no podemos confirmar que
sea una lectura segura, asi que asumimos lo peor.

Contrato de hooks de Claude Code: JSON por stdin
`{"tool_name": "Bash", "tool_input": {"command": "..."}}`.
Exit 0 = permitir. Exit 2 = bloquear (stderr se muestra al agente).

Stdlib-only: este script corre con el python3 del SISTEMA, fuera del venv
del proyecto (asi lo invoca `.claude/settings.json`).
"""
import json
import re
import sys

MENSAJE = (
    "BLOQUEADO por el motor de riesgo (scripts/risk_hook.py): las ordenes de "
    "trading en eToro solo pueden ejecutarse via "
    "`.venv/bin/python scripts/place_order.py` (valida limites de riesgo) o "
    "consultarse via `scripts/snapshot.py`. No se permite invocar la API de "
    "escritura ni el cliente EtoroClient por ninguna otra via (curl/wget/"
    "httpie, python -c, heredoc, etc). Si necesitas operar, usa "
    "scripts/place_order.py o scripts/snapshot.py.\n"
)

API_DOMAIN = "public-api.etoro.com"

# Quitamos comillas y backslashes para neutralizar el quote-splitting de
# bash: "trad""ing" y 'trad''ing' se leen como "trading" una vez que el
# shell las concatena; si buscaramos el substring solo en el texto crudo,
# alguien podria partir cualquier patron vigilado en pedazos quoted.
_QUOTE_CHARS = "'\"\\"
_NORMALIZAR_TABLE = str.maketrans("", "", _QUOTE_CHARS)


def _normalizar(command: str) -> str:
    return command.translate(_NORMALIZAR_TABLE)


# Indicadores de payload de escritura (curl/wget/httpie u otro cliente
# HTTP). Cualquiera de estos, combinado con el dominio de la API, implica
# una escritura por fuera de place_order.py — independiente del metodo.
_DATA_INDICATORS_RE = re.compile(
    r"("
    r"--data-raw\b"
    r"|--data-binary\b"
    r"|--data\b"
    r"|-d\s"
    r"|--json\b"
    r"|-F\s"
    r"|--form\b"
    r"|--post-data\b"
    r")",
    re.IGNORECASE,
)

# Flags de metodo HTTP: -X, --request, --method. Capturamos el valor tal
# cual aparece (pegado o separado, con "=" o espacio) para evaluarlo
# despues: solo GET/HEAD literales se consideran lectura segura.
_METODO_FLAG_RE = re.compile(
    r"(?:(?<=\s)|^)-X\s*(\S+)"
    r"|--request(?:=|\s+)(\S+)"
    r"|--method(?:=|\s+)(\S+)",
    re.IGNORECASE,
)
_METODOS_SEGUROS = {"GET", "HEAD"}
_VALOR_STRIP_CHARS = "'\"\\;&|"


def _metodo_no_es_lectura_segura(command: str) -> bool:
    """True si aparece -X/--request/--method con un valor que no sea
    literalmente GET/HEAD: variables de shell ($METHOD), valores partidos
    por comillas, o cualquier cosa no reconocible se tratan fail-closed
    como escritura (no podemos confirmar que sea una lectura segura)."""
    for match in _METODO_FLAG_RE.finditer(command):
        valor = next((g for g in match.groups() if g), "").strip(_VALOR_STRIP_CHARS)
        if valor.upper() not in _METODOS_SEGUROS:
            return True
    return False


def _es_escritura_directa_a_api(command: str) -> bool:
    if API_DOMAIN not in command.lower():
        return False
    if _DATA_INDICATORS_RE.search(command):
        return True
    return _metodo_no_es_lectura_segura(command)


# Referencias directas a los endpoints/metodos de trading, sin importar el
# dominio ni el cliente HTTP usado: si el comando los menciona, se bloquea.
# "Mejor cerrado que abierto" (ver Task 6 en el plan).
_ENDPOINT_TRADING_RE = re.compile(
    r"(market-open-orders|market-close-orders|trading/execution"
    r"|open_position_by_amount|close_position\()",
    re.IGNORECASE,
)

# Uso inline del cliente Python (EtoroClient) fuera de los scripts
# autorizados: python -c / python3 -c / heredoc con EtoroClient(...).
_ETORO_CLIENT_RE = re.compile(r"EtoroClient\s*\(")
_PYTHON_INLINE_RE = re.compile(r"python3?\s+(-\w*\s*)*-c\b")


def _referencia_trading_por_cualquier_via(command: str) -> bool:
    if _ENDPOINT_TRADING_RE.search(command):
        return True
    if _ETORO_CLIENT_RE.search(command):
        if _PYTHON_INLINE_RE.search(command) or "<<" in command:
            return True
    return False


def _bloqueado(command: str) -> bool:
    """Corre todos los chequeos sobre UNA variante del comando (cruda o
    normalizada). El llamador decide sobre cuales variantes iterar."""
    return _es_escritura_directa_a_api(command) or _referencia_trading_por_cualquier_via(
        command
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # payload raro/invalido -> no romper la sesion

    if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return 0

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0
    command = tool_input.get("command") or ""
    if not isinstance(command, str):
        return 0

    # Corremos todos los patrones sobre el comando crudo Y sobre la version
    # normalizada (sin comillas/backslashes): alcanza con que cualquiera de
    # las dos variantes matchee para bloquear (ver docstring del modulo).
    if _bloqueado(command) or _bloqueado(_normalizar(command)):
        sys.stderr.write(MENSAJE)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())

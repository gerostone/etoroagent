#!/usr/bin/env python3
"""Hook PreToolUse (Bash): bloquea escrituras directas a la API de eToro.

Defensa en profundidad: el agente de trading es Claude Code headless y
podria, por error o por seguir instrucciones inyectadas, intentar ejecutar
una orden por fuera de `scripts/place_order.py` (que valida los limites de
riesgo de `scripts/risk.py`). Este hook intercepta cada tool call Bash y
bloquea cualquier via alternativa: curl/wget/httpie contra los endpoints de
escritura de la API, uso inline de `EtoroClient`/`requests` desde
`python -c`/heredoc, o referencias directas a los endpoints de trading
cuando aparecen junto a un vehiculo real de ejecucion/red.

Vias de LECTURA autorizadas (no las bloquea, no necesitan caso especial:
su invocacion normal no contiene ningun patron vigilado en el texto del
comando): `scripts/snapshot.py` (estado del portfolio) y
`scripts/candles.py` (velas de un simbolo). La unica via de ESCRITURA es
`scripts/place_order.py`.

Que SI intenta cerrar este hook (ver funciones mas abajo):
  - Escrituras directas a la API (curl/wget con metodo no-GET/HEAD o con
    payload), detectadas por segmento de comando (partido por && || ; —
    a proposito NO por pipe simple, ver _split_segments — respetando
    comillas y heredocs) y evaluadas en 3 variantes de texto:
    cruda, sin comillas/backslashes, y sin comillas+sin operador de
    concatenacion "+" (para no dejarse enganiar por
    "trad""ing" o 'a' + 'b').
  - Referencias a los endpoints/metodos de trading (market-open-orders,
    open_position_by_amount, etc.) o a `EtoroClient(` inline, PERO SOLO
    cuando el segmento tambien contiene un vehiculo real de
    ejecucion/red (curl, wget, http(s), python, eval, xargs, sh -c,
    bash -c) — así `grep -rn market-open-orders`, un mensaje de commit,
    o `git log --grep=...` no bloquean la corrida del agente por mencionar
    el nombre de un endpoint en texto plano.
  - `requests.post/put/patch/delete(...)` (o `.post(`/`.put(`/etc. generico)
    inline dentro de `python -c`/heredoc, cuando el resultado hace
    referencia a eToro (dominio, EtoroClient, o fragmentos de endpoint).

Que NO cierra este hook (bypasses residuales aceptados, ver mas abajo).

Contrato de hooks de Claude Code: JSON por stdin
`{"tool_name": "Bash", "tool_input": {"command": "..."}}`.
Exit 0 = permitir. Exit 2 = bloquear (stderr se muestra al agente).

Stdlib-only: este script corre con el python3 del SISTEMA, fuera del venv
del proyecto (asi lo invoca `.claude/settings.json`).

BYPASSES RESIDUALES ACEPTADOS (no cerrados por este hook, a proposito):
este hook hace matching de texto sobre el comando tal como Claude Code lo
pasa, SIN evaluar el shell. Cerrar todo lo de abajo requeriria evaluar
shell real (arbitrariamente complejo, alto riesgo de falsos positivos que
rompan corridas legitimas) — por eso quedan afuera del alcance, "cerrado
por costo/beneficio":
  - Expansion de variables de shell para reconstruir texto en tiempo de
    ejecucion (`A=mar; B=ket-open-orders; curl .../${A}${A}${B}`... o
    simplemente `${A}${B}`): el valor real solo existe cuando bash lo
    evalua, este hook nunca ejecuta el comando.
  - ANSI-C quoting (`$'...'`, interpreta escapes) para ocultar bytes.
  - Payload codificado y decodificado en la misma linea
    (`echo <base64> | base64 -d | bash`, o similar con `openssl enc`, etc).
  - `eval`/`printf` para reconstruir y ejecutar un comando
    (`eval "$(printf ...)"`).
  - Brace expansion de bash (`{a,b}`, `{1..10}`) para partir literales.
  - Escribir un script .py NUEVO a disco (Write/Edit) y despues
    ejecutarlo: este hook solo mira el texto del comando Bash, no el
    contenido de archivos escritos por otra tool call.
Todo lo anterior se mitiga con PLAYBOOK.md (que exige que
`scripts/place_order.py` sea la unica via de escritura) y con la
validacion de riesgo INTERNA de place_order.py — que corre pase lo que
pase por como fue invocado, no por tener acceso exclusivo a credenciales
(las env vars ETORO_API_KEY/ETORO_USER_KEY estan disponibles para
cualquier proceso del agente, no solo para place_order.py). Es defensa en
profundidad, no una sandbox completa; la revision humana del journal es la
ultima red.
"""
import json
import re
import sys

MENSAJE = (
    "BLOQUEADO por el motor de riesgo (scripts/risk_hook.py): las ordenes de "
    "trading en eToro solo pueden ejecutarse via "
    "`.venv/bin/python scripts/place_order.py` (valida limites de riesgo). "
    "Para leer datos usa `scripts/snapshot.py` (estado del portfolio) o "
    "`scripts/candles.py` (velas de un simbolo). No se permite invocar la API "
    "de escritura, `requests`/`EtoroClient` inline, ni referenciar los "
    "endpoints de trading junto a un vehiculo de ejecucion (curl/wget/http, "
    "python -c, heredoc, eval, xargs, sh -c/bash -c). Si necesitas operar o "
    "leer datos, usa scripts/place_order.py, scripts/snapshot.py o "
    "scripts/candles.py.\n"
)

API_DOMAIN = "public-api.etoro.com"


# -- Normalizacion de texto -------------------------------------------------
#
# Quitamos comillas y backslashes para neutralizar el quote-splitting de
# bash: "trad""ing" y 'trad''ing' se leen como "trading" una vez que el
# shell las concatena; si buscaramos el substring solo en el texto crudo,
# alguien podria partir cualquier patron vigilado en pedazos quoted.
#
# Ademas generamos una tercera variante que TAMBIEN quita el operador de
# concatenacion de strings "+" (con espacios opcionales alrededor): esto
# reconstruye literales partidos por concatenacion en python inline, p.ej.
# 'trad' + 'ing/execution' -> tradinging... no: "trad" seguido inmediato de
# "ing/execution" = "trading/execution" (ver I2 en el docstring del modulo).

_QUOTE_CHARS = "'\"\\"
_NORMALIZAR_TABLE = str.maketrans("", "", _QUOTE_CHARS)
_CONCAT_RE = re.compile(r"\s*\+\s*")


def _normalizar(texto: str) -> str:
    return texto.translate(_NORMALIZAR_TABLE)


def _normalizar_sin_concat(texto: str) -> str:
    return _CONCAT_RE.sub("", _normalizar(texto))


# -- Segmentacion del comando ------------------------------------------------
#
# Partimos el comando por && || ; (comandos verdaderamente independientes)
# respetando comillas (no partimos dentro de un string quoted) y heredocs
# (<<DELIM ... DELIM: el cuerpo completo se trata como parte de un unico
# segmento, nunca se parte por operadores que puedan aparecer dentro de el).
# Evaluar cada segmento por separado evita que un flag de escritura en un
# segmento "contamine" la evaluacion de un segmento de solo lectura distinto
# encadenado con &&/; (y viceversa: un comando encadenado con una escritura
# real sigue bloqueando, porque ese segmento se evalua solo).
#
# A PROPOSITO no partimos por "|" (pipe simple): a diferencia de && / ; / ||,
# un pipe hace fluir datos de un lado al otro del mismo comando logico (p.ej.
# `echo <url-de-trading> | xargs curl -X POST` — el endpoint viaja por stdin
# hacia el lado que realmente ejecuta la escritura). Partir ahi rompería esa
# deteccion sin necesidad: ninguno de los falsos positivos que este hook
# corrige depende de partir por pipe.

_HEREDOC_START_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def _split_segments(command: str) -> list:
    segments = []
    current = []
    quote = None
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            current.append(ch)
            i += 1
            continue
        if command.startswith("<<", i):
            match = _HEREDOC_START_RE.match(command, i)
            if match:
                delim = match.group(2)
                end = match.end()
                current.append(command[i:end])
                i = end
                closing_re = re.compile(r"(?m)^[ \t]*" + re.escape(delim) + r"[ \t]*$")
                closing_match = closing_re.search(command, i)
                if closing_match:
                    current.append(command[i : closing_match.end()])
                    i = closing_match.end()
                else:
                    current.append(command[i:])
                    i = n
                continue
        if command.startswith("&&", i):
            segments.append("".join(current))
            current = []
            i += 2
            continue
        if command.startswith("||", i):
            segments.append("".join(current))
            current = []
            i += 2
            continue
        if ch == ";":
            segments.append("".join(current))
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    segments.append("".join(current))
    return segments


# -- Escritura directa a la API (curl/wget/httpie u otro cliente HTTP) ------
#
# Indicadores de payload SIEMPRE-escritura (formas largas, sin ambiguedad de
# case con otros flags -> case-insensitive esta bien aca).
_ALWAYS_WRITE_RE = re.compile(r"(--json\b|--form\b|--post-data\b)", re.IGNORECASE)

# -F (multipart form) — SOLO mayuscula: -f minuscula es "fail silently" en
# curl, no tiene nada que ver con form-data. Sin IGNORECASE a proposito.
_F_SHORT_RE = re.compile(r"(?:^|\s)-F(?=[\s'\"@]|$)")

# --data*/-d (payload) — neutralizables por -G (ver mas abajo). -d SOLO
# minuscula: -D mayuscula es "dump headers", no data. Sin IGNORECASE.
_G_NEUTRALIZABLE_LONG_RE = re.compile(
    r"(--data-raw\b|--data-binary\b|--data\b)", re.IGNORECASE
)
_D_SHORT_RE = re.compile(r"(?:^|\s)-d(?=[\s'\"@=]|$)")
_D_AT_RE = re.compile(r"(?:^|\s)-d@")  # explicito (M6), redundante con el lookahead de arriba

# -G: curl manda el payload de -d/--data como query string de un GET, en vez
# de en el body -> no es una escritura. SOLO mayuscula (curl real).
_G_FLAG_RE = re.compile(r"(?:^|\s)-G(?=[\s'\"]|$)")


def _tiene_payload_de_escritura(segmento: str) -> bool:
    if _ALWAYS_WRITE_RE.search(segmento):
        return True
    if _F_SHORT_RE.search(segmento):
        return True
    es_data = (
        _G_NEUTRALIZABLE_LONG_RE.search(segmento)
        or _D_SHORT_RE.search(segmento)
        or _D_AT_RE.search(segmento)
    )
    if es_data:
        if _G_FLAG_RE.search(segmento):
            return False  # -G: -d/--data va como query GET, no como body
        return True
    return False


# Flags de metodo HTTP: -X (y combos curl como -sX), --request, --method.
# -X: SOLO mayuscula, anclado, y el caracter siguiente debe ser whitespace,
# "=", otra mayuscula (valor pegado tipo -XPOST), comilla, o fin de string
# — asi "-x proxy" (proxy, minuscula) y palabras sueltas con una X adentro
# no matchean. El combo -[a-zA-Z]*X cubre "-sX POST" (curl flags cortos
# combinados: -s + -X) sin matchear "-x" (minuscula) ni "-sS" (sin X).
_METODO_X_RE = re.compile(r"(?:^|\s)-[a-zA-Z]*X(?=[\s=A-Z\"']|$)(?:=|\s*)(\S*)")
_METODO_REQUEST_RE = re.compile(r"--request(?:=|\s+)(\S+)", re.IGNORECASE)
_METODO_METHOD_RE = re.compile(r"--method(?:=|\s+)(\S+)", re.IGNORECASE)

_METODOS_SEGUROS = {"GET", "HEAD"}
_VALOR_STRIP_CHARS = "'\"\\;&|"


def _valores_de_metodo(segmento: str):
    valores = [m.group(1) for m in _METODO_X_RE.finditer(segmento)]
    valores += [m.group(1) for m in _METODO_REQUEST_RE.finditer(segmento)]
    valores += [m.group(1) for m in _METODO_METHOD_RE.finditer(segmento)]
    return valores


def _metodo_no_es_lectura_segura(segmento: str) -> bool:
    """True si aparece -X/--request/--method con un valor que no sea
    literalmente GET/HEAD: variables de shell ($METHOD), valores partidos
    por comillas, o cualquier cosa no reconocible se tratan fail-closed
    como escritura (no podemos confirmar que sea una lectura segura)."""
    for valor in _valores_de_metodo(segmento):
        limpio = valor.strip(_VALOR_STRIP_CHARS).upper()
        if limpio not in _METODOS_SEGUROS:
            return True
    return False


def _es_escritura_directa_a_api(segmento: str) -> bool:
    if API_DOMAIN not in segmento.lower():
        return False
    if _tiene_payload_de_escritura(segmento):
        return True
    return _metodo_no_es_lectura_segura(segmento)


# -- Referencias a endpoints/cliente de trading, solo con vehiculo real ----
#
# Patrones de endpoints/metodos de trading, sin importar el dominio ni el
# cliente HTTP usado.
_ENDPOINT_TRADING_RE = re.compile(
    r"(market-open-orders|market-close-orders|trading/execution"
    r"|open_position_by_amount|close_position\()",
    re.IGNORECASE,
)

# Uso inline del cliente Python (EtoroClient) fuera de los scripts
# autorizados: python -c / python3 -c / heredoc con EtoroClient(...).
_ETORO_CLIENT_RE = re.compile(r"EtoroClient\s*\(")
_PYTHON_INLINE_RE = re.compile(r"python3?\s+(-\w*\s*)*-c\b")

# Vehiculo real de ejecucion/red: sin uno de estos, una mencion de texto al
# endpoint (grep, mensaje de commit, git log --grep) no es una ejecucion,
# es solo texto -> no bloquea (M8). "http"/"https" NO cuentan si son el
# esquema de una URL (seguidos de "://"): ahi lo que importa es si hay un
# cliente real (curl/wget/etc), no la sola presencia de una URL en texto.
_VEHICULO_RE = re.compile(
    r"\b(curl|wget|python3?|eval|xargs)\b"
    r"|\bhttps?\b(?!://)"
    r"|\b(?:sh|bash)\s+-c\b",
    re.IGNORECASE,
)


def _referencia_trading_con_vehiculo(segmento: str) -> bool:
    if not _VEHICULO_RE.search(segmento):
        return False
    if _ENDPOINT_TRADING_RE.search(segmento):
        return True
    if _ETORO_CLIENT_RE.search(segmento):
        if _PYTHON_INLINE_RE.search(segmento) or "<<" in segmento:
            return True
    return False


# -- requests inline (I2): bypass de EtoroClient usando `requests` directo -

_REQUEST_CALL_RE = re.compile(
    r"requests\.(post|put|patch|delete|request)\s*\(|\.(post|put|patch|delete)\s*\(",
    re.IGNORECASE,
)


def _es_requests_inline_con_etoro(segmento: str) -> bool:
    if not (_PYTHON_INLINE_RE.search(segmento) or "<<" in segmento):
        return False
    if not _REQUEST_CALL_RE.search(segmento):
        return False
    if API_DOMAIN in segmento.lower():
        return True
    if _ETORO_CLIENT_RE.search(segmento):
        return True
    if _ENDPOINT_TRADING_RE.search(segmento):
        return True
    return False


# -- Orquestacion -------------------------------------------------------


def _bloqueado_variante(texto: str) -> bool:
    return (
        _es_escritura_directa_a_api(texto)
        or _referencia_trading_con_vehiculo(texto)
        or _es_requests_inline_con_etoro(texto)
    )


def _bloqueado(command: str) -> bool:
    for segmento in _split_segments(command):
        for variante in (
            segmento,
            _normalizar(segmento),
            _normalizar_sin_concat(segmento),
        ):
            if _bloqueado_variante(variante):
                return True
    return False


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

    if _bloqueado(command):
        sys.stderr.write(MENSAJE)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())

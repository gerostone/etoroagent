#!/usr/bin/env python3
"""Hook PreToolUse (Bash): bloquea escrituras directas a la API de eToro y
escrituras de shell sobre los guardrails del propio agente.

Defensa en profundidad: el agente de trading es Claude Code headless y
podria, por error o por seguir instrucciones inyectadas, intentar ejecutar
una orden por fuera de `scripts/place_order.py` (que valida los limites de
riesgo de `scripts/risk.py`), o reescribir sus propios guardrails (los
scripts, la config de `.claude/`, PLAYBOOK.md, RISK.md, .env) para eludirlos
la proxima corrida. Este hook intercepta cada tool call Bash y bloquea:

  (A) Cualquier via alternativa de ESCRITURA A LA API: curl/wget/httpie
      contra los endpoints de escritura de la API, uso inline de
      `EtoroClient`/`requests` desde `python -c`/heredoc, o referencias
      directas a los endpoints de trading cuando aparecen junto a un
      vehiculo real de ejecucion/red.
  (B) Cualquier ESCRITURA DE SHELL sobre los guardrails (`scripts/*.py`,
      `scripts/*.sh`, `.claude/`, `PLAYBOOK.md`, `RISK.md`, `.env`):
      `permissions.deny` en `.claude/settings.json` ya bloquea Write/Edit
      sobre esas rutas, pero eso NO cubre escribirlas via Bash — `tee`,
      redireccion (`>`/`>>`), `sed -i`, `mv`, `cp`, `install`, `dd of=`,
      `truncate` o `ln` apuntando a esas rutas eluden `permissions.deny`
      por completo (ver Task 7 / Task 10). El journaling normal del agente
      hacia `state/` y `reports/` (que SI usa `tee`/`>>`, ver PLAYBOOK.md
      §Cierre de corrida) sigue permitido: la regla mira el DESTINO de la
      escritura, no la sola presencia de `tee`/`>>` en el comando.

  (C) WP4/N3: escrituras de shell (incluido `rm`) sobre los archivos de
      CONTROL state/.run_orders.json (presupuesto de órdenes) y
      state/.needs_reconciliation (flag de reconciliación). A diferencia
      del resto de state/ (positions.json, equity.csv, journal.md, que el
      agente journalea/lee libremente), estos dos son de solo lectura por
      Bash -- si el agente pudiera `rm state/.needs_reconciliation`, el
      protocolo de reconciliación de PLAYBOOK.md sería honor system puro.
      `scripts/reconcile.py --done` es la única vía autorizada para
      borrar el flag (verifica el journal antes -- ver su docstring).
  (D) WP4/N4a: asignar ETOROAGENT_RUN_ID o ETOROAGENT_STATE_DIR inline
      (export o prefijo VAR=valor) en un comando que además invoque
      scripts/place_order.py, scripts/snapshot.py, scripts/candles.py o
      scripts/reconcile.py. runner.sh setea estas variables por entorno
      heredado, antes de invocar `claude` -- el agente nunca necesita
      asignarlas inline. Si pudiera, resetearía a voluntad el presupuesto
      de órdenes por corrida (WP1) o redirigiría state/ para evadir la
      reconciliación pendiente (WP2) o el presupuesto (WP1).

Vias de LECTURA autorizadas (no las bloquea, no necesitan caso especial:
su invocacion normal no contiene ningun patron vigilado en el texto del
comando): `scripts/snapshot.py` (estado del portfolio) y
`scripts/candles.py` (velas de un simbolo). La unica via de ESCRITURA A LA
API es `scripts/place_order.py`.

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
  - Escrituras de shell (tee/redireccion/sed -i/mv/cp/install/dd/truncate/ln)
    cuyo DESTINO cae dentro de scripts/*.py, scripts/*.sh, .claude/,
    PLAYBOOK.md, RISK.md o .env — ver _escribe_sobre_guardrail() mas abajo.

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
por costo/beneficio". Esto aplica por igual a la regla (A) de escritura a
la API y a la regla (B) de escritura sobre guardrails: ninguna de las dos
evalua el shell, asi que ambas comparten la misma superficie de bypass
(ver tests `test_residual_aceptado_*` en `tests/test_risk_hook.py`, que
documentan y verifican explicitamente que estos casos quedan abiertos):
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
`scripts/place_order.py` sea la unica via de escritura), con la
validacion de riesgo INTERNA de place_order.py — que corre pase lo que
pase por como fue invocado, no por tener acceso exclusivo a credenciales
(las env vars ETORO_API_KEY/ETORO_USER_KEY estan disponibles para
cualquier proceso del agente, no solo para place_order.py) —, y con la
regla (B) de este mismo hook mas `permissions.deny`, que en conjunto
cierran la via mas barata y directa de reescribir los guardrails (tee y
redireccion simples sin ofuscacion). Es defensa en profundidad, no una
sandbox completa; la revision humana del journal es la ultima red.
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

MENSAJE_GUARDRAIL = (
    "BLOQUEADO por el motor de riesgo (scripts/risk_hook.py): los guardrails "
    "(scripts/, .claude/, PLAYBOOK.md, RISK.md, .env) son de solo lectura "
    "para el agente. No se permite escribirlos via Bash (tee, redireccion "
    "> o >>, sed -i, mv, cp, install, dd, truncate, ln) — permissions.deny "
    "ya bloquea Write/Edit sobre esas rutas, esta regla cierra la misma via "
    "por Bash. El journaling normal hacia state/ y reports/ (tee, >>) sigue "
    "permitido: revisa que el DESTINO del comando sea uno de esos dos "
    "directorios, no un guardrail.\n"
)

MENSAJE_ESTADO_PROTEGIDO = (
    "BLOQUEADO por el motor de riesgo (scripts/risk_hook.py): "
    "state/.run_orders.json y state/.needs_reconciliation son archivos de "
    "control -- de solo lectura para el agente via Bash (incluido `rm`). "
    "Para cerrar una reconciliacion pendiente usa "
    "`.venv/bin/python scripts/reconcile.py --done` (verifica el journal "
    "antes de borrar el flag). El journaling normal hacia state/journal.md "
    "y reports/ sigue permitido.\n"
)

MENSAJE_ENV_SPOOF = (
    "BLOQUEADO por el motor de riesgo (scripts/risk_hook.py): no se permite "
    "asignar ETOROAGENT_RUN_ID ni ETOROAGENT_STATE_DIR inline (export o "
    "prefijo VAR=valor) en un comando que invoque scripts/place_order.py, "
    "scripts/snapshot.py, scripts/candles.py o scripts/reconcile.py -- esas "
    "variables las setea runner.sh por entorno heredado, nunca el agente. "
    "Asignarlas inline evade el presupuesto de ordenes por corrida y/o "
    "el aislamiento de state usado en tests.\n"
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


def _enmascarar_citas(segmento: str) -> str:
    """Devuelve una copia de `segmento` con el CONTENIDO (no las comillas en
    si) de cada span citado ('...'/"...") reemplazado por espacios, y los
    heredocs (`<<DELIM ... DELIM`) copiados intactos — mismo criterio de
    quote/heredoc-tracking que `_split_segments`, para que un heredoc con
    comillas sueltas adentro no le arruine la paridad de comillas al resto
    del segmento. Preserva longitud y posiciones exactas de todo lo demas,
    para que los offsets de un `re.finditer` sobre el resultado sigan
    siendo validos como indices contra el `segmento` original.

    Para que hace falta esto (Task 10, fix reviewer): un caracter '>' que
    aparece DENTRO de comillas (p.ej. `--reason "precio > SMA50, ver
    RISK.md"`) NO es una redireccion real — el shell nunca lo interpreta
    como tal, es un caracter literal de un argumento — pero antes de este
    fix `_REDIR_RE` no distinguia esto y lo trataba igual que un '>' real,
    dando falso positivo. `_REDIR_RE` se busca SOLO sobre esta version
    enmascarada (nunca sobre el texto crudo ni sobre las variantes
    normalizadas, que BORRAN las comillas en vez de enmascararlas y
    volverian a confundir un '>' citado con uno real: ver
    `_escribe_sobre_guardrail`).

    A diferencia de `_REDIR_RE`, los operadores-PALABRA (tee/mv/cp/install/
    ln/sed/dd/truncate) no necesitan esto: bash ejecuta un comando llamado
    `"tee"` exactamente igual que uno llamado `tee` (las comillas alrededor
    de una palabra no le cambian el significado, solo evitan que el shell
    interprete METACARACTERES adentro) — por eso esos operadores se siguen
    buscando en el segmento normal (ver `_escribe_sobre_guardrail`)."""
    resultado = list(segmento)
    quote = None
    i = 0
    n = len(segmento)
    while i < n:
        ch = segmento[i]
        if quote:
            if ch == quote:
                quote = None
            else:
                resultado[i] = " "
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if segmento.startswith("<<", i):
            match = _HEREDOC_START_RE.match(segmento, i)
            if match:
                delim = match.group(2)
                end = match.end()
                i = end
                closing_re = re.compile(r"(?m)^[ \t]*" + re.escape(delim) + r"[ \t]*$")
                closing_match = closing_re.search(segmento, i)
                i = closing_match.end() if closing_match else n
                continue
        i += 1
    return "".join(resultado)


# -- Escritura directa a la API (curl/wget/httpie u otro cliente HTTP) ------
#
# Indicadores de payload SIEMPRE-escritura (formas largas, sin ambiguedad de
# case con otros flags -> case-insensitive esta bien aca).
_ALWAYS_WRITE_RE = re.compile(r"(--json\b|--form\b|--post-data\b)", re.IGNORECASE)

# -F (multipart form) — SOLO mayuscula: -f minuscula es "fail silently" en
# curl, no tiene nada que ver con form-data. Sin IGNORECASE a proposito.
#
# N1 (Task 10): -F por si sola NO alcanza. Un comando de otro tipo que use
# "-F" con otro significado (p.ej. `sort -F`) y que ademas mencione el
# dominio de la API en texto plano (p.ej. dentro de un `echo`/`printf` sin
# ningun cliente HTTP real) daba falso positivo. Ahora exigimos que el
# MISMO segmento tenga ademas un cliente HTTP real (curl/wget/http) — ver
# _HTTP_CLIENT_RE — antes de contar "-F" como indicador de payload.
_F_SHORT_RE = re.compile(r"(?:^|\s)-F(?=[\s'\"@]|$)")
_HTTP_CLIENT_RE = re.compile(r"\b(curl|wget)\b|\bhttp\b(?!://)", re.IGNORECASE)

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
    if _F_SHORT_RE.search(segmento) and _HTTP_CLIENT_RE.search(segmento):
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


# -- Escritura de shell sobre guardrails protegidos (Task 10) --------------
#
# `permissions.deny` en .claude/settings.json bloquea Write/Edit sobre
# scripts/**, .claude/**, PLAYBOOK.md y RISK.md — pero SOLO para esas dos
# tools. Nada impide que el agente use el tool Bash para lograr el mismo
# efecto con `tee`, redireccion (`>`/`>>`), `sed -i`, `mv`, `cp`, `install`,
# `dd of=`, `truncate` o `ln` (gap descubierto en Task 7). Esta seccion
# cierra esa via especificamente para las rutas guardrail, SIN bloquear el
# journaling legitimo del agente hacia `state/` y `reports/` (que tambien
# usa estos mismos operadores — ver PLAYBOOK.md §Cierre de corrida).
#
# Estrategia: para cada operador de escritura encontrado en el segmento,
# miramos solo el texto que viene DESPUES de ese operador (nunca antes: asi
# `cat PLAYBOOK.md > reports/copy.txt` no bloquea por la lectura de
# PLAYBOOK.md que aparece del lado izquierdo) y lo cortamos en el primer
# limite que ya no es parte del destino de ESE operador — heredoc (`<<`),
# redireccion de entrada (`<`), pipe (`|`), fin de linea, o puntuacion de
# prosa (`,;:()`) — para que texto de OTRO comando en el mismo segmento (el
# cuerpo de un heredoc journaleado, un comando posterior a un pipe, o una
# frase que menciona la palabra de un operador) no se confunda con el
# destino real. Ejemplos concretos que esto evita (ambos FP reales,
# encontrados en revision):
#   - Journaling legitimo: `tee -a state/journal.md <<'EOF' ... - ejecutado
#     via scripts/place_order.py ... EOF` — sin el corte en `<<`, el cuerpo
#     del heredoc (que menciona "scripts/place_order.py" como texto de la
#     entrada del journal, no como destino de escritura) bloquearia una
#     operacion legitima.
#   - Prosa en un commit message: "...install, dd of=, truncate, ln) cuyo
#     destino cae en scripts/x.py..." — sin el corte en `,`/`:`/`(`/`)`, la
#     palabra "install" usada en prosa (no como comando) mas la mencion de
#     una ruta guardrail mas adelante en la misma linea bloqueaba el commit
#     que describe esta misma regla.
#
# Los operadores-PALABRA (tee/sed -i/mv/cp/install/dd of=/truncate/ln) se
# buscan en las 3 variantes de texto del segmento (cruda/normalizada/sin
# concat), igual que el resto del hook: bash los ejecuta igual esten o no
# citados, asi que las variantes normalizadas siguen sirviendo contra
# quote-splitting del propio operador (p.ej. "te""e" -> tee).
#
# `_REDIR_RE` (`>`/`>>`) es distinto: un '>' SI cambia de significado segun
# este citado o no (citado = caracter literal, no citado = redireccion real
# de shell) — por eso se busca SOLO sobre el segmento crudo con las
# comillas enmascaradas (`_enmascarar_citas`), nunca sobre las variantes
# normalizadas (que borran las comillas y reintroducirian la ambiguedad).
# Ademas, `_REDIR_RE` excluye "->" (flecha, comun en texto de --reason) y
# ">=" (comparacion) — ninguno de los dos es sintaxis de redireccion real
# en bash, y ambos daban falso positivo en --reason con prosa tipo
# "regla -> ver PLAYBOOK.md" o "stop-loss > 12%, ver RISK.md" (dentro o
# fuera de comillas).
#
# Fail-closed deliberado para `mv`/`cp`/`install`/`ln`/`sed -i`/`dd of=`/
# `truncate`: no distinguimos con precision el argumento ORIGEN del
# argumento DESTINO (ambos quedan del lado derecho del operador) — es
# preferible bloquear de mas un uso infrecuente que mencione un guardrail
# como origen, a dejar pasar una escritura real sobre el destino.

_TEE_RE = re.compile(r"\btee\b")
# Nota: "->" NO se excluye: en bash `echo x ->f` es una redireccion real a f.
# (?!=) excluye ">=" (comparacion): una '>' seguida de '=' no es redireccion.
# Ninguna de las dos es sintaxis de redireccion real de bash.
_REDIR_RE = re.compile(r">{1,2}(?!=)")
_SED_I_RE = re.compile(r"\bsed\b[\s\S]{0,60}?-i(?:\.[\w-]+)?\b")
_MV_RE = re.compile(r"\bmv\b")
_CP_RE = re.compile(r"\bcp\b")
_INSTALL_RE = re.compile(r"\binstall\b")
_DD_OF_RE = re.compile(r"\bdd\b[\s\S]{0,60}?\bof=")
_TRUNCATE_RE = re.compile(r"\btruncate\b")
_LN_RE = re.compile(r"\bln\b")

# Operadores-palabra: buscados en el segmento tal cual (y sus variantes
# normalizadas, ver _escribe_sobre_guardrail). _REDIR_RE NO va aca: tiene su
# propio tratamiento (enmascarado de comillas) mas abajo.
_OPERADORES_ESCRITURA_ARCHIVO = (
    _TEE_RE,
    _SED_I_RE,
    _MV_RE,
    _CP_RE,
    _INSTALL_RE,
    _DD_OF_RE,
    _TRUNCATE_RE,
    _LN_RE,
)

_LIMITE_OBJETIVO_RE = re.compile(r"<<|<|\||\n|[,;:()]")

# Rutas guardrail: scripts/*.py y scripts/*.sh (no cualquier archivo bajo
# scripts/, solo codigo), .claude/, PLAYBOOK.md, RISK.md, .env (y
# .env.example, ya que contiene ".env" como substring — no es sensible en
# si mismo pero tampoco hay ninguna razon legitima para que el agente lo
# reescriba via Bash).
_PROTEGIDO_RE = re.compile(
    r"scripts/[^\s]*\.(?:py|sh)"
    r"|\.claude/"
    r"|PLAYBOOK\.md"
    r"|RISK\.md"
    r"|\.env\b"
)


def _region_objetivo(segmento: str, desde: int) -> str:
    resto = segmento[desde:]
    limite = _LIMITE_OBJETIVO_RE.search(resto)
    return resto[: limite.start()] if limite else resto


def _region_apunta_a_guardrail(segmento: str, desde: int) -> bool:
    region = _region_objetivo(segmento, desde)
    return bool(
        _PROTEGIDO_RE.search(region)
        or _PROTEGIDO_RE.search(_normalizar(region))
        or _PROTEGIDO_RE.search(_normalizar_sin_concat(region))
    )


def _escribe_sobre_guardrail(segmento: str) -> bool:
    # Operadores-palabra: sobre las 3 variantes del segmento (indices
    # consistentes: el match y la region objetivo salen de la MISMA
    # variante de texto).
    for variante in (
        segmento,
        _normalizar(segmento),
        _normalizar_sin_concat(segmento),
    ):
        for operador_re in _OPERADORES_ESCRITURA_ARCHIVO:
            for m in operador_re.finditer(variante):
                if _region_apunta_a_guardrail(variante, m.end()):
                    return True

    # Redireccion >/>>: solo sobre el segmento crudo con comillas
    # enmascaradas (ver _enmascarar_citas y el comentario de arriba).
    enmascarado = _enmascarar_citas(segmento)
    for m in _REDIR_RE.finditer(enmascarado):
        if _region_apunta_a_guardrail(segmento, m.end()):
            return True

    return False


# -- Escritura de shell sobre archivos de ESTADO protegidos (WP4/N3c) ------
#
# state/.run_orders.json (presupuesto de ordenes) y
# state/.needs_reconciliation (flag de reconciliacion) son archivos de
# CONTROL, no de journaling libre -- a diferencia del resto de state/
# (positions.json, equity.csv, journal.md), que el agente lee/journalea
# normalmente. Si el agente pudiera borrarlos/reescribirlos por Bash, el
# protocolo de reconciliacion (y el presupuesto de ordenes) serian honor
# system. scripts/reconcile.py --done es la unica via autorizada para
# borrar el flag de reconciliacion (verifica el journal antes).
#
# A diferencia de _PROTEGIDO_RE (guardrails de codigo), aca SI se
# considera `rm` un verbo de escritura -- de hecho el mas relevante: es
# justamente como se evadia el honor system antes de este fix. No se
# agrega `rm` a la lista general de guardrails de codigo a proposito
# (fuera de alcance de este fix especifico).
_ESTADO_PROTEGIDO_RE = re.compile(
    r"state/\.run_orders\.json|state/\.needs_reconciliation"
)
_RM_RE = re.compile(r"\brm\b")

_OPERADORES_ESCRITURA_ESTADO = _OPERADORES_ESCRITURA_ARCHIVO + (_RM_RE,)

# Documentar esta protección (docstrings, PLAYBOOK.md, commits) inevitablemente
# menciona "rm state/.needs_reconciliation" como PROSA -- una convención común
# es envolver ese texto entre backticks (formato markdown de código inline).
# Backtick es ADEMÁS sintaxis real de shell (command substitution): un operador
# mencionado ASÍ (`rm X`) nunca es una invocación real de la forma en que este
# hook necesita detectar (una invocación real jamás envuelve el NOMBRE del
# comando entre backticks). Detectar esto evita el falso positivo sin abrir una
# vía nueva: una sustitución de comando real detrás de backticks ya es, como el
# resto de construcciones de shell no evaluadas por este hook (expansión de
# variables, eval, base64|bash...), un bypass residual aceptado (ver docstring
# del módulo) -- no una protección que este fix relaje.
_BACKTICK_SPAN_RE = re.compile(r"`[^`]*`")


def _enmascarar_cuerpos_de_heredoc(segmento: str) -> str:
    """Reemplaza el CONTENIDO (cuerpo) de cada heredoc (<<DELIM ...
    DELIM) del segmento por espacios, preservando todo lo demas verbatim
    -- incluida la linea `<<DELIM` de apertura (con el operador y su
    destino real, si los hay) y el delimitador de cierre. Un heredoc es
    DATA que el shell redirige a stdin del comando (tipicamente
    cat/tee escribiendo a state/journal.md, o -- en los propios tests de
    este hook -- a un archivo de test), nunca texto de COMANDOS nuevos
    para el resto del segmento. Enmascarar su cuerpo evita que una
    mencion de un operador (p.ej. "rm") como DATO/prosa DENTRO del
    cuerpo (un ejemplo de test, una entrada de journal citando esta
    misma proteccion) se confunda con una invocacion real -- sin afectar
    la deteccion del operador+destino que aparecen ANTES del marcador
    `<<` (esa parte no se toca). Residual aceptado, igual que el resto
    de construcciones de shell no evaluadas por este hook: un heredoc
    piped a `bash`/`sh` SIN `-c` (que SI ejecutaria su cuerpo como
    comandos) queda fuera de esta deteccion -- ver docstring del modulo.
    """
    resultado = list(segmento)
    quote = None
    i = 0
    n = len(segmento)
    while i < n:
        ch = segmento[i]
        if quote:
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if segmento.startswith("<<", i):
            match = _HEREDOC_START_RE.match(segmento, i)
            if match:
                delim = match.group(2)
                end = match.end()
                i = end
                closing_re = re.compile(r"(?m)^[ \t]*" + re.escape(delim) + r"[ \t]*$")
                closing_match = closing_re.search(segmento, i)
                body_end = closing_match.start() if closing_match else n
                for j in range(i, body_end):
                    resultado[j] = " "
                i = closing_match.end() if closing_match else n
                continue
        i += 1
    return "".join(resultado)


def _region_apunta_a_estado_protegido(segmento: str, desde: int) -> bool:
    region = _region_objetivo(segmento, desde)
    return bool(
        _ESTADO_PROTEGIDO_RE.search(region)
        or _ESTADO_PROTEGIDO_RE.search(_normalizar(region))
        or _ESTADO_PROTEGIDO_RE.search(_normalizar_sin_concat(region))
    )


def _escribe_sobre_estado_protegido(segmento: str) -> bool:
    """Mismo mecanismo que _escribe_sobre_guardrail (operador de escritura
    -> region objetivo despues de el -> matchea la ruta protegida), pero
    contra _ESTADO_PROTEGIDO_RE y con `rm` incluido como operador. Un
    operador cuyo propio NOMBRE cae dentro de un span entre backticks se
    descarta (prosa/markdown, no invocación real) -- ver comentario de
    _BACKTICK_SPAN_RE arriba. Un ARGUMENTO citado de una invocación real
    (p.ej. rm "state/.needs_reconciliation", sin backticks sobre `rm`)
    sigue detectándose igual: solo se ignora el operador en sí, no la
    región objetivo que viene después."""
    base = _enmascarar_cuerpos_de_heredoc(segmento)
    for variante in (
        base,
        _normalizar(base),
        _normalizar_sin_concat(base),
    ):
        backtick_spans = list(_BACKTICK_SPAN_RE.finditer(variante))
        for operador_re in _OPERADORES_ESCRITURA_ESTADO:
            for m in operador_re.finditer(variante):
                if any(s.start() <= m.start() < s.end() for s in backtick_spans):
                    continue
                if _region_apunta_a_estado_protegido(variante, m.end()):
                    return True

    enmascarado = _enmascarar_citas(segmento)
    for m in _REDIR_RE.finditer(enmascarado):
        if _region_apunta_a_estado_protegido(segmento, m.end()):
            return True

    return False


# -- Asignacion inline de env vars que evaden presupuesto/aislamiento ------
# -- de state (WP4/N4a) ------------------------------------------------
#
# ETOROAGENT_RUN_ID (presupuesto de ordenes por corrida, WP1) y
# ETOROAGENT_STATE_DIR (aislamiento de state para tests, WP4/N5) las setea
# runner.sh por entorno heredado, ANTES de invocar `claude` -- el agente
# nunca necesita asignarlas inline en un Bash call. Bloquea CUALQUIER
# asignacion (export o prefijo VAR=valor) de estas variables que aparezca
# en el mismo comando que una invocacion de uno de los 4 scripts
# autorizados (place_order/snapshot/candles/reconcile), siempre que
# ademas haya un vehiculo real de ejecucion (_VEHICULO_RE) -- mismo
# criterio que _referencia_trading_con_vehiculo, para no bloquear un
# mensaje de commit en prosa que mencione ambos patrones sin ejecutar
# nada. A proposito evalua el COMANDO ENTERO, no solo un segmento: un
# `export ETOROAGENT_RUN_ID=x && ...` encadenado con && (segmentos
# distintos para el resto de este hook) sigue exportando la variable
# para el resto del comando en un shell real -- partir por segmento
# aca dejaria pasar exactamente la variante de evasion mas obvia.
_ENV_SPOOF_ASSIGN_RE = re.compile(
    r"\b(?:export\s+)?(?:ETOROAGENT_RUN_ID|ETOROAGENT_STATE_DIR)\s*="
)
_ENV_SPOOF_SCRIPT_RE = re.compile(
    r"scripts/(?:place_order|snapshot|candles|reconcile)\.py\b"
)


def _bloqueado_env_spoof(command: str) -> bool:
    for variante in (command, _normalizar(command), _normalizar_sin_concat(command)):
        if (
            _ENV_SPOOF_ASSIGN_RE.search(variante)
            and _ENV_SPOOF_SCRIPT_RE.search(variante)
            and _VEHICULO_RE.search(variante)
        ):
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


def _bloqueado_guardrail(command: str) -> bool:
    for segmento in _split_segments(command):
        if _escribe_sobre_guardrail(segmento):
            return True
    return False


def _bloqueado_estado_protegido(command: str) -> bool:
    for segmento in _split_segments(command):
        if _escribe_sobre_estado_protegido(segmento):
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

    if _bloqueado_estado_protegido(command):
        sys.stderr.write(MENSAJE_ESTADO_PROTEGIDO)
        return 2

    if _bloqueado_env_spoof(command):
        sys.stderr.write(MENSAJE_ENV_SPOOF)
        return 2

    if _bloqueado_guardrail(command):
        sys.stderr.write(MENSAJE_GUARDRAIL)
        return 2

    if _bloqueado(command):
        sys.stderr.write(MENSAJE)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/bin/bash
# Lanza una corrida del agente. Uso: runner.sh equities|crypto
#
# WP7: unica via sancionada para corridas reales (DRY_RUN=0 desatendido).
# Exporta ETOROAGENT_AUTHORIZED_RUN=1 (place_order.py lo exige para toda
# apertura en modo real -- ver su docstring, pieza 3) y sincroniza la
# sombra de integridad fuera del repo (scripts/shadow_sync.py) antes de
# lanzar `claude` y despues de que termina (haya operado o no) -- ver el
# docstring de shadow_sync.py para el algoritmo de merge exacto.
#
# WP8: despues de la sincronizacion POST-corrida (el orden importa: sync
# primero -- nunca sincroniza hacia abajo --, chequeo despues), corre
# scripts/integrity_check.py. Si detecta una divergencia (exit 3):
# journalea, crea state/.needs_reconciliation (bloquea aperturas por el
# mecanismo ya existente), notifica al operador, y descarga ambos jobs de
# launchd (kill-switch) para que no vuelva a correr desatendido hasta
# revision manual.
#
# WP8b (sign-off del auditor sobre WP8): comparar archivo contra sombra
# no alcanza si un compuesto de sustituciones coordinadas envenena AMBOS
# en sincronia al mismo valor mas bajo -- ni check_equity ni check_budget
# ven nada, porque archivo y sombra siguen "de acuerdo" entre si. Este
# runner lee el pico de equity combinado (max de archivo y sombra) ANTES
# de lanzar `claude` y lo retiene EN UNA VARIABLE DE SHELL -- memoria del
# propio proceso del runner, que ningun archivo puede envenenar durante
# la corrida. Despues, se lo pasa a integrity_check.py via
# --expected-peak: si el pico combinado ACTUAL bajo respecto de ese valor
# retenido en memoria, es divergencia, sin importar que archivo y sombra
# sigan de acuerdo entre si (ver check_peak_monotonicity en
# scripts/integrity_check.py para el calculo exacto -- este script solo
# transporta el numero). Ademas, un fallo del chequeo mismo (exit 1) hoy
# frena igual que una divergencia (exit 3): un verificador que no puede
# leerse a si mismo no puede certificar integridad.
set -euo pipefail
cd "$(dirname "$0")/.."
MODE="${1:?uso: runner.sh equities|crypto}"
case "$MODE" in equities|crypto) ;; *) echo "modo inválido: $MODE" >&2; exit 64 ;; esac

mkdir -p state reports

# WP2: helper para journalear entradas que nunca llegan a place_order.py
# (skips del runner: lock ocupado, mercado cerrado). Mismo formato de
# timestamp que journal() en scripts/place_order.py (hora local, offset
# explícito), para quedar legible junto al resto del journal.
journal_line() {
  printf -- "- %s %s\n" "$(date +"%Y-%m-%d %H:%M %z")" "$1" >> state/journal.md
}

# WP7: sincroniza la sombra de integridad fuera del repo. Best-effort a
# proposito (no aborta la corrida si falla) -- si la sombra queda
# desactualizada/ausente, place_order.py ya bloquea aperturas en modo
# real hasta que una sincronizacion exitosa la restaure (fail-closed del
# lado del candado, no de este script).
sync_shadow() {
  /usr/bin/python3 scripts/shadow_sync.py || echo "WARN: fallo la sincronizacion de la sombra de integridad" >&2
}

LOCK="state/.runner.lock"
PIDFILE="$LOCK/pid"
GOT_LOCK=0

if mkdir "$LOCK" 2>/dev/null; then
  GOT_LOCK=1
else
  # el directorio de lock ya existe: decidir por liveness del PID, no por edad
  OLD_PID="$(cat "$PIDFILE" 2>/dev/null || true)"
  if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    echo "corrida en curso, salgo"
    journal_line "SKIP | corrida $MODE no corrió | lock ocupado (pid $OLD_PID vivo)"
    exit 0
  else
    echo "lock huérfano (proceso $OLD_PID no vive), limpiando"
    rm -f "$PIDFILE"
    rmdir "$LOCK" 2>/dev/null || true
    if mkdir "$LOCK" 2>/dev/null; then
      GOT_LOCK=1
    else
      echo "corrida en curso, salgo"
      journal_line "SKIP | corrida $MODE no corrió | lock ocupado (perdida la carrera por el lock tras limpiar el huérfano)"
      exit 0
    fi
  fi
fi

echo "$$" > "$PIDFILE"
cleanup() {
  # solo borrar el lock si sigue siendo nuestro (mismo pid grabado)
  if [ "$(cat "$PIDFILE" 2>/dev/null || true)" = "$$" ]; then
    rm -f "$PIDFILE"
    rmdir "$LOCK" 2>/dev/null || true
  fi
}
trap cleanup EXIT

if [ ! -f .env ]; then echo "ERROR: falta .env" >&2; exit 1; fi
set -a; source .env; set +a
: "${ETORO_API_KEY:?ETORO_API_KEY vacía en .env}"
: "${ETORO_USER_KEY:?ETORO_USER_KEY vacía en .env}"
export DRY_RUN="${DRY_RUN:-1}"

if [ "$MODE" = "equities" ]; then
  set +e
  MO_OUT="$(/usr/bin/python3 scripts/market_open.py 2>&1)"
  MO_RC=$?
  set -e
  case "$MO_RC" in
    0) : ;; # mercado abierto, seguir
    1)
      case "$MO_OUT" in
        "mercado cerrado"*)
          echo "skip: $MO_OUT"
          journal_line "SKIP | corrida $MODE no corrió | $MO_OUT"
          exit 0 ;;
        *)
          echo "ERROR: market_open.py falló (rc=1, salida inesperada): $MO_OUT" >&2; exit 1 ;;
      esac
      ;;
    *)
      echo "ERROR: market_open.py falló (rc=$MO_RC): $MO_OUT" >&2; exit 1 ;;
  esac
fi

# Sanitizar entorno anidado: lanzado desde adentro de una sesion de Claude Code,
# las variables ANTHROPIC_*/CLAUDE_* heredadas rompen la auth del CLI anidado (401).
# Bajo launchd (entorno limpio) esto es un no-op. Se preserva CLAUDE_BIN si vino seteada.
_CLAUDE_BIN_OVERRIDE="${CLAUDE_BIN:-}"
# CLAUDE_CODE_OAUTH_TOKEN (de `claude setup-token`, viene de .env) se preserva:
# es la credencial de larga duracion para headless — el login OAuth interactivo
# expira y no se renueva sin usuario (visto: corridas 08-08 y 10-08 abortadas).
_CLAUDE_TOKEN_OVERRIDE="${CLAUDE_CODE_OAUTH_TOKEN:-}"
while IFS='=' read -r _v _; do
  case "$_v" in ANTHROPIC*|CLAUDE*) unset "$_v" 2>/dev/null || true ;; esac
done < <(env)
CLAUDE_BIN="$_CLAUDE_BIN_OVERRIDE"
if [ -n "$_CLAUDE_TOKEN_OVERRIDE" ]; then
  export CLAUDE_CODE_OAUTH_TOKEN="$_CLAUDE_TOKEN_OVERRIDE"
fi

CLAUDE_BIN="${CLAUDE_BIN:-$(command -v claude || true)}"
if [ -z "$CLAUDE_BIN" ]; then
  # PATH de launchd es mínimo; probar ubicaciones típicas
  for c in "$HOME/.local/bin/claude" /usr/local/bin/claude /opt/homebrew/bin/claude; do
    [ -x "$c" ] && CLAUDE_BIN="$c" && break
  done
fi
[ -z "$CLAUDE_BIN" ] && { echo "ERROR: claude CLI no encontrado (setear CLAUDE_BIN)" >&2; exit 1; }

PROMPT_FILE="prompts/run_${MODE}.md"
[ -s "$PROMPT_FILE" ] || { echo "ERROR: falta o está vacío $PROMPT_FILE" >&2; exit 1; }

STAMP="$(date +%F-%H%M%S)"
# WP1: presupuesto de órdenes por corrida (state/.run_orders.json, ver
# scripts/place_order.py) se trackea por ETOROAGENT_RUN_ID -- una corrida
# real de principio a fin, no un proceso individual de place_order.py.
export ETOROAGENT_RUN_ID="${STAMP}-${MODE}"
# WP7/pieza 3: única vía sancionada para corridas reales -- place_order.py
# exige esta variable para toda apertura en modo real (DRY_RUN=0). El
# agente nunca la asigna inline (scripts/risk_hook.py lo bloquea, mismo
# criterio que ETOROAGENT_RUN_ID/ETOROAGENT_STATE_DIR).
export ETOROAGENT_AUTHORIZED_RUN="1"

sync_shadow  # WP7: ANTES de lanzar claude.

# WP8b: pico de equity combinado (max archivo/sombra) retenido EN
# MEMORIA de este proceso, ANTES de que `claude` corra -- ver el
# docstring de scripts/integrity_check.py para el porque. --print-peak
# nunca falla (devuelve vacio si no hay dato todavia); set +e/-e igual
# por prudencia, para que un fallo inesperado tampoco aborte el runner
# via set -e.
set +e
PRE_RUN_PEAK="$(/usr/bin/python3 scripts/integrity_check.py --print-peak)"
set -e

"$CLAUDE_BIN" -p "$(cat "$PROMPT_FILE")" \
  --allowedTools "Bash,Read,Write,Glob,Grep" \
  --max-turns 60 \
  > "reports/${STAMP}-${MODE}.log" 2>&1 || {
  CLAUDE_RC=$?
  echo "WARN: claude salió con código $CLAUDE_RC (ver log)"
  printf -- "- %s ABORTADA | corrida %s abortada (claude exit %s) | ver reports/%s-%s.log\n" \
    "$(date +"%Y-%m-%d %H:%M %z")" "$MODE" "$CLAUDE_RC" "$STAMP" "$MODE" >> state/journal.md
  # WP2: la corrida emitió órdenes (o pudo haberlo hecho) y murió antes de
  # journalear el razonamiento narrativo -- dejamos un flag para que la
  # PRÓXIMA corrida reconcilie posiciones vs journal antes de operar (ver
  # PLAYBOOK.md §Reconciliación tras corrida abortada) y place_order.py
  # bloquee aperturas hasta que esto se resuelva (los cierres siguen
  # permitidos: dirección fail-safe, siempre se puede reducir riesgo).
  printf '{\n  "reason": "corrida %s abortada (claude exit %s)",\n  "log": "reports/%s-%s.log",\n  "at": "%s"\n}\n' \
    "$MODE" "$CLAUDE_RC" "$STAMP" "$MODE" "$(date +"%Y-%m-%d %H:%M %z")" > state/.needs_reconciliation
  # Notificación best-effort al operador: si osascript no está disponible o
  # falla (headless sin sesión gráfica, no-mac), no debe romper el runner.
  osascript -e "display notification \"corrida ${MODE} abortada — ver journal\" with title \"etoro-agent\"" || true
}

sync_shadow  # WP7: DESPUÉS de que claude termina (haya operado o no).

# WP8: deteccion de divergencia archivo-vs-sombra (compuesto de dos
# sustituciones coordinadas, una sobre state/ y otra sobre la sombra, que
# la prevencion de WP7 por si sola no puede cerrar del todo -- ver el
# docstring de scripts/integrity_check.py). El orden importa: DESPUES del
# sync de arriba, nunca antes.
INTEGRITY_LOG="reports/${STAMP}-${MODE}-integrity.log"
set +e
if [ -n "$PRE_RUN_PEAK" ]; then
  /usr/bin/python3 scripts/integrity_check.py --expected-peak "$PRE_RUN_PEAK" > "$INTEGRITY_LOG" 2>&1
else
  # Sin pico previo conocido (primera corrida real de la maquina, o la
  # sombra/el archivo todavia no tienen ningun dato) -- la comparacion de
  # monotonia no aplica todavia, las otras dos (archivo vs sombra) siguen
  # activas igual.
  /usr/bin/python3 scripts/integrity_check.py > "$INTEGRITY_LOG" 2>&1
fi
INTEGRITY_RC=$?
set -e

if [ "$INTEGRITY_RC" -eq 3 ]; then
  printf -- "- %s INTEGRIDAD | divergencia archivo-vs-sombra detectada | ver %s | launchd descargado, aperturas bloqueadas -- revision manual requerida\n" \
    "$(date +"%Y-%m-%d %H:%M %z")" "$INTEGRITY_LOG" >> state/journal.md
  # Mismo mecanismo que ya bloquea aperturas tras una corrida abortada
  # (WP2, ver mas arriba) -- reconcile.py --done exige revision manual
  # antes de poder borrar este flag.
  printf '{\n  "reason": "divergencia de integridad archivo-vs-sombra",\n  "log": "%s",\n  "at": "%s"\n}\n' \
    "$INTEGRITY_LOG" "$(date +"%Y-%m-%d %H:%M %z")" > state/.needs_reconciliation
  osascript -e "display notification \"divergencia de integridad detectada -- corridas reales suspendidas\" with title \"etoro-agent\"" || true
  # Kill-switch: descarga ambos jobs de launchd para que no vuelva a
  # correr desatendido. DETACHADO y con una pausa corta a proposito: el
  # runner puede estar corriendo BAJO uno de esos mismos jobs -- si lo
  # descargamos en linea, launchd podria matar este proceso a mitad de
  # su propio cleanup (trap EXIT, liberacion del lock). El journal de
  # arriba YA quedo escrito antes de disparar esto.
  nohup bash -c 'sleep 2; launchctl unload "$HOME/Library/LaunchAgents/com.etoroagent.equities.plist" "$HOME/Library/LaunchAgents/com.etoroagent.crypto.plist"' >/dev/null 2>&1 &
elif [ "$INTEGRITY_RC" -eq 1 ]; then
  echo "WARN: integrity_check.py falló (rc=1) -- ver $INTEGRITY_LOG" >&2
  printf -- "- %s WARN | integrity_check.py falló, no se detiene la operación | ver %s\n" \
    "$(date +"%Y-%m-%d %H:%M %z")" "$INTEGRITY_LOG" >> state/journal.md
fi

echo "corrida ${MODE} terminada: reports/${STAMP}-${MODE}.log"

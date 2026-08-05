#!/bin/bash
# Lanza una corrida del agente. Uso: runner.sh equities|crypto
set -euo pipefail
cd "$(dirname "$0")/.."
MODE="${1:?uso: runner.sh equities|crypto}"
case "$MODE" in equities|crypto) ;; *) echo "modo inválido: $MODE" >&2; exit 64 ;; esac

mkdir -p state reports
LOCK="state/.runner.lock"
PIDFILE="$LOCK/pid"
GOT_LOCK=0

if mkdir "$LOCK" 2>/dev/null; then
  GOT_LOCK=1
else
  # el directorio de lock ya existe: decidir por liveness del PID, no por edad
  OLD_PID="$(cat "$PIDFILE" 2>/dev/null || true)"
  if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    echo "corrida en curso, salgo"; exit 0
  else
    echo "lock huérfano (proceso $OLD_PID no vive), limpiando"
    rm -f "$PIDFILE"
    rmdir "$LOCK" 2>/dev/null || true
    if mkdir "$LOCK" 2>/dev/null; then
      GOT_LOCK=1
    else
      echo "corrida en curso, salgo"; exit 0
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
          echo "skip: $MO_OUT"; exit 0 ;;
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
while IFS='=' read -r _v _; do
  case "$_v" in ANTHROPIC*|CLAUDE*) unset "$_v" 2>/dev/null || true ;; esac
done < <(env)
CLAUDE_BIN="$_CLAUDE_BIN_OVERRIDE"

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
"$CLAUDE_BIN" -p "$(cat "$PROMPT_FILE")" \
  --allowedTools "Bash,Read,Write,Glob,Grep" \
  --max-turns 60 \
  > "reports/${STAMP}-${MODE}.log" 2>&1 || echo "WARN: claude salió con código $? (ver log)"
echo "corrida ${MODE} terminada: reports/${STAMP}-${MODE}.log"

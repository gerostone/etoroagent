#!/bin/bash
# Lanza una corrida del agente. Uso: runner.sh equities|crypto
set -euo pipefail
cd "$(dirname "$0")/.."
MODE="${1:?uso: runner.sh equities|crypto}"
case "$MODE" in equities|crypto) ;; *) echo "modo inválido: $MODE" >&2; exit 64 ;; esac

mkdir -p state reports
LOCK="state/.runner.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  # lock huérfano: si tiene más de 2 horas, limpiarlo y seguir; si no, salir
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +120 2>/dev/null)" ]; then
    echo "lock huérfano (>2h), limpiando"
    rmdir "$LOCK" 2>/dev/null || true
    mkdir "$LOCK" 2>/dev/null || { echo "corrida en curso, salgo"; exit 0; }
  else
    echo "corrida en curso, salgo"; exit 0
  fi
fi
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT

if [ ! -f .env ]; then echo "ERROR: falta .env" >&2; exit 1; fi
set -a; source .env; set +a
: "${ETORO_API_KEY:?ETORO_API_KEY vacía en .env}"
: "${ETORO_USER_KEY:?ETORO_USER_KEY vacía en .env}"
export DRY_RUN="${DRY_RUN:-1}"

if [ "$MODE" = "equities" ]; then
  /usr/bin/python3 scripts/market_open.py || { echo "skip: mercado cerrado"; exit 0; }
fi

CLAUDE_BIN="${CLAUDE_BIN:-$(command -v claude || true)}"
if [ -z "$CLAUDE_BIN" ]; then
  # PATH de launchd es mínimo; probar ubicaciones típicas
  for c in "$HOME/.local/bin/claude" /usr/local/bin/claude /opt/homebrew/bin/claude; do
    [ -x "$c" ] && CLAUDE_BIN="$c" && break
  done
fi
[ -z "$CLAUDE_BIN" ] && { echo "ERROR: claude CLI no encontrado (setear CLAUDE_BIN)" >&2; exit 1; }

STAMP="$(date +%F-%H%M)"
"$CLAUDE_BIN" -p "$(cat "prompts/run_${MODE}.md")" \
  --allowedTools "Bash,Read,Write,Glob,Grep" \
  --max-turns 60 \
  > "reports/${STAMP}-${MODE}.log" 2>&1 || echo "WARN: claude salió con código $? (ver log)"
echo "corrida ${MODE} terminada: reports/${STAMP}-${MODE}.log"

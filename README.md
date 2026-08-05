# etoro-agent

Agente de trading autónomo sobre [eToro Agent Portfolios](https://www.etoro.com/news-and-analysis/etoro-updates/agent-portfolios-let-your-ai-agent-trade-for-you/):
Claude Code headless + señales cuantitativas (momentum, tendencia, régimen de
volatilidad, fuerza relativa sectorial) + un motor de riesgo determinista que
valida cada orden antes de que llegue a la API.

> ⚠️ **Opera dinero real.** Empezá SIEMPRE en `DRY_RUN=1` y con el
> presupuesto mínimo que permita eToro para el Agent Portfolio (≈ $200).
> Nada de lo que hace este agente es asesoramiento financiero — es
> software que ejecuta una estrategia mecánica con límites de riesgo
> fijos, sin garantía de resultado.

## Qué es

- **Claude Code headless** (`claude -p`, sin intervención humana durante la
  corrida) corre como el "cerebro" de cada sesión: lee `PLAYBOOK.md` y
  `RISK.md`, calcula señales con `scripts/candles.py`, y decide qué
  proponer.
- **El motor de riesgo** (`scripts/risk.py`) es la fuente de verdad
  ejecutable de los límites — no negociable por el agente — y se aplica
  automáticamente a toda orden que pase por `scripts/place_order.py`.
- **Un hook `PreToolUse`** (`scripts/risk_hook.py`) más
  `permissions.deny` en `.claude/settings.json` cierran cualquier otra vía
  de escritura: ni la API de eToro ni los propios guardrails del agente
  (`scripts/`, `.claude/`, `PLAYBOOK.md`, `RISK.md`, `.env`) se pueden
  tocar por fuera de los caminos autorizados.

## Setup

1. **Crear el Agent Portfolio en eToro** (desde el sitio de escritorio, no
   la app): menú → **Agent Portfolios (Beta)** → crear cartera → asignarle
   un presupuesto (empezá con el mínimo permitido, ≈ $200) → copiar la
   **API key** del portfolio que te muestra ahí.
2. **Configurar las credenciales:**
   ```bash
   cp .env.example .env
   chmod 600 .env
   ```
   Editá `.env` y completá las dos claves:
   - `ETORO_API_KEY`: **no es secreta por usuario** — es el valor fijo
     publicado en la skill oficial de eToro para agent-portfolios (ver
     `docs/api-notes.md`, generado localmente y no versionado; no lo
     inventes, hay que sacarlo de la skill oficial o de la documentación
     de eToro).
   - `ETORO_USER_KEY`: el `userToken` propio de TU portfolio — este SÍ es
     secreto, es el que copiaste en el paso 1. No lo compartas ni lo
     commitees.
   - Dejá `DRY_RUN=1` (ya viene así en `.env.example`). Recién se cambia a
     `0` después de validar en dry-run (ver más abajo).
3. **Preparar el entorno:**
   ```bash
   python3 -m venv .venv
   .venv/bin/pip install requests pytest
   ```
4. **Correr la suite de tests** (no necesita `.env` ni red):
   ```bash
   .venv/bin/pytest tests/ -q
   ```
   Tiene que dar todo verde antes de seguir.
5. **Primera corrida manual, en dry-run:**
   ```bash
   bash scripts/runner.sh crypto
   ```
   Revisá el log en `reports/<fecha-hora>-crypto.log` (snapshot del
   portfolio, señales calculadas, y las decisiones tomadas — en dry-run
   ninguna orden llega a ejecutarse de verdad) y las entradas nuevas en
   `state/journal.md`. Si ves `401`, revisá las dos claves en `.env`.

## Activar las corridas programadas

Este proyecto corre en macOS vía `launchd` (no cron): un plist para
equities/ETFs (dos corridas diarias, mercado NYSE) y otro para cripto (una
corrida diaria, 24/7). Las horas de `run/*.plist` son **11:35 y 16:30**
para equities y **09:00** para cripto — todas en la hora local del Mac
donde corre `launchd` (si tu Mac no está en horario de Argentina, ajustá
los plists antes de cargarlos, o corré el bloque de abajo desde un Mac que
sí lo esté).

```bash
sed "s|__PROJECT__|$PWD|g" run/com.etoroagent.equities.plist > ~/Library/LaunchAgents/com.etoroagent.equities.plist
sed "s|__PROJECT__|$PWD|g" run/com.etoroagent.crypto.plist > ~/Library/LaunchAgents/com.etoroagent.crypto.plist
launchctl load ~/Library/LaunchAgents/com.etoroagent.equities.plist
launchctl load ~/Library/LaunchAgents/com.etoroagent.crypto.plist
```

Cada corrida escribe su log en `reports/launchd-<modo>.log` (stdout+stderr
de `runner.sh`) además del log con timestamp por corrida en `reports/`.

## Pasar a real (tras 1–2 semanas de dry-run satisfactorio)

Checklist antes de tocar `DRY_RUN`:

1. **1–2 semanas de corridas en dry-run** sin errores no manejados
   (`reports/launchd-*.log` limpio de `ERROR:` inesperados).
2. **Revisar `state/journal.md` contra `PLAYBOOK.md`**: ¿las decisiones
   son consistentes con la estrategia? ¿el agente respetó la regla de oro
   ("ante la duda, no operar")? ¿los bloqueos del motor de riesgo tienen
   sentido?
3. Editar `.env`: `DRY_RUN=0`.
4. **Monitoreo diario** de `reports/` y `state/journal.md` — sobre todo
   las primeras corridas en real.
5. **Kill-switch** (frena todo de inmediato, sin tocar código):
   ```bash
   launchctl unload ~/Library/LaunchAgents/com.etoroagent.equities.plist
   launchctl unload ~/Library/LaunchAgents/com.etoroagent.crypto.plist
   ```

## Arquitectura

| Script | Rol |
|---|---|
| `scripts/snapshot.py` | Lee el estado real del portfolio (eToro) y lo persiste en `state/` — primer paso obligatorio de toda corrida. |
| `scripts/candles.py` | Única vía de lectura de velas/precios para calcular señales. |
| `scripts/place_order.py` | **Única vía de ESCRITURA** — abrir/cerrar posiciones. Valida contra `scripts/risk.py` antes de tocar la API. |
| `scripts/risk.py` | Motor de riesgo determinista (lógica pura, sin red/filesystem) — límites duros no negociables. |
| `scripts/risk_hook.py` | Hook `PreToolUse` de Claude Code: bloquea escrituras a la API por fuera de `place_order.py`, y escrituras de shell sobre los guardrails del propio agente. |
| `scripts/market_open.py` | Chequea si NYSE está abierto (usado por `runner.sh` antes de la corrida de equities). |
| `scripts/runner.sh` | Orquesta una corrida completa: lock, `.env`, chequeo de mercado, invoca `claude -p` con el prompt correspondiente. |

**Límites duros** (fuente de verdad ejecutable en `scripts/risk.py`, prosa
en `RISK.md`): máximo 25% del portfolio por posición, máximo 35% en
cripto, stop-loss obligatorio ≤12%, modo defensivo (solo cierres) a -25%
de drawdown, `DRY_RUN` fail-safe por default.

**Toda orden pasa por `scripts/place_order.py`.** `permissions.deny` en
`.claude/settings.json` bloquea que el agente use Write/Edit sobre
`scripts/`, `.claude/`, `PLAYBOOK.md` y `RISK.md`; `scripts/risk_hook.py`
cierra las vías restantes — llamadas directas a la API (curl/wget/cliente
Python inline) y escrituras de shell (`tee`, redirección, `sed -i`, `mv`,
`cp`, etc.) sobre esos mismos guardrails. Ninguna de las dos capas evalúa
shell real, así que quedan bypasses residuales conocidos y aceptados (ver
docstring de `scripts/risk_hook.py`) — mitigados por la validación interna
de `place_order.py` (corre sin importar cómo fue invocado) y por la
revisión humana del journal.

**Regla de oro:** ante la duda, o ante cualquier dato faltante o
incompleto, el agente no opera y journalea el motivo.

Diseño completo: `docs/superpowers/specs/2026-08-04-etoro-agent-design.md`.
Plan de implementación: `docs/superpowers/plans/2026-08-04-etoro-agent.md`.

## Seguridad

- La API key del Agent Portfolio (`ETORO_USER_KEY`) tiene scope limitado a
  ESE portfolio — no uses una key de tu cuenta principal de eToro.
- Si eToro lo permite para el Agent Portfolio, activá una **whitelist de
  IP** para restringir desde dónde se puede usar la key.
- `.env` **nunca** va al repo (`.gitignore` ya lo excluye) — contiene el
  `userToken` real. `chmod 600 .env` para que solo tu usuario lo pueda
  leer.
- El agente **no tiene acceso web**: `runner.sh` invoca `claude -p` sin
  herramientas de navegación/búsqueda, a propósito, para reducir la
  superficie de prompt-injection en un agente que mueve dinero.

## Troubleshooting

- **`401` en cualquier request**: el `userToken` expiró o es inválido —
  revisá `expiresAt` del token en el Agent Portfolio de eToro y generá uno
  nuevo si hace falta; actualizá `ETORO_USER_KEY` en `.env`.
- **`corrida en curso, salgo`** al lanzar `runner.sh` sin que haya
  ninguna corrida real activa: el lock (`state/.runner.lock`) quedó
  huérfano de una corrida anterior que no limpió bien. `runner.sh` ya
  detecta y limpia locks de PIDs muertos automáticamente en la siguiente
  invocación — si persiste, confirmá que el PID en
  `state/.runner.lock/pid` no esté vivo y borrá el directorio a mano.
- **Logs**: cada corrida deja `reports/<fecha-hora>-<modo>.log` (salida
  completa de `claude -p`) y, vía `launchd`, además
  `reports/launchd-<modo>.log` (stdout+stderr del propio `runner.sh`,
  incluye errores de setup como `.env` faltante o `claude` no encontrado).
- **State stale (más de 24hs)**: `scripts/risk.py` bloquea toda operación
  si `state/positions.json`/`state/equity.csv` tienen más de 24 horas o no
  se pudieron interpretar. Corré `scripts/snapshot.py` a mano para
  refrescarlo, o dejá que la próxima corrida programada lo haga.

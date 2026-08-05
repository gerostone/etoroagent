# Spec: `etoro-agent` — Agente de trading autónomo con Claude Code headless + eToro Agent Portfolios

**Fecha:** 2026-08-04
**Estado:** Aprobado por el usuario (diseño validado en sesión de brainstorming)

## 1. Objetivo

Construir un agente AI que opera de forma autónoma una subcartera **Agent Portfolio** de eToro (funcionalidad beta, marzo 2026), usando Claude Code en modo headless como motor de decisión, la skill oficial de eToro como capa de integración con la API, y límites de riesgo deterministas que el LLM no puede violar.

## 2. Decisiones de diseño (validadas con el usuario)

| Dimensión | Decisión |
|---|---|
| Estrategia | Híbrida: señales cuantitativas proponen, el LLM filtra/decide con contexto de mercado |
| Cadencia | Diaria programada (sin intervención humana por corrida) |
| Universo | ETFs de índices/sectores/defensivos (SPY, QQQ, XLK, XLE, XLF, XLV, XLI, XLP, XLU, GLD, TLT) + cripto (BTC, ETH) |
| Riesgo | Moderado, cableado: máx 25% por posición, máx 35% cripto total, stop-loss -12% por posición, drawdown de portfolio -25% → modo defensivo |
| Arquitectura | Claude Code headless (`claude -p`) + skill oficial de eToro, lanzado por launchd |

## 3. Contexto técnico de la plataforma eToro

- **Agent Portfolios**: subcartera separada con presupuesto propio (mínimo $200) y API key con permisos limitados a esa cartera. El usuario la crea desde el menú de eToro desktop y copia la key.
- **API**: `https://public-api.etoro.com/api/v1`. Autenticación por headers `x-api-key` + `x-user-key` (+ `x-request-id` UUID por request), o OAuth 2.0 con scopes `etoro-public:agent-portfolio:read/write`.
- **Rate limit**: 60 requests / 60 segundos (cuota compartida). Headers `RateLimit-*` en las respuestas.
- **Skill oficial**: publicada por eToro en `https://api-portal.etoro.com/ai-agents/etoro-skill` (instalable con "Install the etoro skill from https://www.etoro.com/wp-content/uploads/agent-portfolios/SKILL.md"). Cubre autenticación, trading, market data, portfolios, watchlists y social.
- **Endpoint verificado**: `GET /api/v1/agent-portfolios` devuelve `agentPortfolios[]` con `agentPortfolioId`, `agentPortfolioVirtualBalance` (USD), `mirrorId`, `userTokens[]` (scopes, expiración, whitelist de IPs).
- **Docs completas**: `https://api-portal.etoro.com/llms.txt` y portal MCP `https://api-portal.etoro.com/mcp`.

## 4. Arquitectura

### 4.1 Modelo de ejecución

Cada corrida es una **sesión fresca** de Claude Code headless lanzada por launchd:

- **Días hábiles, 2 corridas para ETFs**: ~10:00 ET (post-apertura NYSE) y ~15:30 ET (pre-cierre).
- **Diaria (7 días), 1 corrida solo-cripto**: BTC/ETH cotizan 24/7.

La sesión carga: skill de eToro + `PLAYBOOK.md` + estado persistido (`state/`). Analiza → decide → ejecuta → registra. Sin memoria entre sesiones salvo lo persistido en `state/`.

### 4.2 Estructura del proyecto

```
etoroagent/
├── .claude/
│   ├── skills/etoro/SKILL.md      # skill oficial de eToro (descargada)
│   └── settings.json              # hook PreToolUse → scripts/risk_check
├── PLAYBOOK.md                    # estrategia completa (ver §5)
├── RISK.md                        # límites de riesgo en prosa (fuente de verdad legible)
├── scripts/
│   ├── risk_check.sh              # hook determinista que valida/bloquea órdenes
│   └── runner.sh                  # preflight + lock + lanza claude -p
├── run/
│   ├── com.etoroagent.equities.plist   # launchd: corridas ETF días hábiles
│   └── com.etoroagent.crypto.plist     # launchd: corrida cripto diaria
├── state/
│   ├── journal.md                 # diario de decisiones con razonamiento
│   ├── equity.csv                 # fecha,valor → cálculo de drawdown
│   └── positions.json             # snapshot del último estado conocido
├── reports/                       # un resumen legible por corrida
├── .env                           # ETORO_API_KEY, ETORO_USER_KEY (usuario las pega; gitignored)
└── docs/superpowers/specs/        # esta spec
```

## 5. Estrategia (contenido de PLAYBOOK.md)

### 5.1 Señales cuantitativas (el agente las calcula cada corrida con datos de la API)

- **Momentum absoluto**: retorno 3 y 6 meses por instrumento.
- **Tendencia**: precio vs medias móviles de 50 y 200 días.
- **Régimen de volatilidad**: nivel y pendiente del VIX → clasifica el mercado en risk-on / neutral / risk-off.
- **Fuerza relativa sectorial**: ranking de ETFs sectoriales vs SPY.
- **Cripto**: momentum 30/90 días y tendencia propia; independiente del régimen de acciones.

### 5.2 Reglas de decisión

- **Risk-on**: sobreponderar los 2-3 sectores con mejor fuerza relativa + QQQ; cripto permitida hasta su tope.
- **Neutral**: núcleo SPY + 1-2 sectores líderes; reducir cripto a la mitad de su tope.
- **Risk-off**: rotar hacia GLD/TLT/XLP/XLU; sin nuevas compras de riesgo; cripto solo mantener o reducir.
- **Entrada**: solo instrumentos con momentum positivo Y por encima de la media de 50 días.
- **Salida**: stop-loss -12% (obligatorio), o pérdida de tendencia (cierre bajo media de 200 días), o caída al fondo del ranking sectorial.

### 5.3 Rol del LLM (filtro y juez)

Las señales generan **propuestas**; el agente las revisa con juicio: coherencia entre señales, eventos conocidos (earnings, Fed, halvings), tamaño relativo, y puede **rechazar o reducir** una propuesta, nunca ampliarla más allá de los límites de riesgo. Debe registrar el razonamiento de cada decisión en el journal.

## 6. Motor de riesgo (no negociable por el LLM)

Implementado como **hook PreToolUse de Claude Code**: `scripts/risk_check.sh` intercepta las llamadas Bash que contengan requests de apertura/modificación de posiciones a la API de eToro y valida contra `state/positions.json` + `state/equity.csv`:

1. Posición resultante ≤ 25% del valor del portfolio.
2. Exposición cripto total resultante ≤ 35%.
3. Toda apertura lleva stop-loss a -12% (o el hook la rechaza).
4. Si drawdown desde máximo histórico de `equity.csv` ≥ 25% → **modo defensivo**: bloquea toda compra; solo permite cierres.
5. `DRY_RUN=1` → bloquea toda orden real (fase de validación).

El hook devuelve exit code de bloqueo con mensaje explicativo, para que el agente entienda el rechazo y lo journalee.

## 7. Flujo de una corrida

1. **Preflight** (`runner.sh`): `.env` presente, lockfile libre, ¿corresponde correr? (calendario de mercado para ETFs).
2. **Snapshot**: `GET /agent-portfolios` + posiciones → actualiza `positions.json` y `equity.csv`.
3. **Market data**: precios/históricos del universo (≤ 60 req/min, con `x-request-id` UUID nuevo por request).
4. **Señales → propuestas** (§5).
5. **Validación**: cada orden pasa por el hook de riesgo.
6. **Ejecución**: órdenes aprobadas contra la API. Los POSTs de trading NUNCA se reintentan. Un fallo tras el envío se trata como resultado ambiguo: journalear AMBIGUO, esperar >=60s (cache del endpoint de posiciones), correr snapshot y verificar el estado real antes de cualquier decisión.
7. **Registro**: journal (decisiones + razonamiento), reporte de corrida, estado actualizado.

## 8. Manejo de errores

- Regla de oro: **ante la duda, no operar**. Análisis incompleto o datos faltantes → terminar sin operar y registrar.
- `401` → abortar, marcar alerta en el reporte (key inválida/expirada — los tokens tienen `expiresAt`).
- `429` → backoff respetando `RateLimit-Reset`.
- Los POSTs de trading NUNCA se reintentan. Un fallo tras el envío se trata como resultado ambiguo: journalear AMBIGUO, esperar >=60s (cache del endpoint de posiciones), correr snapshot y verificar el estado real antes de cualquier decisión.
- Corrida colgada → lockfile con timeout; launchd no solapa corridas.

## 9. Seguridad

- Credenciales solo en `.env` (gitignored, permisos 600); el usuario las pega manualmente. Nunca en prompts, logs ni reportes.
- La API key es de scope limitado al Agent Portfolio (aislada del resto de la cuenta eToro).
- Opcional recomendado: configurar whitelist de IP del token en eToro.

## 10. Puesta en marcha y validación

1. **Tests del hook de riesgo**: casos unitarios (orden que excede 25%, cripto sobre tope, sin stop-loss, drawdown en modo defensivo) — todos deben bloquearse.
2. **Fase dry-run (1-2 semanas)**: corridas reales completas con `DRY_RUN=1`; se evalúa el journal: ¿las decisiones son sensatas y consistentes con el playbook?
3. **Fase real**: presupuesto mínimo ($200), monitoreo por reportes diarios.

## 11. Fuera de alcance (YAGNI)

- Backtesting histórico completo (la lógica LLM no es backtesteable; se valida por dry-run).
- Trading intradiario / WebSocket en tiempo real.
- Notificaciones push (los reportes en archivo son suficientes para v1).
- Interfaz web/dashboard.
- Copy trading social (los datos sociales de eToro quedan disponibles vía skill pero fuera de la estrategia v1).

## 12. Criterios de éxito

- Corridas programadas ejecutan de punta a punta sin intervención, con journal y reporte por corrida.
- El hook de riesgo bloquea el 100% de las órdenes que violan límites (verificado por tests).
- En dry-run, cero órdenes reales ejecutadas.
- El agente nunca opera cuando el análisis está incompleto (fail-safe verificado en el journal).

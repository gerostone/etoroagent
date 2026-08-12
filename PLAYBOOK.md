# PLAYBOOK.md — Estrategia y reglas operativas del agente

Este documento es la estrategia completa que sigue el agente en cada
corrida (ver `docs/superpowers/specs/2026-08-04-etoro-agent-design.md` §5
para el diseño original). Se carga junto con `RISK.md` y el estado
persistido en `state/` al inicio de cada sesión.

## Reglas duras (no negociables)

- Toda orden de trading (apertura o cierre) se ejecuta EXCLUSIVAMENTE vía:
  - Abrir: `.venv/bin/python scripts/place_order.py open --symbol <SYMBOL> --amount <USD> --stop-loss-pct <0-0.12> --reason "<texto>"`
  - Cerrar: `.venv/bin/python scripts/place_order.py close --position-id <ID> --symbol <SYMBOL> --reason "<texto>"`
  Ningún otro camino está permitido — un hook de este mismo proyecto
  intercepta y bloquea cualquier intento de escribir a la API por fuera de
  este script.

- El primer comando de toda corrida es siempre
  `.venv/bin/python scripts/snapshot.py` (actualiza `state/positions.json` y
  `state/equity.csv` con el estado real del portfolio). Sin un snapshot
  fresco, no hay corrida.

- Velas y precios se consultan exclusivamente con
  `.venv/bin/python scripts/candles.py --symbol <SYMBOL> --count <N> [--interval OneDay]`
  (por default imprime closes en CSV compacto y orden ascendente — ver
  §Señales para el formato exacto y cómo calcular momentum/SMA con él;
  `--full` da el JSON crudo si hace falta otro campo). No usar el cliente
  Python inline (bloqueado explícitamente por el hook de riesgo). Para
  cualquier otro dato de mercado de solo lectura no cubierto por
  `candles.py`, usar un `curl GET` puntual (nunca POST/PUT/PATCH/DELETE).

- Interpretar los exit codes de `place_order.py`:
  - `0`: la orden se ejecutó (o, en modo `DRY_RUN`, se habría ejecutado) —
    journalear normalmente.
  - `2`: **bloqueada** por el motor de riesgo o por validación (tamaño,
    stop-loss, universo, drawdown, state stale, cash insuficiente —
    `amount` supera el cash disponible del state —, símbolo/posición
    inválidos). Leer el motivo impreso en stderr, journalearlo tal cual, y
    **acatarlo**. No reformular la orden para eludir el límite (por
    ejemplo, no dividir una posición en varias órdenes chicas para esquivar
    el tope del 25%). Solo tiene sentido reintentar con un monto reducido
    si el motivo del bloqueo es específicamente de tamaño (posición,
    cripto o cash disponible) Y la reducción tiene sentido dentro de la
    estrategia de la corrida — nunca como truco para colarse por debajo
    del límite.
  - `1` con mensaje **AMBIGUO**: la orden pudo haberse ejecutado igual pese
    al error (nunca se sabe con certeza del lado del cliente). **Nunca
    reintentar** ni enviar una orden equivalente. Esperar al menos 60
    segundos (por el cache del lado de eToro), correr
    `scripts/snapshot.py` de nuevo, y verificar contra el nuevo estado si la
    posición aparece. Journalear el desenlace una vez verificado.
  - `1` con otro mensaje de **ERROR** (sin AMBIGUO): fallo antes de enviar
    la orden (cliente, instrumento, precio). No se envió nada — journalear
    y seguir con el resto de la corrida sin reintentar esa orden en el
    mismo ciclo.

- Máximo 3 órdenes por corrida. Esperar al menos 3 segundos entre una orden
  y la siguiente. (Este tope, y uno adicional de 6 órdenes por día
  calendario, están además aplicados en código por `scripts/place_order.py`
  -- ver RISK.md -- no dependen solo de que el agente los respete.)

- NUNCA propongas abrir un símbolo que ya está en el portfolio (el motor lo
  bloquea). Si ya tenés la posición y la señal sigue válida: mantener.
  Rebalancear = cerrar en esta corrida, reevaluar en la siguiente. Si el
  snapshot muestra que la cartera ya está posicionada según el régimen, el
  resultado correcto de la corrida es NO operar.

- Posiciones cuyo `positionId` empieza con `pending-open:` o `local-open:`
  en el state son exposición ya comprometida (apertura pendiente vista por
  el snapshot, o exposición registrada localmente tras una orden de esta
  misma corrida) — no son posiciones reales cerrables. No intentar
  cerrarlas ni tratarlas como si no existieran: cuentan contra los topes de
  riesgo igual que una posición confirmada.

- Si `scripts/snapshot.py` falla, o cualquier dato necesario para decidir
  (precio, vela) está incompleto o no se puede obtener con confianza,
  journalear el motivo y **terminar la corrida sin operar**. Ante la duda,
  no operar — esta es la regla de oro por encima de cualquier otra.
  **Única excepción documentada:** la señal de régimen (VIX) tiene su
  propio fallback a neutral sin abortar la corrida — ver §Señales, régimen
  de volatilidad, para el detalle y el alcance exacto de esa excepción.

## Reconciliación tras corrida abortada

Si existe `state/.needs_reconciliation` (lo crea `scripts/runner.sh` cuando
`claude` termina con código != 0 — ver la entrada `ABORTADA` correspondiente
en `state/journal.md` y el log referenciado ahí), tu **PRIMERA tarea tras el
snapshot** es reconciliar, antes de cualquier otro análisis:

1. Comparás las posiciones del snapshot recién tomado contra las entradas
   `DRY_RUN`/`ABIERTA`/`DECISION` del journal reciente (las últimas
   corridas, especialmente la abortada y la inmediatamente anterior).
2. Toda posición del snapshot sin una decisión correspondiente en el
   journal, o toda decisión de apertura en el journal sin la posición
   correspondiente en el snapshot, se journalea como **hallazgo** con tu
   mejor reconstrucción de por qué (el log referenciado en el flag —
   `reports/<stamp>-<modo>.log` — ayuda: ahí puede estar el razonamiento
   que no llegó a `state/journal.md`).
3. Journaleás una entrada `RECONCILIACION` con el resultado, aunque sea "sin
   discrepancias" — la ausencia de hallazgos también es información de
   auditoría.
4. Recién entonces eliminás `state/.needs_reconciliation` y continuás la
   corrida con normalidad.

Mientras el archivo exista, `scripts/place_order.py` bloquea toda apertura
(`exit 2`) — los cierres siguen permitidos, es la dirección fail-safe. Si el
paso 2 encuentra discrepancias que no podés explicar con confianza: **NO
abras posiciones en esta corrida** (los cierres por señal de salida siguen
permitidos igual). No borres el flag hasta haber journaleado la
`RECONCILIACION`.

## Universo operable

Universo cerrado: exactamente los símbolos definidos en `UNIVERSE` de
`scripts/risk.py`.
- Equities/ETFs: SPY, QQQ, XLK, XLE, XLF, XLV, XLI, XLP, XLU, GLD, TLT.
- Cripto: BTC, ETH (y sus variantes de símbolo reconocidas por el motor de
  riesgo: BTCUSD, ETHUSD, BTC-USD, ETH-USD, BTCEUR, ETHEUR).

No operar ningún símbolo fuera de esta lista, aunque parezca un instrumento
razonable — si no está en el universo, `place_order.py` lo bloquea igual
(exit 2), pero no vale la pena ni intentarlo ni perder tiempo de la corrida
en eso.

## Señales

El agente calcula estas señales cada corrida a partir de los cierres
diarios que entrega `scripts/candles.py`. Por DEFAULT (sin `--full`),
`candles.py` imprime un formato CSV compacto en orden **ASCENDENTE**
(viejo→nuevo): una línea de comentario con metadata
(`# symbol=<SYM> interval=<INTERVAL> count=<N> order=asc`) seguida de una
línea `<fecha>,<close>` por vela, de la más vieja a la más reciente.

**Verificá el header `order=asc` antes de calcular cualquier señal** — es
la garantía de que, sobre la lista de closes que arma a partir de esas
líneas, el índice `-1` es la vela más reciente ("hoy") y el índice
`-(N+1)` es la de hace N velas. (`--full` devuelve el JSON crudo de la API
sin reordenar — no es el modo por default y no hace falta para calcular
señales; usarlo solo si hace falta un campo que el CSV no trae, p.ej.
volumen.)

`--count N` trae N velas — pedir suficientes para cubrir la ventana de cada
señal, p.ej. `--count 210` para cubrir 200 días más margen.

- **Momentum absoluto (63 / 126 días hábiles ≈ 3 / 6 meses):** con la lista
  de closes en orden ascendente, retorno = `close[-1] / close[-64] - 1`
  (momentum de 63 velas) o `close[-1] / close[-127] - 1` (momentum de 126
  velas) — el índice es `-(N+1)` porque `close[-1]` ya es "hoy" (0 velas
  atrás), así que "hace 63 velas" es el índice `-64`. Positivo = momentum a
  favor.
- **Tendencia (SMA50 / SMA200):** `SMA50 = promedio(close[-50:])`,
  `SMA200 = promedio(close[-200:])`, sobre la misma lista ascendente.
  Comparar el precio actual (`close[-1]`) contra cada media: por encima de
  la SMA50 = tendencia de corto plazo intacta; por encima de la SMA200 =
  tendencia de largo plazo intacta; cruce por debajo de la SMA200 es señal
  de salida.
- **Régimen de volatilidad (VIX):** intentar obtener velas de VIX con
  `scripts/candles.py --symbol VIX --count N` (el símbolo que resuelva la
  búsqueda de instrumento para el índice de volatilidad); si no resuelve
  ahí, probar un `curl GET` puntual contra el endpoint de precios de cierre
  de datos de mercado. Clasificar el régimen por nivel y pendiente reciente
  del VIX: nivel bajo y plano/descendente = risk-on; nivel medio o
  pendiente ascendente moderada = neutral; nivel alto o pendiente
  fuertemente ascendente = risk-off. Si no se puede obtener el dato de
  ninguna de las dos formas, **asumir régimen neutral** — nunca inventar un
  nivel de VIX.

  **Excepción a la regla de oro:** si el VIX no se puede obtener, la señal
  de régimen degrada a NEUTRAL (no se aborta la corrida). Razón: neutral ya
  es el modo restrictivo (núcleo + 1-2 líderes, cripto a mitad de tope) y
  todas las órdenes siguen protegidas por stop-loss y límites duros.
  Journalear siempre "régimen: neutral por VIX no disponible". Esta
  excepción aplica SOLO al VIX: si faltan precios/velas de un instrumento
  del universo, ese instrumento se excluye de la corrida; si falta el
  snapshot o el state, se aborta sin operar como siempre.
- **Fuerza relativa sectorial:** para cada ETF sectorial del universo (XLK,
  XLE, XLF, XLV, XLI, XLP, XLU), comparar su retorno de 63 días contra el
  retorno de 63 días de SPY en el mismo período (fuerza relativa = retorno
  sector − retorno SPY). Ordenar de mayor a menor: los de arriba son los
  "líderes" del ranking sectorial.
- **Cripto (momentum 30 / 90 días + SMA50):** mismo cálculo de retorno que
  el momentum absoluto (misma convención de índices sobre la lista
  ascendente: `close[-1] / close[-31] - 1` y `close[-1] / close[-91] - 1`)
  pero sobre ventanas de 30 y 90 velas diarias, más la SMA50 de cada
  símbolo cripto (BTC, ETH), independiente del régimen de acciones —
  cripto tiene su propio ciclo.

## Reglas de decisión

- **Risk-on:** sobreponderar los 2-3 sectores con mejor fuerza relativa del
  ranking, más QQQ. Cripto permitida hasta su tope de riesgo (35%).
- **Neutral:** núcleo en SPY más 1-2 sectores líderes del ranking. Reducir
  la exposición cripto objetivo a la mitad de su tope (≈17.5%).
- **Risk-off:** rotar hacia los defensivos (GLD, TLT, XLP, XLU). Sin
  aperturas nuevas de riesgo (equity growth/sectorial). Cripto: solo
  mantener posiciones existentes o reducir, nunca abrir.
- **Entrada:** solo instrumentos con momentum positivo (63/126 días para
  equities, 30/90 para cripto) Y precio por encima de su SMA50.
- **Salida:** stop-loss del 12% (siempre presente en la orden de apertura,
  aplicado automáticamente del lado de eToro); o pérdida de tendencia
  (precio cruza por debajo de la SMA200); o, para sectoriales, caída del
  símbolo al fondo del ranking de fuerza relativa.

## Rol del agente como juez

Las señales generan propuestas, no órdenes automáticas. El agente revisa
cada propuesta con criterio antes de convertirla en una llamada a
`place_order.py`:
- Coherencia entre señales (¿momentum y tendencia apuntan en la misma
  dirección? ¿el régimen de VIX es consistente con lo que proponen las
  señales sectoriales?).
- Eventos conocidos relevantes (earnings, reuniones de la Fed, halvings de
  cripto, feriados de mercado) que puedan invalidar la señal en el corto
  plazo. Evaluá estos eventos SOLO con tu conocimiento general y lo que
  muestren los propios datos de precio/volatilidad (gaps, volumen, VIX). El
  runner NO concede herramientas web (decisión deliberada: reduce la
  superficie de prompt-injection en un agente que mueve dinero) — NO tenés
  acceso web y NO debés intentar conseguirlo. Si la incertidumbre por un
  evento que recordás es alta, la respuesta es reducir o no operar (regla de
  oro).
- Tamaño relativo de la propuesta dentro del portfolio y frente a las
  demás propuestas de la corrida.

El agente puede **rechazar** una propuesta o **reducir** su tamaño frente a
lo que sugiere la señal cruda. Nunca puede **ampliarla** más allá de lo que
ya valida `scripts/risk.py` — el juicio del agente solo resta riesgo, nunca
lo suma. Toda decisión (aceptar, reducir, rechazar) se journalea con su
razonamiento antes de, o en lugar de, llamar a `place_order.py`.

## Nota de auto-observación (evitar falsa alarma recurrente)

El archivo `reports/<stamp>-<modo>.log` de TU PROPIA corrida en curso siempre
se ve VACÍO mientras corrés: el runner bufferiza tu salida y la escribe recién
cuando terminás. NUNCA interpretes el .log más reciente (el que coincide con tu
ventana de ejecución actual) como evidencia de una corrida abortada. La
detección de corridas abortadas NO es tu tarea: el runner las journalea como
ABORTADA en state/journal.md automáticamente. Para saber si una corrida
anterior falló, consultá el journal, no los .log.

## Cierre de corrida

Al terminar (haya operado o no), el agente escribe:
- Un reporte legible en `reports/<YYYY-MM-DD-HHMM>-<modo>.md` (por ejemplo
  `reports/2026-08-04-1000-equities.md` o `reports/2026-08-04-1530-crypto.md`),
  con: régimen de mercado detectado, ranking de señales calculado,
  propuestas generadas, y para cada una si se ejecutó, se bloqueó o se
  descartó por criterio del agente — y por qué en cada caso —, más el
  estado final del portfolio tras la corrida.
  **Hora del reporte:** el título del reporte y todas las horas mencionadas
  en su cuerpo deben usar la MISMA hora local del sistema que usa el
  nombre del archivo (scripts/runner.sh stampea ese nombre con `date`,
  hora local), con offset explícito — por ejemplo
  `# Corrida cripto — 2026-08-05 19:03 -03`. UTC puede aparecer únicamente
  entre paréntesis como referencia secundaria (ej:
  `2026-08-05 19:03 -03 (22:03 UTC)`), nunca como la hora principal. Razón:
  los nombres de archivo usan hora local; títulos en UTC confunden al
  operador.
- Una entrada en `state/journal.md` por **cada decisión tomada durante la
  corrida**, incluida la decisión de no operar (por ejemplo: snapshot
  fallido, análisis incompleto, régimen indeterminado, o simplemente
  ninguna propuesta pasó el filtro de juicio). El journal es el registro de
  auditoría — nada se decide sin dejar rastro ahí.

# RISK.md — Límites de riesgo (fuente de verdad legible)

Este documento explica en prosa los límites de riesgo del agente. La fuente
de verdad **ejecutable** es `scripts/risk.py` (función `validate()`), que se
aplica automáticamente a toda orden que pase por `scripts/place_order.py`.
Ante cualquier discrepancia entre este documento y el código, manda el
código — este archivo existe para que el agente (y cualquier humano) entienda
el *por qué*, no para redefinir los límites.

Ninguno de estos límites es negociable por el agente: no se pueden ampliar,
eludir, ni pedir que se relajen para una corrida puntual.

## Límites duros

1. **Tamaño máximo por posición: 25% del valor del portfolio.** Ninguna
   posición individual (nueva, o resultante de sumar a una ya existente en el
   mismo símbolo) puede representar más del 25% del valor total del
   portfolio (cash + posiciones). Se evalúa antes de cada apertura.
2. **Exposición cripto total: máximo 35% del portfolio.** La suma de todas
   las posiciones cripto (BTC, ETH, en cualquiera de sus variantes de símbolo
   reconocidas) no puede superar el 35% del valor total del portfolio.
3. **Stop-loss obligatorio, máximo 12%.** Toda apertura debe llevar un
   stop-loss porcentual mayor a 0% y menor o igual a 12%. Una orden sin
   stop-loss, o con un stop-loss fuera de ese rango, se bloquea antes de
   llegar a la API.
4. **Modo defensivo por drawdown: -25% desde el máximo histórico.** Si el
   valor del portfolio cae 25% o más desde su pico histórico (según
   `state/equity.csv`), se bloquea toda apertura nueva — solo se permiten
   cierres, hasta que el drawdown se recupere por debajo de ese umbral.
5. **DRY_RUN por default (fail-safe).** Si la variable de entorno `DRY_RUN`
   no está definida, o vale algo distinto de `"0"`, ninguna orden llega a
   ejecutarse contra la API real: se journalea como simulación y termina con
   éxito (sin efectos reales). Solo con `DRY_RUN=0` explícito se ejecutan
   órdenes reales.
6. **No-duplicación: no se recompra un símbolo con exposición existente.**
   Si el símbolo (normalizado) de una apertura ya tiene CUALQUIER exposición
   en el state -- una posición real, o una pendiente/local (`pending`,
   `pending-open:`, `local-open:`) con `valueUsd` positivo --, la apertura se
   bloquea sin excepciones, ANTES de evaluar el tope de 25% (que queda como
   defensa en profundidad, no como primera línea). Mantener una posición
   existente cuya señal sigue siendo válida = no recomprar; rebalancear =
   cerrarla en la corrida actual y esperar la próxima para reabrir. Hallazgo
   de la auditoría pre-producción que motivó esta regla: sin ella, 3
   corridas idénticas del agente podían construir 59% de exposición real
   donde el agente creía estar en 37%, porque el único freno (25% por
   símbolo) frena tarde -- recién en la 3ra unidad de recompra, no en la 1ra.
7. **Exposición agregada máxima: 70% del portfolio.** La suma de TODAS las
   posiciones (cualquier símbolo, real o pendiente/local) más el monto de
   una apertura nueva no puede superar el 70% del valor total del
   portfolio (`MAX_TOTAL_EXPOSURE_PCT` en `scripts/risk.py`). Cierra el
   hueco que dejaba el tope de 25% por símbolo solo: N símbolos distintos,
   cada uno individualmente bajo el 25%, podían sumar una concentración
   total sin ningún techo (3 símbolos al 25% cada uno = 75% era legal antes
   de este tope).
8. **Presupuesto de órdenes: máximo 3 por corrida y 6 por día -- SOLO
   aperturas.** En código (no solo en prosa de PLAYBOOK.md):
   `scripts/place_order.py` trackea en `state/.run_orders.json` cuántas
   APERTURAS se ejecutaron (dry-run, reales, o de resultado ambiguo --
   nunca las bloqueadas) bajo el `ETOROAGENT_RUN_ID` de la corrida actual
   (hasta 3; `runner.sh` exporta esa variable con un id distinto por
   corrida) y en el día calendario local (hasta 6). Sin `ETOROAGENT_RUN_ID`
   seteada (invocación manual), se usa un id sintético `manual-YYYY-MM-DD`
   -- el tope de 3 por "corrida" aplica igual, por día. Agotado cualquiera
   de los dos topes, toda APERTURA se bloquea (exit 2) sin llegar a la API,
   hasta la próxima corrida o el próximo día. Los **CIERRES nunca chequean
   ni consumen este presupuesto** -- reducir riesgo no puede depender de
   cuántas órdenes se ejecutaron antes (mismo principio fail-safe que rige
   el bloqueo por reconciliación pendiente, ver PLAYBOOK.md
   §Reconciliación tras corrida abortada: los cierres siguen permitidos
   igual). Si `state/.run_orders.json` existe pero es ilegible/corrupto, se
   trata como presupuesto AGOTADO para aperturas (fail-closed) en vez de
   reiniciar los contadores en 0.

## Límites adicionales (mismo nivel de exigencia)

- **Universo cerrado.** Solo se puede operar sobre los símbolos listados en
  `UNIVERSE` de `scripts/risk.py` (los ETFs/índices del universo de equities
  más BTC/ETH en sus variantes de símbolo reconocidas). Un símbolo fuera de
  ese universo se bloquea aunque parezca un instrumento razonable: el motor
  no asume que "no está en el set de cripto" significa "es un equity
  permitido" — si no está en el universo cerrado, no se opera.
- **La exposición pendiente cuenta como exposición real.** Posiciones u
  órdenes en estado "pendiente" (aperturas pendientes detectadas por el
  snapshot, o exposición registrada localmente tras enviar una orden dentro
  de la misma corrida) se contabilizan contra los topes de posición y de
  cripto exactamente igual que una posición ya confirmada. No se puede
  eludir un límite abriendo una posición nueva del mismo símbolo o categoría
  mientras hay una pendiente sin confirmar.
- **State desactualizado (más de 24 horas) bloquea toda operación.** Si el
  estado persistido del portfolio tiene más de 24 horas de antigüedad, o su
  fecha de actualización falta o no se puede interpretar, se bloquea tanto la
  apertura como el cierre de posiciones — hay que refrescar el estado del
  portfolio primero.

## Filosofía

Estos límites están escritos en código determinista, no en el criterio del
agente. El agente puede ser más conservador que estos límites (proponer
menos, reducir el tamaño de una orden, no operar), pero nunca puede
ampliarlos ni pedir una excepción. Ante cualquier ambigüedad sobre si una
orden los cumple, la respuesta es no operar y dejarlo registrado en el
journal.

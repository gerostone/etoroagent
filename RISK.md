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

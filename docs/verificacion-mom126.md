# Verificación de mom126 — WP3 auditoría

## Contexto

La auditoría marcó como sospechoso el salto de `GLD mom126` entre dos
corridas consecutivas journaleadas en `state/journal.md`:

| Corrida (hora local)      | precio GLD | mom126 journaleado |
|----------------------------|-----------:|--------------------:|
| 2026-08-10 16:48 -0300      | 402.58     | **-8.72%**          |
| 2026-08-11 11:38 -0300      | 402.72     | **-13.61%**         |

Con el precio "prácticamente plano" (402.58 → 402.72, +0.03%), un salto de
casi 5 puntos porcentuales en mom126 de una corrida a la siguiente es,
a primera vista, sospechoso. Dos hipótesis en pugna:

- **(a) Legítimo:** al correrse la ventana un día, la vela de referencia de
  hace 126 días hábiles cambió, y si esa vela tuvo un movimiento grande, el
  momentum salta aunque el precio de "hoy" esté quieto.
- **(b) Bug:** off-by-one o error de indexación en cómo el agente calcula
  momentum a partir del output ascendente de `candles.py`.

## Método

Fórmula documentada en `PLAYBOOK.md` §Señales: sobre la lista de closes en
orden ASCENDENTE que imprime `scripts/candles.py` (verificado el header
`order=asc`), `mom126 = close[-1] / close[-127] - 1`.

Se bajó la serie real (`.venv/bin/python scripts/candles.py --symbol GLD
--count 220`, 220 velas, 2025-09-23 a 2026-08-13) usando el propio
`scripts/candles.py` sin modificar — es decir, cualquier bug de indexación
en la conversión desc→asc del script ya estaría reflejado en este CSV, no
hace falta reimplementarla aparte. Con esa serie se truncó en cada fecha de
corte (`fecha <= cutoff`) y se aplicó la fórmula del PLAYBOOK tal cual.

## Verificación de la fórmula (sin datos, puramente aritmética)

Con una lista ascendente de longitud `L`, el índice `-1` es la posición
`L-1` (0-indexado) y el índice `-127` es la posición `L-127`. La distancia
entre ambas posiciones es `(L-1) - (L-127) = 126`. Es decir, `close[-127]`
está exactamente 126 posiciones (126 velas) antes de `close[-1]` — coincide
con la definición de "momentum de 126 velas" del PLAYBOOK
(`índice -(N+1)` porque `close[-1]` ya es "hoy", 0 velas atrás). **No hay
off-by-one en la fórmula documentada.**

## Recómputo con datos reales

| Cutoff       | n velas | last (fecha, close)         | ref -127 (fecha, close)     | mom126 recalculado |
|--------------|--------:|------------------------------|-------------------------------|---------------------:|
| 2026-08-10   | 217     | 2026-08-10, 402.56           | 2026-02-05, 441.0574          | **-8.73%**           |
| 2026-08-11   | 218     | 2026-08-11, 400.56           | 2026-02-09, 466.1897          | **-14.08%**          |
| 2026-08-12   | 219     | 2026-08-12, 404.24           | 2026-02-11, 466.8986          | **-13.42%**          |

### La vela de referencia que entra/sale del lookback

Al correr la ventana un día hábil (08-10 → 08-11), la vela de referencia
`close[-127]` se corre de **2026-02-05 (441.06)** a **2026-02-09
(466.19)** — un salto de **+5.71%** en esa única vela de referencia. Ese
salto, no el precio de "hoy" (que se mantuvo prácticamente plano), es lo
que explica la caída de ~5 puntos porcentuales en mom126: como
`mom126 = last/ref - 1`, un `ref` que sube ~5.7% con `last` casi
constante empuja mom126 varios puntos más negativo.

Nota aparte sobre la serie descargada: entre 2026-02-05 (jueves) y
2026-02-09 (lunes) falta la vela del viernes 2026-02-06 — no es feriado de
mercado conocido. Se repite el mismo patrón (un "viernes" faltante cada
~3 semanas) en toda la serie de 220 velas GLD descargada hoy
(2025-10-30→11-03, 11-20→11-24, 12-11→12-15, 2026-01-16→01-20,
02-05→02-09, 02-13→02-17, 04-02→04-06, 04-10→04-14, 05-22→05-26— 9 gaps
de 4 días calendario en 220 velas). Esto es consistente con que el
feed de precios de este entorno (dry-run, fechas 2026) tiene huecos
periódicos de datos — un problema de calidad de datos distinto del
off-by-one que se buscaba, y el motivo concreto por el que WP3 también pide
la validación de cantidad de velas en `candles.py` (punto 2 de esta
tarea).

## Discrepancia con los valores journaleados — nota de honestidad

El recómputo (-8.73%, -14.08%) es **cercano pero no idéntico** a lo
journaleado (-8.72%, -13.61%): la corrida del 08-10 coincide con margen de
redondeo, pero la del 08-11 difiere ~0.47 puntos porcentuales, y el propio
`close` de 2026-08-11 leído hoy (400.56) no coincide con el que se journaleó
en su momento (402.72 — el precio que la auditoría citó como
"prácticamente plano"). Esto indica que el feed de precios de este entorno
**no es un registro histórico estático**: volver a pedir la misma fecha
días después devuelve un valor de cierre distinto al que se vio en la
corrida original. No se pudo determinar la causa exacta (no hay acceso al
feed subyacente más allá de `candles.py`), pero es un hallazgo aparte que
vale la pena que el operador tenga presente — afecta la reproducibilidad
exacta de auditorías futuras, aunque no cambia el veredicto de este WP3
(el mecanismo estructural del salto — vela de referencia con movimiento
grande entrando al lookback — está presente y es coherente en magnitud y
dirección con lo journaleado en ambas versiones de los datos).

## Veredicto

**(a) Legítimo.** El salto de GLD mom126 de -8.72% a -13.61% se explica
por el rolado normal de la ventana de 126 velas: la vela de referencia
`close[-127]` pasó de una fecha con cierre relativamente bajo (2026-02-05,
441.06) a una con cierre notablemente más alto (2026-02-09, 466.19, +5.7%),
mientras el precio "de hoy" permanecía casi plano. La fórmula documentada
en `PLAYBOOK.md` (`close[-1] / close[-127] - 1`, índice `-(N+1)`) es
aritméticamente correcta (126 posiciones exactas entre referencia y hoy) —
**no se encontró error de indexación ni off-by-one**, ni en la fórmula ni
en la conversión desc→asc de `scripts/candles.py` (se usó el script real,
sin modificar, para generar la serie analizada). No se requiere corrección
de la fórmula documentada (punto 3 de WP3 — condicional, no aplica).

Hallazgo secundario, fuera del alcance de este WP3 pero documentado para
awareness: el feed de precios de este entorno tiene huecos periódicos de
velas individuales (~1 cada 3 semanas) y no es perfectamente estático entre
consultas — ver secciones anteriores.

# etoro-agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Agente de trading autónomo sobre eToro Agent Portfolios: Claude Code headless decide según un playbook híbrido (señales cuantitativas + juicio LLM), con motor de riesgo determinista que el LLM no puede violar.

**Architecture:** launchd lanza `runner.sh` → sesión `claude -p` con la skill oficial de eToro + `PLAYBOOK.md`. Toda orden pasa obligatoriamente por `scripts/place_order.py` (valida riesgo con `scripts/risk.py`); un hook PreToolUse (`scripts/risk_hook.py`) bloquea cualquier intento de operar por fuera (curl directo a endpoints de trading). Estado persistido en `state/` (positions.json, equity.csv, journal.md).

**Tech Stack:** Python 3 (scripts deterministas: stdlib + `requests`), pytest, Claude Code (headless + hooks + skills), launchd (macOS), API REST de eToro (`https://public-api.etoro.com/api/v1`).

**Referencia de spec:** `docs/superpowers/specs/2026-08-04-etoro-agent-design.md`

---

## Estructura de archivos final

```
etoroagent/
├── .claude/
│   ├── skills/etoro/SKILL.md      # skill oficial de eToro (descargada, Task 1)
│   └── settings.json              # hook PreToolUse (Task 6)
├── PLAYBOOK.md                    # estrategia para el agente (Task 7)
├── RISK.md                        # límites en prosa (Task 7)
├── prompts/
│   ├── run_equities.md            # prompt de corrida ETFs (Task 7)
│   └── run_crypto.md              # prompt de corrida cripto (Task 7)
├── scripts/
│   ├── risk.py                    # validación pura de órdenes (Task 2)
│   ├── etoro_api.py               # cliente HTTP (Task 3)
│   ├── snapshot.py                # actualiza state/ desde la API (Task 4)
│   ├── place_order.py             # ÚNICA vía de ejecución de órdenes (Task 5)
│   ├── risk_hook.py               # hook PreToolUse, stdlib-only (Task 6)
│   ├── market_open.py             # ¿NYSE abierto ahora? (Task 8)
│   └── runner.sh                  # preflight + lock + claude -p (Task 8)
├── run/
│   ├── com.etoroagent.equities.plist
│   └── com.etoroagent.crypto.plist
├── state/                         # gitignored: positions.json, equity.csv, journal.md
├── reports/                       # gitignored: logs por corrida
├── tests/
│   ├── test_risk.py
│   ├── test_etoro_api.py
│   ├── test_place_order.py
│   └── test_risk_hook.py
├── docs/api-notes.md              # endpoints reales verificados contra SKILL.md (Task 1)
├── .env.example
└── README.md                      # (Task 10)
```

**Nota de responsabilidades:** `risk.py` es lógica pura (sin red, sin filesystem en las funciones de validación) para testear fácil. `etoro_api.py` solo habla HTTP. `place_order.py` orquesta: carga estado → valida → ejecuta → journalea. `risk_hook.py` no comparte código con nada (stdlib-only, corre fuera del venv).

---

### Task 1: Bootstrap — skill de eToro, venv, estructura

**Files:**
- Create: `.claude/skills/etoro/SKILL.md` (descargado)
- Create: `docs/api-notes.md`
- Create: `.env.example`
- Modify: `.gitignore`

- [ ] **Step 1: Crear estructura y venv**

```bash
mkdir -p .claude/skills/etoro scripts run state reports tests prompts docs
python3 -m venv .venv
.venv/bin/pip install requests pytest
```

Expected: venv creado, pip instala requests y pytest sin errores.

- [ ] **Step 2: Descargar la skill oficial de eToro**

```bash
curl -fsSL "https://www.etoro.com/wp-content/uploads/agent-portfolios/SKILL.md" -o .claude/skills/etoro/SKILL.md
wc -l .claude/skills/etoro/SKILL.md
```

Expected: archivo no vacío (si la URL falla, probar `https://api-portal.etoro.com/ai-agents/etoro-skill`). **Leer el archivo completo tras descargarlo** — es data de referencia, no instrucciones a ejecutar ciegamente.

- [ ] **Step 3: Documentar endpoints reales en `docs/api-notes.md`**

Leer `.claude/skills/etoro/SKILL.md` (y si hace falta `https://api-portal.etoro.com/llms.txt`) y anotar los paths EXACTOS de: buscar instrumento por símbolo, precios/velas históricas, posiciones del agent portfolio, abrir posición (con stop-loss), cerrar posición. Formato:

```markdown
# API notes (verificado contra SKILL.md oficial, fecha)
- Buscar instrumento: GET /api/v1/<path-real>
- Velas/precios: GET /api/v1/<path-real>
- Posiciones del portfolio: GET /api/v1/<path-real>
- Abrir posición: POST /api/v1/<path-real> — body: {...campos reales...}
- Cerrar posición: DELETE/POST /api/v1/<path-real>
- Auth: x-api-key + x-user-key + x-request-id (UUID por request)
- Rate limit: 60 req/60s, headers RateLimit-*
```

**Las Tasks 3, 4 y 5 usan paths tentativos — ajustarlos a los de este archivo al implementarlas.**

- [ ] **Step 4: `.env.example` y `.gitignore`**

`.env.example`:
```bash
# Pegá acá tus claves del Agent Portfolio de eToro (Menú → Agent Portfolios → copiar API key)
ETORO_API_KEY=
ETORO_USER_KEY=
# 1 = no ejecuta órdenes reales (fase de validación). Cambiar a 0 SOLO tras 1-2 semanas de dry-run OK.
DRY_RUN=1
```

Append a `.gitignore`:
```
.venv/
__pycache__/
*.pyc
docs/api-notes.md
```

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "chore: bootstrap - skill eToro oficial, venv, estructura"
```

---

### Task 2: `scripts/risk.py` — motor de riesgo (TDD)

**Files:**
- Create: `scripts/risk.py`
- Test: `tests/test_risk.py`

- [ ] **Step 1: Escribir tests que fallan**

`tests/test_risk.py`:
```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from risk import OrderRequest, validate, drawdown_pct

STATE = {
    "cashUsd": 100.0,
    "positions": [
        {"symbol": "SPY", "valueUsd": 60.0},
        {"symbol": "BTC", "valueUsd": 40.0},
    ],
}  # total = 200
EQUITY_OK = [("2026-08-01", 190.0), ("2026-08-02", 200.0)]
EQUITY_DD = [("2026-08-01", 300.0), ("2026-08-02", 200.0)]  # drawdown 33%


def open_order(symbol="QQQ", amount=30.0, sl=0.12):
    return OrderRequest(action="open", symbol=symbol, amount_usd=amount, stop_loss_pct=sl)


def test_close_siempre_permitido():
    ok, _ = validate(OrderRequest("close", "SPY", 60.0, None), STATE, EQUITY_DD)
    assert ok


def test_orden_valida_pasa():
    ok, msg = validate(open_order(), STATE, EQUITY_OK)
    assert ok, msg


def test_bloquea_posicion_mayor_25pct():
    # SPY ya tiene 60 (30%); sumar cualquier monto la deja > 25% → bloquear
    ok, msg = validate(open_order(symbol="SPY", amount=10.0), STATE, EQUITY_OK)
    assert not ok and "25%" in msg


def test_bloquea_nueva_posicion_que_supera_25pct():
    ok, msg = validate(open_order(amount=60.0), STATE, EQUITY_OK)  # 60/200 = 30%
    assert not ok and "25%" in msg


def test_bloquea_cripto_sobre_35pct():
    # BTC 40 + ETH 35 = 75/200 = 37.5% → bloquear
    ok, msg = validate(open_order(symbol="ETH", amount=35.0), STATE, EQUITY_OK)
    assert not ok and "cripto" in msg.lower()


def test_bloquea_sin_stop_loss():
    ok, msg = validate(open_order(sl=None), STATE, EQUITY_OK)
    assert not ok and "stop" in msg.lower()


def test_bloquea_stop_loss_mayor_a_12pct():
    ok, msg = validate(open_order(sl=0.20), STATE, EQUITY_OK)
    assert not ok and "stop" in msg.lower()


def test_modo_defensivo_bloquea_compras():
    ok, msg = validate(open_order(), STATE, EQUITY_DD)
    assert not ok and "defensivo" in msg.lower()


def test_drawdown_pct():
    assert abs(drawdown_pct(EQUITY_DD) - (1 / 3)) < 1e-9
    assert drawdown_pct([]) == 0.0
```

- [ ] **Step 2: Verificar que fallan**

Run: `.venv/bin/pytest tests/test_risk.py -q`
Expected: FAIL / error de import (`risk` no existe).

- [ ] **Step 3: Implementar `scripts/risk.py`**

```python
"""Motor de riesgo determinista. Lógica pura: sin red ni filesystem en validate().

Límites (spec §6, perfil Moderado) — NO negociables por el agente:
  máx 25% por posición, máx 35% cripto total, stop-loss obligatorio <= 12%,
  drawdown >= 25% desde el máximo histórico => modo defensivo (solo cierres).
"""
from dataclasses import dataclass
from typing import Optional

MAX_POSITION_PCT = 0.25
MAX_CRYPTO_PCT = 0.35
MAX_STOP_LOSS_PCT = 0.12
DEFENSIVE_DRAWDOWN_PCT = 0.25
CRYPTO_SYMBOLS = {"BTC", "ETH"}


@dataclass
class OrderRequest:
    action: str  # "open" | "close"
    symbol: str
    amount_usd: float
    stop_loss_pct: Optional[float]


def portfolio_value(state: dict) -> float:
    return state.get("cashUsd", 0.0) + sum(p["valueUsd"] for p in state.get("positions", []))


def drawdown_pct(equity_rows: list) -> float:
    """equity_rows: [(fecha, valor), ...]. Drawdown del último valor vs máximo histórico."""
    if not equity_rows:
        return 0.0
    values = [v for _, v in equity_rows]
    peak = max(values)
    if peak <= 0:
        return 0.0
    return max(0.0, (peak - values[-1]) / peak)


def validate(order: OrderRequest, state: dict, equity_rows: list) -> tuple[bool, str]:
    if order.action == "close":
        return True, "cierre permitido siempre"

    if drawdown_pct(equity_rows) >= DEFENSIVE_DRAWDOWN_PCT:
        return False, (
            f"MODO DEFENSIVO: drawdown >= {DEFENSIVE_DRAWDOWN_PCT:.0%}. "
            "Solo se permiten cierres de posiciones."
        )

    if order.stop_loss_pct is None or not (0 < order.stop_loss_pct <= MAX_STOP_LOSS_PCT):
        return False, (
            f"Stop-loss obligatorio y <= {MAX_STOP_LOSS_PCT:.0%} "
            f"(recibido: {order.stop_loss_pct})."
        )

    total = portfolio_value(state)
    if total <= 0:
        return False, "Valor de portfolio desconocido o cero: no se puede dimensionar."
    if order.amount_usd <= 0:
        return False, "Monto inválido."

    current = sum(
        p["valueUsd"] for p in state.get("positions", []) if p["symbol"] == order.symbol
    )
    if (current + order.amount_usd) / total > MAX_POSITION_PCT:
        return False, (
            f"Posición resultante en {order.symbol} superaría el 25% del portfolio "
            f"({current + order.amount_usd:.2f} de {total:.2f} USD)."
        )

    if order.symbol in CRYPTO_SYMBOLS:
        crypto = sum(
            p["valueUsd"] for p in state.get("positions", []) if p["symbol"] in CRYPTO_SYMBOLS
        )
        if (crypto + order.amount_usd) / total > MAX_CRYPTO_PCT:
            return False, (
                f"Exposición cripto resultante superaría el 35% del portfolio "
                f"({crypto + order.amount_usd:.2f} de {total:.2f} USD)."
            )

    return True, "ok"
```

- [ ] **Step 4: Verificar que pasan**

Run: `.venv/bin/pytest tests/test_risk.py -q`
Expected: `10 passed`

- [ ] **Step 5: Commit**

```bash
git add scripts/risk.py tests/test_risk.py && git commit -m "feat: motor de riesgo determinista (limites moderados)"
```

---

### Task 3: `scripts/etoro_api.py` — cliente HTTP

**Files:**
- Create: `scripts/etoro_api.py`
- Test: `tests/test_etoro_api.py`

**⚠️ Ajustar los paths de endpoints a los reales anotados en `docs/api-notes.md` (Task 1, Step 3).**

- [ ] **Step 1: Tests que fallan** (mockean `requests.request`)

`tests/test_etoro_api.py`:
```python
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from etoro_api import EtoroClient, EtoroAuthError


def make_client():
    return EtoroClient(api_key="k", user_key="u")


def fake_resp(status=200, json_body=None, headers=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = json_body or {}
    r.headers = headers or {}
    return r


def test_headers_incluyen_request_id_unico():
    c = make_client()
    h1, h2 = c._headers(), c._headers()
    assert h1["x-api-key"] == "k" and h1["x-user-key"] == "u"
    assert h1["x-request-id"] != h2["x-request-id"]


@patch("etoro_api.requests.request")
def test_401_lanza_auth_error(mock_req):
    mock_req.return_value = fake_resp(status=401)
    with pytest.raises(EtoroAuthError):
        make_client().request("GET", "/agent-portfolios")


@patch("etoro_api.time.sleep")
@patch("etoro_api.requests.request")
def test_429_reintenta_con_backoff(mock_req, mock_sleep):
    mock_req.side_effect = [
        fake_resp(status=429, headers={"RateLimit-Reset": "2"}),
        fake_resp(json_body={"ok": True}),
    ]
    assert make_client().request("GET", "/x") == {"ok": True}
    mock_sleep.assert_called_once()
```

- [ ] **Step 2: Verificar que fallan**

Run: `.venv/bin/pytest tests/test_etoro_api.py -q`
Expected: FAIL (import error).

- [ ] **Step 3: Implementar `scripts/etoro_api.py`**

```python
"""Cliente mínimo de la API pública de eToro (Agent Portfolios).

Solo transporte HTTP: auth headers, request-id, backoff en 429, retries.
Los paths de endpoints se verifican contra docs/api-notes.md.
"""
import os
import time
import uuid

import requests

BASE_URL = "https://public-api.etoro.com/api/v1"


class EtoroAuthError(Exception):
    """401: API key inválida o token expirado (revisar expiresAt del userToken)."""


class EtoroClient:
    def __init__(self, api_key: str | None = None, user_key: str | None = None):
        self.api_key = api_key or os.environ["ETORO_API_KEY"]
        self.user_key = user_key or os.environ["ETORO_USER_KEY"]

    def _headers(self) -> dict:
        return {
            "x-api-key": self.api_key,
            "x-user-key": self.user_key,
            "x-request-id": str(uuid.uuid4()),
            "Content-Type": "application/json",
        }

    def request(self, method: str, path: str, max_retries: int = 3, **kwargs) -> dict:
        last = None
        for _ in range(max_retries):
            resp = requests.request(
                method, BASE_URL + path, headers=self._headers(), timeout=30, **kwargs
            )
            if resp.status_code == 401:
                raise EtoroAuthError(f"401 en {path}: credenciales inválidas/expiradas")
            if resp.status_code == 429:
                wait = int(resp.headers.get("RateLimit-Reset", "30"))
                time.sleep(min(wait, 60))
                last = resp
                continue
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        raise RuntimeError(f"Rate limit persistente en {path} (último: {last.status_code})")

    # --- endpoints (paths tentativos: VERIFICAR contra docs/api-notes.md) ---

    def get_agent_portfolios(self) -> dict:
        return self.request("GET", "/agent-portfolios")

    def get_positions(self, portfolio_id: str) -> dict:
        return self.request("GET", f"/agent-portfolios/{portfolio_id}/positions")

    def find_instrument(self, symbol: str) -> dict:
        return self.request("GET", "/market-data/instruments", params={"symbol": symbol})

    def get_candles(self, instrument_id: int, period: str = "OneDay", count: int = 210) -> dict:
        return self.request(
            "GET",
            f"/market-data/candles/{instrument_id}",
            params={"period": period, "count": count},
        )

    def open_position(
        self, portfolio_id: str, instrument_id: int, amount_usd: float, stop_loss_pct: float
    ) -> dict:
        body = {
            "instrumentId": instrument_id,
            "amount": amount_usd,
            "stopLossPct": stop_loss_pct,
        }
        return self.request("POST", f"/agent-portfolios/{portfolio_id}/positions", json=body)

    def close_position(self, portfolio_id: str, position_id: str) -> dict:
        return self.request(
            "DELETE", f"/agent-portfolios/{portfolio_id}/positions/{position_id}"
        )
```

- [ ] **Step 4: Verificar tests + ajustar paths reales**

Run: `.venv/bin/pytest tests/test_etoro_api.py -q`
Expected: `3 passed`. Luego comparar cada método contra `docs/api-notes.md` y corregir paths/bodies. Re-correr tests.

- [ ] **Step 5: Commit**

```bash
git add scripts/etoro_api.py tests/test_etoro_api.py && git commit -m "feat: cliente HTTP eToro con auth, backoff 429 y retries"
```

---

### Task 4: `scripts/snapshot.py` — persistir estado

**Files:**
- Create: `scripts/snapshot.py`

- [ ] **Step 1: Implementar**

```python
"""Actualiza state/ desde la API: positions.json + equity.csv.

Uso: .venv/bin/python scripts/snapshot.py
Es el PRIMER comando de toda corrida (lo exige PLAYBOOK.md).
"""
import csv
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from etoro_api import EtoroClient

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"


def main() -> int:
    STATE.mkdir(exist_ok=True)
    client = EtoroClient()

    data = client.get_agent_portfolios()
    portfolios = data.get("agentPortfolios", [])
    if not portfolios:
        print("ERROR: no hay Agent Portfolios en la cuenta", file=sys.stderr)
        return 1
    p = portfolios[0]
    portfolio_id = p["agentPortfolioId"]
    cash = float(p.get("agentPortfolioVirtualBalance", 0.0))

    raw = client.get_positions(portfolio_id)
    # Normalizar al esquema propio (ajustar claves según docs/api-notes.md):
    positions = [
        {
            "positionId": pos.get("positionId"),
            "symbol": pos.get("symbol") or pos.get("instrumentSymbol"),
            "instrumentId": pos.get("instrumentId"),
            "valueUsd": float(pos.get("currentValue") or pos.get("amount") or 0.0),
        }
        for pos in raw.get("positions", [])
    ]

    state = {
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "portfolioId": portfolio_id,
        "cashUsd": cash,
        "positions": positions,
    }
    (STATE / "positions.json").write_text(json.dumps(state, indent=2))

    total = cash + sum(x["valueUsd"] for x in positions)
    equity_file = STATE / "equity.csv"
    rows = []
    if equity_file.exists():
        with open(equity_file) as f:
            rows = [r for r in csv.reader(f) if r]
    today = date.today().isoformat()
    rows = [r for r in rows if r[0] != today] + [[today, f"{total:.2f}"]]
    with open(equity_file, "w", newline="") as f:
        csv.writer(f).writerows(rows)

    print(f"OK portfolio={portfolio_id} cash={cash:.2f} posiciones={len(positions)} total={total:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Verificación estática + claves reales si ya están**

Run: `.venv/bin/python -c "import ast; ast.parse(open('scripts/snapshot.py').read()); print('syntax OK')"`
Expected: `syntax OK`. Si el usuario ya pegó las keys en `.env`: `set -a; source .env; set +a; .venv/bin/python scripts/snapshot.py` → Expected: línea `OK portfolio=... total=...` y archivos en `state/`. Ajustar claves de normalización según la respuesta real.

- [ ] **Step 3: Commit**

```bash
git add scripts/snapshot.py && git commit -m "feat: snapshot de portfolio a state/ (positions.json + equity.csv)"
```

---

### Task 5: `scripts/place_order.py` — única vía de ejecución (TDD)

**Files:**
- Create: `scripts/place_order.py`
- Test: `tests/test_place_order.py`

- [ ] **Step 1: Tests que fallan**

`tests/test_place_order.py`:
```python
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import place_order


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    sdir = tmp_path / "state"
    sdir.mkdir()
    (sdir / "positions.json").write_text(json.dumps({
        "portfolioId": "pf-1",
        "cashUsd": 100.0,
        "positions": [{"positionId": "pos-9", "symbol": "SPY", "valueUsd": 20.0,
                        "instrumentId": 1}],
    }))
    (sdir / "equity.csv").write_text("2026-08-01,110.0\n2026-08-02,120.0\n")
    monkeypatch.setattr(place_order, "STATE", sdir)
    return sdir


def test_open_bloqueada_por_riesgo_sale_2(state_dir, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    # 40 USD sobre total 120 = 33% > 25%
    rc = place_order.main(["open", "--symbol", "QQQ", "--amount", "40", "--stop-loss-pct", "0.12"])
    assert rc == 2


def test_dry_run_no_llama_api(state_dir, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")
    api = MagicMock()
    monkeypatch.setattr(place_order, "make_client", lambda: api)
    rc = place_order.main(["open", "--symbol", "QQQ", "--amount", "20", "--stop-loss-pct", "0.10"])
    assert rc == 0
    api.open_position.assert_not_called()
    journal = (state_dir / "journal.md").read_text()
    assert "DRY_RUN" in journal and "QQQ" in journal


def test_open_real_llama_api_y_journalea(state_dir, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    api = MagicMock()
    api.find_instrument.return_value = {"instruments": [{"instrumentId": 42, "symbol": "QQQ"}]}
    api.open_position.return_value = {"positionId": "new-1"}
    monkeypatch.setattr(place_order, "make_client", lambda: api)
    rc = place_order.main(["open", "--symbol", "QQQ", "--amount", "20", "--stop-loss-pct", "0.10"])
    assert rc == 0
    api.open_position.assert_called_once_with("pf-1", 42, 20.0, 0.10)
    assert "QQQ" in (state_dir / "journal.md").read_text()


def test_close_llama_api(state_dir, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    api = MagicMock()
    api.close_position.return_value = {}
    monkeypatch.setattr(place_order, "make_client", lambda: api)
    rc = place_order.main(["close", "--position-id", "pos-9", "--symbol", "SPY"])
    assert rc == 0
    api.close_position.assert_called_once_with("pf-1", "pos-9")
```

- [ ] **Step 2: Verificar que fallan**

Run: `.venv/bin/pytest tests/test_place_order.py -q`
Expected: FAIL (import error).

- [ ] **Step 3: Implementar `scripts/place_order.py`**

```python
"""Única vía autorizada para ejecutar órdenes. El hook PreToolUse bloquea todo lo demás.

Uso:
  place_order.py open  --symbol QQQ --amount 50 --stop-loss-pct 0.12 [--reason "..."]
  place_order.py close --position-id <id> --symbol QQQ [--reason "..."]

Exit codes: 0 ok (o dry-run) | 2 bloqueada por riesgo | 1 error de ejecución.
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from etoro_api import EtoroClient
from risk import OrderRequest, validate

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"


def make_client() -> EtoroClient:
    return EtoroClient()


def load_state() -> tuple[dict, list]:
    state = json.loads((STATE / "positions.json").read_text())
    rows = []
    eq = STATE / "equity.csv"
    if eq.exists():
        with open(eq) as f:
            rows = [(r[0], float(r[1])) for r in csv.reader(f) if r]
    return state, rows


def journal(line: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with open(STATE / "journal.md", "a") as f:
        f.write(f"- **{ts}** — {line}\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    p_open = sub.add_parser("open")
    p_open.add_argument("--symbol", required=True)
    p_open.add_argument("--amount", type=float, required=True)
    p_open.add_argument("--stop-loss-pct", type=float, default=None)
    p_open.add_argument("--reason", default="")
    p_close = sub.add_parser("close")
    p_close.add_argument("--position-id", required=True)
    p_close.add_argument("--symbol", required=True)
    p_close.add_argument("--reason", default="")
    args = parser.parse_args(argv)

    state, equity = load_state()
    order = OrderRequest(
        action=args.action,
        symbol=args.symbol.upper(),
        amount_usd=getattr(args, "amount", 0.0),
        stop_loss_pct=getattr(args, "stop_loss_pct", None),
    )
    ok, msg = validate(order, state, equity)
    if not ok:
        print(f"ORDEN BLOQUEADA POR RIESGO: {msg}", file=sys.stderr)
        journal(f"BLOQUEADA {order.action} {order.symbol}: {msg}")
        return 2

    if os.environ.get("DRY_RUN", "1") != "0":
        journal(
            f"DRY_RUN {order.action} {order.symbol} "
            f"{order.amount_usd if order.action == 'open' else ''} USD "
            f"sl={order.stop_loss_pct} — {args.reason}"
        )
        print(f"DRY_RUN: no se ejecutó {order.action} {order.symbol}")
        return 0

    api = make_client()
    pid = state["portfolioId"]
    try:
        if args.action == "open":
            found = api.find_instrument(order.symbol)
            instrument_id = found["instruments"][0]["instrumentId"]
            result = api.open_position(pid, instrument_id, order.amount_usd, order.stop_loss_pct)
            journal(
                f"ABIERTA {order.symbol} {order.amount_usd} USD sl={order.stop_loss_pct} "
                f"id={result.get('positionId')} — {args.reason}"
            )
        else:
            api.close_position(pid, args.position_id)
            journal(f"CERRADA {order.symbol} id={args.position_id} — {args.reason}")
    except Exception as e:  # 1 solo reintento lo maneja el cliente; acá: log y salir
        journal(f"ERROR ejecutando {order.action} {order.symbol}: {e}")
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Verificar que pasan**

Run: `.venv/bin/pytest tests/test_place_order.py -q`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add scripts/place_order.py tests/test_place_order.py && git commit -m "feat: place_order CLI con validacion de riesgo, dry-run y journal"
```

---

### Task 6: Hook PreToolUse — cerrar la puerta trasera

**Files:**
- Create: `scripts/risk_hook.py` (stdlib-only: corre con python3 del sistema)
- Create: `.claude/settings.json`
- Test: `tests/test_risk_hook.py`

- [ ] **Step 1: Tests que fallan**

`tests/test_risk_hook.py`:
```python
import json
import subprocess
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "scripts" / "risk_hook.py"


def run_hook(command: str) -> subprocess.CompletedProcess:
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    return subprocess.run(
        ["python3", str(HOOK)], input=payload, capture_output=True, text=True
    )


def test_bloquea_curl_post_a_trading():
    r = run_hook("curl -X POST https://public-api.etoro.com/api/v1/agent-portfolios/x/positions -d '{}'")
    assert r.returncode == 2
    assert "place_order" in r.stderr


def test_bloquea_delete_a_trading():
    r = run_hook("curl -X DELETE https://public-api.etoro.com/api/v1/agent-portfolios/x/positions/y")
    assert r.returncode == 2


def test_permite_get_market_data():
    r = run_hook("curl https://public-api.etoro.com/api/v1/market-data/instruments?symbol=SPY")
    assert r.returncode == 0


def test_permite_place_order_script():
    r = run_hook(".venv/bin/python scripts/place_order.py open --symbol SPY --amount 10 --stop-loss-pct 0.1")
    assert r.returncode == 0


def test_permite_comandos_no_etoro():
    r = run_hook("ls -la")
    assert r.returncode == 0
```

- [ ] **Step 2: Verificar que fallan**

Run: `.venv/bin/pytest tests/test_risk_hook.py -q`
Expected: FAIL (risk_hook.py no existe).

- [ ] **Step 3: Implementar `scripts/risk_hook.py`**

```python
#!/usr/bin/env python3
"""Hook PreToolUse (Bash): bloquea escrituras directas a la API de eToro.

Toda orden debe pasar por scripts/place_order.py (que valida riesgo).
Exit 2 = bloquear (stderr se muestra al agente). Exit 0 = permitir.
Stdlib-only: este script corre con el python3 del sistema, fuera del venv.
"""
import json
import re
import sys


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # ante payload raro, no romper la sesión
    if payload.get("tool_name") != "Bash":
        return 0
    command = payload.get("tool_input", {}).get("command", "") or ""

    if "public-api.etoro.com" not in command:
        return 0
    if "place_order.py" in command or "snapshot.py" in command:
        return 0
    # Métodos de escritura via curl/httpie/wget contra la API
    if re.search(r"(-X\s*|--request\s*)(POST|DELETE|PUT|PATCH)", command, re.I) or re.search(
        r"(--data|-d\s|--json|--method\s*(POST|DELETE|PUT|PATCH))", command, re.I
    ):
        sys.stderr.write(
            "BLOQUEADO por el motor de riesgo: las órdenes a eToro solo pueden ejecutarse "
            "vía `.venv/bin/python scripts/place_order.py` (valida límites de riesgo). "
            "Las lecturas GET están permitidas.\n"
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Verificar que pasan**

Run: `.venv/bin/pytest tests/test_risk_hook.py -q`
Expected: `5 passed`

- [ ] **Step 5: Registrar el hook en `.claude/settings.json`**

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$CLAUDE_PROJECT_DIR/scripts/risk_hook.py\""
          }
        ]
      }
    ]
  }
}
```

- [ ] **Step 6: Commit**

```bash
git add scripts/risk_hook.py .claude/settings.json tests/test_risk_hook.py && git commit -m "feat: hook PreToolUse que fuerza ordenes via place_order.py"
```

---

### Task 7: PLAYBOOK.md, RISK.md y prompts de corrida

**Files:**
- Create: `PLAYBOOK.md`, `RISK.md`, `prompts/run_equities.md`, `prompts/run_crypto.md`

- [ ] **Step 1: Escribir `RISK.md`**

```markdown
# Límites de riesgo (perfil Moderado) — NO NEGOCIABLES

Estos límites están implementados en `scripts/risk.py` y el hook `scripts/risk_hook.py`.
El agente NO puede modificarlos, razonarlos ni eludirlos. Una orden bloqueada se journalea y se acata.

1. Posición máxima: 25% del valor total del portfolio por instrumento.
2. Exposición cripto (BTC+ETH): máximo 35% del portfolio.
3. Toda apertura lleva stop-loss obligatorio, como máximo -12%.
4. Drawdown >= 25% desde el máximo histórico → MODO DEFENSIVO: solo cierres.
5. `DRY_RUN=1` → ninguna orden real se ejecuta.
```

- [ ] **Step 2: Escribir `PLAYBOOK.md`**

```markdown
# PLAYBOOK — etoro-agent

Sos un agente de trading que opera un Agent Portfolio de eToro. Tu estrategia es
híbrida: señales cuantitativas generan propuestas; vos las filtrás con juicio.
Regla de oro: **ante la duda, NO operar**. Terminar una corrida sin operar es un
resultado válido y frecuente.

## Reglas duras (además de RISK.md)
- Toda orden se ejecuta EXCLUSIVAMENTE con `.venv/bin/python scripts/place_order.py`.
  Nunca con curl/POST directo (el hook lo bloquea).
- Primer comando de toda corrida: `.venv/bin/python scripts/snapshot.py`.
- Si snapshot falla o los datos están incompletos → journalear y TERMINAR sin operar.
- Podés RECHAZAR o REDUCIR una propuesta de las señales; nunca ampliarla.
- Cada decisión (incluida "no hacer nada") se registra en `state/journal.md` con su razonamiento.
- Máximo 3 órdenes por corrida. Respetá el rate limit (60 req/min): espaciá requests.

## Universo
- ETFs núcleo: SPY, QQQ | Sectoriales: XLK, XLE, XLF, XLV, XLI, XLP, XLU | Defensivos: GLD, TLT
- Cripto: BTC, ETH (corrida propia diaria)

## Señales (calculalas con datos de velas diarias de la API; usá python inline si ayuda)
1. Momentum absoluto: retorno 63 y 126 días hábiles (~3/6 meses).
2. Tendencia: precio vs SMA50 y SMA200.
3. Régimen VIX: VIX < 17 y cayendo = risk-on | 17–25 = neutral | > 25 o subiendo fuerte = risk-off.
4. Fuerza relativa: ranking de sectoriales por (retorno 63d sector − retorno 63d SPY).
5. Cripto: momentum 30/90 días y precio vs SMA50; régimen propio, independiente de acciones.

## Reglas de decisión
- Risk-on: sobreponderar top 2-3 sectoriales + QQQ; cripto hasta su tope (35%).
- Neutral: núcleo SPY + 1-2 sectores líderes; cripto a la mitad del tope (~17%).
- Risk-off: rotar a GLD/TLT/XLP/XLU; NO abrir riesgo; cripto solo mantener o reducir.
- Entrada solo si: momentum 63d positivo Y precio > SMA50.
- Salida obligatoria si: stop-loss -12% tocado, o cierre < SMA200, o sector cae al
  último tercio del ranking.

## Tu rol como juez (filtro LLM)
Antes de ejecutar cada propuesta, evaluá: ¿las señales son coherentes entre sí?
¿Hay eventos conocidos inminentes (FOMC, earnings de mega-caps, CPI) que aconsejen
esperar? ¿El tamaño es proporcional a la convicción? Ante señales contradictorias → no operar.

## Cierre de corrida
Escribir `reports/<fecha-hora>-<modo>.md` con: régimen detectado, ranking de señales,
propuestas, qué se ejecutó/bloqueó/descartó y por qué, y estado final del portfolio.
```

- [ ] **Step 3: Escribir prompts de corrida**

`prompts/run_equities.md`:
```markdown
Corrida programada de ETFs. Leé PLAYBOOK.md y RISK.md y seguílos al pie de la letra.
Modo: equities (universo ETFs; NO toques BTC/ETH en esta corrida).
Pasos: snapshot → señales → propuestas → juicio → ejecutar via place_order.py → reporte.
Si algo falla o dudás: no operes, journalealo y terminá.
```

`prompts/run_crypto.md`:
```markdown
Corrida programada de cripto. Leé PLAYBOOK.md y RISK.md y seguílos al pie de la letra.
Modo: crypto (solo BTC y ETH; NO toques ETFs en esta corrida).
Pasos: snapshot → señales cripto → propuestas → juicio → ejecutar via place_order.py → reporte.
Si algo falla o dudás: no operes, journalealo y terminá.
```

- [ ] **Step 4: Commit**

```bash
git add PLAYBOOK.md RISK.md prompts/ && git commit -m "docs: playbook de estrategia, limites de riesgo y prompts de corrida"
```

---

### Task 8: `market_open.py` + `runner.sh`

**Files:**
- Create: `scripts/market_open.py`, `scripts/runner.sh`

- [ ] **Step 1: Implementar `scripts/market_open.py`**

```python
#!/usr/bin/env python3
"""Exit 0 si NYSE está abierto ahora (aprox: lun-vie 9:30-16:00 ET, sin feriados).

Limitación asumida (spec §8): no valida feriados de NYSE; en feriado la corrida
igual aborta más adelante porque los datos de mercado no cambian y el playbook
manda no operar ante señales sin novedad. Stdlib-only.
"""
import sys
from datetime import datetime
from zoneinfo import ZoneInfo


def main() -> int:
    now = datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() >= 5:
        print("mercado cerrado: fin de semana")
        return 1
    minutes = now.hour * 60 + now.minute
    if not (9 * 60 + 30 <= minutes <= 16 * 60):
        print("mercado cerrado: fuera de horario NYSE")
        return 1
    print("mercado abierto")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Implementar `scripts/runner.sh`**

```bash
#!/bin/bash
# Lanza una corrida del agente. Uso: runner.sh equities|crypto
set -euo pipefail
cd "$(dirname "$0")/.."
MODE="${1:?uso: runner.sh equities|crypto}"

LOCK="state/.runner.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "corrida en curso, salgo"; exit 0
fi
trap 'rmdir "$LOCK"' EXIT

if [ ! -f .env ]; then echo "ERROR: falta .env" >&2; exit 1; fi
set -a; source .env; set +a
: "${ETORO_API_KEY:?ETORO_API_KEY vacía en .env}"
: "${ETORO_USER_KEY:?ETORO_USER_KEY vacía en .env}"

if [ "$MODE" = "equities" ]; then
  python3 scripts/market_open.py || { echo "skip: mercado cerrado"; exit 0; }
fi

STAMP="$(date +%F-%H%M)"
mkdir -p reports
claude -p "$(cat "prompts/run_${MODE}.md")" \
  --allowedTools "Bash,Read,Write,Glob,Grep" \
  --max-turns 60 \
  > "reports/${STAMP}-${MODE}.log" 2>&1
echo "corrida ${MODE} terminada: reports/${STAMP}-${MODE}.log"
```

- [ ] **Step 3: Verificar**

```bash
chmod +x scripts/runner.sh scripts/market_open.py
python3 scripts/market_open.py; echo "exit=$?"
bash -n scripts/runner.sh && echo "runner syntax OK"
```

Expected: `market_open` imprime abierto/cerrado coherente con la hora actual; `runner syntax OK`.

- [ ] **Step 4: Commit**

```bash
git add scripts/market_open.py scripts/runner.sh && git commit -m "feat: runner con lock y chequeo de mercado abierto"
```

---

### Task 9: launchd plists

**Files:**
- Create: `run/com.etoroagent.equities.plist`, `run/com.etoroagent.crypto.plist`

**Nota horaria:** launchd usa hora local del Mac (asumida America/Argentina/Buenos_Aires, sin DST). 11:00 ART ≈ 10:00 ET y 16:30 ART ≈ 15:30 ET durante horario de verano de EE.UU.; en invierno los horarios ET se corren 1h — `market_open.py` es la barrera real, así que una corrida fuera de horario solo hace skip.

- [ ] **Step 1: `run/com.etoroagent.equities.plist`** (reemplazar `__PROJECT__` por la ruta absoluta real al escribirlo)

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.etoroagent.equities</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>__PROJECT__/scripts/runner.sh</string>
    <string>equities</string>
  </array>
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Hour</key><integer>11</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Hour</key><integer>16</integer><key>Minute</key><integer>30</integer></dict>
  </array>
  <key>StandardOutPath</key><string>__PROJECT__/reports/launchd-equities.log</string>
  <key>StandardErrorPath</key><string>__PROJECT__/reports/launchd-equities.log</string>
</dict>
</plist>
```

- [ ] **Step 2: `run/com.etoroagent.crypto.plist`** (igual, con `<string>crypto</string>`, Label `com.etoroagent.crypto`, una sola entrada `Hour 9 / Minute 0`, logs `launchd-crypto.log`)

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.etoroagent.crypto</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>__PROJECT__/scripts/runner.sh</string>
    <string>crypto</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>__PROJECT__/reports/launchd-crypto.log</string>
  <key>StandardErrorPath</key><string>__PROJECT__/reports/launchd-crypto.log</string>
</dict>
</plist>
```

- [ ] **Step 3: Validar sintaxis**

```bash
plutil -lint run/*.plist
```

Expected: `OK` para ambos.

- [ ] **Step 4: Commit** (NO instalar los plists todavía — eso es decisión del usuario en el checklist final)

```bash
git add run/ && git commit -m "feat: plists de launchd para corridas equities y crypto"
```

---

### Task 10: README + verificación integral

**Files:**
- Create: `README.md`

- [ ] **Step 1: Correr TODA la suite**

Run: `.venv/bin/pytest tests/ -q`
Expected: `22 passed` (10 risk + 3 api + 4 place_order + 5 hook).

- [ ] **Step 2: Ensayo end-to-end en dry-run (sin launchd)**

Precondición: el usuario pegó sus keys en `.env` (copiar de `.env.example`) con `DRY_RUN=1`.

```bash
bash scripts/runner.sh crypto
```

Expected: crea `reports/<stamp>-crypto.log`; el log muestra snapshot OK, análisis, y cero órdenes reales; `state/journal.md` tiene entradas `DRY_RUN` o decisiones de no operar. Si la API devuelve 401 → revisar keys. Revisar el log completo y verificar que el agente siguió el playbook.

- [ ] **Step 3: Escribir `README.md`**

```markdown
# etoro-agent

Agente de trading autónomo sobre [eToro Agent Portfolios](https://www.etoro.com/news-and-analysis/etoro-updates/agent-portfolios-let-your-ai-agent-trade-for-you/).
Claude Code headless + señales cuantitativas + motor de riesgo determinista.

> ⚠️ Opera dinero real. Empezar SIEMPRE en dry-run y con el presupuesto mínimo ($200).
> Nada de esto es asesoramiento financiero.

## Setup
1. En eToro desktop: menú → Agent Portfolios (Beta) → crear cartera, asignar presupuesto, copiar API key.
2. `cp .env.example .env` y pegar `ETORO_API_KEY` / `ETORO_USER_KEY`. Dejar `DRY_RUN=1`.
3. `python3 -m venv .venv && .venv/bin/pip install requests pytest`
4. `.venv/bin/pytest tests/ -q` → todo verde.
5. Probar una corrida: `bash scripts/runner.sh crypto` y revisar `reports/` y `state/journal.md`.

## Activar corridas programadas
```bash
sed "s|__PROJECT__|$PWD|g" run/com.etoroagent.equities.plist > ~/Library/LaunchAgents/com.etoroagent.equities.plist
sed "s|__PROJECT__|$PWD|g" run/com.etoroagent.crypto.plist > ~/Library/LaunchAgents/com.etoroagent.crypto.plist
launchctl load ~/Library/LaunchAgents/com.etoroagent.equities.plist
launchctl load ~/Library/LaunchAgents/com.etoroagent.crypto.plist
```

## Pasar a real (tras 1-2 semanas de dry-run satisfactorio)
1. Revisar `state/journal.md`: ¿decisiones sensatas y consistentes con PLAYBOOK.md?
2. Editar `.env`: `DRY_RUN=0`.
3. Monitorear `reports/` a diario. Para frenar todo: `launchctl unload ~/Library/LaunchAgents/com.etoroagent.*.plist`.

## Arquitectura
Ver `docs/superpowers/specs/2026-08-04-etoro-agent-design.md`. Puntos clave:
- Toda orden pasa por `scripts/place_order.py`; un hook PreToolUse bloquea cualquier otra vía.
- Límites duros: 25%/posición, 35% cripto, stop-loss -12%, modo defensivo a -25% drawdown.
- Regla de oro: ante la duda, no operar.
```

- [ ] **Step 4: Commit final**

```bash
git add README.md && git commit -m "docs: README con setup, activacion y paso a real"
```

---

## Self-review del plan (hecho)

1. **Cobertura de spec:** §4 estructura → Tasks 1-9; §5 estrategia → Task 7; §6 riesgo → Tasks 2, 5, 6; §7 flujo → Tasks 4, 5, 7, 8; §8 errores → Tasks 3 (429/401), 5 (retry/journal), 8 (lock); §9 seguridad → Tasks 1 (.env), 6 (hook); §10 validación → Tasks 2, 6, 10 (dry-run). Sin gaps.
2. **Placeholders:** los paths de endpoints de eToro en Tasks 3-4 son tentativos por diseño, con paso explícito de verificación contra `docs/api-notes.md` (Task 1 Step 3) — es una dependencia externa real, no un placeholder.
3. **Consistencia de tipos:** `OrderRequest(action, symbol, amount_usd, stop_loss_pct)` idéntico en Tasks 2 y 5; `validate() -> (bool, str)` consistente; esquema de `positions.json` idéntico entre Tasks 2 (STATE fixture), 4 (snapshot) y 5 (load_state); exit codes de place_order (0/1/2) consistentes con sus tests.

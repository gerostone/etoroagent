"""Tests del hook PreToolUse `scripts/risk_hook.py`.

El hook corre con el python3 del SISTEMA (stdlib-only, sin venv) porque asi lo
invoca Claude Code segun `.claude/settings.json`. Por eso estos tests lanzan
un subprocess con `python3` en vez de importar el modulo directamente: asi
tambien verificamos que el script no dependa de paquetes de terceros.

Contrato del hook (stdin JSON -> exit code):
- exit 0 = permitir la tool call.
- exit 2 = bloquear; stderr se le muestra al agente como explicacion.
"""
import json
import subprocess
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "scripts" / "risk_hook.py"


def run_hook(command: str, tool_name: str = "Bash") -> subprocess.CompletedProcess:
    payload = json.dumps({"tool_name": tool_name, "tool_input": {"command": command}})
    return subprocess.run(
        ["python3", str(HOOK)], input=payload, capture_output=True, text=True
    )


def test_bloquea_curl_post_open_position():
    r = run_hook(
        "curl -X POST "
        "https://public-api.etoro.com/api/v1/trading/execution/market-open-orders/by-amount "
        "-d '{}'"
    )
    assert r.returncode == 2
    assert "place_order" in r.stderr


def test_bloquea_curl_delete():
    r = run_hook(
        "curl -X DELETE "
        "https://public-api.etoro.com/api/v1/trading/execution/market-close-orders/positions/y"
    )
    assert r.returncode == 2


def test_bloquea_curl_data_sin_dash_x():
    r = run_hook(
        "curl --data '{\"InstrumentID\":1}' "
        "https://public-api.etoro.com/api/v1/trading/execution/market-open-orders/by-amount"
    )
    assert r.returncode == 2


def test_permite_curl_get_market_data():
    r = run_hook(
        "curl https://public-api.etoro.com/api/v1/market-data/instruments?symbol=SPY"
    )
    assert r.returncode == 0


def test_permite_place_order_script():
    r = run_hook(
        ".venv/bin/python scripts/place_order.py open --symbol SPY --amount 10 "
        "--stop-loss-pct 0.1"
    )
    assert r.returncode == 0


def test_permite_snapshot_script():
    r = run_hook(".venv/bin/python scripts/snapshot.py")
    assert r.returncode == 0


def test_bloquea_place_order_encadenado_con_curl_post():
    r = run_hook(
        ".venv/bin/python scripts/place_order.py open --symbol SPY --amount 10 "
        "--stop-loss-pct 0.1 && curl -X POST "
        "https://public-api.etoro.com/api/v1/trading/execution/market-open-orders/by-amount"
    )
    assert r.returncode == 2


def test_bloquea_python_c_con_etoro_client_inline():
    r = run_hook(
        'python3 -c "from etoro_api import EtoroClient; '
        "c = EtoroClient(); c.open_position_by_amount(1, 'Buy', 10, 5)\""
    )
    assert r.returncode == 2
    assert "place_order" in r.stderr


def test_bloquea_heredoc_con_open_position_by_amount():
    r = run_hook(
        "python3 <<'EOF'\n"
        "from etoro_api import EtoroClient\n"
        "c = EtoroClient()\n"
        "c.open_position_by_amount(1, 'Buy', 10, 5)\n"
        "EOF"
    )
    assert r.returncode == 2


def test_permite_comandos_no_relacionados_con_etoro():
    r = run_hook("ls -la")
    assert r.returncode == 0


def test_json_invalido_por_stdin_no_rompe_la_sesion():
    r = subprocess.run(
        ["python3", str(HOOK)], input="esto no es json", capture_output=True, text=True
    )
    assert r.returncode == 0


def test_tool_name_distinto_de_bash_se_permite():
    r = run_hook(
        "curl -X POST https://public-api.etoro.com/api/v1/trading/execution/"
        "market-open-orders/by-amount",
        tool_name="Read",
    )
    assert r.returncode == 0


def test_bloquea_referencia_a_market_close_orders_via_wget():
    r = run_hook(
        "wget --post-data='{}' "
        "https://public-api.etoro.com/api/v1/trading/execution/market-close-orders/positions/1"
    )
    assert r.returncode == 2


def test_bloquea_referencia_a_market_close_orders_via_httpie_sin_dominio_explicito():
    # httpie usa una sintaxis distinta a curl/wget; el patron de endpoint
    # (market-close-orders) debe bastar para bloquear, sin depender del
    # dominio ni de los indicadores de metodo de curl.
    r = run_hook("http POST api.internal-proxy.local/market-close-orders/positions/1")
    assert r.returncode == 2


def test_bloquea_open_position_by_amount_referenciado_directo():
    r = run_hook('python3 -c "import etoro_api; etoro_api.open_position_by_amount(1)"')
    assert r.returncode == 2


def test_bloquea_close_position_referenciado_directo():
    r = run_hook('python3 -c "EtoroClient().close_position(position_id=1, instrument_id=2)"')
    assert r.returncode == 2


def test_missing_command_key_no_rompe():
    payload = json.dumps({"tool_name": "Bash", "tool_input": {}})
    r = subprocess.run(
        ["python3", str(HOOK)], input=payload, capture_output=True, text=True
    )
    assert r.returncode == 0


def test_missing_tool_input_no_rompe():
    payload = json.dumps({"tool_name": "Bash"})
    r = subprocess.run(
        ["python3", str(HOOK)], input=payload, capture_output=True, text=True
    )
    assert r.returncode == 0


# --- Fixes del spec review de Task 6: quote-splitting + metodo no explicito ---
#
# (a) combina las dos tecnicas del review: el endpoint viene partido por
#     comillas adyacentes ("trad""ing/exec""ution/market-open""-orders", que
#     bash concatena a "trading/execution/market-open-orders") Y el metodo
#     viaja en una variable de shell ($M) en vez de como literal POST. Antes
#     del fix ninguna de las dos capas (indicador de escritura + patron de
#     endpoint) lo detectaba.
def test_bloquea_quote_split_endpoint_mas_metodo_en_variable():
    r = run_hook(
        'M=POST; curl -X "$M" '
        'https://public-api.etoro.com/api/v1/'
        '"trad""ing/exec""ution/market-open""-orders"/by-amount'
    )
    assert r.returncode == 2
    assert "place_order" in r.stderr


# (b) metodo puro en variable de shell, contra un endpoint que NO matchea
#     ningun patron hardcodeado (agent-portfolios/.../positions no contiene
#     "market-open-orders" ni "trading/execution"): solo el fail-closed de
#     "-X con valor no literal GET/HEAD" puede bloquear esto.
def test_bloquea_metodo_en_variable_de_shell_sin_endpoint_conocido():
    r = run_hook(
        'curl -X "$METHOD" https://public-api.etoro.com/api/v1/agent-portfolios/x/positions'
    )
    assert r.returncode == 2


def test_bloquea_dash_x_post_pegado_sin_espacio():
    r = run_hook(
        "curl -XPOST https://public-api.etoro.com/api/v1/agent-portfolios/x/positions"
    )
    assert r.returncode == 2


def test_bloquea_request_igual_post_con_signo_igual():
    r = run_hook(
        "curl --request=POST https://public-api.etoro.com/api/v1/agent-portfolios/x/positions"
    )
    assert r.returncode == 2


def test_permite_dash_x_get_explicito():
    r = run_hook(
        "curl -X GET https://public-api.etoro.com/api/v1/agent-portfolios/x/positions"
    )
    assert r.returncode == 0


def test_permite_request_get_explicito():
    r = run_hook(
        "curl --request GET https://public-api.etoro.com/api/v1/agent-portfolios/x/positions"
    )
    assert r.returncode == 0


def test_bloquea_dominio_quote_split_con_data():
    r = run_hook(
        "curl --data '{}' https://\"public-api\"\".etoro.com\"/api/v1/agent-portfolios/x/positions"
    )
    assert r.returncode == 2

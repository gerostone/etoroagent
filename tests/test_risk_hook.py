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


# --- Quality review round 2: falsos positivos (C1), requests inline (I2), ---
# --- candles.py (I3), FPs de dev (M8) --------------------------------------


# C1: -f (fail silently) NO es -F (form). Antes matcheaba case-insensitive.
def test_permite_curl_dash_f_minuscula_no_es_form():
    r = run_hook(
        "curl -f -sS https://public-api.etoro.com/api/v1/market-data/instruments?symbol=SPY"
    )
    assert r.returncode == 0


# C1: -D (dump headers) NO es -d (data). Antes matcheaba case-insensitive.
def test_permite_curl_dash_d_mayuscula_no_es_data():
    r = run_hook(
        "curl -D headers.txt https://public-api.etoro.com/api/v1/market-data/instruments?symbol=SPY"
    )
    assert r.returncode == 0


# C1: -x (proxy) NO es -X (method). Antes matcheaba case-insensitive y
# tomaba "proxy.local:8080" como un metodo no-GET/HEAD.
def test_permite_curl_dash_x_minuscula_es_proxy_no_metodo():
    r = run_hook(
        "curl -x proxy.local:8080 "
        "https://public-api.etoro.com/api/v1/market-data/instruments?symbol=SPY"
    )
    assert r.returncode == 0


# C1: un comando previo inocuo encadenado con && no debe contaminar la
# lectura GET del segundo segmento (segmentacion + fix de mayus/minusculas).
def test_permite_comando_previo_encadenado_con_curl_get_de_solo_lectura():
    r = run_hook(
        "mkdir -p reports && curl -f -sS "
        "https://public-api.etoro.com/api/v1/market-data/instruments?symbol=SPY"
    )
    assert r.returncode == 0


# C1: -G convierte -d en query string (GET), no es una escritura.
def test_permite_curl_dash_g_con_dash_d_es_get_con_query():
    r = run_hook(
        "curl -G -d 'internalSymbolFull=SPY' "
        "https://public-api.etoro.com/api/v1/market-data/search"
    )
    assert r.returncode == 0


# C1/M6: -sX POST (flags cortos curl combinados) debe detectarse igual que -X POST.
def test_bloquea_combo_dash_sx_post():
    r = run_hook(
        "curl -sX POST https://public-api.etoro.com/api/v1/agent-portfolios/x/positions"
    )
    assert r.returncode == 2


# M6: -d@archivo (payload leido de archivo, pegado) debe bloquear igual que
# -d 'json'. URL sin ningun patron de endpoint hardcodeado (agent-portfolios,
# no market-open-orders) para aislar especificamente el indicador -d@ del
# chequeo de referencia a endpoint.
def test_bloquea_dash_d_arroba_pegado():
    r = run_hook(
        "curl -d@payload.json "
        "https://public-api.etoro.com/api/v1/agent-portfolios/x/positions"
    )
    assert r.returncode == 2


# I2: requests.post inline con la URL completamente partida por comillas Y
# por el operador de concatenacion "+" (ni el dominio ni el endpoint quedan
# contiguos en el texto crudo ni en la version solo-sin-comillas).
def test_bloquea_requests_post_inline_con_concatenacion_partida():
    r = run_hook(
        "python3 -c \"import requests; requests.post('https://public-api' + "
        "'.etoro.com/api/v1/trad' + 'ing/execution/market-open' + "
        "'-orders/by-amount', json={'InstrumentID': 1})\""
    )
    assert r.returncode == 2
    assert "place_order" in r.stderr


# I2 negativo (M8): mencionar "requests.post(" en un grep no es ejecucion
# inline (no hay python -c / heredoc) -> no debe bloquear.
def test_permite_grep_de_requests_post_sin_contexto_python_inline():
    r = run_hook('grep -rn "requests.post(" .')
    assert r.returncode == 0


# I3: candles.py es la tercera via de lectura autorizada.
def test_permite_candles_script():
    r = run_hook(
        ".venv/bin/python scripts/candles.py --symbol SPY --count 210"
    )
    assert r.returncode == 0


def test_permite_candles_script_con_interval():
    r = run_hook(
        ".venv/bin/python scripts/candles.py --symbol SPY --count 210 --interval OneDay"
    )
    assert r.returncode == 0


# M8: grep/git no son "vehiculos de ejecucion" — mencionar un endpoint de
# trading en texto (busqueda, mensaje de commit, filtro de log) no debe
# bloquear la corrida del agente.
def test_permite_grep_de_endpoint_sin_vehiculo_de_ejecucion():
    r = run_hook('grep -rn "market-open-orders" .')
    assert r.returncode == 0


def test_permite_git_commit_con_endpoint_en_el_mensaje():
    r = run_hook(
        'git commit -m "fix: retry bug en market-open-orders"'
    )
    assert r.returncode == 0


def test_permite_git_log_grep_con_endpoint():
    r = run_hook("git log --grep=market-close-orders")
    assert r.returncode == 0


# M8 control positivo: la referencia al endpoint SIGUE bloqueando cuando
# aparece junto a un vehiculo de ejecucion real (ya cubierto por tests mas
# arriba con curl/wget/python3 -c, repetido aca para dejar el contraste
# explicito con los negativos de arriba).
def test_bloquea_endpoint_con_vehiculo_xargs():
    r = run_hook(
        "echo https://public-api.etoro.com/api/v1/trading/execution/"
        "market-open-orders/by-amount | xargs -I{} curl -X POST {}"
    )
    assert r.returncode == 2

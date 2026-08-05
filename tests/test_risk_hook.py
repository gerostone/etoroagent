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


# --- N1 (Task 10): -F por si sola no alcanza, hace falta un cliente HTTP --
# real (curl/wget/http) en el mismo segmento. Antes, cualquier comando que
# usara "-F" con otro significado (p.ej. `sort -F`) Y mencionara el dominio
# de la API en texto plano (sin ningun cliente HTTP) daba falso positivo.


def test_permite_sort_dash_f_con_dominio_mencionado_sin_cliente_http():
    r = run_hook('echo "public-api.etoro.com/status" | sort -F')
    assert r.returncode == 0


def test_bloquea_curl_dash_f_mayuscula_con_dominio_sigue_bloqueando():
    # Control positivo (N1 no debe romper la deteccion real): -F con curl
    # (cliente HTTP real) presente en el mismo segmento sigue bloqueando.
    r = run_hook(
        "curl -F 'file=@x.json' "
        "https://public-api.etoro.com/api/v1/trading/execution/"
        "market-open-orders/by-amount"
    )
    assert r.returncode == 2


# --- Task 10: hook protege guardrails (scripts/, .claude/, PLAYBOOK.md, ---
# --- RISK.md, .env) contra escrituras de shell (gap descubierto en Task 7:
# --- permissions.deny bloquea Write/Edit pero no Bash `tee`/redireccion/etc).


def test_bloquea_tee_sobre_risk_hook_py():
    r = run_hook("echo malicioso | tee scripts/risk_hook.py")
    assert r.returncode == 2
    assert "guardrails" in r.stderr


def test_bloquea_redireccion_simple_sobre_place_order_py():
    r = run_hook("echo malicioso > scripts/place_order.py")
    assert r.returncode == 2


def test_bloquea_append_sobre_dotenv():
    r = run_hook("echo 'ETORO_API_KEY=hackeada' >> .env")
    assert r.returncode == 2


def test_bloquea_sed_i_sobre_risk_py():
    # Intento de aflojar el limite de tamano de posicion editando risk.py
    # in-place con sed en vez de Edit (bloqueado por permissions.deny).
    r = run_hook("sed -i 's/0.25/0.99/' scripts/risk.py")
    assert r.returncode == 2


def test_bloquea_sed_i_con_sufijo_de_backup_sobre_risk_py():
    r = run_hook("sed -i.bak 's/0.25/0.99/' scripts/risk.py")
    assert r.returncode == 2


def test_bloquea_mv_sobre_claude_settings():
    r = run_hook("mv payload.json .claude/settings.json")
    assert r.returncode == 2


def test_bloquea_cp_sobre_playbook():
    r = run_hook("cp fake_playbook.md PLAYBOOK.md")
    assert r.returncode == 2


def test_bloquea_install_sobre_risk_md():
    r = run_hook("install -m 644 fake_risk.md RISK.md")
    assert r.returncode == 2


def test_bloquea_dd_of_sobre_risk_hook_py():
    r = run_hook("dd if=/dev/zero of=scripts/risk_hook.py bs=1 count=0")
    assert r.returncode == 2


def test_bloquea_truncate_sobre_playbook():
    r = run_hook("truncate -s 0 PLAYBOOK.md")
    assert r.returncode == 2


def test_bloquea_ln_sobre_claude_dir():
    r = run_hook("ln -sf /tmp/evil.json .claude/settings.json")
    assert r.returncode == 2


def test_bloquea_tee_heredoc_sobre_risk_hook_py():
    # El heredoc va COMO ARGUMENTO de tee (el destino real es
    # scripts/risk_hook.py, el heredoc es el contenido a escribir) -> debe
    # bloquear igual que la forma con pipe.
    r = run_hook("tee scripts/risk_hook.py <<'EOF'\nevil\nEOF")
    assert r.returncode == 2


def test_bloquea_escritura_a_guardrail_encadenada_tras_orden_legitima():
    # Un segmento legitimo (place_order.py) encadenado con && no debe
    # "blanquear" un segundo segmento que reescribe un guardrail.
    r = run_hook(
        ".venv/bin/python scripts/place_order.py open --symbol SPY --amount 10 "
        "--stop-loss-pct 0.1 && echo x > scripts/risk_hook.py"
    )
    assert r.returncode == 2


# Falsos positivos que DEBEN seguir permitidos: lectura de guardrails, y el
# journaling normal del agente hacia state/ y reports/ (que tambien usa
# tee/redireccion, ver PLAYBOOK.md §Cierre de corrida).


def test_permite_cat_playbook_lectura():
    r = run_hook("cat PLAYBOOK.md")
    assert r.returncode == 0


def test_permite_grep_scripts_risk_py_lectura():
    r = run_hook('grep -rn "stop-loss" scripts/risk.py')
    assert r.returncode == 0


def test_permite_tee_a_reports():
    r = run_hook('echo "reporte de la corrida" | tee reports/2026-08-04-1000-equities.md')
    assert r.returncode == 0


def test_permite_tee_append_a_journal():
    r = run_hook("echo '- no operar: sin señales' | tee -a state/journal.md")
    assert r.returncode == 0


def test_permite_tee_heredoc_a_journal_que_menciona_place_order():
    # Caso FP real: una entrada de journal legitima puede mencionar el
    # nombre de un script guardrail como TEXTO de la decision (no como
    # destino de escritura) — el heredoc es el contenido de state/journal.md,
    # no del script mencionado adentro. No debe bloquear.
    r = run_hook(
        "tee -a state/journal.md <<'EOF'\n"
        "- 2026-08-04T10:00:00Z ejecutado via scripts/place_order.py, symbol SPY\n"
        "EOF"
    )
    assert r.returncode == 0


def test_permite_redireccion_simple_a_reports():
    r = run_hook("echo 'log' > reports/out.log")
    assert r.returncode == 0


def test_permite_mv_entre_archivos_de_reports():
    r = run_hook("mv reports/tmp.md reports/final.md")
    assert r.returncode == 0


def test_permite_ejecutar_runner_con_redireccion_de_salida_a_reports():
    # Ejecutar (leer) scripts/runner.sh via bash y redirigir la SALIDA a
    # reports/ no es una escritura sobre el guardrail scripts/runner.sh.
    r = run_hook("bash scripts/runner.sh crypto > reports/out.log 2>&1")
    assert r.returncode == 0


def test_permite_grep_de_guardrail_con_pipe_a_tee_reports():
    # El grep (lectura) aparece ANTES de tee en el mismo segmento; el
    # destino real de tee es reports/refs.md, no scripts/risk_hook.py.
    r = run_hook('grep -rn "scripts/risk_hook.py" . | tee reports/refs.md')
    assert r.returncode == 0


def test_permite_pip_install_en_venv_sin_fp_por_palabra_install():
    r = run_hook(".venv/bin/pip install requests pytest")
    assert r.returncode == 0


def test_permite_ls_scripts_sin_operador_de_escritura():
    r = run_hook("ls scripts/")
    assert r.returncode == 0


def test_permite_prosa_con_palabra_install_y_mencion_de_ruta_sin_coma():
    # Regresion real: encontrada al commitear este mismo cambio. Un mensaje
    # (commit, journal) que USA la palabra "install" en prosa y menciona
    # una ruta guardrail mas adelante en la misma linea no es una escritura.
    r = run_hook(
        "git commit -m 'ln) cuyo destino cae en scripts/place_order.py descripcion'"
    )
    assert r.returncode == 0


def test_permite_prosa_con_operadores_y_coma_antes_de_la_ruta():
    r = run_hook(
        "git commit -m 'install, dd of=, truncate, ln) cuyo destino cae en "
        "scripts/place_order.py, listo'"
    )
    assert r.returncode == 0


def test_bloquea_install_real_sin_puntuacion_antes_del_destino():
    # Control positivo: sin la coma/parentesis de la prosa, `install` como
    # comando real con el guardrail como destino sigue bloqueando.
    r = run_hook("install fake.py scripts/place_order.py")
    assert r.returncode == 2


# --- N4 (Task 10): bypasses residuales ACEPTADOS a proposito ---------------
#
# Estos tests documentan (y verifican, para que una regresion futura no los
# "arregle" sin que sea una decision consciente) los bypasses que el hook
# NO cierra porque hacerlo requeriria evaluar shell real: alto costo/riesgo
# de falsos positivos para un beneficio marginal, dado que estan mitigados
# por otras capas — PLAYBOOK.md (place_order.py como unica via de
# escritura aceptada), la validacion de riesgo interna de place_order.py
# (corre sin importar como fue invocado), y la regla de este mismo hook +
# permissions.deny sobre los guardrails (que cierran la via barata y
# directa, sin ofuscacion). Ver docstring de risk_hook.py, seccion
# "BYPASSES RESIDUALES ACEPTADOS".


def test_residual_aceptado_ansi_c_quoting():
    # $'...' interpreta escapes de bytes (\xNN) en tiempo de shell: el hook
    # nunca ve el dominio/endpoint literal, solo los escapes sin evaluar.
    r = run_hook(
        "curl -X POST $'https://public-api\\x2eetoro.com/api/v1/"
        "trading\\x2fexecution/market\\x2dopen-orders/by-amount' -d '{}'"
    )
    assert r.returncode == 0


def test_residual_aceptado_expansion_de_variables():
    # ${A}${B} solo tiene el endpoint completo en tiempo de ejecucion de
    # bash; el hook nunca ejecuta el comando, solo lee su texto.
    r = run_hook(
        "A=https://public-api.etoro.com/api/v1/trading/execution/"
        "market-open-orders; B=/by-amount; curl -X POST ${A}${B} -d '{}'"
    )
    assert r.returncode == 0


def test_residual_aceptado_base64_pipe_bash():
    # Payload codificado y decodificado en la misma linea: el texto crudo
    # del comando no contiene ningun patron vigilado en claro.
    r = run_hook(
        "echo Y3VybCAtWCBQT1NUIGh0dHBzOi8vcHVibGljLWFwaS5ldG9yby5jb20= "
        "| base64 -d | bash"
    )
    assert r.returncode == 0


def test_residual_aceptado_brace_expansion():
    # {a,b} de bash parte el literal en el texto crudo (nunca queda
    # contiguo en el comando tal como Claude Code lo pasa), aunque bash lo
    # expanda a comandos completos en tiempo de ejecucion.
    r = run_hook(
        "curl -X POST https://public{-api,X}.etoro.com/api/v1/"
        "trading{/execution,X}/market{-open-orders,X}/by-amount -d '{}'"
    )
    assert r.returncode == 0

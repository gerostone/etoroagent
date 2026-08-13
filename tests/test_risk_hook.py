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


# --- Fixes reviewer (post Task 10): _REDIR_RE matcheaba '>' DENTRO de -----
# --- comillas (no es redireccion real) y "->"/">=" (tampoco son redireccion)


def test_permite_reason_con_mayor_que_citado_y_risk_md_sin_puntuacion():
    # El '>' de "stop-loss > 12%" esta DENTRO del string de --reason: no es
    # una redireccion real de shell (el shell nunca la interpreta como tal
    # citada). Sin este fix, region_objetivo despues de ese '>' llegaba
    # hasta "RISK.md" (sin coma/parentesis en el medio) y bloqueaba una
    # orden legitima.
    r = run_hook(
        '.venv/bin/python scripts/place_order.py open --symbol SPY --amount 100 '
        '--stop-loss-pct 0.1 --reason "stop-loss > 12% respeta RISK.md"'
    )
    assert r.returncode == 0


def test_permite_reason_con_mayor_que_citado_y_playbook_md():
    r = run_hook(
        '.venv/bin/python scripts/place_order.py open --symbol QQQ --amount 50 '
        '--stop-loss-pct 0.1 --reason "momentum > SMA50 ver PLAYBOOK.md antes de operar"'
    )
    assert r.returncode == 0


def test_permite_reason_con_flecha_citada_seguida_de_risk_md():
    # "->" (flecha) no es sintaxis de redireccion en bash.
    r = run_hook(
        '.venv/bin/python scripts/place_order.py open --symbol SPY --amount 10 '
        '--stop-loss-pct 0.1 --reason "aplicar regla -> revisar RISK.md antes"'
    )
    assert r.returncode == 0


def test_permite_flecha_fuera_de_comillas_encadenada_con_orden_legitima():
    r = run_hook(
        "echo 'motivo -> ver RISK.md sin comillas alrededor de la flecha misma' && "
        ".venv/bin/python scripts/place_order.py open --symbol SPY --amount 10 "
        "--stop-loss-pct 0.1"
    )
    assert r.returncode == 0


def test_permite_mayor_o_igual_no_es_redireccion():
    r = run_hook("curl --version && echo 'a >= b'")
    assert r.returncode == 0


def test_permite_mencion_y_mayor_que_totalmente_dentro_de_comillas():
    # Tanto el '>' como la mencion a PLAYBOOK.md estan DENTRO del mismo
    # string citado -> ni siquiera hay un operador real que evaluar.
    r = run_hook('echo "PLAYBOOK.md > backup"')
    assert r.returncode == 0


def test_bloquea_tee_playbook_fuera_de_comillas_sigue_bloqueando():
    # Control positivo (regresion): el fix de comillas/flechas no debe
    # debilitar la deteccion de una escritura real, sin comillas de por medio.
    r = run_hook("echo malicioso | tee PLAYBOOK.md")
    assert r.returncode == 2


def test_bloquea_redireccion_sobre_scripts_fuera_de_comillas_sigue_bloqueando():
    r = run_hook("echo malicioso > scripts/place_order.py")
    assert r.returncode == 2


def test_bloquea_append_fuera_de_comillas_sobre_risk_md_sigue_bloqueando():
    r = run_hook("echo x >> RISK.md")
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


def test_bloquea_redireccion_con_flecha_pegada_a_ruta_protegida():
    # En bash `echo x ->f` tokeniza como `echo x - > f`: es una redireccion
    # real. El lookbehind (?<!-) que la exceptuaba era un bypass.
    r = run_hook("echo x ->scripts/risk.py")
    assert r.returncode == 2


def test_permite_flecha_dentro_de_comillas_en_reason():
    r = run_hook(
        '.venv/bin/python scripts/place_order.py open --symbol SPY '
        '--amount 10 --stop-loss-pct 0.1 --reason "señal->entrada ver PLAYBOOK.md"'
    )
    assert r.returncode == 0


# --- WP4/N3(c): protección de state/.run_orders.json y ---------------------
# --- state/.needs_reconciliation contra escritura de shell (incluido `rm`) -
#
# Re-auditoría: estos dos archivos son de CONTROL (presupuesto de órdenes,
# flag de reconciliación) -- a diferencia del resto de state/ (positions.json,
# equity.csv, journal.md), que el agente journalea/lee libremente. Si el
# agente pudiera `rm state/.needs_reconciliation` por Bash, el protocolo de
# reconciliación (PLAYBOOK.md) sería honor system puro. A diferencia de los
# guardrails de código (scripts/, PLAYBOOK.md, etc.), acá `rm` SÍ cuenta
# como verbo de escritura (es la vía más obvia de evadir el flag).


def test_bloquea_rm_sobre_needs_reconciliation():
    r = run_hook("rm state/.needs_reconciliation")
    assert r.returncode == 2
    assert "reconcile.py" in r.stderr


def test_bloquea_rm_dash_f_sobre_needs_reconciliation():
    r = run_hook("rm -f state/.needs_reconciliation")
    assert r.returncode == 2


def test_bloquea_rm_sobre_run_orders_json():
    r = run_hook("rm state/.run_orders.json")
    assert r.returncode == 2


def test_bloquea_redireccion_sobre_needs_reconciliation():
    r = run_hook("echo '{}' > state/.needs_reconciliation")
    assert r.returncode == 2


def test_bloquea_tee_sobre_run_orders_json():
    r = run_hook("echo '{}' | tee state/.run_orders.json")
    assert r.returncode == 2


def test_bloquea_mv_sobre_needs_reconciliation():
    r = run_hook("mv fake.json state/.needs_reconciliation")
    assert r.returncode == 2


def test_bloquea_escritura_encadenada_tras_orden_legitima_sobre_needs_reconciliation():
    r = run_hook(
        ".venv/bin/python scripts/snapshot.py && rm state/.needs_reconciliation"
    )
    assert r.returncode == 2


def test_permite_reconcile_script_con_done():
    r = run_hook(".venv/bin/python scripts/reconcile.py --done")
    assert r.returncode == 0


def test_permite_lectura_cat_needs_reconciliation():
    r = run_hook("cat state/.needs_reconciliation")
    assert r.returncode == 0


def test_permite_lectura_grep_run_orders_json():
    r = run_hook("grep count state/.run_orders.json")
    assert r.returncode == 0


def test_permite_journal_normal_no_bloquea_por_mencion_de_estado_protegido():
    # Journalear (tee -a a journal.md) SIN mencionar ninguno de los cuatro
    # archivos protegidos (PROTECTED_STATE_FILES) sigue permitido -- el
    # destino real es journal.md, que no está en la lista. Ver
    # test_bloquea_journal_que_menciona_archivo_protegido_wp6 para el caso
    # (post-WP6) en el que el journaling SÍ nombra un archivo protegido.
    r = run_hook(
        "tee -a state/journal.md <<'EOF'\n"
        "- 2026-08-12 20:20 -0300 RECONCILIACION | sin discrepancias\n"
        "EOF"
    )
    assert r.returncode == 0


def test_bloquea_journal_que_menciona_archivo_protegido_wp6():
    # WP6/N11: a diferencia de WP4/N3c y WP5/N8 (que enmascaraban el CUERPO
    # de un heredoc para no confundir DATO con comando), el default-deny
    # por mención NO tiene esa excepción -- un journaling que nombre
    # literalmente uno de los cuatro archivos protegidos también se
    # bloquea, aunque el destino real sea journal.md. Costo aceptado a
    # propósito (ver docstring de PROTECTED_STATE_FILES en risk_hook.py):
    # journalear una entrada debe referirse a estos archivos de forma
    # descriptiva ("el flag de reconciliación") en vez de nombrarlos.
    r = run_hook(
        "tee -a state/journal.md <<'EOF'\n"
        "- 2026-08-12 20:20 -0300 RECONCILIACION | sin discrepancias vs state/.needs_reconciliation\n"
        "EOF"
    )
    assert r.returncode == 2


# --- WP4/N4(a): asignación inline de ETOROAGENT_RUN_ID/ETOROAGENT_STATE_DIR -
#
# Re-auditoría: el presupuesto de órdenes por corrida (WP1) y el
# aislamiento de state para tests (WP4/N5) confían en que
# ETOROAGENT_RUN_ID/ETOROAGENT_STATE_DIR las setea runner.sh por entorno
# heredado -- el agente nunca necesita asignarlas inline. Si pudiera,
# "ETOROAGENT_RUN_ID=fresh .venv/bin/python scripts/place_order.py open..."
# resetearía el presupuesto de la corrida a voluntad.


def test_bloquea_run_id_inline_prefijo_con_place_order():
    r = run_hook(
        "ETOROAGENT_RUN_ID=fake-run .venv/bin/python scripts/place_order.py "
        "open --symbol SPY --amount 10 --stop-loss-pct 0.1"
    )
    assert r.returncode == 2
    assert "ETOROAGENT_RUN_ID" in r.stderr


def test_bloquea_export_run_id_encadenado_con_place_order():
    r = run_hook(
        "export ETOROAGENT_RUN_ID=fake-run && .venv/bin/python "
        "scripts/place_order.py open --symbol SPY --amount 10 --stop-loss-pct 0.1"
    )
    assert r.returncode == 2


def test_bloquea_state_dir_inline_con_snapshot():
    r = run_hook(
        "ETOROAGENT_STATE_DIR=/tmp/fake .venv/bin/python scripts/snapshot.py"
    )
    assert r.returncode == 2


def test_bloquea_run_id_inline_con_reconcile():
    r = run_hook(
        "ETOROAGENT_RUN_ID=fake .venv/bin/python scripts/reconcile.py --done"
    )
    assert r.returncode == 2


def test_bloquea_run_id_inline_con_candles():
    r = run_hook(
        "ETOROAGENT_RUN_ID=fake .venv/bin/python scripts/candles.py --symbol SPY --count 10"
    )
    assert r.returncode == 2


def test_permite_asignacion_de_run_id_sin_script_autorizado():
    # Asignar la variable sin invocar ninguno de los 4 scripts no es el
    # patrón de evasión -- no hay nada que evadir.
    r = run_hook("ETOROAGENT_RUN_ID=fake echo hola")
    assert r.returncode == 0


def test_permite_mencion_de_run_id_sin_asignacion():
    # Sin "=" no es una asignación -- p.ej. imprimir la variable actual.
    r = run_hook("echo \"corriendo con ETOROAGENT_RUN_ID $ETOROAGENT_RUN_ID\"")
    assert r.returncode == 0


def test_permite_mencion_en_commit_message_sin_vehiculo():
    # Mencionar ambos patrones en un mensaje de commit (prosa, sin invocar
    # python realmente) no debe bloquear -- mismo criterio que ya aplica el
    # hook para endpoints de trading en mensajes de commit.
    r = run_hook(
        'git commit -m "fix: bloquear ETOROAGENT_RUN_ID= inline junto a '
        'scripts/place_order.py en risk_hook.py"'
    )
    assert r.returncode == 0


def test_permite_pytest_con_etoroagent_run_id_en_nombre_de_test():
    r = run_hook(".venv/bin/pytest tests/test_place_order.py -k ETOROAGENT_RUN_ID")
    assert r.returncode == 0


def test_bloquea_mencion_de_rm_entre_backticks_en_prosa_de_commit_wp6():
    # WP6/N11: a diferencia de la versión WP4/N3c de esta protección (que
    # descartaba explícitamente un operador cuyo NOMBRE cayera dentro de
    # backticks, como convención markdown de prosa/documentación), el
    # default-deny por mención NO distingue prosa de invocación real --
    # cualquier mención del archivo protegido bloquea, sin excepción para
    # backticks. Este test antes esperaba rc==0 (exención de prosa); ahora
    # documenta el comportamiento correcto tras el cambio de estrategia:
    # incluso un mensaje de commit que solo DOCUMENTA la protección debe
    # evitar nombrar el archivo protegido literalmente.
    r = run_hook(
        "git commit -m \"antes nada impedia \\`rm state/.needs_reconciliation\\` "
        "sin haber reconciliado nada\""
    )
    assert r.returncode == 2


def test_bloquea_mencion_de_rm_sin_backticks_dentro_de_heredoc_wp6():
    # WP6/N11: ídem el anterior, pero para el enmascarado de CUERPO de
    # heredoc que WP4/N3c y WP5/N8 aplicaban (para no confundir un
    # ejemplo de texto/test citado dentro de un heredoc con una invocación
    # real). El default-deny por mención ya no distingue: el archivo de
    # destino real de este heredoc (tests/some_file.py) no es uno de los
    # protegidos, pero el CUERPO menciona "state/.needs_reconciliation"
    # como dato/texto -- eso alcanza para bloquear, aunque no sea una
    # invocación real sobre el archivo protegido.
    r = run_hook(
        "cat >> tests/some_file.py << 'EOF'\n"
        'r = run_hook("rm state/.needs_reconciliation")\n'
        "EOF"
    )
    assert r.returncode == 2


def test_bloquea_rm_sobre_needs_reconciliation_sin_backticks():
    # Control positivo: la invocación real (sin ningún backtick de por
    # medio) sigue bloqueada, como siempre.
    r = run_hook("rm state/.needs_reconciliation")
    assert r.returncode == 2


def test_bloquea_rm_con_argumento_citado_sin_backticks_sobre_rm():
    # Invocación real con el argumento citado (comillas normales) -- debe
    # seguir bloqueando.
    r = run_hook('rm "state/.needs_reconciliation"')
    assert r.returncode == 2


def test_bloquea_tee_heredoc_sobre_needs_reconciliation_target_antes_del_heredoc():
    # Control: el archivo protegido se menciona ANTES del marcador `<<`
    # (como destino real de tee), no solo dentro del cuerpo -- debe
    # bloquear igual bajo el default-deny por mención.
    r = run_hook("tee state/.needs_reconciliation <<'EOF'\nevil\nEOF")
    assert r.returncode == 2


# --- WP5/N8 (histórico, superado por WP6/N11 -- ver más abajo): ------------
# --- intérpretes alternativos que borran los guards sin pasar por ----------
# --- ningún operador de shell vigilado (tee/mv/rm/etc) ---------------------
#
# Re-auditoría original (N8): la detección por operador+región (tee/mv/
# rm/... seguidos del path protegido) no veía `python3 -c
# "os.remove(...)"`, `perl -e unlink(...)`, o `find ... -delete`, que
# borran el archivo DENTRO del intérprete/comando. N8 lo cerró enumerando
# vehículos de ejecución/borrado conocidos -- pero esa lista NUNCA
# convergió (siguiente ronda: `node -e`). WP6/N11 (ver la sección de más
# abajo) reemplazó por completo ese mecanismo con default-deny por
# mención: los tests de ESTA sección siguen pasando porque el nuevo
# mecanismo bloquea un superconjunto de lo que bloqueaba N8 -- se dejan
# acá como regresión histórica, no porque el código todavía use listas de
# vehículos.


def test_bloquea_python_os_remove_sobre_run_orders():
    r = run_hook(
        "python3 -c \"import os; os.remove('state/.run_orders.json')\""
    )
    assert r.returncode == 2
    assert "risk_hook.py" in r.stderr


def test_bloquea_python_os_remove_sobre_needs_reconciliation():
    r = run_hook(
        "python3 -c \"import os; os.remove('state/.needs_reconciliation')\""
    )
    assert r.returncode == 2


def test_bloquea_perl_unlink_sobre_needs_reconciliation():
    r = run_hook(
        "perl -e \"unlink('state/.needs_reconciliation')\""
    )
    assert r.returncode == 2


def test_bloquea_perl_unlink_sobre_run_orders():
    r = run_hook("perl -e \"unlink('state/.run_orders.json')\"")
    assert r.returncode == 2


def test_bloquea_ruby_file_delete_sobre_needs_reconciliation():
    r = run_hook(
        "ruby -e \"File.delete('state/.needs_reconciliation')\""
    )
    assert r.returncode == 2


def test_bloquea_find_delete_sobre_needs_reconciliation():
    r = run_hook("find state -name .needs_reconciliation -delete")
    assert r.returncode == 2


def test_bloquea_find_delete_sobre_run_orders():
    r = run_hook("find state -name .run_orders.json -delete")
    assert r.returncode == 2


def test_bloquea_xargs_rm_sobre_needs_reconciliation():
    r = run_hook("echo state/.needs_reconciliation | xargs rm")
    assert r.returncode == 2


def test_bloquea_eval_reconstruido_sobre_needs_reconciliation():
    r = run_hook('eval "rm state/.needs_reconciliation"')
    assert r.returncode == 2


def test_bloquea_sh_dash_c_sobre_run_orders():
    r = run_hook("sh -c \"rm state/.run_orders.json\"")
    assert r.returncode == 2


def test_bloquea_shred_sobre_needs_reconciliation():
    r = run_hook("shred -u state/.needs_reconciliation")
    assert r.returncode == 2


def test_bloquea_dd_sobre_run_orders():
    r = run_hook("dd if=/dev/null of=state/.run_orders.json")
    assert r.returncode == 2


# -- Permitidos: lecturas puras y scripts autorizados -----------------------


def test_permite_cat_needs_reconciliation_n8():
    r = run_hook("cat state/.needs_reconciliation")
    assert r.returncode == 0


def test_permite_ls_state_n8():
    r = run_hook("ls -la state/")
    assert r.returncode == 0


def test_permite_grep_run_orders_n8():
    r = run_hook("grep count state/.run_orders.json")
    assert r.returncode == 0


def test_permite_head_needs_reconciliation_n8():
    r = run_hook("head state/.needs_reconciliation")
    assert r.returncode == 0


def test_permite_tail_run_orders_n8():
    r = run_hook("tail state/.run_orders.json")
    assert r.returncode == 0


def test_permite_reconcile_done_pese_a_mencionar_needs_reconciliation():
    r = run_hook(".venv/bin/python scripts/reconcile.py --done")
    assert r.returncode == 0


def test_permite_place_order_con_reason_que_menciona_run_orders():
    # Invocación real de un script autorizado que menciona el archivo
    # protegido en su propio texto (--reason) -- no es un intento de
    # evadirlo, place_order.py maneja ese archivo internamente.
    r = run_hook(
        '.venv/bin/python scripts/place_order.py open --symbol SPY --amount 10 '
        '--stop-loss-pct 0.1 --reason "verificado tras chequear state/.run_orders.json"'
    )
    assert r.returncode == 0


def test_permite_snapshot_script_n8():
    r = run_hook(
        '.venv/bin/python scripts/snapshot.py'
    )
    assert r.returncode == 0


# --- WP6/N11: default-deny por mención (reemplaza la lista negra de -------
# --- vehículos de N8) -------------------------------------------------
#
# Re-auditoría: la lista de vehículos de N8 (python/perl/ruby/find/xargs/
# eval/rm/unlink/shred/dd/sh -c/bash -c) NUNCA converge -- cada ronda
# encontró un vehículo nuevo no enumerado (rm -> python -c -> node -e/
# heredoc). PROTECTED_STATE_FILES (.run_orders, .needs_reconciliation,
# positions.json, equity.csv -- N11 agrega los dos últimos, INPUTS de los
# seis límites duros de risk.py) ahora se protege por DEFAULT-DENY: toda
# mención bloquea, salvo (a) invocación real de un script autorizado, o
# (b) una lectura pura y completa (cat/head/tail/less/more/wc/ls/stat/
# grep/diff, o `python3 -m json.tool`) sin ningún otro operador de shell.


def test_bloquea_node_unlink_sobre_run_orders():
    # El vector que motivó el cambio de estrategia: "node" nunca estuvo
    # en ninguna lista de vehículos -- bajo default-deny no hace falta
    # enumerarlo, la sola mención del archivo alcanza para bloquear.
    r = run_hook(
        "node -e \"require('fs').unlinkSync('state/.run_orders.json')\""
    )
    assert r.returncode == 2


def test_bloquea_heredoc_python_os_remove_sobre_positions_json():
    r = run_hook(
        "python3 - <<EOF\n"
        "import os\n"
        "os.remove('state/positions.json')\n"
        "EOF"
    )
    assert r.returncode == 2


def test_bloquea_echo_redireccion_sobre_equity_csv():
    r = run_hook("echo x > state/equity.csv")
    assert r.returncode == 2


def test_bloquea_chflags_sobre_positions_json():
    r = run_hook("chflags uchg state/positions.json")
    assert r.returncode == 2


def test_bloquea_mv_sobre_positions_json():
    r = run_hook("mv fake.json state/positions.json")
    assert r.returncode == 2


def test_bloquea_cp_sobre_positions_json():
    r = run_hook("cp fake.json state/positions.json")
    assert r.returncode == 2


def test_bloquea_awk_inplace_sobre_equity_csv():
    # Otro vehículo nunca antes enumerado (ni en N8 ni en ninguna lista
    # negra) -- prueba directa de que el default-deny no depende de
    # reconocer el nombre del intérprete/herramienta.
    r = run_hook("awk '{print}' state/equity.csv > /tmp/x")
    assert r.returncode == 2


def test_permite_cat_positions_json():
    r = run_hook("cat state/positions.json")
    assert r.returncode == 0


def test_permite_grep_positions_json():
    r = run_hook("grep BTC state/positions.json")
    assert r.returncode == 0


def test_permite_python_json_tool_positions_json():
    r = run_hook("python3 -m json.tool state/positions.json")
    assert r.returncode == 0


def test_permite_venv_python_json_tool():
    # FP reportado por el auditor: el python del venv tiene prefijo de ruta.
    r = run_hook(".venv/bin/python -m json.tool state/positions.json")
    assert r.returncode == 0


def test_bloquea_venv_json_tool_con_pipe_a_vehiculo():
    r = run_hook(".venv/bin/python -m json.tool state/positions.json | bash")
    assert r.returncode == 2


def test_permite_tail_equity_csv():
    r = run_hook("tail state/equity.csv")
    assert r.returncode == 0


def test_bloquea_cat_positions_json_pipe_a_bash():
    # Excepción (b) exige la invocación COMPLETA sin ningún otro operador
    # de shell -- un pipe a un vehículo de ejecución saca al comando de
    # la excepción de lectura pura y cae al default-deny.
    r = run_hook("cat state/positions.json | bash")
    assert r.returncode == 2


def test_permite_pytest_tests_completo():
    r = run_hook(".venv/bin/pytest tests/ -q")
    assert r.returncode == 0


def test_permite_printf_append_a_journal_sin_mencionar_protegidos():
    # Crítico para el flujo real: el journaling normal del agente
    # (printf/tee >> state/journal.md) sigue funcionando -- journal.md no
    # está en PROTECTED_STATE_FILES y este comando no menciona ninguno de
    # los cuatro archivos que sí lo están.
    r = run_hook(
        "printf -- '- 2026-08-13 10:00 -0300 DRY_RUN | open QQQ amount=50\\n' "
        ">> state/journal.md"
    )
    assert r.returncode == 0


def test_permite_tee_append_a_journal_sin_mencionar_protegidos_wp6():
    r = run_hook("echo 'nota de journal' | tee -a state/journal.md")
    assert r.returncode == 0


def test_permite_grep_con_pipe_de_regex_citado_no_es_pipe_de_shell():
    # Regresión real: encontrada al verificar este mismo cambio. Un patrón
    # de grep con alternancia ("a\|b") trae un "|" LITERAL dentro de las
    # comillas -- no es un pipe de shell real, y _es_lectura_segura_de_estado
    # debe reconocerlo como tal (chequea operadores sobre el segmento con
    # las comillas enmascaradas, no sobre el texto crudo) para no perder la
    # excepción (b) de lectura pura.
    r = run_hook(
        'grep -n "needs_reconciliation\\|run_orders\\|positions.json\\|equity.csv" '
        "PLAYBOOK.md"
    )
    assert r.returncode == 0


def test_bloquea_grep_con_pipe_de_shell_real_pese_a_comillas_en_otro_lado():
    # Control negativo: un pipe de shell REAL (fuera de comillas) sigue
    # descalificando la excepción, aunque el comando tenga comillas en
    # otra parte.
    r = run_hook('grep "BTC" state/positions.json | bash')
    assert r.returncode == 2

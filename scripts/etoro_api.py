"""Cliente HTTP para la API de eToro Agent Portfolios.

Solo transporte: auth (x-api-key/x-user-key), x-request-id por request,
backoff/retries ante 429, y métodos finos por endpoint (paths reales,
verificados en docs/api-notes.md). NO contiene lógica de riesgo ni de
negocio — eso vive en risk.py y en los scripts que consumen este cliente
(snapshot.py, place_order.py).
"""
import os
import time
import uuid

import requests

BASE_URL = "https://public-api.etoro.com"
DEFAULT_TIMEOUT = 30
RETRY_BACKOFF_SECONDS = [15, 30, 60]
MAX_RETRY_AFTER_SECONDS = 60


class EtoroAuthError(Exception):
    """401 de la API: credenciales inválidas. No tiene sentido reintentar."""


class EtoroUnknownOutcomeError(Exception):
    """Respuesta 2xx cuyo body no es JSON parseable (ej. WAF/proxy intermedio).

    Esto NO prueba que la orden haya fallado: un POST de apertura/cierre pudo
    haberse ejecutado igual del lado de eToro pese a este error del lado
    cliente. Ver el docstring de EtoroClient.request() para la política de
    no-reintento ante este y otros errores en POSTs de trading.
    """


class EtoroClient:
    def __init__(self, api_key=None, user_key=None):
        self.api_key = api_key or os.environ.get("ETORO_API_KEY")
        self.user_key = user_key or os.environ.get("ETORO_USER_KEY")
        if not self.api_key:
            raise ValueError(
                "falta ETORO_API_KEY: pasalo como api_key= o definí la variable de entorno ETORO_API_KEY"
            )
        if not self.user_key:
            raise ValueError(
                "falta ETORO_USER_KEY: pasalo como user_key= o definí la variable de entorno ETORO_USER_KEY"
            )

    def _headers(self):
        return {
            "x-api-key": self.api_key,
            "x-user-key": self.user_key,
            "x-request-id": str(uuid.uuid4()),
            "Content-Type": "application/json",
        }

    def request(self, method, path, max_retries=3, **kwargs):
        """Ejecuta un request HTTP con auth, backoff en 429 y manejo de errores.

        401 -> EtoroAuthError (terminal, no reintenta).
        429 -> espera Retry-After (capado a MAX_RETRY_AFTER_SECONDS=60s; si no
               es un número parseable —p.ej. viene como HTTP-date RFC 7231—
               cae al backoff fijo 15/30/60s) y reintenta hasta max_retries
               veces; si persiste, RuntimeError.
        otros 4xx/5xx -> raise_for_status() (nunca reintentado acá).
        2xx con body no-JSON (WAF/proxy intermedio) -> EtoroUnknownOutcomeError.
        Devuelve el body parseado como JSON (o {} si no hay body).

        IMPORTANTE para callers de POSTs de trading (abrir/cerrar posición):
        este método NUNCA reintenta un POST ante un error (HTTPError,
        ConnectionError, Timeout, EtoroUnknownOutcomeError, etc.) — un error
        acá no prueba que la orden no se haya ejecutado del lado de eToro.
        El caller tampoco debe reintentar a ciegas: ante duda, re-leer
        GET /api/v1/trading/info/real/pnl (cache 60s) para verificar el
        estado real del portfolio antes de asumir que la orden falló o de
        disparar una orden equivalente de nuevo.
        """
        url = f"{BASE_URL}{path}"
        attempt = 0
        while True:
            resp = requests.request(
                method, url, headers=self._headers(), timeout=DEFAULT_TIMEOUT, **kwargs
            )

            if resp.status_code == 401:
                raise EtoroAuthError(f"autenticación rechazada por eToro ({method} {path})")

            if resp.status_code == 429:
                if attempt >= max_retries:
                    raise RuntimeError(
                        f"rate limit persistente tras {max_retries} reintentos ({method} {path})"
                    )
                time.sleep(self._retry_wait(resp.headers.get("Retry-After"), attempt))
                attempt += 1
                continue

            resp.raise_for_status()
            if not resp.content:
                return {}
            try:
                return resp.json()
            except ValueError as exc:
                raise EtoroUnknownOutcomeError(
                    f"respuesta 2xx con body no-JSON en {method} {path}: {exc}"
                ) from exc

    @staticmethod
    def _retry_wait(retry_after, attempt):
        """Segundos a esperar ante un 429.

        Retry-After (si es un número) se capa a MAX_RETRY_AFTER_SECONDS para
        no dejar el proceso dormido horas por un valor absurdo del servidor.
        Si Retry-After no es parseable como número (ej. HTTP-date), cae al
        backoff fijo 15/30/60s según el intento.
        """
        if retry_after is not None:
            try:
                return min(float(retry_after), MAX_RETRY_AFTER_SECONDS)
            except (TypeError, ValueError):
                pass
        return RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]

    # -- Portfolio / info --------------------------------------------------

    def get_agent_portfolios(self):
        """GET /api/v1/agent-portfolios — lista los agent portfolios del usuario."""
        return self.request("GET", "/api/v1/agent-portfolios")

    def get_pnl(self):
        """GET /api/v1/trading/info/real/pnl — portfolio completo (cache 60s)."""
        return self.request("GET", "/api/v1/trading/info/real/pnl")

    # -- Market data ---------------------------------------------------------

    def search_instrument(self, symbol):
        """GET /api/v1/market-data/search — resuelve instrumentId por símbolo."""
        return self.request(
            "GET",
            "/api/v1/market-data/search",
            params={"internalSymbolFull": symbol},
        )

    def get_instruments_metadata(self, instrument_ids):
        """GET /api/v1/market-data/instruments — metadata para mapear id->símbolo."""
        return self.request(
            "GET",
            "/api/v1/market-data/instruments",
            params={"instrumentIds": ",".join(str(i) for i in instrument_ids)},
        )

    def get_candles(self, instrument_id, *, direction="desc", interval="OneDay", count):
        """GET .../instruments/{id}/history/candles/{direction}/{interval}/{count}.

        count es requerido: cuántas velas pedir es una decisión de estrategia
        del caller (snapshot.py/place_order.py), no un default de negocio que
        deba vivir en el cliente HTTP.
        """
        return self.request(
            "GET",
            f"/api/v1/market-data/instruments/{instrument_id}/history/candles/"
            f"{direction}/{interval}/{count}",
        )

    # -- Trading execution -----------------------------------------------

    def open_position_by_amount(
        self, instrument_id, amount_usd, stop_loss_rate=None, is_buy=True, leverage=1
    ):
        """POST .../market-open-orders/by-amount — abre posición. Body PascalCase.

        StopLossRate es un PRECIO absoluto (no porcentaje) y solo se incluye
        en el body si se pasa explícitamente.
        """
        body = {
            "InstrumentID": instrument_id,
            "IsBuy": is_buy,
            "Leverage": leverage,
            "Amount": amount_usd,
        }
        if stop_loss_rate is not None:
            body["StopLossRate"] = stop_loss_rate
        return self.request(
            "POST", "/api/v1/trading/execution/market-open-orders/by-amount", json=body
        )

    def close_position(self, position_id, instrument_id, units_to_deduct=None):
        """POST .../market-close-orders/positions/{positionId} — cierra posición.

        units_to_deduct=None => cierre total.
        """
        body = {"InstrumentId": instrument_id, "UnitsToDeduct": units_to_deduct}
        return self.request(
            "POST",
            f"/api/v1/trading/execution/market-close-orders/positions/{position_id}",
            json=body,
        )

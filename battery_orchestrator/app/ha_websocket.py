"""
Cliente WebSocket persistente hacia Home Assistant.

Hasta ahora `ha_client.py` habla con HA casi siempre por REST — sirve para
"preguntar", pero no para "que HA avise". Este modulo abre una conexion
WebSocket (`/api/websocket`) y se suscribe a `state_changed`: en cuanto
cambia el estado de un sensor que nos interesa (consumo, solar, SOC,
potencia de baterias declaradas por HA...), HA lo empuja al instante, sin
que el add-on tenga que sondear. Es el mismo mecanismo que ya usan Node-RED
o cualquier custom_component reactivo (como Climate Orchestrator) — la
diferencia es que ellos corren integrados en HA o via WebSocket, y este
add-on, al ser un proceso aparte (Supervisor), necesita abrir la conexion
el mismo.

Diseño deliberadamente simple y con red de seguridad:
  - Reconexion automatica con backoff si se cae la conexion (WiFi, reinicio
    de HA Core, lo que sea) — nunca deja el add-on sin datos por un fallo
    puntual.
  - El ciclo PERIODICO (`background_loop`/`run_cycle` en main.py) sigue
    funcionando exactamente igual, como respaldo — si el WebSocket falla o
    tarda en reconectar, el add-on sigue re-planificando cada
    `cycle_seconds` como siempre lo ha hecho. El WebSocket es una mejora de
    LATENCIA (reaccionar en segundos, no esperar hasta el proximo ciclo),
    nunca una dependencia dura.
  - Nunca lanza `run_cycle()` directamente desde el hilo del WebSocket:
    solo marca "hay algo nuevo que mirar" (un `threading.Event`) y un
    trabajador aparte decide cuando ejecutar de verdad, con un margen
    minimo entre ejecuciones (`REACTIVE_MIN_INTERVAL_SECONDS`) para no
    lanzar el ciclo completo de planificacion decenas de veces por segundo
    si varios sensores cambian casi a la vez.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

log = logging.getLogger("ha_websocket")

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN")
if SUPERVISOR_TOKEN:
    WS_URL = "ws://supervisor/core/websocket"
    TOKEN = SUPERVISOR_TOKEN
else:
    _ha_url = os.environ.get("HA_URL", "http://localhost:8123/api")
    WS_URL = _ha_url.replace("http://", "ws://").replace("https://", "wss://").removesuffix("/api") + "/api/websocket"
    TOKEN = os.environ.get("HA_TOKEN", "")

# Backoff de reconexion: crece hasta el ultimo valor y se queda ahi (nunca
# deja de reintentar del todo). Una conexion que llega a autenticarse bien
# resetea el contador — un fallo puntual no debe ir acumulando backoff para
# siempre.
RECONNECT_BACKOFF_SECONDS = (2, 5, 10, 30, 60)

# Cuanto tiempo minimo, como poco, entre dos ejecuciones reactivas seguidas
# del ciclo de planificacion — el propio `run_cycle` ya tarda un rato en
# hacer sus llamadas reales a HA/EcoFlow, no tiene sentido relanzarlo antes
# de que la vuelta anterior haya podido terminar y sentar los cambios.
REACTIVE_MIN_INTERVAL_SECONDS = 5


class HAWebSocketClient:
    """Una instancia por add-on. `set_watched_entities` se llama cada vez
    que `run_cycle` recarga la config (baterias/sensores pueden cambiar en
    caliente desde la interfaz) — la suscripcion en si es a TODOS los
    `state_changed` (HA no permite filtrar por entidad en la suscripcion),
    el filtrado a "nos interesa esta o no" se hace aqui, en memoria, barato."""

    def __init__(self, on_relevant_change) -> None:
        self._on_relevant_change = on_relevant_change
        self._watched: set[str] = set()
        self._watched_lock = threading.Lock()
        self._ws = None
        self._stop = False
        self.connected = False

    def set_watched_entities(self, entities: set[str]) -> None:
        with self._watched_lock:
            self._watched = {e for e in entities if e}

    def _is_watched(self, entity_id: str) -> bool:
        with self._watched_lock:
            return entity_id in self._watched

    def stop(self) -> None:
        self._stop = True
        try:
            if self._ws is not None:
                self._ws.close()
        except Exception:
            pass

    def run_forever(self) -> None:
        """Bucle de conexion — pensado para correr en su propio hilo daemon,
        para siempre (hasta que el proceso del add-on termine)."""
        attempt = 0
        while not self._stop:
            try:
                self._connect_and_listen()
                attempt = 0  # conexion que llego a autenticarse: resetea el backoff
            except Exception:
                log.warning("WebSocket de HA: conexion perdida (%s), reintentando", "sin detalle")
                log.debug("Detalle del fallo de WebSocket", exc_info=True)
            finally:
                self.connected = False
            if self._stop:
                return
            delay = RECONNECT_BACKOFF_SECONDS[min(attempt, len(RECONNECT_BACKOFF_SECONDS) - 1)]
            attempt += 1
            time.sleep(delay)

    def _connect_and_listen(self) -> None:
        import websocket as ws_lib

        self._ws = ws_lib.create_connection(WS_URL, timeout=30)
        try:
            hello = json.loads(self._ws.recv())
            if hello.get("type") != "auth_required":
                raise RuntimeError(f"Handshake inesperado del WebSocket de HA: {hello}")
            self._ws.send(json.dumps({"type": "auth", "access_token": TOKEN}))
            auth_result = json.loads(self._ws.recv())
            if auth_result.get("type") != "auth_ok":
                raise RuntimeError(f"Autenticacion WebSocket de HA fallida: {auth_result}")

            self._ws.send(json.dumps({"id": 1, "type": "subscribe_events", "event_type": "state_changed"}))
            sub_ack = json.loads(self._ws.recv())
            if not sub_ack.get("success"):
                raise RuntimeError(f"No se pudo suscribir a state_changed: {sub_ack}")

            self.connected = True
            log.info("WebSocket de HA conectado y suscrito a state_changed")

            while not self._stop:
                raw = self._ws.recv()
                if not raw:
                    raise RuntimeError("WebSocket de HA cerrado por el otro lado")
                msg = json.loads(raw)
                if msg.get("type") != "event":
                    continue
                event = msg.get("event") or {}
                if event.get("event_type") != "state_changed":
                    continue
                data = event.get("data") or {}
                entity_id = data.get("entity_id")
                if not entity_id or not self._is_watched(entity_id):
                    continue
                old_state = (data.get("old_state") or {}).get("state")
                new_state = (data.get("new_state") or {}).get("state")
                if old_state == new_state:
                    # Solo el ATRIBUTO cambio (p.ej. jitter interno de otra
                    # integracion) — no es una lectura nueva de verdad,
                    # ignorarlo evita relanzar el ciclo por nada.
                    continue
                try:
                    self._on_relevant_change(entity_id, new_state)
                except Exception:
                    log.exception("Fallo procesando el evento reactivo de %s", entity_id)
        finally:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None


class ReactiveTrigger:
    """Debounce + coalesce de disparos reactivos: cualquier numero de
    eventos que lleguen mientras se espera o se esta ejecutando el ciclo se
    reducen a UNA sola ejecucion mas, justo despues del margen minimo — ni
    se pierde ningun cambio real (si algo cambio durante la espera, se
    vuelve a ejecutar), ni se satura `run_cycle` con ejecuciones
    superpuestas."""

    def __init__(self, run_once) -> None:
        self._run_once = run_once
        self._event = threading.Event()
        self._lock = threading.Lock()

    def trigger(self) -> None:
        self._event.set()

    def worker_loop(self) -> None:
        while True:
            self._event.wait()
            self._event.clear()
            with self._lock:
                try:
                    self._run_once()
                except Exception:
                    log.exception("Fallo en la ejecucion reactiva del ciclo de planificacion")
            time.sleep(REACTIVE_MIN_INTERVAL_SECONDS)

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
import queue
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
        # Peticion/respuesta sobre la MISMA conexion persistente (ver
        # `call`) -- ademas de escuchar eventos, cualquier hilo puede
        # pedir algo puntual (get_states, historico, llamar a un
        # servicio...) y esperar su respuesta, correlacionada por id de
        # mensaje. Todo lo que hable con HA pasa por aqui, nunca por REST
        # aparte -- una unica conexion, un unico transporte.
        self._id_lock = threading.Lock()
        self._next_id = 0
        self._send_lock = threading.Lock()
        self._pending: dict[int, queue.Queue] = {}
        # BUG REAL, confirmado por el usuario: incluso con la latencia de
        # zonas/luces ya arreglada, el encendido seguia tardando 3-5s en
        # TODAS las zonas por igual (Tapo, Tuya, o luces nativas de HA sin
        # ningun bridge de por medio) -- la causa comun era esta: `get_
        # states()` trae el volcado COMPLETO de estados de HA (1770
        # entidades, ~870KB en esta instalacion) por WebSocket, y Lighting
        # lo pedia de nuevo en CADA ciclo reactivo. Ahora se mantiene una
        # copia local (`_states_cache`), sembrada UNA vez al conectar y
        # actualizada en vivo con cada evento `state_changed` que ya nos
        # llega de todos modos (la suscripcion es a TODOS los cambios,
        # filtrados aqui) -- `get_states()`/`get_state()` pasan a ser
        # lecturas locales instantaneas, sin ningun viaje de red.
        self._states_lock = threading.Lock()
        self._states_cache: dict[str, dict] = {}

    def _next_msg_id(self) -> int:
        with self._id_lock:
            self._next_id += 1
            return self._next_id

    def call(self, msg_type: str, timeout: float = 20, **kwargs):
        """Pide algo puntual a HA por la conexion ya abierta y espera su
        respuesta -- bloqueante, pensado para llamarse desde CUALQUIER hilo
        que no sea el propio lector (`run_forever`). Lanza si no hay
        conexion, si HA responde error, o si no responde a tiempo (nunca
        se queda esperando para siempre)."""
        if not self.connected or self._ws is None:
            raise RuntimeError("WebSocket de HA no conectado todavia")
        msg_id = self._next_msg_id()
        q: queue.Queue = queue.Queue(maxsize=1)
        self._pending[msg_id] = q
        payload = {"id": msg_id, "type": msg_type, **kwargs}
        try:
            with self._send_lock:
                self._ws.send(json.dumps(payload))
            try:
                result = q.get(timeout=timeout)
            except queue.Empty:
                raise TimeoutError(f"WebSocket de HA: sin respuesta a '{msg_type}' en {timeout}s")
            if not result.get("success", True):
                raise RuntimeError(f"WebSocket de HA devolvio error para '{msg_type}': {result.get('error')}")
            return result.get("result")
        finally:
            self._pending.pop(msg_id, None)

    # ---------------------------------------------------- atajos comunes --

    def get_states(self) -> list[dict]:
        """Lectura LOCAL de la copia mantenida en vivo (ver `_states_
        cache` en `__init__` y `_connect_and_listen`) -- ya NO pide el
        volcado completo a HA en cada llamada (bug real de latencia,
        confirmado por el usuario). Si el WebSocket aun no ha terminado
        de conectar/sembrar la copia (arranque en frio), devuelve lo que
        haya ahora mismo (vacio -- los llamantes ya manejan bien "sin
        datos todavia", igual que manejaban un fallo de `call()`)."""
        with self._states_lock:
            return list(self._states_cache.values())

    def get_state(self, entity_id: str) -> dict | None:
        with self._states_lock:
            return self._states_cache.get(entity_id)

    def call_service(self, domain: str, service: str, service_data: dict | None = None,
                      target: dict | None = None, return_response: bool = False):
        kwargs = {"domain": domain, "service": service}
        if service_data:
            kwargs["service_data"] = service_data
        if target:
            kwargs["target"] = target
        if return_response:
            kwargs["return_response"] = True
        result = self.call("call_service", **kwargs)
        return result.get("response") if return_response and result else None

    def get_history(self, entity_id: str, start_iso: str, with_attributes: bool = False) -> list[dict]:
        """
        Historico de UNA entidad desde `start_iso` hasta ahora, normalizado
        a una lista de puntos `{"state", "last_updated", "attributes"}` —
        oculta el formato compacto real del WebSocket (`history/
        history_during_period`, claves "s"/"lu"/"a") a quien llama.

        OJO con `with_attributes=True`: cada punto solo trae los
        atributos que CAMBIARON respecto al anterior (formato comprimido
        de HA), no el diccionario completo — aqui se rellenan hacia
        adelante (el primer punto SI trae el conjunto completo, los
        siguientes se van fusionando encima) para que quien llama siempre
        vea el estado de atributos COMPLETO en cada punto, nunca uno a
        medias.
        """
        result = self.call(
            "history/history_during_period",
            start_time=start_iso,
            entity_ids=[entity_id],
            minimal_response=not with_attributes,
            no_attributes=not with_attributes,
            significant_changes_only=False,
        )
        raw = (result or {}).get(entity_id) or []
        points = []
        known_attrs: dict = {}
        for p in raw:
            if with_attributes:
                known_attrs = {**known_attrs, **(p.get("a") or {})}
                attrs = dict(known_attrs)
            else:
                attrs = {}
            points.append({"state": p.get("s"), "last_updated": p.get("lu"), "attributes": attrs})
        return points

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

            sub_id = self._next_msg_id()
            self._ws.send(json.dumps({"id": sub_id, "type": "subscribe_events", "event_type": "state_changed"}))
            sub_ack = json.loads(self._ws.recv())
            if not sub_ack.get("success"):
                raise RuntimeError(f"No se pudo suscribir a state_changed: {sub_ack}")

            # Siembra UNA vez la copia local completa -- directo por
            # `recv()`, NO via `call()` (que esperaria la respuesta desde
            # ESTE MISMO hilo lector, un interbloqueo seguro: nadie mas
            # va a leer el socket para entregarsela). En este punto de la
            # conexion (justo tras el ack de suscripcion, antes de que
            # ningun otro hilo haya podido mandar nada) el SIGUIENTE
            # mensaje que llegue solo puede ser esta respuesta.
            states_id = self._next_msg_id()
            self._ws.send(json.dumps({"id": states_id, "type": "get_states"}))
            states_resp = json.loads(self._ws.recv())
            if states_resp.get("success"):
                with self._states_lock:
                    self._states_cache = {
                        s["entity_id"]: s for s in (states_resp.get("result") or []) if s.get("entity_id")
                    }
            else:
                log.warning("WebSocket de HA: fallo sembrando la copia local de estados: %s", states_resp.get("error"))

            self.connected = True
            log.info(
                "WebSocket de HA conectado y suscrito a state_changed (%d entidades sembradas)",
                len(self._states_cache),
            )

            while not self._stop:
                raw = self._ws.recv()
                if not raw:
                    raise RuntimeError("WebSocket de HA cerrado por el otro lado")
                msg = json.loads(raw)
                msg_type = msg.get("type")

                if msg_type == "result":
                    # Respuesta a una llamada puntual hecha desde OTRO hilo
                    # (ver `call`) -- se entrega a quien esperaba ese id
                    # concreto, nunca se procesa aqui mismo.
                    q = self._pending.get(msg.get("id"))
                    if q is not None:
                        try:
                            q.put_nowait(msg)
                        except queue.Full:
                            pass
                    continue

                if msg_type != "event":
                    continue
                event = msg.get("event") or {}
                if event.get("event_type") != "state_changed":
                    continue
                data = event.get("data") or {}
                entity_id = data.get("entity_id")
                if not entity_id:
                    continue
                new_state_obj = data.get("new_state")
                # La copia local se mantiene con TODOS los cambios, no
                # solo los "vigilados" -- seguimos suscritos a TODO
                # `state_changed` (HA no permite filtrar por entidad en
                # la suscripcion, ver docstring de la clase), asi que
                # esto no cuesta ninguna llamada de red extra, solo
                # actualizar el dict local. El filtro "vigilado o no"
                # (mas abajo) sigue existiendo tal cual -- decide si esto
                # dispara un ciclo reactivo, nunca si se guarda o no.
                with self._states_lock:
                    if new_state_obj is None:
                        self._states_cache.pop(entity_id, None)
                    else:
                        self._states_cache[entity_id] = new_state_obj
                if not self._is_watched(entity_id):
                    continue
                old_state = (data.get("old_state") or {}).get("state")
                new_state = new_state_obj.get("state") if new_state_obj else None
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
            # Cualquier `call()` en espera no se queda colgada hasta su
            # propio timeout si la conexion se cae entera -- se entera YA
            # de que ha fallado.
            for q in list(self._pending.values()):
                try:
                    q.put_nowait({"success": False, "error": {"message": "conexion WebSocket perdida"}})
                except queue.Full:
                    pass


class ReactiveTrigger:
    """Debounce + coalesce de disparos reactivos: cualquier numero de
    eventos que lleguen mientras se espera o se esta ejecutando el ciclo se
    reducen a UNA sola ejecucion mas, justo despues del margen minimo — ni
    se pierde ningun cambio real (si algo cambio durante la espera, se
    vuelve a ejecutar), ni se satura `run_cycle` con ejecuciones
    superpuestas.

    `min_interval_seconds` es configurable por instancia (antes era un
    valor fijo global, `REACTIVE_MIN_INTERVAL_SECONDS`) -- BUG REAL,
    confirmado por el usuario: Lighting comparte esta misma clase con
    Battery, cuyo `run_cycle` SI hace llamadas externas caras (EcoFlow,
    forecast) y necesita ese margen de 5s para no saturar nada. Lighting
    no tiene ese coste (decidir y encender una zona es barato, todo en
    proceso/LAN) y el usuario esperaba una reaccion inmediata a la
    presencia, igual que tenia con Node-RED -- con el margen de 5s
    heredado de Battery, si CUALQUIER otra entidad vigilada (de
    cualquier zona) cambiaba justo antes de detectarse presencia, el
    encendido real quedaba esperando el resto de ese margen antes de
    poder procesarse. Battery/Climate mantienen el margen por defecto
    (comportamiento sin cambios); Lighting pasa uno mucho mas bajo."""

    def __init__(self, run_once, min_interval_seconds: float = REACTIVE_MIN_INTERVAL_SECONDS) -> None:
        self._run_once = run_once
        self._min_interval_seconds = min_interval_seconds
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
            time.sleep(self._min_interval_seconds)

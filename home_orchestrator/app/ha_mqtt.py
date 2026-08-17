"""
Cliente MQTT hacia el broker LOCAL de HA (Mosquitto, `core-mosquitto`) --
distinto del cliente MQTT que ya existe en `ecoflow_cloud.py` (ese habla
con el broker EN LA NUBE de EcoFlow, un servidor totalmente aparte).

Las credenciales NUNCA se guardan en disco ni en el repo: se piden en
caliente a Supervisor (`http://supervisor/services/mqtt`, con el
`SUPERVISOR_TOKEN` que el addon ya tiene inyectado) cada vez que hace
falta reconectar -- mismo criterio que el resto de credenciales
gestionadas por Supervisor. Requiere que `config.yaml` declare
`services: [mqtt:want]` (ver v0.11.57).

Sirve de base para MQTT Discovery: publicar una entidad `climate.*` (u
otro dominio) nativa de HA desde fuera de HA Core, con topics de
comando (HA -> nosotros) y de estado (nosotros -> HA). Es el mecanismo
que va a usar el plugin de Climate (fase 2 de Home Orchestrator).
"""

from __future__ import annotations

import json
import logging
import os
import threading

import requests

log = logging.getLogger("ha_mqtt")

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN")
DISCOVERY_PREFIX = "homeassistant"


def _fetch_broker_credentials() -> dict | None:
    """
    Pide a Supervisor las credenciales del broker local -- solo funciona
    dentro de un addon con `services: [mqtt:want]` declarado. Fuera de un
    addon (desarrollo local sin Supervisor), no hay forma de auto-
    descubrir el broker; se puede indicar a mano con las variables de
    entorno MQTT_HOST/MQTT_PORT/MQTT_USERNAME/MQTT_PASSWORD para pruebas.
    """
    if not SUPERVISOR_TOKEN:
        host = os.environ.get("MQTT_HOST")
        if not host:
            return None
        return {
            "host": host,
            "port": int(os.environ.get("MQTT_PORT", 1883)),
            "username": os.environ.get("MQTT_USERNAME", ""),
            "password": os.environ.get("MQTT_PASSWORD", ""),
        }
    try:
        r = requests.get(
            "http://supervisor/services/mqtt",
            headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"},
            timeout=10,
        )
        r.raise_for_status()
        body = r.json()
        if body.get("result") != "ok":
            log.warning("Supervisor no dio credenciales MQTT: %s", body)
            return None
        return body["data"]
    except Exception:
        log.exception("Fallo pidiendo credenciales MQTT a Supervisor")
        return None


class MqttCommandWorker:
    """Ejecuta comandos llegados por MQTT FUERA del hilo de red de paho, en
    serie, y avisa al terminar para que se publique el estado de vuelta.

    BUG REAL de latencia (sintoma reportado: desde la interfaz de un plugin los
    cambios son inmediatos, desde la entidad que ese plugin publica por MQTT
    Discovery van lentisimos). Dos causas, ambas cubiertas aqui:

    1) `message_callback_add` ejecuta los callbacks en el hilo de RED de paho,
       el mismo que atiende el socket. Los manejadores de comando hacian I/O
       real ahi dentro: llamadas de servicio a HA una por luz, o una orden al
       dispositivo con `future.result(timeout=10)` (Tuya/TP-Link). Mientras eso
       corre, paho no lee ni escribe el socket -- los ACK de QoS 1 y todos los
       mensajes siguientes se encolan. Y como el cliente MQTT es UNO para todo
       el add-on, un solo dispositivo que no responde deja lento a TODO lo
       demas, no solo a si mismo.

    2) Los manejadores no publicaban el estado tras aplicar el comando, asi que
       HA se quedaba con el valor anterior hasta el siguiente sondeo o ciclo
       reactivo. Con `on_done` se publica en cuanto el comando termina, que es
       lo que ya hacian los endpoints HTTP equivalentes (de ahi la diferencia).

    En serie y con cola: dos ordenes seguidas de la misma entidad (arrastrar un
    deslizador) se aplican en el orden en que llegaron, nunca al reves.
    """

    def __init__(self, name: str, on_done=None) -> None:
        import queue

        self._queue = queue.Queue()
        self._on_done = on_done
        self._thread = threading.Thread(target=self._loop, name=name, daemon=True)
        self._thread.start()

    def submit(self, apply_command) -> None:
        self._queue.put(apply_command)

    def _loop(self) -> None:
        while True:
            apply_command = self._queue.get()
            try:
                apply_command()
            except Exception:
                log.exception("Fallo aplicando un comando MQTT en %s", self._thread.name)
            else:
                if self._on_done is not None:
                    try:
                        self._on_done()
                    except Exception:
                        log.exception(
                            "Comando MQTT aplicado en %s pero fallo al publicar el estado de "
                            "vuelta -- HA puede quedarse con el valor anterior", self._thread.name,
                        )
            finally:
                self._queue.task_done()


class HAMqttClient:
    """
    Una instancia por addon. `connect()` es bloqueante hasta la primera
    conexion (o hasta agotar el primer intento); a partir de ahi
    paho-mqtt reconecta solo con su propio backoff interno.
    """

    def __init__(self, client_id: str = "home_orchestrator_battery") -> None:
        self._client = None
        self._lock = threading.Lock()
        self.connected = False
        self._client_id = client_id

    def connect(self) -> bool:
        creds = _fetch_broker_credentials()
        if not creds:
            log.warning("Sin credenciales del broker MQTT local -- MQTT Discovery no disponible")
            return False

        import paho.mqtt.client as mqtt

        client_id = self._client_id
        c = mqtt.Client(client_id=client_id, callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        if creds.get("username"):
            c.username_pw_set(creds["username"], creds.get("password") or "")

        def _on_connect(client, userdata, flags, reason_code, properties=None):
            self.connected = reason_code == 0
            if self.connected:
                log.info("MQTT local de HA conectado (%s:%s)", creds["host"], creds["port"])
            else:
                log.warning("MQTT local de HA: fallo de conexion, codigo %s", reason_code)

        def _on_disconnect(client, userdata, flags, reason_code, properties=None):
            self.connected = False
            log.info("MQTT local de HA desconectado (reason_code=%s), paho reintentara solo", reason_code)

        c.on_connect = _on_connect
        c.on_disconnect = _on_disconnect
        c.connect_async(creds["host"], int(creds["port"]), keepalive=60)
        c.loop_start()
        self._client = c
        return True

    def publish(self, topic: str, payload, retain: bool = False) -> None:
        if self._client is None:
            return
        body = json.dumps(payload) if isinstance(payload, (dict, list)) else str(payload)
        self._client.publish(topic, body, qos=1, retain=retain)

    def subscribe(self, topic: str, on_message) -> None:
        if self._client is None:
            return
        self._client.message_callback_add(topic, on_message)
        self._client.subscribe(topic, qos=1)

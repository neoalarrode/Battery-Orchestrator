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


class HAMqttClient:
    """
    Una instancia por addon. `connect()` es bloqueante hasta la primera
    conexion (o hasta agotar el primer intento); a partir de ahi
    paho-mqtt reconecta solo con su propio backoff interno.
    """

    def __init__(self) -> None:
        self._client = None
        self._lock = threading.Lock()
        self.connected = False

    def connect(self) -> bool:
        creds = _fetch_broker_credentials()
        if not creds:
            log.warning("Sin credenciales del broker MQTT local -- MQTT Discovery no disponible")
            return False

        import paho.mqtt.client as mqtt

        client_id = "home_orchestrator_battery"
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

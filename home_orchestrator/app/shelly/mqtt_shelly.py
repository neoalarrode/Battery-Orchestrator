"""
Expone un dispositivo Shelly a Home Assistant via MQTT Discovery --
OPCIONAL y por dispositivo, mismo criterio que `govee/mqtt_govee.py`/
`tplink/mqtt_tplink.py` (`expose_mqtt`, ver shelly_store.py). A diferencia
de Govee (siempre bombilla RGB+CCT), aqui SI hace falta mirar la
`capability` detectada (ver shelly/device_manager.py) para decidir que
entidad publicar -- `switch.*` para un rele puro, `light.*` con brillo
para un atenuador blanco, `light.*` con brillo+hs para RGBW.
"""

from __future__ import annotations

import logging

log = logging.getLogger("shelly.mqtt")

DISCOVERY_PREFIX = "homeassistant"
NODE_ID = "home_orchestrator_shelly"


class MqttShellyDevice:
    def __init__(self, mqtt_client, manager, device_id: str, device_name: str) -> None:
        self._mqtt = mqtt_client
        self._manager = manager
        self.device_id = device_id
        self.device_name = device_name or device_id
        self._device_block = {
            "identifiers": [f"{NODE_ID}_{device_id}"],
            "name": self.device_name,
            "manufacturer": "Shelly",
            "model": self.device_name,
        }

    def _capability(self) -> str:
        info = self._manager.get_device(self.device_id)
        return info["capability"] if info else "switch"

    def _base(self, domain: str) -> str:
        return f"{DISCOVERY_PREFIX}/{domain}/{NODE_ID}/{self.device_id}"

    # ---------------------------------------------------------- discovery -

    def publish_discovery(self) -> None:
        capability = self._capability()
        if capability == "switch":
            self._publish_switch()
        else:
            self._publish_light(capability)

    def _publish_switch(self) -> None:
        base = self._base("switch")
        payload = {
            "name": None,
            "unique_id": f"{NODE_ID}_{self.device_id}_switch",
            "state_topic": f"{base}/state",
            "command_topic": f"{base}/set",
            "payload_on": "ON", "payload_off": "OFF", "state_on": "ON", "state_off": "OFF",
            "availability_topic": f"{base}/availability",
            "device": self._device_block,
        }
        self._mqtt.subscribe(f"{base}/set", self._on_power)
        self._mqtt.publish(f"{base}/config", payload, retain=True)
        self._mqtt.publish(f"{base}/availability", "online", retain=True)

    def _publish_light(self, capability: str) -> None:
        base = self._base("light")
        payload = {
            "name": None,
            "unique_id": f"{NODE_ID}_{self.device_id}_light",
            "state_topic": f"{base}/state",
            "command_topic": f"{base}/set",
            "payload_on": "ON", "payload_off": "OFF",
            "availability_topic": f"{base}/availability",
            "brightness_state_topic": f"{base}/brightness/state",
            "brightness_command_topic": f"{base}/brightness/set",
            "brightness_scale": 100,
            "device": self._device_block,
        }
        if capability == "rgbw":
            payload.update(hs_state_topic=f"{base}/hs/state", hs_command_topic=f"{base}/hs/set")
            self._mqtt.subscribe(f"{base}/hs/set", self._on_hs)
        self._mqtt.subscribe(f"{base}/set", self._on_power)
        self._mqtt.subscribe(f"{base}/brightness/set", self._on_brightness)
        self._mqtt.publish(f"{base}/config", payload, retain=True)
        self._mqtt.publish(f"{base}/availability", "online", retain=True)

    def remove_discovery(self) -> None:
        domain = "switch" if self._capability() == "switch" else "light"
        self._mqtt.publish(f"{self._base(domain)}/config", "", retain=True)

    # -------------------------------------------------------------- estado

    def publish_state(self) -> None:
        handle = self._manager.light_handle(self.device_id)
        if handle is None:
            return
        capability = self._capability()
        domain = "switch" if capability == "switch" else "light"
        base = self._base(domain)
        self._mqtt.publish(f"{base}/state", "ON" if handle.is_on else "OFF", retain=True)
        if domain == "light" and handle.brightness_pct is not None:
            self._mqtt.publish(f"{base}/brightness/state", round(handle.brightness_pct), retain=True)

    # ------------------------------------------------------------- comandos

    def _on_power(self, client, userdata, msg) -> None:
        try:
            if msg.payload.decode() == "ON":
                self._manager.turn_on(self.device_id)
            else:
                self._manager.turn_off(self.device_id)
        except Exception:
            log.exception("Shelly %s: fallo aplicando encendido/apagado", self.device_id)

    def _on_brightness(self, client, userdata, msg) -> None:
        try:
            self._manager.turn_on(self.device_id, brightness_pct=max(1, min(100, round(float(msg.payload.decode())))))
        except Exception:
            log.exception("Shelly %s: fallo aplicando brillo", self.device_id)

    def _on_hs(self, client, userdata, msg) -> None:
        try:
            h_str, s_str = msg.payload.decode().split(",")
            self._manager.turn_on(self.device_id, hs=(float(h_str), float(s_str)))
        except Exception:
            log.exception("Shelly %s: fallo aplicando color", self.device_id)

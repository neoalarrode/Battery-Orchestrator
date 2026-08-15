"""
Expone un dispositivo TP-Link a Home Assistant via MQTT Discovery --
OPCIONAL y por dispositivo, mismo criterio que `tuya/mqtt_tuya.py`
(`expose_mqtt`, ver tplink_store.py). A diferencia de Tuya, aqui NO hace
falta ningun perfil declarativo: `python-kasa` ya dice en tiempo real que
modulos tiene el dispositivo (`device.modules`), asi que el tipo de
entidad a publicar (light.*/switch.*/sensor.* de potencia) se decide
mirando el dispositivo real, no un YAML aparte.

Tambien de paso: aqui NO hay ninguna conversion mireds<->escala propia
del fabricante que hacer (el bug real que hubo que arreglar en
mqtt_tuya.py) -- `python-kasa` ya trabaja en Kelvin de verdad
(`Light.color_temp`/`set_color_temp`), igual que el propio componente
`tplink` de Home Assistant.
"""

from __future__ import annotations

import logging
from functools import partial

from kasa import Module

log = logging.getLogger("tplink.mqtt")

DISCOVERY_PREFIX = "homeassistant"
NODE_ID = "home_orchestrator_tplink"


class MqttTplinkDevice:
    def __init__(self, mqtt_client, manager, device_id: str, device_name: str) -> None:
        self._mqtt = mqtt_client
        self._manager = manager
        self.device_id = device_id
        self.device_name = device_name or device_id
        self._device_block = {
            "identifiers": [f"{NODE_ID}_{device_id}"],
            "name": self.device_name,
            "manufacturer": "TP-Link",
            "model": self.device_name,
        }

    def _base(self, suffix: str) -> str:
        return f"{DISCOVERY_PREFIX}/{{domain}}/{NODE_ID}/{self.device_id}_{suffix}"

    def _device(self):
        return self._manager.get_device(self.device_id)

    # ---------------------------------------------------------- discovery -

    def publish_discovery(self) -> None:
        device = self._device()
        if device is None:
            log.warning("TP-Link %s: sin dispositivo conectado, no se publica nada por MQTT", self.device_id)
            return
        light = device.modules.get(Module.Light)
        if light is not None:
            self._publish_light(light)
        else:
            self._publish_switch()
        energy = device.modules.get(Module.Energy)
        if energy is not None:
            self._publish_power_sensor()

    def _publish_light(self, light) -> None:
        base = self._base("light").format(domain="light")
        payload = {
            "name": None,  # None = usa el nombre del propio `device` (entidad principal)
            "unique_id": f"{NODE_ID}_{self.device_id}_light",
            "state_topic": f"{base}/state",
            "command_topic": f"{base}/set",
            "payload_on": "ON", "payload_off": "OFF",
            "availability_topic": f"{base}/availability",
            "device": self._device_block,
        }
        if light.is_dimmable:
            payload.update(
                brightness_state_topic=f"{base}/brightness/state",
                brightness_command_topic=f"{base}/brightness/set",
                brightness_scale=100,
            )
            self._mqtt.subscribe(f"{base}/brightness/set", self._on_brightness)
        if light.is_variable_color_temp:
            lo, hi = light.valid_temperature_range
            payload.update(
                color_temp_state_topic=f"{base}/color_temp_kelvin/state",
                color_temp_command_topic=f"{base}/color_temp_kelvin/set",
                # HA moderno acepta min/max_kelvin directamente en el
                # discovery de MQTT light -- evita la conversion a
                # mireds por completo (y el bug que eso causo en Tuya).
                min_kelvin=int(lo), max_kelvin=int(hi),
            )
            self._mqtt.subscribe(f"{base}/color_temp_kelvin/set", self._on_color_temp)
        if light.is_color:
            payload.update(hs_state_topic=f"{base}/hs/state", hs_command_topic=f"{base}/hs/set")
            self._mqtt.subscribe(f"{base}/hs/set", self._on_hs)

        self._mqtt.subscribe(f"{base}/set", self._on_power)
        self._mqtt.publish(f"{base}/config", payload, retain=True)
        self._mqtt.publish(f"{base}/availability", "online", retain=True)

    def _publish_switch(self) -> None:
        base = self._base("switch").format(domain="switch")
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

    def _publish_power_sensor(self) -> None:
        base = self._base("power").format(domain="sensor")
        payload = {
            "name": "Potencia",
            "unique_id": f"{NODE_ID}_{self.device_id}_power",
            "state_topic": f"{base}/state",
            "availability_topic": f"{base}/availability",
            "device_class": "power",
            "unit_of_measurement": "W",
            "device": self._device_block,
        }
        self._mqtt.publish(f"{base}/config", payload, retain=True)
        self._mqtt.publish(f"{base}/availability", "online", retain=True)

    def remove_discovery(self) -> None:
        device = self._device()
        if device is None:
            return
        light = device.modules.get(Module.Light)
        domain, suffix = ("light", "light") if light is not None else ("switch", "switch")
        self._mqtt.publish(self._base(suffix).format(domain=domain) + "/config", "", retain=True)
        if device.modules.get(Module.Energy) is not None:
            self._mqtt.publish(self._base("power").format(domain="sensor") + "/config", "", retain=True)

    # -------------------------------------------------------------- estado

    def publish_state(self) -> None:
        device = self._device()
        if device is None:
            return
        light = device.modules.get(Module.Light)
        if light is not None:
            base = self._base("light").format(domain="light")
            self._mqtt.publish(f"{base}/state", "ON" if device.is_on else "OFF", retain=True)
            if light.is_dimmable:
                self._mqtt.publish(f"{base}/brightness/state", round(light.brightness), retain=True)
            if light.is_variable_color_temp:
                self._mqtt.publish(f"{base}/color_temp_kelvin/state", round(light.color_temp), retain=True)
            if light.is_color:
                h, s, _v = light.hsv
                self._mqtt.publish(f"{base}/hs/state", f"{h:.1f},{s:.1f}", retain=True)
        else:
            base = self._base("switch").format(domain="switch")
            self._mqtt.publish(f"{base}/state", "ON" if device.is_on else "OFF", retain=True)

        energy = device.modules.get(Module.Energy)
        if energy is not None and energy.current_consumption is not None:
            base = self._base("power").format(domain="sensor")
            self._mqtt.publish(f"{base}/state", round(energy.current_consumption, 1), retain=True)

    # ------------------------------------------------------------- comandos

    def _on_power(self, client, userdata, msg) -> None:
        try:
            if msg.payload.decode() == "ON":
                self._manager.turn_on(self.device_id)
            else:
                self._manager.turn_off(self.device_id)
        except Exception:
            log.exception("TP-Link %s: fallo aplicando encendido/apagado", self.device_id)

    def _on_brightness(self, client, userdata, msg) -> None:
        try:
            self._manager.turn_on(self.device_id, brightness_pct=max(1, min(100, round(float(msg.payload.decode())))))
        except Exception:
            log.exception("TP-Link %s: fallo aplicando brillo", self.device_id)

    def _on_color_temp(self, client, userdata, msg) -> None:
        try:
            self._manager.turn_on(self.device_id, color_temp_kelvin=round(float(msg.payload.decode())))
        except Exception:
            log.exception("TP-Link %s: fallo aplicando temperatura de color", self.device_id)

    def _on_hs(self, client, userdata, msg) -> None:
        try:
            h_str, s_str = msg.payload.decode().split(",")
            self._manager.turn_on(self.device_id, hs=(float(h_str), float(s_str)))
        except Exception:
            log.exception("TP-Link %s: fallo aplicando color", self.device_id)

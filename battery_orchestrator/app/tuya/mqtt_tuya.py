"""
Expone un dispositivo Tuya a Home Assistant via MQTT Discovery -- OPCIONAL
y por dispositivo (no todo lo que se ingesta tiene que publicarse: un
termostato consumido internamente por Climate no necesita aparecer aqui
tambien, ver tuya_plugin.py). Genera un dominio MQTT distinto por cada
`dps:` del perfil segun su `platform` (switch/sensor/number/binary_sensor
/select) -- no solo climates, la mayoria de dispositivos Tuya no son
termostatos. Las entidades `climates:` del perfil se publican como
`climate.*` nativo, mismo mecanismo que ya usa mqtt_climate.py para
Climate Orchestrator.

Un `MqttTuyaDevice` por dispositivo. El `ha_mqtt.HAMqttClient` es
compartido entre todos los dispositivos del plugin -- una sola conexion
al broker, no una por dispositivo (mismo criterio que Climate).
"""

from __future__ import annotations

import json
import logging
from functools import partial

log = logging.getLogger("tuya.mqtt")

DISCOVERY_PREFIX = "homeassistant"
NODE_ID = "home_orchestrator_tuya"

# platform (DPMapping) -> dominio MQTT Discovery. Nombres iguales a
# proposito -- no hace falta traducir, HA usa los mismos.
_DOMAIN_FOR_PLATFORM = {
    "switch": "switch",
    "sensor": "sensor",
    "number": "number",
    "binary_sensor": "binary_sensor",
    "select": "select",
}


class MqttTuyaDevice:
    def __init__(self, mqtt_client, manager, device_id: str, device_name: str) -> None:
        self._mqtt = mqtt_client
        self._manager = manager
        self.device_id = device_id
        self.device_name = device_name or device_id
        self._device_block = {
            "identifiers": [f"{NODE_ID}_{device_id}"],
            "name": self.device_name,
            "manufacturer": "Tuya",
            "model": self.device_name,
        }

    def _base(self, suffix: str) -> str:
        return f"{DISCOVERY_PREFIX}/{{domain}}/{NODE_ID}/{self.device_id}_{suffix}"

    # ---------------------------------------------------------- discovery -

    def publish_discovery(self) -> None:
        profile = self._manager.profile(self.device_id)
        if profile is None:
            log.warning("Tuya %s: sin perfil, no se publica nada por MQTT", self.device_id)
            return
        for dp in profile.dps:
            domain = _DOMAIN_FOR_PLATFORM.get(dp.platform)
            if domain:
                self._publish_dp(domain, dp)
        for i, cm in enumerate(profile.climates):
            self._publish_climate(i, cm)

    def _publish_dp(self, domain: str, dp) -> None:
        base = self._base(f"dp{dp.dp_id}").format(domain=domain)
        payload = {
            "name": dp.name,
            "unique_id": f"{NODE_ID}_{self.device_id}_dp{dp.dp_id}",
            "state_topic": f"{base}/state",
            "availability_topic": f"{base}/availability",
            "device": self._device_block,
        }
        if dp.icon:
            payload["icon"] = dp.icon
        if dp.device_class:
            payload["device_class"] = dp.device_class
        if dp.unit:
            payload["unit_of_measurement"] = dp.unit

        if domain == "switch":
            payload.update(command_topic=f"{base}/set", payload_on="ON", payload_off="OFF", state_on="ON", state_off="OFF")
            self._mqtt.subscribe(f"{base}/set", partial(self._on_bool_command, dp))
        elif domain == "binary_sensor":
            payload.update(payload_on="ON", payload_off="OFF")
        elif domain == "number":
            payload["command_topic"] = f"{base}/set"
            if dp.min_value is not None:
                payload["min"] = dp.min_value
            if dp.max_value is not None:
                payload["max"] = dp.max_value
            if dp.step is not None:
                payload["step"] = dp.step
            self._mqtt.subscribe(f"{base}/set", partial(self._on_number_command, dp))
        elif domain == "select":
            payload["command_topic"] = f"{base}/set"
            payload["options"] = list((dp.value_map or {}).values())
            self._mqtt.subscribe(f"{base}/set", partial(self._on_select_command, dp))

        self._mqtt.publish(f"{base}/config", payload, retain=True)
        self._mqtt.publish(f"{base}/availability", "online", retain=True)

    def _publish_climate(self, index: int, cm) -> None:
        base = self._base(f"climate{index}").format(domain="climate")
        modes = ["off"]
        if cm.mode_dp is not None and cm.mode_map:
            modes = ["off", *sorted(set(cm.mode_map.values()))]
        elif cm.switch_dp is not None:
            modes = ["off", "heat"]
        payload = {
            "name": cm.name,
            "unique_id": f"{NODE_ID}_{self.device_id}_climate{index}",
            "modes": modes,
            "mode_state_topic": f"{base}/mode/state",
            "mode_command_topic": f"{base}/mode/set",
            "current_temperature_topic": f"{base}/current_temp/state",
            "availability_topic": f"{base}/availability",
            "device": self._device_block,
        }
        if cm.target_temp_dp is not None:
            payload.update(
                temperature_state_topic=f"{base}/temp/state",
                temperature_command_topic=f"{base}/temp/set",
                min_temp=cm.target_temp_min, max_temp=cm.target_temp_max, temp_step=cm.target_temp_step,
            )
            self._mqtt.subscribe(f"{base}/temp/set", partial(self._on_climate_temp, index))
        if cm.icon:
            payload["icon"] = cm.icon
        self._mqtt.subscribe(f"{base}/mode/set", partial(self._on_climate_mode, index))
        self._mqtt.publish(f"{base}/config", payload, retain=True)
        self._mqtt.publish(f"{base}/availability", "online", retain=True)

    def remove_discovery(self) -> None:
        profile = self._manager.profile(self.device_id)
        if profile is None:
            return
        for dp in profile.dps:
            domain = _DOMAIN_FOR_PLATFORM.get(dp.platform)
            if domain:
                self._mqtt.publish(self._base(f"dp{dp.dp_id}").format(domain=domain) + "/config", "", retain=True)
        for i in range(len(profile.climates)):
            self._mqtt.publish(self._base(f"climate{i}").format(domain="climate") + "/config", "", retain=True)

    # -------------------------------------------------------------- estado

    def publish_state(self) -> None:
        """Llamar tras cualquier cambio conocido (on_any_change del
        device_manager) -- publica el valor actual de cada entidad
        expuesta. Simple y sin debounce: son solo unos pocos topics MQTT
        por dispositivo, no hace falta optimizar."""
        profile = self._manager.profile(self.device_id)
        if profile is None:
            return
        for dp in profile.dps:
            domain = _DOMAIN_FOR_PLATFORM.get(dp.platform)
            if not domain:
                continue
            value = self._manager.get_decoded(self.device_id, dp.dp_id)
            base = self._base(f"dp{dp.dp_id}").format(domain=domain)
            self._mqtt.publish(f"{base}/state", self._encode_state(domain, value), retain=True)
        for i, cm in enumerate(profile.climates):
            self._publish_climate_state(i, cm)

    def _publish_climate_state(self, index: int, cm) -> None:
        base = self._base(f"climate{index}").format(domain="climate")
        handle = self._manager.climate_handle(self.device_id, index)
        if handle is None:
            return
        self._mqtt.publish(f"{base}/mode/state", handle.hvac_mode, retain=True)
        if handle.current_temperature is not None:
            self._mqtt.publish(f"{base}/current_temp/state", handle.current_temperature, retain=True)
        if handle.target_temperature is not None:
            self._mqtt.publish(f"{base}/temp/state", handle.target_temperature, retain=True)

    @staticmethod
    def _encode_state(domain: str, value) -> str:
        if domain in ("switch", "binary_sensor"):
            return "ON" if value else "OFF"
        return "" if value is None else str(value)

    # ----------------------------------------------------------- comandos -
    # paho-mqtt normalmente ya protege su propio bucle de despacho contra
    # una excepcion de un callback, pero sin captura aqui un comando
    # llegado para un dispositivo momentaneamente desconectado (LAN caida,
    # reconectando...) se perderia sin dejar ni una linea de log --
    # exactamente el tipo de fallo silencioso contra el que ya se protege
    # el resto de este proyecto (ver coordinator.py original).

    def _on_bool_command(self, dp, client, userdata, msg) -> None:
        try:
            self._manager.set_dp(self.device_id, dp.dp_id, msg.payload.decode() == "ON")
        except Exception:
            log.exception("Tuya %s: fallo aplicando comando booleano DP %s", self.device_id, dp.dp_id)

    def _on_number_command(self, dp, client, userdata, msg) -> None:
        try:
            raw = dp.encode(float(msg.payload.decode()))
            self._manager.set_dp(self.device_id, dp.dp_id, raw)
        except Exception:
            log.exception("Tuya %s: fallo aplicando comando numerico DP %s", self.device_id, dp.dp_id)

    def _on_select_command(self, dp, client, userdata, msg) -> None:
        try:
            raw = dp.encode(msg.payload.decode())
            self._manager.set_dp(self.device_id, dp.dp_id, raw)
        except Exception:
            log.exception("Tuya %s: fallo aplicando comando de seleccion DP %s", self.device_id, dp.dp_id)

    def _on_climate_mode(self, index, client, userdata, msg) -> None:
        handle = self._manager.climate_handle(self.device_id, index)
        if not handle:
            return
        try:
            handle.set_hvac_mode(msg.payload.decode())
        except Exception:
            log.exception("Tuya %s: fallo aplicando modo climate %s", self.device_id, index)

    def _on_climate_temp(self, index, client, userdata, msg) -> None:
        handle = self._manager.climate_handle(self.device_id, index)
        if not handle:
            return
        try:
            handle.set_temperature(float(msg.payload.decode()))
        except Exception:
            log.exception("Tuya %s: fallo aplicando temperatura climate %s", self.device_id, index)

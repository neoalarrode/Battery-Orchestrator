"""
Publica una zona como entidad `climate.*` NATIVA de HA via MQTT Discovery
— validado en la Fase 2a contra produccion real (HomeKit/Matter incluido).
Traduce entre el estado interno de un `ZoneRunner` (ver zone_runner.py) y
los topics MQTT que HA espera, en ambas direcciones: publica estado
(HA <- nosotros) y recibe comandos (HA -> nosotros), delegando cada
comando al metodo correspondiente del runner (`set_hvac_mode`,
`set_temperature`, `set_preset_mode`, `set_fan_mode`, `set_humidity`).

Un `MqttClimateZone` por zona configurada. `ha_mqtt.HAMqttClient` (ver ese
modulo) es compartido entre todas las zonas del plugin -- una sola
conexion al broker, no una por zona.
"""

from __future__ import annotations

import json
import logging

DISCOVERY_PREFIX = "homeassistant"
NODE_ID = "home_orchestrator_climate"

log = logging.getLogger("climate.mqtt")


class MqttClimateZone:
    def __init__(self, mqtt_client, zone_id: str, zone: dict) -> None:
        self._mqtt = mqtt_client
        self.zone_id = zone_id
        self.zone_name = zone.get("name") or zone_id
        self._base = f"{DISCOVERY_PREFIX}/climate/{NODE_ID}/{zone_id}"
        self._runner = None  # asignado por ClimatePlugin tras crear el ZoneRunner (dependencia circular si no)

    def bind(self, runner) -> None:
        self._runner = runner

    # ---------------------------------------------------------- discovery -

    def publish_discovery(self, min_temp: float, max_temp: float) -> None:
        t = self._base
        # `modes`/`fan_modes`: los REALES de la zona (ver ZoneRunner.hvac_modes/
        # .fan_modes), no una lista fija -- bug real, confirmado en produccion:
        # antes de esto se anunciaba SIEMPRE el mismo set completo a HA (incluido
        # heat_cool/dry/fan_only) aunque el actuador real de la zona no los
        # soportase, y "fan_modes" quedaba fijo en ["auto"] aunque el
        # dispositivo real tuviese velocidades de verdad (p.ej. un AC Tuya con
        # strong/high/mid/low/mute) -- el selector de HA nunca las mostraba
        # porque nunca se anunciaban. Con fallback identico al de antes si el
        # runner todavia no ha calculado su capacidad real (zona recien creada).
        runner = self._runner
        modes = (runner.hvac_modes if runner and runner.hvac_modes else
                 ["off", "heat_cool", "heat", "cool", "dry", "fan_only"])
        fan_modes = (runner.fan_modes if runner and runner.fan_modes else ["auto"])
        payload = {
            "name": None,  # con "name": None + has_entity_name via device, HA usa el nombre del dispositivo tal cual
            "unique_id": f"{NODE_ID}_{self.zone_id}",
            "object_id": self.zone_id,
            "modes": modes,
            "mode_state_topic": f"{t}/mode/state",
            "mode_command_topic": f"{t}/mode/set",
            "temperature_state_topic": f"{t}/temp/state",
            "temperature_command_topic": f"{t}/temp/set",
            "temperature_low_state_topic": f"{t}/temp_low/state",
            "temperature_low_command_topic": f"{t}/temp_low/set",
            "temperature_high_state_topic": f"{t}/temp_high/state",
            "temperature_high_command_topic": f"{t}/temp_high/set",
            "current_temperature_topic": f"{t}/current_temp/state",
            "current_humidity_topic": f"{t}/current_humidity/state",
            "target_humidity_state_topic": f"{t}/target_humidity/state",
            "target_humidity_command_topic": f"{t}/target_humidity/set",
            "min_humidity": 20, "max_humidity": 80,
            "fan_modes": fan_modes,
            "fan_mode_state_topic": f"{t}/fan_mode/state",
            "fan_mode_command_topic": f"{t}/fan_mode/set",
            "preset_modes": ["Automático", "Manual"],
            "preset_mode_state_topic": f"{t}/preset_mode/state",
            "preset_mode_command_topic": f"{t}/preset_mode/set",
            "action_topic": f"{t}/action/state",
            "json_attributes_topic": f"{t}/attributes/state",
            "min_temp": min_temp,
            "max_temp": max_temp,
            "temp_step": 0.5,
            "availability_topic": f"{t}/availability",
            "device": {
                "identifiers": [f"home_orchestrator_climate_{self.zone_id}"],
                "name": self.zone_name,
                "manufacturer": "neoalarrode",
                "model": "Home Orchestrator — Climate",
            },
        }
        self._mqtt.publish(f"{t}/config", payload, retain=True)
        self._mqtt.subscribe(f"{t}/mode/set", self._on_mode)
        self._mqtt.subscribe(f"{t}/temp/set", self._on_temp)
        self._mqtt.subscribe(f"{t}/temp_low/set", self._on_temp_low)
        self._mqtt.subscribe(f"{t}/temp_high/set", self._on_temp_high)
        self._mqtt.subscribe(f"{t}/fan_mode/set", self._on_fan_mode)
        self._mqtt.subscribe(f"{t}/preset_mode/set", self._on_preset_mode)
        self._mqtt.subscribe(f"{t}/target_humidity/set", self._on_target_humidity)
        self._mqtt.publish(f"{t}/availability", "online", retain=True)

    def remove_discovery(self) -> None:
        """Retira la entidad de HA (payload de config vacio, ver
        convencion de MQTT Discovery) -- para cuando se borra una zona."""
        self._mqtt.publish(f"{self._base}/config", "", retain=True)

    # ------------------------------------------------------------ estado --

    def publish_state(self, runner) -> None:
        t = self._base
        self._mqtt.publish(f"{t}/availability", "online" if runner.available else "offline", retain=True)
        self._mqtt.publish(f"{t}/mode/state", runner.hvac_mode, retain=True)
        self._mqtt.publish(f"{t}/action/state", runner.hvac_action, retain=True)
        if runner.current_temperature is not None:
            self._mqtt.publish(f"{t}/current_temp/state", runner.current_temperature, retain=True)
        if runner.current_humidity is not None:
            self._mqtt.publish(f"{t}/current_humidity/state", runner.current_humidity, retain=True)
        if runner.target_temperature is not None:
            self._mqtt.publish(f"{t}/temp/state", runner.target_temperature, retain=True)
        if runner.target_temperature_low is not None:
            self._mqtt.publish(f"{t}/temp_low/state", runner.target_temperature_low, retain=True)
        if runner.target_temperature_high is not None:
            self._mqtt.publish(f"{t}/temp_high/state", runner.target_temperature_high, retain=True)
        self._mqtt.publish(f"{t}/target_humidity/state", runner.target_humidity, retain=True)
        self._mqtt.publish(f"{t}/preset_mode/state", runner._preset_mode, retain=True)
        if runner._fan_mode:
            self._mqtt.publish(f"{t}/fan_mode/state", runner._fan_mode, retain=True)
        self._mqtt.publish(f"{t}/attributes/state", runner.extra_attributes(), retain=True)

    # ----------------------------------------------------------- comandos -

    def _on_mode(self, client, userdata, msg) -> None:
        if self._runner:
            self._runner.set_hvac_mode(msg.payload.decode())

    def _on_temp(self, client, userdata, msg) -> None:
        if self._runner:
            self._runner.set_temperature(single=float(msg.payload.decode()))

    def _on_temp_low(self, client, userdata, msg) -> None:
        if self._runner:
            self._runner.set_temperature(low=float(msg.payload.decode()))

    def _on_temp_high(self, client, userdata, msg) -> None:
        if self._runner:
            self._runner.set_temperature(high=float(msg.payload.decode()))

    def _on_fan_mode(self, client, userdata, msg) -> None:
        if self._runner:
            self._runner.set_fan_mode(msg.payload.decode())

    def _on_preset_mode(self, client, userdata, msg) -> None:
        if self._runner:
            self._runner.set_preset_mode(msg.payload.decode())

    def _on_target_humidity(self, client, userdata, msg) -> None:
        if self._runner:
            self._runner.set_humidity(float(msg.payload.decode()))

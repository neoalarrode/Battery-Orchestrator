"""
Publica UNA luz "dummy" por zona via MQTT Discovery -- para controlar el
CONJUNTO de la zona desde HomeKit/Matter/Lovelace con un solo interruptor,
en vez de tener que exponer y tocar cada bombilla suelta. Mismo patron que
`climate/mqtt_climate.py` (una entidad nativa de HA por zona, traduciendo
estado en los dos sentidos), aqui aplicado a `light.*` en vez de
`climate.*`.

La luz dummy NO es una bombilla mas de la zona -- es una fachada:
  - Estado: ON si alguna de las luces OBJETIVO ahora mismo (las de la
    regla activa, o todas las de la zona si no hay presencia/ninguna
    regla coincide -- ver `ZoneRunner.group_state`) esta encendida;
    brillo/color = los que la curva solar de la zona tiene calculados
    ahora mismo (`current_values`, ver zone_runner.py).
  - Comandos: encender/apagar/ajustar la luz dummy reenvia el comando a
    esas MISMAS luces objetivo (`ZoneRunner.manual_command`) -- no
    inventa logica nueva, es la via manual mas.

Un `MqttLightingZone` por zona. `ha_mqtt.HAMqttClient` (ver ese modulo)
es compartido entre todas las zonas del plugin -- una sola conexion al
broker, no una por zona.
"""

from __future__ import annotations

import logging

DISCOVERY_PREFIX = "homeassistant"
NODE_ID = "home_orchestrator_lighting"

log = logging.getLogger("lighting.mqtt")


class MqttLightingZone:
    def __init__(self, mqtt_client, zone_id: str, zone: dict) -> None:
        self._mqtt = mqtt_client
        self.zone_id = zone_id
        self.zone_name = zone.get("name") or zone_id
        self._base = f"{DISCOVERY_PREFIX}/light/{NODE_ID}/{zone_id}"
        self._runner = None  # asignado por LightingPlugin tras crear el ZoneRunner (dependencia circular si no)

    def bind(self, runner) -> None:
        self._runner = runner

    # ---------------------------------------------------------- discovery -

    def publish_discovery(self, min_color_temp_kelvin: float, max_color_temp_kelvin: float) -> None:
        t = self._base
        payload = {
            "name": None,  # con "name": None + has_entity_name via device, HA usa el nombre del dispositivo (la zona) tal cual
            "unique_id": f"{NODE_ID}_{self.zone_id}",
            "object_id": f"{self.zone_id}_zona",
            "state_topic": f"{t}/state",
            "command_topic": f"{t}/set",
            "payload_on": "ON", "payload_off": "OFF",
            "brightness_state_topic": f"{t}/brightness/state",
            "brightness_command_topic": f"{t}/brightness/set",
            "brightness_scale": 100,
            "color_temp_state_topic": f"{t}/color_temp_kelvin/state",
            "color_temp_command_topic": f"{t}/color_temp_kelvin/set",
            # `color_temp_kelvin: true` -- nombre real del campo en el
            # schema MQTT de HA (`CONF_COLOR_TEMP_KELVIN`, ver
            # homeassistant/components/mqtt/const.py). SIN esto el
            # payload de los topics de arriba se sigue interpretando
            # como MIREDS por retrocompatibilidad, sin importar que
            # min/max_kelvin esten declarados -- bug real ya encontrado
            # y corregido una vez en tplink/mqtt_tplink.py, aqui se evita
            # desde el principio.
            "color_temp_kelvin": True,
            "min_kelvin": int(min_color_temp_kelvin), "max_kelvin": int(max_color_temp_kelvin),
            "availability_topic": f"{t}/availability",
            "device": {
                "identifiers": [f"home_orchestrator_lighting_{self.zone_id}"],
                "name": self.zone_name,
                "manufacturer": "neoalarrode",
                "model": "Home Orchestrator — Lighting",
            },
        }
        self._mqtt.publish(f"{t}/config", payload, retain=True)
        self._mqtt.subscribe(f"{t}/set", self._on_power)
        self._mqtt.subscribe(f"{t}/brightness/set", self._on_brightness)
        self._mqtt.subscribe(f"{t}/color_temp_kelvin/set", self._on_color_temp)
        self._mqtt.publish(f"{t}/availability", "online", retain=True)

    def remove_discovery(self) -> None:
        """Retira la entidad de HA (payload de config vacio, ver
        convencion de MQTT Discovery) -- para cuando se borra una zona."""
        self._mqtt.publish(f"{self._base}/config", "", retain=True)

    # ------------------------------------------------------------ estado --

    def publish_state(self, runner) -> None:
        t = self._base
        group = runner.group_state()
        self._mqtt.publish(f"{t}/state", "ON" if group["on"] else "OFF", retain=True)
        if group.get("brightness_pct") is not None:
            self._mqtt.publish(f"{t}/brightness/state", round(group["brightness_pct"]), retain=True)
        if group.get("color_temp_kelvin") is not None:
            self._mqtt.publish(f"{t}/color_temp_kelvin/state", round(group["color_temp_kelvin"]), retain=True)

    # ----------------------------------------------------------- comandos -

    def _on_power(self, client, userdata, msg) -> None:
        if self._runner:
            payload = msg.payload.decode()
            if payload == "ON":
                self._runner.manual_command(on=True)
            elif payload == "OFF":
                self._runner.manual_command(on=False)

    def _on_brightness(self, client, userdata, msg) -> None:
        if self._runner:
            self._runner.manual_command(on=True, brightness_pct=float(msg.payload.decode()))

    def _on_color_temp(self, client, userdata, msg) -> None:
        if self._runner:
            self._runner.manual_command(on=True, color_temp_kelvin=float(msg.payload.decode()))

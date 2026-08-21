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

import ha_mqtt

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
        # Ver `_dispatch` y ha_mqtt.MqttCommandWorker.
        self._after_command = None
        self._commands: ha_mqtt.MqttCommandWorker | None = None

    def bind(self, runner, after_command=None) -> None:
        """`after_command(runner)`: que hacer cuando un comando MQTT ya se ha
        aplicado -- persistir el estado de la zona y publicarlo de vuelta. Lo
        inyecta LightingPlugin para que sea EXACTAMENTE lo mismo que hace su
        endpoint HTTP de comando manual (ver el bug de latencia en
        `_dispatch`)."""
        self._runner = runner
        self._after_command = after_command
        if self._commands is None:
            self._commands = ha_mqtt.MqttCommandWorker(
                name=f"lighting-mqtt-cmd-{self.zone_id}", on_done=self._publish_after_command,
            )

    # ----------------------------------------------------------- despacho --

    def _publish_after_command(self) -> None:
        if self._after_command is not None:
            self._after_command(self._runner)
        else:
            self.publish_state(self._runner)

    def _dispatch(self, apply_command) -> None:
        """BUG REAL de latencia (sintoma: desde la interfaz del plugin los
        cambios son inmediatos, desde la entidad MQTT de HA van muy lentos).

        La causa concreta de ESTE modulo: los manejadores de comando no
        publicaban el estado de vuelta. El endpoint HTTP equivalente
        (`/api/zones/<id>/manual_command`) hace `manual_command` +
        `update_zone_state` + `publish_state`, asi que HA recibe el eco al
        instante. Por MQTT solo se llamaba a `manual_command`: la entidad de HA
        se quedaba con el valor viejo hasta que OTRO disparo publicara estado --
        el ciclo reactivo (que depende de que la bombilla real cambie, y lleva
        su propio debounce) o el reajuste periodico, hasta `reapply_minutes`
        (5 min por defecto) despues.

        La segunda causa (no ejecutar en el hilo de red de paho) es comun a
        todos los puentes MQTT del add-on y vive en ha_mqtt.MqttCommandWorker."""
        if self._runner is None or self._commands is None:
            return
        self._commands.submit(apply_command)

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
            # Color manual (HS), a peticion expresa del usuario -- ademas
            # de la curva automatica de blancos, se puede fijar un color
            # concreto a mano desde HomeKit/Lovelace (ver
            # `ZoneRunner.manual_command`). `color_mode_state_topic`
            # explicito (no inferido de cual topic llego mas tarde) --
            # mismo mecanismo real de HA que ya se uso para arreglar este
            # mismo problema en tplink/mqtt_tplink.py.
            "hs_state_topic": f"{t}/hs/state",
            "hs_command_topic": f"{t}/hs/set",
            "color_mode_state_topic": f"{t}/color_mode/state",
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
        self._mqtt.subscribe(f"{t}/hs/set", self._on_hs)
        self._mqtt.publish(f"{t}/availability", "online", retain=True)

    def remove_discovery(self) -> None:
        """Retira la entidad de HA (payload de config vacio, ver
        convencion de MQTT Discovery) -- para cuando se borra una zona."""
        self._mqtt.publish(f"{self._base}/config", "", retain=True)

    # ------------------------------------------------------------ estado --

    def publish_state(self, runner, states: dict[str, dict] | None = None) -> None:
        """`states`: lectura de HA ya hecha por quien llama. Se pasa hasta
        `group_state` para que no pida su PROPIO volcado completo -- con varias
        zonas eso suponia una lectura entera de HA extra por zona y por evento,
        deshaciendo la lectura compartida del ciclo reactivo."""
        t = self._base
        group = runner.group_state(states)
        self._mqtt.publish(f"{t}/state", "ON" if group["on"] else "OFF", retain=True)
        if group.get("brightness_pct") is not None:
            self._mqtt.publish(f"{t}/brightness/state", round(group["brightness_pct"]), retain=True)
        hs = group.get("hs_color")
        if hs is not None:
            self._mqtt.publish(f"{t}/hs/state", f"{hs[0]:.1f},{hs[1]:.1f}", retain=True)
            self._mqtt.publish(f"{t}/color_mode/state", "hs", retain=True)
        elif group.get("color_temp_kelvin") is not None:
            self._mqtt.publish(f"{t}/color_temp_kelvin/state", round(group["color_temp_kelvin"]), retain=True)
            self._mqtt.publish(f"{t}/color_mode/state", "color_temp", retain=True)

    # ----------------------------------------------------------- comandos -

    # El payload se valida AQUI (en el hilo de paho: es solo parsear, cuesta
    # nada) y solo se despacha si es correcto -- asi un payload basura no llega
    # nunca al worker, y sobre todo no revienta dentro del hilo de red de paho,
    # que antes es justo lo que pasaba con cada `float(msg.payload.decode())`
    # sin proteger.

    def _on_power(self, client, userdata, msg) -> None:
        payload = msg.payload.decode(errors="replace").strip()
        if payload == "ON":
            self._dispatch(lambda: self._runner.manual_command(on=True))
        elif payload == "OFF":
            self._dispatch(lambda: self._runner.manual_command(on=False))
        else:
            log.warning("Zona lighting %s: payload de encendido invalido: %r", self.zone_id, msg.payload)

    def _on_brightness(self, client, userdata, msg) -> None:
        value = self._as_float(msg, "brillo")
        if value is None:
            return

        # Un cambio de brillo por si solo NO debe tirar abajo un color
        # manual que ya estuviera activo (ver ZoneRunner._manual_hs) --
        # se reenvia junto con el, no en vez de el. `_manual_hs` se lee
        # DENTRO del worker (no al encolar) para usar el valor vigente en el
        # momento de aplicar el comando.
        def apply() -> None:
            self._runner.manual_command(
                on=True, brightness_pct=value, hs=self._runner._manual_hs,
            )

        self._dispatch(apply)

    def _on_color_temp(self, client, userdata, msg) -> None:
        value = self._as_float(msg, "temperatura de color")
        if value is None:
            return
        self._dispatch(lambda: self._runner.manual_command(on=True, color_temp_kelvin=value))

    def _on_hs(self, client, userdata, msg) -> None:
        try:
            h_str, s_str = msg.payload.decode(errors="replace").split(",")
            hs = (float(h_str), float(s_str))
        except ValueError:
            log.warning("Zona lighting %s: payload de color HS invalido: %r", self.zone_id, msg.payload)
            return
        self._dispatch(lambda: self._runner.manual_command(on=True, hs=hs))

    def _as_float(self, msg, what: str) -> float | None:
        try:
            return float(msg.payload.decode(errors="replace"))
        except ValueError:
            log.warning("Zona lighting %s: payload de %s invalido: %r", self.zone_id, what, msg.payload)
            return None

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

import ha_mqtt

from . import presets

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
        # Ver `_dispatch` y ha_mqtt.MqttCommandWorker.
        self._after_command = None
        self._commands: ha_mqtt.MqttCommandWorker | None = None

    def bind(self, runner, after_command=None) -> None:
        """`after_command(runner)`: que hacer cuando un comando MQTT ya se ha
        aplicado -- persistir el estado de la zona (el estado hacia HA lo
        publica ya `ZoneRunner._maybe_publish_state` desde `decide_and_act`)."""
        self._runner = runner
        self._after_command = after_command
        if self._commands is None:
            self._commands = ha_mqtt.MqttCommandWorker(
                name=f"climate-mqtt-cmd-{self.zone_id}", on_done=self._after_applied,
            )

    # ----------------------------------------------------------- despacho --

    def _after_applied(self) -> None:
        if self._after_command is not None:
            self._after_command(self._runner)

    def _dispatch(self, apply_command) -> None:
        """Los comandos NO se ejecutan en el hilo de RED de paho (ver
        ha_mqtt.MqttCommandWorker): cada `set_*` termina en `decide_and_act()`,
        que lee estados de HA y puede lanzar consultas de historico y llamadas
        de servicio en serie -- mientras eso corriera en el hilo de paho, el
        cliente no atendia el socket y todo el camino MQTT parecia lentisimo
        comparado con la interfaz del plugin (que corre en un hilo de Flask
        aparte).

        Ademas, el comando MQTT no persistia el estado de la zona: solo lo hacia
        el endpoint HTTP equivalente, asi que una consigna puesta desde HA se
        perdia al reiniciar el add-on."""
        if self._runner is None or self._commands is None:
            return
        self._commands.submit(apply_command)

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
        # BUG REAL: esto estaba fijo en ["Automático", "Manual"], asi que los
        # presets que declara el usuario (`presets_text` -> ZoneRunner.
        # _preset_modes, p.ej. "Confort"/"Ausente") NUNCA se anunciaban a HA:
        # no se podian seleccionar desde la entidad, y HA descarta un valor de
        # estado que no este en la lista anunciada -- asi que al publicar
        # `preset_mode` = "Confort" la entidad se quedaba incoherente. Es
        # exactamente el mismo bug que ya se corrigio justo arriba para "modes"
        # y "fan_modes"; los presets se quedaron sin arreglar.
        preset_modes = (
            runner._preset_modes if runner and getattr(runner, "_preset_modes", None)
            else [presets.PRESET_AUTO, presets.PRESET_MANUAL]
        )
        payload = {
            "name": None,  # con "name": None + has_entity_name via device, HA usa el nombre del dispositivo tal cual
            "unique_id": f"{NODE_ID}_{self.zone_id}",
            "object_id": self.zone_id,
            "modes": modes,
            "mode_state_topic": f"{t}/mode/state",
            "mode_command_topic": f"{t}/mode/set",
            # `_state_template`: SIN esto, un payload vacio (lo que
            # publish_state manda para "no aplica a este modo", ver ahi)
            # no limpia nada -- HA simplemente no consigue convertirlo a
            # numero y se queda con el ULTIMO valor valido en memoria para
            # siempre, ignorando el mensaje (comportamiento de fondo de
            # MQTT climate, no algo que se pueda arreglar solo publicando
            # distinto). Bug real, confirmado en produccion: una zona que
            # alguna vez tuvo modo unico y luego pasa a heat_cool se
            # quedaba con el termostato mostrando un solo mando de
            # temperatura para siempre, aunque el backend llevase rato en
            # heat_cool real. Con la plantilla, un payload vacio se
            # traduce a `None` explicito -- eso SI limpia el atributo de
            # verdad en HA.
            "temperature_state_topic": f"{t}/temp/state",
            "temperature_state_template": "{{ value if value not in (None, '') else None }}",
            "temperature_command_topic": f"{t}/temp/set",
            "temperature_low_state_topic": f"{t}/temp_low/state",
            "temperature_low_state_template": "{{ value if value not in (None, '') else None }}",
            "temperature_low_command_topic": f"{t}/temp_low/set",
            "temperature_high_state_topic": f"{t}/temp_high/state",
            "temperature_high_state_template": "{{ value if value not in (None, '') else None }}",
            "temperature_high_command_topic": f"{t}/temp_high/set",
            "current_temperature_topic": f"{t}/current_temp/state",
            "current_humidity_topic": f"{t}/current_humidity/state",
            "target_humidity_state_topic": f"{t}/target_humidity/state",
            "target_humidity_command_topic": f"{t}/target_humidity/set",
            "min_humidity": 20, "max_humidity": 80,
            "fan_modes": fan_modes,
            "fan_mode_state_topic": f"{t}/fan_mode/state",
            "fan_mode_command_topic": f"{t}/fan_mode/set",
            "preset_modes": preset_modes,
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
        # Bug real, confirmado en produccion: estos tres topics son
        # RETAIN=True (asi HA conoce el ultimo valor nada mas suscribirse,
        # sin esperar al proximo ciclo) -- pero antes esto solo publicaba
        # cuando el atributo correspondiente NO era None, sin limpiar
        # nunca el topic contrario. Resultado: una zona que alguna vez
        # estuvo en modo unico (heat/cool, con "temperature") y luego pasa
        # a heat_cool (con "temp_low"/"temp_high") se queda con el valor
        # RETENIDO antiguo de "temp/state" en el broker para siempre --
        # HA seguia mostrando un unico mando de temperatura en vez del par
        # calor/frio, aunque el backend ya llevase rato en heat_cool de
        # verdad. Publicar payload vacio con retain=True es la forma
        # estandar de MQTT de borrar un mensaje retenido -- se hace en el
        # "else" de cada uno de los tres, para que el topic que no aplica
        # al modo actual quede siempre limpio.
        if runner.target_temperature is not None:
            self._mqtt.publish(f"{t}/temp/state", runner.target_temperature, retain=True)
        else:
            self._mqtt.publish(f"{t}/temp/state", "", retain=True)
        if runner.target_temperature_low is not None:
            self._mqtt.publish(f"{t}/temp_low/state", runner.target_temperature_low, retain=True)
        else:
            self._mqtt.publish(f"{t}/temp_low/state", "", retain=True)
        if runner.target_temperature_high is not None:
            self._mqtt.publish(f"{t}/temp_high/state", runner.target_temperature_high, retain=True)
        else:
            self._mqtt.publish(f"{t}/temp_high/state", "", retain=True)
        self._mqtt.publish(f"{t}/target_humidity/state", runner.target_humidity, retain=True)
        self._mqtt.publish(f"{t}/preset_mode/state", runner._preset_mode, retain=True)
        if runner._fan_mode:
            self._mqtt.publish(f"{t}/fan_mode/state", runner._fan_mode, retain=True)
        self._mqtt.publish(f"{t}/attributes/state", runner.extra_attributes(), retain=True)

    # ----------------------------------------------------------- comandos -

    # El payload se valida AQUI (en el hilo de paho: solo parsear, cuesta nada) y
    # solo se despacha si es correcto -- antes cada `float(msg.payload.decode())`
    # sin proteger podia lanzar ValueError DENTRO del hilo de red de paho.

    def _as_float(self, msg, what: str) -> float | None:
        try:
            return float(msg.payload.decode(errors="replace"))
        except ValueError:
            log.warning("Zona climate %s: payload de %s invalido: %r", self.zone_id, what, msg.payload)
            return None

    def _on_mode(self, client, userdata, msg) -> None:
        mode = msg.payload.decode(errors="replace").strip()
        # Un payload suelto no debe poder meter la zona en un modo que no
        # soporta (p.ej. "heat_cool" en una zona solo-calor), que luego llevaria
        # a `_execute` por la rama equivocada.
        valid = getattr(self._runner, "hvac_modes", None) if self._runner else None
        if valid and mode not in valid:
            log.warning(
                "Zona climate %s: modo '%s' no soportado (validos: %s) -- ignorado",
                self.zone_id, mode, ", ".join(valid),
            )
            return
        self._dispatch(lambda: self._runner.set_hvac_mode(mode))

    def _on_temp(self, client, userdata, msg) -> None:
        value = self._as_float(msg, "consigna")
        if value is not None:
            self._dispatch(lambda: self._runner.set_temperature(single=value))

    def _on_temp_low(self, client, userdata, msg) -> None:
        value = self._as_float(msg, "consigna baja")
        if value is not None:
            self._dispatch(lambda: self._runner.set_temperature(low=value))

    def _on_temp_high(self, client, userdata, msg) -> None:
        value = self._as_float(msg, "consigna alta")
        if value is not None:
            self._dispatch(lambda: self._runner.set_temperature(high=value))

    def _on_fan_mode(self, client, userdata, msg) -> None:
        mode = msg.payload.decode(errors="replace").strip()
        self._dispatch(lambda: self._runner.set_fan_mode(mode))

    def _on_preset_mode(self, client, userdata, msg) -> None:
        preset = msg.payload.decode(errors="replace").strip()
        self._dispatch(lambda: self._runner.set_preset_mode(preset))

    def _on_target_humidity(self, client, userdata, msg) -> None:
        value = self._as_float(msg, "humedad objetivo")
        if value is not None:
            self._dispatch(lambda: self._runner.set_humidity(value))

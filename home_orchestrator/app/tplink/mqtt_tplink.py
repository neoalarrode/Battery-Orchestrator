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

import ha_mqtt
from kasa import Module

log = logging.getLogger("tplink.mqtt")

DISCOVERY_PREFIX = "homeassistant"
NODE_ID = "home_orchestrator_tplink"


def _color_temp_active(light) -> bool:
    """Replica exacta de `_determine_color_mode` real de
    `homeassistant/components/tplink/light.py`: en un dispositivo que
    soporta color_temp Y hs a la vez, `light.color_temp` por si solo NO
    basta -- se queda con el ULTIMO valor conocido incluso estando en
    modo color de verdad (confirmado contra hardware real: 6500 con el
    dispositivo mostrando azul). `has_feature("color_temp")` es la
    comprobacion real de que el modo color-temp esta ACTIVO ahora mismo,
    no solo que el dispositivo lo soporte en general. Con una version de
    `python-kasa` demasiado vieja para tener `has_feature` (no en 0.7.x,
    si en 0.10.x -- ver Dockerfile para la version realmente instalada),
    se cae a mirar solo `color_temp`, peor que nada pero mejor que
    reventar."""
    if not (light.is_variable_color_temp and light.is_color):
        return light.is_variable_color_temp
    has_feature = getattr(light, "has_feature", None)
    if has_feature is not None:
        return bool(has_feature("color_temp")) and bool(light.color_temp)
    return bool(light.color_temp)


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
        # Comandos fuera del hilo de red de paho + publicacion del estado en
        # cuanto se aplican (ver ha_mqtt.MqttCommandWorker).
        self._commands = ha_mqtt.MqttCommandWorker(
            name=f"tplink-mqtt-cmd-{device_id}", on_done=self.publish_state,
        )

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
                # BUG REAL, confirmado en produccion: `min_kelvin`/
                # `max_kelvin` NO bastan por si solos -- sin el flag
                # `color_temp_kelvin: true` (el nombre real en el schema
                # MQTT de HA, ver `homeassistant/components/mqtt/const.py:
                # CONF_COLOR_TEMP_KELVIN`), el payload de
                # `color_temp_state_topic`/`command_topic` se sigue
                # interpretando como MIREDS por defecto (retrocompatible)
                # sin importar que min/max_kelvin esten declarados --
                # visto tal cual: se publico 6500 (Kelvin) y HA lo
                # convirtio de vuelta como si fueran 6500 MIREDS,
                # mostrando "153K" en la entidad real.
                color_temp_kelvin=True,
                min_kelvin=int(lo), max_kelvin=int(hi),
            )
            self._mqtt.subscribe(f"{base}/color_temp_kelvin/set", self._on_color_temp)
        if light.is_color:
            payload.update(hs_state_topic=f"{base}/hs/state", hs_command_topic=f"{base}/hs/set")
            self._mqtt.subscribe(f"{base}/hs/set", self._on_hs)
        if light.is_variable_color_temp and light.is_color:
            # Mecanismo REAL y explicito de HA para decir que modo esta
            # activo (`color_mode_state_topic`, ver
            # `mqtt/light/schema_basic.py:_color_mode_received`) --
            # mas robusto que dejar que HA lo infiera de cual topic de
            # estado llego mas tarde (la ambiguedad que causo el bug real
            # ya corregido en `publish_state`, ver `_color_temp_active`).
            payload.update(color_mode_state_topic=f"{base}/color_mode/state")

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
            # BUG REAL, confirmado en produccion comparando contra la
            # entidad NATIVA de HA para el mismo dispositivo fisico: esto
            # publicaba `color_temp_kelvin/state` Y `hs/state` a la vez,
            # sin mirar cual de los dos modos esta REALMENTE activo ahora
            # mismo -- HA (esquema MQTT "legacy") infiere el color_mode
            # de cual topic recibio valor mas tarde, asi que publicar
            # siempre los dos en el MISMO orden dejaba la entidad
            # encallada en "hs" con un color antiguo aunque el
            # dispositivo real llevase un rato en color_temp (visto tal
            # cual: `light.barra_1` nativa de HA marcaba color_temp/6500K
            # mientras esta entidad seguia en hs/(210,80) del comando
            # anterior). Mismo bug de fondo que ya se corrigio en
            # mqtt_tuya.py, aqui con su propia causa (ver
            # `_color_temp_active`, replica exacta de la logica real de
            # `_determine_color_mode` del `light.py` de Home Assistant).
            ct_active = light.is_variable_color_temp and _color_temp_active(light)
            if ct_active:
                self._mqtt.publish(f"{base}/color_temp_kelvin/state", round(light.color_temp), retain=True)
            elif light.is_color:
                h, s, _v = light.hsv
                self._mqtt.publish(f"{base}/hs/state", f"{h:.1f},{s:.1f}", retain=True)
            if light.is_variable_color_temp and light.is_color:
                # Reporte EXPLICITO del modo activo (ver `_publish_light`)
                # -- HA ya no tiene que adivinar de cual topic de estado
                # llego mas tarde.
                self._mqtt.publish(f"{base}/color_mode/state", "color_temp" if ct_active else "hs", retain=True)
        else:
            base = self._base("switch").format(domain="switch")
            self._mqtt.publish(f"{base}/state", "ON" if device.is_on else "OFF", retain=True)

        energy = device.modules.get(Module.Energy)
        if energy is not None and energy.current_consumption is not None:
            base = self._base("power").format(domain="sensor")
            self._mqtt.publish(f"{base}/state", round(energy.current_consumption, 1), retain=True)

    # ------------------------------------------------------------- comandos

    # El payload se valida en el hilo de paho (solo parsear) y la orden al
    # dispositivo se ejecuta en el worker -- ver ha_mqtt.MqttCommandWorker.
    # Aqui era especialmente grave: `manager.turn_on` espera con
    # `future.result(timeout=10)`, asi que un Tapo que no respondiera bloqueaba
    # hasta 10s el hilo de red de paho, que es UNO para todo el add-on -- todas
    # las entidades MQTT del add-on se quedaban lentas por un solo dispositivo.

    def _on_power(self, client, userdata, msg) -> None:
        on = msg.payload.decode(errors="replace").strip() == "ON"
        self._commands.submit(
            lambda: self._manager.turn_on(self.device_id) if on
            else self._manager.turn_off(self.device_id)
        )

    def _on_brightness(self, client, userdata, msg) -> None:
        value = self._as_float(msg, "brillo")
        if value is None:
            return
        pct = max(1, min(100, round(value)))
        self._commands.submit(lambda: self._manager.turn_on(self.device_id, brightness_pct=pct))

    def _on_color_temp(self, client, userdata, msg) -> None:
        value = self._as_float(msg, "temperatura de color")
        if value is None:
            return
        kelvin = round(value)
        self._commands.submit(lambda: self._manager.turn_on(self.device_id, color_temp_kelvin=kelvin))

    def _on_hs(self, client, userdata, msg) -> None:
        try:
            h_str, s_str = msg.payload.decode(errors="replace").split(",")
            hs = (float(h_str), float(s_str))
        except ValueError:
            log.warning("TP-Link %s: payload de color invalido: %r", self.device_id, msg.payload)
            return
        self._commands.submit(lambda: self._manager.turn_on(self.device_id, hs=hs))

    def _as_float(self, msg, what: str) -> float | None:
        try:
            return float(msg.payload.decode(errors="replace"))
        except ValueError:
            log.warning("TP-Link %s: payload de %s invalido: %r", self.device_id, what, msg.payload)
            return None

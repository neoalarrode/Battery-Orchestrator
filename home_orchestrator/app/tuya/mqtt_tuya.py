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

from tuya.profile import (
    LIGHT_MAX_MIREDS,
    LIGHT_MIN_MIREDS,
    decode_color_hs,
    encode_color_hs,
    light_dp_to_mireds,
    mireds_to_light_dp,
)

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
        for i, lt in enumerate(profile.lights):
            self._publish_light(i, lt)

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

    def _publish_light(self, index: int, lt) -> None:
        """Publica el bloque `lights:` del perfil como una entidad
        `light.*` de verdad (encendido+brillo+color en una tarjeta), no
        como DPs sueltos -- antes esto no existia en absoluto: una
        bombilla nunca tenia una tarjeta de luz real, solo los sensores/
        switches sueltos de `dps:` (los DPs de brillo/color/modo de una
        bombilla NUNCA aparecen en `dps:` de todos modos -- el perfil los
        consume aqui, en `lights:`, precisamente para que no se dupliquen
        como dos entidades para lo mismo)."""
        base = self._base(f"light{index}").format(domain="light")
        payload = {
            "name": lt.name,
            "unique_id": f"{NODE_ID}_{self.device_id}_light{index}",
            "state_topic": f"{base}/state",
            "command_topic": f"{base}/set",
            "payload_on": "ON", "payload_off": "OFF",
            "availability_topic": f"{base}/availability",
            "device": self._device_block,
        }
        if lt.brightness_dp is not None:
            payload.update(
                brightness_state_topic=f"{base}/brightness/state",
                brightness_command_topic=f"{base}/brightness/set",
                brightness_scale=int(lt.brightness_max),
            )
            self._mqtt.subscribe(f"{base}/brightness/set", partial(self._on_light_brightness, index))
        if lt.color_temp_dp is not None:
            # BUG FIXED HERE: esto declaraba `min_mireds=1,
            # max_mireds=color_temp_max` -- tratando la escala CRUDA del
            # DP (0..color_temp_max, especifica del fabricante, NUNCA
            # mireds de verdad) como si YA fuera mireds. HA lo traducia a
            # limites sin sentido fisico (min/max_color_temp_kelvin =
            # 1.000.000K/1000K, confirmado en produccion) y el color real
            # aplicado no correspondia al pedido. Los limites correctos
            # (y la conversion de ida y vuelta, ver `_on_light_color_temp`/
            # `_publish_light_state`) viven en tuya/profile.py, en un solo
            # sitio para no divergir del control directo (TuyaLightHandle).
            payload.update(
                color_temp_state_topic=f"{base}/color_temp/state",
                color_temp_command_topic=f"{base}/color_temp/set",
                min_mireds=LIGHT_MIN_MIREDS, max_mireds=LIGHT_MAX_MIREDS,
            )
            self._mqtt.subscribe(f"{base}/color_temp/set", partial(self._on_light_color_temp, index))
        if lt.color_dp is not None:
            payload.update(hs_state_topic=f"{base}/hs/state", hs_command_topic=f"{base}/hs/set")
            self._mqtt.subscribe(f"{base}/hs/set", partial(self._on_light_hs, index))
        if lt.icon:
            payload["icon"] = lt.icon

        self._mqtt.subscribe(f"{base}/set", partial(self._on_light_power, index))
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
        for i in range(len(profile.lights)):
            self._mqtt.publish(self._base(f"light{i}").format(domain="light") + "/config", "", retain=True)

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
        for i, lt in enumerate(profile.lights):
            self._publish_light_state(i, lt)

    def _publish_light_state(self, index: int, lt) -> None:
        base = self._base(f"light{index}").format(domain="light")
        switch_val = self._manager.get_decoded(self.device_id, lt.switch_dp)
        self._mqtt.publish(f"{base}/state", "ON" if switch_val else "OFF", retain=True)
        if lt.brightness_dp is not None:
            b = self._manager.get_decoded(self.device_id, lt.brightness_dp)
            if b is not None:
                self._mqtt.publish(f"{base}/brightness/state", int(b), retain=True)

        # BUG FIXED HERE: se publicaban `color_temp/state` Y `hs/state` a
        # la vez, sin mirar el `work_mode_dp` real del dispositivo -- el
        # esquema MQTT "legacy" de HA infiere el `color_mode` activo de
        # CUAL topic recibio valor mas recientemente, no del dispositivo,
        # asi que publicar los dos siempre dejaba a HA adivinando (visto
        # en produccion: color_mode="hs" con un color que no era el
        # pedido, mientras el DP real `work_mode` seguia en "white").
        # Ahora solo se publica el topic del modo REALMENTE activo.
        work_mode = self._manager.get_decoded(self.device_id, lt.work_mode_dp) if lt.work_mode_dp is not None else None
        publish_color_temp = lt.color_temp_dp is not None and (work_mode is None or work_mode == lt.work_mode_white)
        publish_hs = lt.color_dp is not None and (work_mode is None or work_mode == lt.work_mode_colour)

        if publish_color_temp:
            ct = self._manager.get_decoded(self.device_id, lt.color_temp_dp)
            if ct is not None:
                # BUG FIXED HERE: se publicaba el valor CRUDO del DP
                # (escala 0..color_temp_max del fabricante) como si ya
                # fueran mireds -- ver el aviso en `_publish_light`.
                self._mqtt.publish(f"{base}/color_temp/state", light_dp_to_mireds(ct, lt), retain=True)
        if publish_hs:
            raw = self._manager.get_decoded(self.device_id, lt.color_dp)
            hs = decode_color_hs(lt, raw)
            if hs is not None:
                self._mqtt.publish(f"{base}/hs/state", f"{hs[0]:.1f},{hs[1]:.1f}", retain=True)

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

    def _light(self, index: int):
        profile = self._manager.profile(self.device_id)
        return profile.lights[index] if profile and index < len(profile.lights) else None

    def _on_light_power(self, index, client, userdata, msg) -> None:
        lt = self._light(index)
        if lt is None:
            return
        try:
            self._manager.set_dp(self.device_id, lt.switch_dp, msg.payload.decode() == "ON")
        except Exception:
            log.exception("Tuya %s: fallo aplicando encendido de luz %s", self.device_id, index)

    def _on_light_brightness(self, index, client, userdata, msg) -> None:
        lt = self._light(index)
        if lt is None or lt.brightness_dp is None:
            return
        try:
            val = max(int(lt.brightness_min), min(int(lt.brightness_max), round(float(msg.payload.decode()))))
            if lt.work_mode_dp is not None:
                # Poner en modo "blanco" ANTES del brillo -- si el
                # dispositivo esta en modo color, cambiar el brillo del
                # lado blanco no tendria efecto visible hasta que se
                # cambia de modo de todos modos.
                self._manager.set_dp(self.device_id, lt.work_mode_dp, lt.work_mode_white)
            self._manager.set_dp(self.device_id, lt.brightness_dp, val)
        except Exception:
            log.exception("Tuya %s: fallo aplicando brillo de luz %s", self.device_id, index)

    def _on_light_color_temp(self, index, client, userdata, msg) -> None:
        lt = self._light(index)
        if lt is None or lt.color_temp_dp is None:
            return
        try:
            # BUG FIXED HERE: esto clampaba el valor RECIBIDO (mireds de
            # verdad, ver `_publish_light`) directo al rango del DP
            # (0..color_temp_max, escala del fabricante) sin convertir --
            # ver el aviso de arriba y `tuya/profile.py:mireds_to_light_dp`.
            mireds = max(LIGHT_MIN_MIREDS, min(LIGHT_MAX_MIREDS, round(float(msg.payload.decode()))))
            val = mireds_to_light_dp(mireds, lt)
            if lt.work_mode_dp is not None:
                self._manager.set_dp(self.device_id, lt.work_mode_dp, lt.work_mode_white)
            self._manager.set_dp(self.device_id, lt.color_temp_dp, val)
        except Exception:
            log.exception("Tuya %s: fallo aplicando temperatura de color de luz %s", self.device_id, index)

    def _on_light_hs(self, index, client, userdata, msg) -> None:
        lt = self._light(index)
        if lt is None or lt.color_dp is None:
            return
        try:
            h_str, s_str = msg.payload.decode().split(",")
            raw = encode_color_hs(lt, float(h_str), float(s_str))
            if lt.work_mode_dp is not None:
                self._manager.set_dp(self.device_id, lt.work_mode_dp, lt.work_mode_colour)
            self._manager.set_dp(self.device_id, lt.color_dp, raw)
        except Exception:
            log.exception("Tuya %s: fallo aplicando color de luz %s", self.device_id, index)

"""
Expone un dispositivo Govee a Home Assistant via MQTT Discovery --
OPCIONAL y por dispositivo, mismo criterio que `tplink/mqtt_tplink.py`/
`tuya/mqtt_tuya.py` (`expose_mqtt`, ver govee_store.py). A diferencia de
Tuya/TP-Link, aqui NO hace falta ninguna deteccion de capacidades por
dispositivo -- el protocolo LAN de Govee es siempre "bombilla RGB +
temperatura de color", nunca un simple rele/switch (a diferencia de
Shelly), asi que la entidad publicada es siempre `light.*` con brillo +
color_temp_kelvin + hs.
"""

from __future__ import annotations

import logging

import ha_mqtt

log = logging.getLogger("govee.mqtt")

DISCOVERY_PREFIX = "homeassistant"
NODE_ID = "home_orchestrator_govee"


class MqttGoveeDevice:
    def __init__(self, mqtt_client, manager, device_id: str, device_name: str) -> None:
        self._mqtt = mqtt_client
        self._manager = manager
        self.device_id = device_id
        self.device_name = device_name or device_id
        self._device_block = {
            "identifiers": [f"{NODE_ID}_{device_id}"],
            "name": self.device_name,
            "manufacturer": "Govee",
            "model": self.device_name,
        }
        # Comandos fuera del hilo de red de paho + publicacion del estado en
        # cuanto se aplican (ver ha_mqtt.MqttCommandWorker).
        self._commands = ha_mqtt.MqttCommandWorker(
            name=f"govee-mqtt-cmd-{device_id}", on_done=self.publish_state,
        )

    def _base(self) -> str:
        return f"{DISCOVERY_PREFIX}/light/{NODE_ID}/{self.device_id}"

    # ---------------------------------------------------------- discovery -

    def publish_discovery(self) -> None:
        base = self._base()
        payload = {
            "name": None,  # None = usa el nombre del propio `device`
            "unique_id": f"{NODE_ID}_{self.device_id}_light",
            "state_topic": f"{base}/state",
            "command_topic": f"{base}/set",
            "payload_on": "ON", "payload_off": "OFF",
            "availability_topic": f"{base}/availability",
            "brightness_state_topic": f"{base}/brightness/state",
            "brightness_command_topic": f"{base}/brightness/set",
            "brightness_scale": 100,
            "color_temp_state_topic": f"{base}/color_temp_kelvin/state",
            "color_temp_command_topic": f"{base}/color_temp_kelvin/set",
            # Mismo flag real que ya se documento en mqtt_tplink.py --
            # sin `color_temp_kelvin: true` HA interpreta el topic como
            # MIREDS por defecto (retrocompatible) aunque min/max_kelvin
            # esten declarados.
            "color_temp_kelvin": True,
            "min_kelvin": 2000, "max_kelvin": 9000,
            "hs_state_topic": f"{base}/hs/state",
            "hs_command_topic": f"{base}/hs/set",
            # Mismo mecanismo EXPLICITO que TP-Link (`color_mode_state_
            # topic`) para no dejar que HA adivine el modo activo de cual
            # topic llego mas tarde -- ver `_color_temp_active` de
            # mqtt_tplink.py, mismo bug de fondo evitado desde el
            # principio aqui.
            "color_mode_state_topic": f"{base}/color_mode/state",
            "device": self._device_block,
        }
        self._mqtt.subscribe(f"{base}/set", self._on_power)
        self._mqtt.subscribe(f"{base}/brightness/set", self._on_brightness)
        self._mqtt.subscribe(f"{base}/color_temp_kelvin/set", self._on_color_temp)
        self._mqtt.subscribe(f"{base}/hs/set", self._on_hs)
        self._mqtt.publish(f"{base}/config", payload, retain=True)
        self._mqtt.publish(f"{base}/availability", "online", retain=True)

    def remove_discovery(self) -> None:
        self._mqtt.publish(f"{self._base()}/config", "", retain=True)

    # -------------------------------------------------------------- estado

    def publish_state(self) -> None:
        handle = self._manager.light_handle(self.device_id)
        if handle is None:
            return
        base = self._base()
        # BUG REAL: la disponibilidad se publicaba "online" retenida UNA vez, al
        # anunciar la entidad, y no se revocaba nunca. Con la bombilla
        # desenchufada HA seguia mostrandola disponible con su ultimo estado
        # retenido (leyendo "encendida" para siempre), aunque `handle.available`
        # ya fuera False. Climate ya lo hacia bien; los cuatro puentes no.
        self._mqtt.publish(
            f"{base}/availability", "online" if handle.available else "offline", retain=True,
        )
        self._mqtt.publish(f"{base}/state", "ON" if handle.is_on else "OFF", retain=True)
        if handle.brightness_pct is not None:
            self._mqtt.publish(f"{base}/brightness/state", round(handle.brightness_pct), retain=True)
        kelvin = handle.color_temp_kelvin
        if kelvin is not None:
            self._mqtt.publish(f"{base}/color_temp_kelvin/state", kelvin, retain=True)
            self._mqtt.publish(f"{base}/color_mode/state", "color_temp", retain=True)
        else:
            # Govee no reporta HS de vuelta (solo RGB crudo, sin HSV) --
            # a diferencia de TP-Link/Tuya no hay `hs/state` real que
            # publicar aqui, solo el modo activo (para que la UI de HA no
            # se quede mostrando "temperatura de color" cuando en
            # realidad esta en modo color).
            self._mqtt.publish(f"{base}/color_mode/state", "hs", retain=True)

    # ------------------------------------------------------------- comandos

    # El payload se valida en el hilo de paho (solo parsear, cuesta nada) y la
    # orden al dispositivo se ejecuta en el worker -- ver
    # ha_mqtt.MqttCommandWorker: antes la E/S al dispositivo corria en el hilo de
    # RED de paho, que es UNO para todo el add-on, asi que una bombilla que no
    # respondia dejaba lento a todo lo demas. Y no se publicaba el estado tras
    # el comando, asi que HA se quedaba con el valor viejo hasta el siguiente
    # sondeo (de ahi que por MQTT pareciera lentisimo y por la interfaz no).

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
            log.warning("Govee %s: payload de color invalido: %r", self.device_id, msg.payload)
            return
        self._commands.submit(lambda: self._manager.turn_on(self.device_id, hs=hs))

    def _as_float(self, msg, what: str) -> float | None:
        try:
            return float(msg.payload.decode(errors="replace"))
        except ValueError:
            log.warning("Govee %s: payload de %s invalido: %r", self.device_id, what, msg.payload)
            return None

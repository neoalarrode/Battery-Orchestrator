"""
Plugin de Shelly para el nucleo Home Orchestrator -- puro puente de
ingesta, mismo papel que GoveePlugin/TuyaPlugin/TplinkPlugin pero
hablando el HTTP local de Shelly directamente (ver
shelly/device_manager.py: Gen1 por querystring, Gen2/3 por RPC JSON,
API oficial y documentada del fabricante, "igual que el original" en el
SITIO donde habla -- LAN, nunca la nube -- aunque el transporte por
dentro sea HTTP en vez del CoIoT/WebSocket que usa `aioshelly`).

Dos formas de usar un dispositivo dado de alta, no excluyentes (mismo
criterio que Govee/Tuya/TP-Link):
  - Consumo INTERNO: Lighting puede pedir un `ShellyLightHandle` (ver
    light_handle()) y controlar el dispositivo EN EL MISMO PROCESO, sin
    pasar por Home Assistant -- incluye reles puros (on/off), no solo
    bombillas de verdad.
  - Exposicion opcional a HA por MQTT Discovery (`expose_mqtt` por
    dispositivo, ver shelly/mqtt_shelly.py).

Descubrimiento: barrido activo de la subred propia bajo demanda
(`/api/discover`, ver ShellyDeviceManager.discover) -- Shelly no tiene un
broadcast LAN tan simple como Govee/Tuya, ver el docstring del propio
device_manager para el porque.
"""

from __future__ import annotations

import logging

import flask

import ha_mqtt
import shelly_store
from shelly.device_manager import ShellyDeviceManager
from shelly.mqtt_shelly import MqttShellyDevice
from plugin_base import Plugin

log = logging.getLogger("shelly_plugin")


class ShellyPlugin(Plugin):
    slug = "shelly"
    name = "Shelly Orchestrator"
    version = "0.1.2"

    def __init__(self) -> None:
        self._manager = ShellyDeviceManager(on_any_change=self._on_device_change)
        self._mqtt = ha_mqtt.HAMqttClient(client_id="home_orchestrator_shelly")
        self._mqtt_devices: dict[str, MqttShellyDevice] = {}
        self._app = flask.Flask("shelly_plugin", template_folder="shelly_templates")
        self._register_routes()

    # --------------------------------------------------------------- Flask -

    def flask_app(self):
        return self._app

    def _register_routes(self) -> None:
        app = self._app

        @app.get("/")
        def _index():
            return flask.render_template("index.html")

        @app.get("/api/devices")
        def _list_devices():
            devices = shelly_store.load_devices()
            out = []
            for d in devices:
                item = {"id": d["id"], "config": d["config"]}
                info = self._manager.get_device(d["id"])
                handle = self._manager.light_handle(d["id"])
                if info is not None and handle is not None:
                    item["live"] = {
                        "connected": handle.available,
                        "is_on": handle.is_on,
                        "brightness_pct": handle.brightness_pct,
                        "capability": info["capability"],
                        "gen": info["gen"],
                        "model": info.get("model"),
                    }
                out.append(item)
            return flask.jsonify(out)

        @app.post("/api/devices")
        def _add_device():
            payload = flask.request.get_json(force=True) or {}
            device = shelly_store.add_device(payload)
            self._start_device(device)
            return flask.jsonify(device), 201

        @app.put("/api/devices/<device_id>")
        def _update_device(device_id):
            payload = flask.request.get_json(force=True) or {}
            device = shelly_store.update_device(device_id, payload)
            if not device:
                return flask.jsonify({"error": "dispositivo no encontrado"}), 404
            self._stop_device(device_id)
            self._start_device(device)
            return flask.jsonify(device)

        @app.delete("/api/devices/<device_id>")
        def _delete_device(device_id):
            self._stop_device(device_id)
            ok = shelly_store.delete_device(device_id)
            return flask.jsonify({"deleted": ok})

        @app.get("/api/status")
        def _status():
            return flask.jsonify({
                "version": self.version,
                "devices": len(shelly_store.load_devices()),
                "mqtt_connected": self._mqtt.connected,
            })

        # ------------------------------------------------ descubrimiento -
        # Barrido ACTIVO bajo demanda (ver docstring del modulo) -- nunca
        # añade nada por su cuenta, solo enseña lo que ha respondido.

        @app.post("/api/discover")
        def _discover():
            added_hosts = {d["config"]["host"] for d in shelly_store.load_devices()}
            try:
                found = self._manager.discover()
            except Exception:
                log.exception("Fallo escaneando la LAN en busca de dispositivos Shelly")
                return flask.jsonify({"error": "fallo escaneando la LAN"}), 502
            out = [
                {
                    "host": info["host"],
                    "model": info.get("model"),
                    "gen": info.get("gen"),
                    "already_added": info["host"] in added_hosts,
                }
                for info in found
            ]
            return flask.jsonify(out)

    # ------------------------------------------------------------- arranque

    def start_background_threads(self) -> None:
        self._manager.start()
        self._mqtt.connect()
        devices = shelly_store.load_devices()
        for device in devices:
            self._start_device(device)
        log.info("Plugin Shelly arrancado con %d dispositivo(s)", len(devices))

    def _start_device(self, device: dict) -> None:
        cfg = device["config"]
        if not cfg.get("host"):
            log.warning("Dispositivo Shelly '%s' sin host -- no se conecta", cfg.get("name") or device["id"])
            return
        try:
            self._manager.add_device(device["id"], cfg["host"])
        except Exception:
            log.exception("Fallo conectando al dispositivo Shelly '%s'", cfg.get("name") or cfg["host"])
            return

        if cfg.get("expose_mqtt"):
            mqtt_dev = MqttShellyDevice(self._mqtt, self._manager, device["id"], cfg.get("name") or cfg["host"])
            mqtt_dev.publish_discovery()
            # Mismo bug ya corregido en Tuya/TP-Link/Govee que se evita
            # desde el principio aqui: sin este publish_state() inicial,
            # la entidad recien expuesta se queda en "unknown" hasta el
            # primer sondeo periodico.
            mqtt_dev.publish_state()
            self._mqtt_devices[device["id"]] = mqtt_dev

    def _stop_device(self, device_id: str) -> None:
        mqtt_dev = self._mqtt_devices.pop(device_id, None)
        if mqtt_dev:
            mqtt_dev.remove_discovery()
        self._manager.remove_device(device_id)

    def _on_device_change(self, device_id: str) -> None:
        mqtt_dev = self._mqtt_devices.get(device_id)
        if mqtt_dev:
            try:
                mqtt_dev.publish_state()
            except Exception:
                log.exception("Fallo publicando estado MQTT de %s", device_id)

    # --------------------------------------------------- API para otros plugins

    def light_handle(self, device_id: str, light_index: int = 0):
        """Punto de entrada para consumo INTERNO desde Lighting -- control
        DIRECTO de un dispositivo Shelly, sin pasar por HA/MQTT.
        `light_index` se ignora (mismo contrato que
        `TuyaPlugin.light_handle`/`GoveePlugin.light_handle`)."""
        return self._manager.light_handle(device_id)

    def list_light_actuators(self) -> list[dict]:
        """Un `{"ref", "name", "brand"}` por cada dispositivo dado de alta
        -- lo que LightingPlugin agrega para que el selector de la
        interfaz de Lighting los ofrezca sin que el usuario tenga que
        escribir `shelly:<id>` a mano."""
        out = []
        for device in shelly_store.load_devices():
            device_id = device["id"]
            cfg = device["config"]
            out.append({
                "ref": f"shelly:{device_id}",
                "name": cfg.get("name") or cfg.get("host") or device_id,
                "brand": "Shelly",
            })
        return out

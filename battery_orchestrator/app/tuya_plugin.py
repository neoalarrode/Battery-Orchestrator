"""
Plugin de Tuya para el nucleo Home Orchestrator -- puro puente de
ingesta, sin logica de decision propia (esa vive en quien consuma los
dispositivos, no aqui). Detecta/da de alta dispositivos que hablan el
protocolo Tuya-por-LAN (via el perfil declarativo YAML, ver tuya/profile.py
-- misma filosofia "sin caja negra" que Climate Orchestrator) y los deja
disponibles de dos formas, no excluyentes:

  - Consumo INTERNO: otro plugin (hoy Climate) puede pedir un
    `TuyaClimateHandle` (ver climate_handle()) y controlar el dispositivo
    EN EL MISMO PROCESO, sin pasar por Home Assistant.
  - Exposicion opcional a HA por MQTT Discovery (`expose_mqtt` por
    dispositivo, ver tuya/mqtt_tuya.py) -- para que el usuario u otro
    sistema lo controle desde HA como una entidad nativa mas.
"""

from __future__ import annotations

import logging
import threading

import flask

import ha_mqtt
from plugin_base import Plugin
from tuya import tuya_store
from tuya.device_manager import TuyaDeviceManager
from tuya.mqtt_tuya import MqttTuyaDevice

log = logging.getLogger("tuya_plugin")


class TuyaPlugin(Plugin):
    slug = "tuya"
    name = "Tuya Orchestrator"
    version = "0.1.0"

    def __init__(self) -> None:
        self._manager = TuyaDeviceManager(on_any_change=self._on_device_change)
        self._mqtt = ha_mqtt.HAMqttClient(client_id="home_orchestrator_tuya")
        self._mqtt_devices: dict[str, MqttTuyaDevice] = {}
        self._app = flask.Flask("tuya_plugin", template_folder="tuya_templates")
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
            devices = tuya_store.load_devices()
            out = []
            for d in devices:
                device_id = d["config"]["device_id"]
                item = {"id": d["id"], "config": d["config"]}
                if device_id in self._manager._devices:  # noqa: SLF001 -- lectura de solo estado, no mutacion
                    item["live"] = {
                        "connected": self._manager.connected(device_id),
                        "dps": dict(self._manager._state.get(device_id, {})),  # noqa: SLF001
                    }
                out.append(item)
            return flask.jsonify(out)

        @app.post("/api/devices")
        def _add_device():
            payload = flask.request.get_json(force=True) or {}
            device = tuya_store.add_device(payload)
            self._start_device(device)
            return flask.jsonify(device), 201

        @app.put("/api/devices/<device_id>")
        def _update_device(device_id):
            payload = flask.request.get_json(force=True) or {}
            device = tuya_store.update_device(device_id, payload)
            if not device:
                return flask.jsonify({"error": "dispositivo no encontrado"}), 404
            self._stop_device(device["config"]["device_id"])
            self._start_device(device)
            return flask.jsonify(device)

        @app.delete("/api/devices/<device_id>")
        def _delete_device(device_id):
            devices = tuya_store.load_devices()
            target = next((d for d in devices if d["id"] == device_id), None)
            if target:
                self._stop_device(target["config"]["device_id"])
            ok = tuya_store.delete_device(device_id)
            return flask.jsonify({"deleted": ok})

        @app.get("/api/status")
        def _status():
            return flask.jsonify({
                "version": self.version,
                "devices": len(self._manager._devices),  # noqa: SLF001
                "mqtt_connected": self._mqtt.connected,
            })

    # ------------------------------------------------------------- arranque

    def start_background_threads(self) -> None:
        self._manager.start()
        self._mqtt.connect()
        devices = tuya_store.load_devices()
        for device in devices:
            self._start_device(device)
        log.info("Plugin Tuya arrancado con %d dispositivo(s)", len(devices))

    def _start_device(self, device: dict) -> None:
        cfg = device["config"]
        if not cfg.get("device_id") or not cfg.get("address") or not cfg.get("local_key"):
            log.warning("Dispositivo Tuya '%s' sin datos suficientes -- no se conecta", cfg.get("name") or device["id"])
            return
        try:
            self._manager.add_device(
                cfg["device_id"], cfg["address"], cfg["local_key"],
                cfg.get("protocol_version", "3.3"), cfg.get("profile_yaml", ""),
            )
        except Exception:
            log.exception("Fallo conectando al dispositivo Tuya '%s'", cfg.get("name") or cfg["device_id"])
            return

        if cfg.get("expose_mqtt"):
            mqtt_dev = MqttTuyaDevice(self._mqtt, self._manager, cfg["device_id"], cfg.get("name") or cfg["device_id"])
            mqtt_dev.publish_discovery()
            self._mqtt_devices[cfg["device_id"]] = mqtt_dev

        threading.Thread(
            target=self._background_reconnect_watch, name=f"tuya-{cfg['device_id']}", daemon=True,
        ).start()

    def _background_reconnect_watch(self) -> None:
        """Marcador de hilo por dispositivo -- el reintento de conexion
        real ya lo hace TuyaDeviceManager._reconnect_loop (una sola vez,
        en su propio event loop, para todos los dispositivos). Este hilo
        no hace nada por ahora; existe para que un futuro watchdog por
        dispositivo tenga donde vivir sin reestructurar nada."""
        return

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

    def climate_handle(self, device_id: str, climate_index: int = 0):
        """Punto de entrada para consumo INTERNO desde otro plugin (hoy
        Climate) -- ver tuya/device_manager.py:TuyaClimateHandle."""
        return self._manager.climate_handle(device_id, climate_index)

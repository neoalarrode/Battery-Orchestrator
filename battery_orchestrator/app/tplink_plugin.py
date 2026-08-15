"""
Plugin de TP-Link para el nucleo Home Orchestrator -- puro puente de
ingesta, mismo papel que TuyaPlugin pero usando `python-kasa` (la MISMA
libreria que el componente `tplink` real de Home Assistant) en vez de
una reimplementacion propia del protocolo: no hace falta un perfil
declarativo por dispositivo, `python-kasa` ya dice en tiempo real que
modulos tiene cada uno (`device.modules`).

A diferencia de Tuya (deteccion pasiva por broadcast continuo, algunos
dispositivos solo emiten de cuando en cuando), el descubrimiento de
`python-kasa` es un escaneo ACTIVO bajo demanda (`Discover.discover()`
manda un broadcast y recoge respuestas durante unos segundos) -- no hace
falta un listener de fondo persistente como `PersistentDiscovery` de
Tuya, ver `/api/discover`.

Dos formas de usar un dispositivo dado de alta, no excluyentes (mismo
criterio que Tuya):
  - Consumo INTERNO: otro plugin (hoy Lighting) puede pedir un
    `TplinkLightHandle` (ver light_handle()) y controlar la bombilla EN
    EL MISMO PROCESO, sin pasar por Home Assistant.
  - Exposicion opcional a HA por MQTT Discovery (`expose_mqtt` por
    dispositivo, ver tplink/mqtt_tplink.py).
"""

from __future__ import annotations

import logging
import threading

import flask
from kasa import Credentials, Discover

import ha_mqtt
import tplink_store
from plugin_base import Plugin
from tplink.device_manager import TplinkDeviceManager
from tplink.mqtt_tplink import MqttTplinkDevice

log = logging.getLogger("tplink_plugin")


class TplinkPlugin(Plugin):
    slug = "tplink"
    name = "TP-Link Orchestrator"
    version = "0.1.7"

    def __init__(self) -> None:
        self._manager = TplinkDeviceManager(on_any_change=self._on_device_change)
        self._mqtt = ha_mqtt.HAMqttClient(client_id="home_orchestrator_tplink")
        self._mqtt_devices: dict[str, MqttTplinkDevice] = {}
        self._app = flask.Flask("tplink_plugin", template_folder="tplink_templates")
        self._register_routes()

    def _credentials(self) -> Credentials | None:
        account = tplink_store.load_account()
        if not account["username"] or not account["password"]:
            return None
        return Credentials(account["username"], account["password"])

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
            devices = tplink_store.load_devices()
            out = []
            for d in devices:
                host = d["config"]["host"]
                item = {"id": d["id"], "config": d["config"]}
                device = self._manager.get_device(d["id"])
                if device is not None:
                    item["live"] = {
                        "connected": self._manager.connected(d["id"]),
                        "is_on": device.is_on,
                        "model": device.model,
                        "device_type": str(device.device_type),
                    }
                out.append(item)
            return flask.jsonify(out)

        @app.post("/api/devices")
        def _add_device():
            payload = flask.request.get_json(force=True) or {}
            device = tplink_store.add_device(payload)
            self._start_device(device)
            return flask.jsonify(device), 201

        @app.put("/api/devices/<device_id>")
        def _update_device(device_id):
            payload = flask.request.get_json(force=True) or {}
            device = tplink_store.update_device(device_id, payload)
            if not device:
                return flask.jsonify({"error": "dispositivo no encontrado"}), 404
            self._stop_device(device_id)
            self._start_device(device)
            return flask.jsonify(device)

        @app.delete("/api/devices/<device_id>")
        def _delete_device(device_id):
            self._stop_device(device_id)
            ok = tplink_store.delete_device(device_id)
            return flask.jsonify({"deleted": ok})

        @app.get("/api/status")
        def _status():
            return flask.jsonify({
                "version": self.version,
                "devices": len(tplink_store.load_devices()),
                "mqtt_connected": self._mqtt.connected,
            })

        @app.get("/api/account")
        def _get_account():
            account = tplink_store.load_account()
            return flask.jsonify({"username": account["username"], "linked": bool(account["username"] and account["password"])})

        @app.post("/api/account")
        def _save_account():
            payload = flask.request.get_json(force=True) or {}
            tplink_store.save_account({"username": payload.get("username", ""), "password": payload.get("password", "")})
            return flask.jsonify({"linked": True})

        # ------------------------------------------------ descubrimiento -
        # Escaneo ACTIVO bajo demanda (ver docstring del modulo) -- nunca
        # añade nada por su cuenta, solo enseña lo que ha respondido.

        @app.post("/api/discover")
        def _discover():
            added_hosts = {d["config"]["host"] for d in tplink_store.load_devices()}
            try:
                found = self._manager.discover(self._credentials())
            except Exception as exc:
                log.exception("Fallo escaneando la LAN en busca de dispositivos TP-Link")
                return flask.jsonify({"error": str(exc)}), 502
            out = [
                {
                    "host": host,
                    "alias": info.get("alias"),
                    "model": info.get("model"),
                    "device_type": info.get("device_type"),
                    "already_added": host in added_hosts,
                    "needs_auth": info.get("needs_auth", False),
                }
                for host, info in found.items()
            ]
            return flask.jsonify(out)

    # ------------------------------------------------------------- arranque

    def start_background_threads(self) -> None:
        self._manager.start()
        self._mqtt.connect()
        devices = tplink_store.load_devices()
        for device in devices:
            self._start_device(device)
        log.info("Plugin TP-Link arrancado con %d dispositivo(s)", len(devices))

    def _start_device(self, device: dict) -> None:
        cfg = device["config"]
        if not cfg.get("host"):
            log.warning("Dispositivo TP-Link '%s' sin host -- no se conecta", cfg.get("name") or device["id"])
            return
        try:
            self._manager.add_device(device["id"], cfg["host"], self._credentials())
        except Exception:
            log.exception("Fallo conectando al dispositivo TP-Link '%s'", cfg.get("name") or cfg["host"])
            return

        if cfg.get("expose_mqtt"):
            mqtt_dev = MqttTplinkDevice(self._mqtt, self._manager, device["id"], cfg.get("name") or cfg["host"])
            mqtt_dev.publish_discovery()
            # Mismo bug de Tuya que ya se corrigio ahi -- se evita desde
            # el principio: sin este publish_state() inicial, la entidad
            # recien expuesta se quedaria en "unknown" hasta el primer
            # sondeo periodico (hasta POLL_INTERVAL_SECONDS de retraso).
            mqtt_dev.publish_state()
            self._mqtt_devices[device["id"]] = mqtt_dev

        threading.Thread(
            target=self._background_reconnect_watch, name=f"tplink-{device['id']}", daemon=True,
        ).start()

    def _background_reconnect_watch(self) -> None:
        """Marcador de hilo por dispositivo, mismo criterio que
        tuya_plugin.py -- el sondeo/reconexion real ya lo hace
        TplinkDeviceManager._poll_loop, para todos los dispositivos a la
        vez."""
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

    def light_handle(self, device_id: str, light_index: int = 0):
        """Punto de entrada para consumo INTERNO desde otro plugin (hoy
        Lighting) -- control DIRECTO de una bombilla TP-Link, sin pasar
        por HA/MQTT. `light_index` se ignora (un dispositivo TP-Link
        expone como mucho una luz por `device_id` -- a diferencia de
        Tuya no hay un `lights:` con varias entradas por dispositivo),
        se acepta solo para cumplir el mismo contrato que
        `TuyaPlugin.light_handle`."""
        return self._manager.light_handle(device_id)

    def list_light_actuators(self) -> list[dict]:
        """Un `{"ref", "name", "brand"}` por cada dispositivo dado de
        alta que resulte ser una luz -- lo que LightingPlugin agrega
        para que el selector de la interfaz de Lighting los ofrezca sin
        que el usuario tenga que escribir `tplink:<id>` a mano."""
        out = []
        for device in tplink_store.load_devices():
            device_id = device["id"]
            if self._manager.light_handle(device_id) is None:
                continue
            cfg = device["config"]
            out.append({
                "ref": f"tplink:{device_id}",
                "name": cfg.get("name") or cfg.get("host") or device_id,
                "brand": "TP-Link",
            })
        return out

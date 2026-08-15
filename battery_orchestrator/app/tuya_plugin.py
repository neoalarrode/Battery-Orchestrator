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
from tuya import auto_profile, tuya_store
from tuya.device_manager import TuyaDeviceManager
from tuya.mqtt_tuya import MqttTuyaDevice
from tuya.profile import profile_to_yaml
from tuya.tuya_cloud import TuyaCloudApi, TuyaCloudAuthError, TuyaCloudApiError

log = logging.getLogger("tuya_plugin")


class TuyaPlugin(Plugin):
    slug = "tuya"
    name = "Tuya Orchestrator"
    version = "0.3.3"

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

        # ------------------------------------------------ descubrimiento -
        # El usuario SIEMPRE decide: descubrir solo enseña lo que se ha
        # visto en la LAN (y, si hay cuenta vinculada, lo que la nube dice
        # que es tuyo) -- nada se añade ni se conecta hasta que se pulsa
        # "Añadir" explicitamente para ESE dispositivo en concreto.

        @app.get("/api/discovered")
        def _list_discovered():
            added_ids = {d["config"]["device_id"] for d in tuya_store.load_devices()}
            seen = self._manager.get_discovered_devices()
            out = [
                {
                    "device_id": d.device_id,
                    "ip": d.ip,
                    "product_key": d.product_key,
                    "version": d.version,
                    "already_added": d.device_id in added_ids,
                }
                for d in seen
            ]
            return flask.jsonify(out)

        @app.get("/api/account")
        def _get_account():
            account = tuya_store.load_account()
            return flask.jsonify({
                "region": account["region"], "access_id": account["access_id"],
                "uid": account["uid"], "linked": bool(account["access_id"] and account["access_secret"]),
            })  # access_secret NUNCA se devuelve

        @app.post("/api/account")
        def _save_account():
            payload = flask.request.get_json(force=True) or {}
            try:
                api = TuyaCloudApi(
                    payload.get("region", "eu"), payload.get("access_id", ""), payload.get("access_secret", ""),
                )
                api.validate()
            except (TuyaCloudAuthError, TuyaCloudApiError) as exc:
                return flask.jsonify({"error": f"credenciales rechazadas por Tuya: {exc}"}), 400
            except Exception as exc:
                return flask.jsonify({"error": str(exc)}), 502
            tuya_store.save_account({
                "region": payload.get("region", "eu"), "access_id": payload.get("access_id", ""),
                "access_secret": payload.get("access_secret", ""), "uid": payload.get("uid", ""),
            })
            return flask.jsonify({"linked": True})

        @app.post("/api/discovered/<device_id>/resolve")
        def _resolve_discovered(device_id):
            """NO da de alta nada -- resuelve el local_key + esquema DP
            real contra la cuenta Tuya vinculada y genera un perfil de
            PARTIDA (ver auto_profile.py), para que la interfaz lo
            precargue en el formulario de siempre y el usuario lo revise/
            edite antes de guardar. Guardar de verdad sigue pasando
            siempre por POST /api/devices, como cualquier alta manual --
            aqui no se conecta ni se persiste nada todavia."""
            seen = {d.device_id: d for d in self._manager.get_discovered_devices()}
            discovered = seen.get(device_id)
            if discovered is None:
                return flask.jsonify({"error": "dispositivo no visto en la LAN (¿sigue encendido?)"}), 404

            account = tuya_store.load_account()
            if not account["access_id"] or not account["access_secret"] or not account["uid"]:
                return flask.jsonify({"error": "vincula primero una cuenta Tuya para poder traer el local_key"}), 400

            try:
                api = TuyaCloudApi(account["region"], account["access_id"], account["access_secret"])
                cloud_devices = {d["device_id"]: d for d in api.get_user_devices(account["uid"])}
                cloud_device = cloud_devices.get(device_id)
                if cloud_device is None or not cloud_device.get("local_key"):
                    return flask.jsonify({"error": "la cuenta vinculada no conoce este dispositivo (¿esta vinculado en Tuya IoT Platform?)"}), 404
                schema = api.get_device_schema(device_id)
            except (TuyaCloudAuthError, TuyaCloudApiError) as exc:
                return flask.jsonify({"error": f"fallo consultando la nube de Tuya: {exc}"}), 502

            profile, warnings = auto_profile.build_profile_from_schema(
                cloud_device["name"], cloud_device.get("category"), cloud_device.get("product_id"), schema,
            )
            return flask.jsonify({
                "name": cloud_device["name"],
                "device_id": device_id,
                "address": discovered.ip,
                "local_key": cloud_device["local_key"],
                "protocol_version": discovered.version or "3.3",
                "profile_yaml": profile_to_yaml(profile),
                "warnings": warnings,
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
            # Bug real, confirmado en produccion: sin esto, una entidad
            # recien expuesta se quedaba en "unknown"/todo-None hasta el
            # PRIMER cambio espontaneo del dispositivo (on_any_change) --
            # que para un dispositivo que no cambia solo (una bombilla
            # apagada y quieta, p.ej.) podia no llegar nunca. Los DPs ya
            # estan en cache tras _manager.add_device() (ver
            # _connect_and_prime), asi que hay estado real que publicar
            # desde el primer instante, no hace falta esperar a nada.
            mqtt_dev.publish_state()
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

    def get_actuator_history(self, device_id: str, climate_index: int, days: int) -> list[dict]:
        """Historico local para que thermal_model.py aprenda la inercia
        termica de un termostato consumido internamente -- ver
        device_manager.py:get_actuator_history."""
        return self._manager.get_actuator_history(device_id, climate_index, days)

    def list_climate_actuators(self) -> list[dict]:
        """Un `{"ref", "name", "brand"}` por cada bloque `climates:` de
        cada dispositivo dado de alta -- lo que ClimatePlugin agrega en
        `/api/actuators` para que el selector de la interfaz de Climate
        los ofrezca sin que el usuario tenga que escribir `tuya:<id>` a
        mano. `ref` es exactamente lo que `climate_entities` de una zona
        espera (ver ZoneRunner.bridges)."""
        out = []
        for device in tuya_store.load_devices():
            cfg = device["config"]
            device_id = cfg.get("device_id")
            if not device_id:
                continue
            profile = self._manager.profile(device_id)
            if profile is None:
                continue
            for i, cm in enumerate(profile.climates):
                ref = f"tuya:{device_id}" if len(profile.climates) == 1 else f"tuya:{device_id}:{i}"
                out.append({
                    "ref": ref,
                    "name": f"{cfg.get('name') or device_id} — {cm.name}",
                    "brand": "Tuya",
                })
        return out

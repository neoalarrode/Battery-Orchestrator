"""
Plugin de Climate para el nucleo Home Orchestrator -- segundo plugin real,
tras Battery. Expone termostatos NATIVOS de HA (`climate.*`, con
HomeKit/Matter incluido) via MQTT Discovery (ver climate/mqtt_climate.py),
con toda la logica de decision portada de Climate Orchestrator
(climate/zone_runner.py) y conectada por WebSocket (nunca REST, ver
ha_websocket.py) tanto para eventos reactivos como para consultas
puntuales (historial, servicios...).

Una sola conexion WebSocket y una sola conexion MQTT para TODAS las zonas
de este plugin -- no una por zona.
"""

from __future__ import annotations

import logging
import threading
import time

import flask

import ha_mqtt
import ha_websocket
from climate import zone_store
from climate.mqtt_climate import MqttClimateZone
from climate.zone_runner import ZoneRunner, zone_stagger_seconds
from plugin_base import Plugin

log = logging.getLogger("climate_plugin")

DEFAULT_FORECAST_REFRESH_MINUTES = 10
REACTIVE_MIN_INTERVAL_SECONDS = 5


class ClimatePlugin(Plugin):
    slug = "climate"
    name = "Climate Orchestrator"
    version = "0.4.3"

    def __init__(self) -> None:
        self._runners: dict[str, ZoneRunner] = {}
        self._mqtt_zones: dict[str, MqttClimateZone] = {}
        self._ws = ha_websocket.HAWebSocketClient(self._on_entity_change)
        self._mqtt = ha_mqtt.HAMqttClient(client_id="home_orchestrator_climate")
        self._reactive = ha_websocket.ReactiveTrigger(self._run_reactive_cycle)
        self._app = flask.Flask("climate_plugin", template_folder="climate_templates")
        # Registro GENERICO de "proveedores de actuadores" -- prefijo ->
        # plugin. Cualquier plugin que ofrezca dispositivos climate.*
        # (Tuya hoy, otra marca mañana) se registra solo aqui (ver
        # register_actuator_provider(), llamado desde core_app.py tras
        # cargar los plugins) sin que este fichero necesite conocer nada
        # especifico de esa marca. Vacio si no hay ninguno cargado: las
        # zonas que referencien un actuador de otro plugin simplemente no
        # lo controlan (ver ZoneRunner._resolve_bridge_handle), no revientan.
        self._actuator_providers: dict[str, object] = {}
        self._register_routes()

    def register_actuator_provider(self, prefix: str, provider) -> None:
        """`provider` debe exponer `.climate_handle(device_id, index) ->
        handle | None` (control) y, si quiere aparecer en el selector de
        la interfaz, `.list_climate_actuators() -> list[dict]` (ver
        api_list_actuators mas abajo)."""
        self._actuator_providers[prefix] = provider
        log.info("Registrado proveedor de actuadores '%s'", prefix)

    def is_bridge_ref(self, ref: str) -> bool:
        return ":" in ref and ref.split(":", 1)[0] in self._actuator_providers

    def resolve_bridge_handle(self, ref: str):
        """`ref` = '<prefijo>:<device_id>[:<indice>]' -> handle, o None
        si el prefijo no tiene proveedor registrado ahora mismo."""
        prefix, rest = ref.split(":", 1)
        provider = self._actuator_providers.get(prefix)
        if provider is None:
            return None
        parts = rest.split(":", 1)
        device_id = parts[0]
        index = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        return provider.climate_handle(device_id, index)

    def get_history(self, ref: str, days: int) -> list[dict]:
        """Historico de un actuador de otro plugin, para que
        thermal_model.py aprenda su inercia termica igual que ya aprende
        de un `climate.*`/switch de HA -- `[]` si el proveedor no expone
        historico (capacidad opcional, ver TuyaPlugin.get_actuator_history)
        o si el prefijo no tiene proveedor registrado ahora mismo."""
        prefix, rest = ref.split(":", 1)
        provider = self._actuator_providers.get(prefix)
        getter = getattr(provider, "get_actuator_history", None)
        if getter is None:
            return []
        parts = rest.split(":", 1)
        device_id = parts[0]
        index = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        try:
            return getter(device_id, index, days)
        except Exception:
            log.exception("Fallo obteniendo historico de '%s'", ref)
            return []

    # --------------------------------------------------------------- Flask -

    def flask_app(self):
        return self._app

    def _register_routes(self) -> None:
        app = self._app

        @app.get("/")
        def _index():
            return flask.render_template("index.html")

        @app.get("/api/actuators")
        def _list_actuators():
            """Actuadores climate.* que OTROS plugins ofrecen (Tuya hoy,
            otra marca mañana) -- agregado de todos los proveedores
            registrados, filtrando los que ya esten en uso en CUALQUIER
            zona (no interesa mostrar dos veces algo que ya esta añadido,
            ni permitir asignarlo por error a una segunda zona a la vez)."""
            used_refs = {
                ref
                for z in zone_store.load_zones()
                for ref in (z["config"].get("climate_entities") or [])
                if ":" in ref
            }
            out = []
            for prefix, provider in self._actuator_providers.items():
                lister = getattr(provider, "list_climate_actuators", None)
                if lister is None:
                    continue
                try:
                    for actuator in lister():
                        actuator = dict(actuator)
                        actuator["already_used"] = actuator.get("ref") in used_refs
                        out.append(actuator)
                except Exception:
                    log.exception("Fallo listando actuadores del proveedor '%s'", prefix)
            return flask.jsonify(out)

        @app.get("/api/zones")
        def _list_zones():
            zones = zone_store.load_zones()
            out = []
            for z in zones:
                runner = self._runners.get(z["id"])
                item = {"id": z["id"], "config": z["config"]}
                if runner:
                    item["live"] = {
                        "available": runner.available,
                        "hvac_mode": runner.hvac_mode,
                        "hvac_action": runner.hvac_action,
                        "current_temperature": runner.current_temperature,
                        "target_temperature": runner.target_temperature,
                        "target_temperature_low": runner.target_temperature_low,
                        "target_temperature_high": runner.target_temperature_high,
                        "reason": runner.reason,
                        # Para que la tarjeta de termostato del dashboard
                        # sepa que opciones ofrecer -- no todas las zonas
                        # soportan los mismos modos (p.ej. una sin
                        # actuador de frio no tiene "cool"/"heat_cool").
                        "hvac_modes": runner.hvac_modes,
                        "preset_mode": runner._preset_mode,
                        "preset_modes": runner._preset_modes,
                    }
                out.append(item)
            return flask.jsonify(out)

        @app.post("/api/zones")
        def _add_zone():
            payload = flask.request.get_json(force=True) or {}
            zone = zone_store.add_zone(payload)
            self._start_zone(zone)
            return flask.jsonify(zone), 201

        @app.put("/api/zones/<zone_id>")
        def _update_zone(zone_id):
            payload = flask.request.get_json(force=True) or {}
            zone = zone_store.update_zone_config(zone_id, payload)
            if not zone:
                return flask.jsonify({"error": "zona no encontrada"}), 404
            self._stop_zone(zone_id)
            self._start_zone(zone)
            return flask.jsonify(zone)

        @app.delete("/api/zones/<zone_id>")
        def _delete_zone(zone_id):
            self._stop_zone(zone_id)
            ok = zone_store.delete_zone(zone_id)
            return flask.jsonify({"deleted": ok})

        @app.get("/api/zones/<zone_id>/forecast")
        def _zone_forecast(zone_id):
            """Datos del grafico de 24h de la zona (historico real + mitad
            futura proyectada EN VIVO con el mismo motor de decision, ver
            climate/zone_forecast.py) -- se calcula bajo demanda, nunca en
            el ciclo periodico normal (implica varias consultas de
            historico a HA, deliberadamente fuera del camino caliente)."""
            runner = self._runners.get(zone_id)
            if not runner:
                return flask.jsonify({"error": "zona no encontrada o no arrancada"}), 404
            try:
                points = runner.build_forecast_chart()
            except Exception:
                log.exception("Fallo calculando el grafico de zona %s", zone_id)
                return flask.jsonify({"error": "fallo calculando la previsión"}), 500
            return flask.jsonify(points)

        @app.post("/api/zones/<zone_id>/refresh")
        def _refresh_zone(zone_id):
            """Fuerza una decisión ahora mismo, sin esperar al próximo evento
            reactivo o al refresco periódico -- util para diagnostico y para
            verificar una zona recien creada."""
            runner = self._runners.get(zone_id)
            if not runner:
                return flask.jsonify({"error": "zona no encontrada o no arrancada"}), 404
            try:
                runner.decide_and_act()
                zone_store.update_zone_state(zone_id, runner.to_persisted_state())
            except Exception:
                log.exception("Fallo forzando decision de zona %s", zone_id)
                return flask.jsonify({"error": "fallo forzando la decision de la zona"}), 500
            return flask.jsonify({"ok": True})

        # ---- comandos de la tarjeta de termostato del Dashboard --------
        #
        # `ZoneRunner.set_temperature`/`set_hvac_mode`/`set_preset_mode` ya
        # existian -- son el MISMO mecanismo que ya usa la orden MQTT real
        # del climate.* expuesto a HA (ver mqtt_climate.py), asi que la
        # tarjeta del dashboard no depende de que HA tenga bien resuelta
        # la entidad ni de ir a buscar su entity_id: habla DIRECTO con el
        # runner, exactamente igual que si la orden viniera de HomeKit.
        # Los tres terminan en `decide_and_act()` (que ya publica el
        # estado nuevo por MQTT solo, ver `_maybe_publish_state`), asi que
        # aqui solo hace falta persistir el estado tras la llamada -- mismo
        # patron que `/refresh` de arriba.
        def _zone_command(zone_id, fn):
            runner = self._runners.get(zone_id)
            if not runner:
                return flask.jsonify({"error": "zona no encontrada o no arrancada"}), 404
            try:
                fn(runner)
                zone_store.update_zone_state(zone_id, runner.to_persisted_state())
            except Exception:
                log.exception("Fallo aplicando comando en zona %s", zone_id)
                return flask.jsonify({"error": "fallo aplicando el comando en la zona"}), 500
            return flask.jsonify({"ok": True})

        @app.post("/api/zones/<zone_id>/set_temperature")
        def _set_temperature(zone_id):
            payload = flask.request.get_json(force=True) or {}
            return _zone_command(zone_id, lambda r: r.set_temperature(
                single=payload.get("temperature"), low=payload.get("target_temp_low"),
                high=payload.get("target_temp_high"),
            ))

        @app.post("/api/zones/<zone_id>/set_hvac_mode")
        def _set_hvac_mode(zone_id):
            payload = flask.request.get_json(force=True) or {}
            hvac_mode = payload.get("hvac_mode")
            if not hvac_mode:
                return flask.jsonify({"error": "falta 'hvac_mode'"}), 400
            return _zone_command(zone_id, lambda r: r.set_hvac_mode(hvac_mode))

        @app.post("/api/zones/<zone_id>/set_preset_mode")
        def _set_preset_mode(zone_id):
            payload = flask.request.get_json(force=True) or {}
            preset_mode = payload.get("preset_mode")
            if not preset_mode:
                return flask.jsonify({"error": "falta 'preset_mode'"}), 400
            return _zone_command(zone_id, lambda r: r.set_preset_mode(preset_mode))

        @app.get("/api/status")
        def _status():
            return flask.jsonify(
                {
                    "version": self.version,
                    "zones": len(self._runners),
                    "ws_connected": getattr(self._ws, "connected", False),
                    "mqtt_connected": self._mqtt.connected,
                }
            )

    # ------------------------------------------------------------- arranque

    def start_background_threads(self) -> None:
        threading.Thread(target=self._ws.run_forever, name="climate-ws", daemon=True).start()
        threading.Thread(target=self._reactive.worker_loop, name="climate-reactive", daemon=True).start()
        self._mqtt.connect()

        zones = zone_store.load_zones()
        for zone in zones:
            self._start_zone(zone)
        self._refresh_watched_entities()

        log.info("Plugin Climate arrancado con %d zona(s)", len(zones))

    def _start_zone(self, zone: dict) -> None:
        zone_id = zone["id"]
        cfg = zone["config"]
        state = zone.get("state") or None
        all_zone_configs = [z["config"] for z in zone_store.load_zones()]

        mqtt_zone = MqttClimateZone(self._mqtt, zone_id, cfg)
        runner = ZoneRunner(zone_id, cfg, self._ws, mqtt_zone, all_zone_configs, state=state, bridges=self)
        mqtt_zone.bind(runner)
        mqtt_zone.publish_discovery(
            min_temp=float(cfg.get("min_temp", 15.0)),
            max_temp=float(cfg.get("max_temp", 30.0)),
        )

        self._runners[zone_id] = runner
        self._mqtt_zones[zone_id] = mqtt_zone

        refresh_minutes = int(cfg.get("forecast_refresh_minutes", DEFAULT_FORECAST_REFRESH_MINUTES))
        stagger = zone_stagger_seconds(zone_id, refresh_minutes)
        threading.Thread(
            target=self._periodic_loop,
            args=(zone_id, refresh_minutes, stagger),
            name=f"climate-periodic-{zone_id}",
            daemon=True,
        ).start()

        self._refresh_watched_entities()

    def _stop_zone(self, zone_id: str) -> None:
        mqtt_zone = self._mqtt_zones.pop(zone_id, None)
        if mqtt_zone:
            mqtt_zone.remove_discovery()
        self._runners.pop(zone_id, None)
        self._refresh_watched_entities()
        # los hilos periodicos de esta zona se auto-terminan al no
        # encontrarse ya en self._runners (ver _periodic_loop)

    def _refresh_watched_entities(self) -> None:
        watched: set[str] = set()
        for runner in self._runners.values():
            try:
                watched |= runner.watched_entities()
            except Exception:
                log.exception("Fallo obteniendo watched_entities de zona %s", runner.zone_id)
        self._ws.set_watched_entities(watched)

    # ----------------------------------------------------------- reactivo -

    def _on_entity_change(self, entity_id: str, new_state: dict) -> None:
        self._reactive.trigger()

    def _run_reactive_cycle(self) -> None:
        for zone_id, runner in list(self._runners.items()):
            try:
                runner.handle_reactive_event()
                zone_store.update_zone_state(zone_id, runner.to_persisted_state())
            except Exception:
                log.exception("Fallo en ciclo reactivo de zona %s", zone_id)

    # ------------------------------------------------------------ periodo -

    def _periodic_loop(self, zone_id: str, refresh_minutes: int, stagger: float) -> None:
        time.sleep(stagger)
        while zone_id in self._runners:
            runner = self._runners.get(zone_id)
            if runner is None:
                return
            try:
                runner.handle_forecast_refresh()
                zone_store.update_zone_state(zone_id, runner.to_persisted_state())
            except Exception:
                log.exception("Fallo en refresco periodico de zona %s", zone_id)
            time.sleep(max(refresh_minutes, 1) * 60)

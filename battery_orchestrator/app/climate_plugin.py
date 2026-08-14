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
    version = "0.2.0"

    def __init__(self) -> None:
        self._runners: dict[str, ZoneRunner] = {}
        self._mqtt_zones: dict[str, MqttClimateZone] = {}
        self._ws = ha_websocket.HAWebSocketClient(self._on_entity_change)
        self._mqtt = ha_mqtt.HAMqttClient(client_id="home_orchestrator_climate")
        self._reactive = ha_websocket.ReactiveTrigger(self._run_reactive_cycle)
        self._app = flask.Flask("climate_plugin", template_folder="climate_templates")
        self._register_routes()

    # --------------------------------------------------------------- Flask -

    def flask_app(self):
        return self._app

    def _register_routes(self) -> None:
        app = self._app

        @app.get("/")
        def _index():
            return flask.render_template("index.html")

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
            except Exception as exc:
                log.exception("Fallo forzando decision de zona %s", zone_id)
                return flask.jsonify({"error": str(exc)}), 500
            return flask.jsonify({"ok": True})

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
        runner = ZoneRunner(zone_id, cfg, self._ws, mqtt_zone, all_zone_configs, state=state)
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

"""
Plugin de Lighting (iluminacion adaptativa) para el nucleo Home
Orchestrator -- tercer plugin de zonas tras Climate. Calcado
deliberadamente del mismo patron de `climate_plugin.py`: una sola
conexion WebSocket para eventos reactivos y consultas puntuales, zonas
persistidas en `lighting/zone_store.py`, un `ZoneRunner` por zona (ver
`lighting/zone_runner.py`).

A diferencia de Climate, este plugin NO controla dispositivos directamente
(nada de Tuya-por-LAN aqui): actua siempre sobre entidades `light.*` YA
expuestas en HA (nativas o publicadas por otro plugin, Tuya incluido, ver
tuya/mqtt_tuya.py) llamando a los servicios estandar `light.turn_on`/
`light.turn_off` por WebSocket -- cualquier bombilla que ya aparezca como
`light.*` en HA sirve, sin que este plugin necesite saber de que marca es.
"""

from __future__ import annotations

import logging
import threading
import time

import flask

import ha_websocket
from lighting import zone_store
from lighting.zone_runner import ZoneRunner
from plugin_base import Plugin

log = logging.getLogger("lighting_plugin")

DEFAULT_REAPPLY_MINUTES = 5
REACTIVE_MIN_INTERVAL_SECONDS = 5


class LightingPlugin(Plugin):
    slug = "lighting"
    name = "Lighting Orchestrator"
    version = "0.1.0"

    def __init__(self) -> None:
        self._runners: dict[str, ZoneRunner] = {}
        self._ws = ha_websocket.HAWebSocketClient(self._on_entity_change)
        self._reactive = ha_websocket.ReactiveTrigger(self._run_reactive_cycle)
        self._app = flask.Flask("lighting_plugin", template_folder="lighting_templates")
        self._register_routes()

    # --------------------------------------------------------------- Flask -

    def flask_app(self):
        return self._app

    def _register_routes(self) -> None:
        app = self._app

        @app.get("/")
        def _index():
            return flask.render_template("index.html")

        @app.get("/api/lights")
        def _list_lights():
            """Entidades `light.*` conocidas por HA ahora mismo -- para el
            selector de la interfaz (comas de entity_id, mismo patron que
            Climate usa para sensores/actuadores)."""
            try:
                states = self._ws.get_states()
            except Exception:
                log.exception("Fallo listando entidades light.*")
                return flask.jsonify([])
            out = [
                {
                    "entity_id": s["entity_id"],
                    "name": (s.get("attributes") or {}).get("friendly_name", s["entity_id"]),
                    "state": s.get("state"),
                }
                for s in states
                if s.get("entity_id", "").startswith("light.")
            ]
            out.sort(key=lambda x: x["name"].lower())
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
                        "occupied": runner.occupied,
                        "active_rule": runner.active_rule,
                        "current_values": runner.current_values,
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
            """Fuerza una decision ahora mismo -- util para probar una
            zona/regla recien creada sin esperar a un evento real."""
            runner = self._runners.get(zone_id)
            if not runner:
                return flask.jsonify({"error": "zona no encontrada o no arrancada"}), 404
            try:
                runner.decide_and_act()
                zone_store.update_zone_state(zone_id, runner.to_persisted_state())
            except Exception as exc:
                log.exception("Fallo forzando decision de zona %s", zone_id)
                return flask.jsonify({"error": str(exc)}), 500
            return flask.jsonify({"ok": True, "reason": runner.reason})

        @app.get("/api/status")
        def _status():
            return flask.jsonify(
                {
                    "version": self.version,
                    "zones": len(self._runners),
                    "ws_connected": getattr(self._ws, "connected", False),
                }
            )

    # ------------------------------------------------------------- arranque

    def start_background_threads(self) -> None:
        threading.Thread(target=self._ws.run_forever, name="lighting-ws", daemon=True).start()
        threading.Thread(target=self._reactive.worker_loop, name="lighting-reactive", daemon=True).start()

        zones = zone_store.load_zones()
        for zone in zones:
            self._start_zone(zone)

        log.info("Plugin Lighting arrancado con %d zona(s)", len(zones))

    def _start_zone(self, zone: dict) -> None:
        zone_id = zone["id"]
        cfg = zone["config"]
        state = zone.get("state") or None

        runner = ZoneRunner(zone_id, cfg, self._ws, state=state)
        self._runners[zone_id] = runner

        # Una decision inicial ya al arrancar la zona -- si no, el panel
        # se queda mostrando "sin evaluar todavia" hasta el primer evento
        # reactivo o hasta el primer ciclo periodico (que puede tardar
        # `reapply_minutes`). Solo falla en silencio si el WebSocket aun
        # no esta conectado (arranque en frio) -- el primer evento
        # reactivo o el primer ciclo periodico lo resuelven igualmente.
        try:
            runner.decide_and_act()
            zone_store.update_zone_state(zone_id, runner.to_persisted_state())
        except Exception:
            log.debug("Zona lighting %s: decision inicial pospuesta (WS aun no listo)", zone_id)

        reapply_minutes = int(cfg.get("reapply_minutes", DEFAULT_REAPPLY_MINUTES) or DEFAULT_REAPPLY_MINUTES)
        threading.Thread(
            target=self._periodic_loop,
            args=(zone_id, reapply_minutes),
            name=f"lighting-periodic-{zone_id}",
            daemon=True,
        ).start()

        self._refresh_watched_entities()

    def _stop_zone(self, zone_id: str) -> None:
        self._runners.pop(zone_id, None)
        self._refresh_watched_entities()
        # el hilo periodico de esta zona se auto-termina al no
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
                log.exception("Fallo en ciclo reactivo de zona lighting %s", zone_id)

    # ------------------------------------------------------------ periodo -

    def _periodic_loop(self, zone_id: str, reapply_minutes: int) -> None:
        # sin "stagger" deliberado (a diferencia de Climate, que sondea
        # historico de HA -- caro): reaplicar la curva de una zona de
        # luces es una llamada de servicio ligera, no hace falta repartir
        # el arranque de los hilos en el tiempo.
        while zone_id in self._runners:
            time.sleep(max(reapply_minutes, 1) * 60)
            runner = self._runners.get(zone_id)
            if runner is None:
                return
            try:
                runner.handle_periodic_reapply()
                zone_store.update_zone_state(zone_id, runner.to_persisted_state())
            except Exception:
                log.exception("Fallo en reaplicacion periodica de zona lighting %s", zone_id)

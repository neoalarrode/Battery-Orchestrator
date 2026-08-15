"""
Plugin de Lighting (iluminacion adaptativa) para el nucleo Home
Orchestrator -- tercer plugin de zonas tras Climate. Calcado
deliberadamente del mismo patron de `climate_plugin.py`: una sola
conexion WebSocket para eventos reactivos y consultas puntuales, zonas
persistidas en `lighting/zone_store.py`, un `ZoneRunner` por zona (ver
`lighting/zone_runner.py`).

DOS vias de control, no una sola -- una regla puede referenciar:
  - Un `light.*` YA expuesto en HA (nativo, o publicado por otro plugin
    via MQTT Discovery, Tuya incluido) -- se controla con los servicios
    estandar `light.turn_on`/`light.turn_off` por WebSocket. Sirve
    cualquier bombilla que ya aparezca como `light.*` en HA, sin que este
    plugin necesite saber de que marca es.
  - Un actuador de OTRO plugin cargado (hoy Tuya) referenciado como
    `tuya:<device_id>[:<indice>]`, controlado DIRECTAMENTE en el mismo
    proceso sin pasar por HA/MQTT -- mismo patron de "proveedor de
    actuadores" que ya usa Climate (ver `register_actuator_provider`,
    `TuyaPlugin.light_handle`). No son excluyentes: el mismo dispositivo
    Tuya puede seguir viendose como `light.*` en HA (voz, Lovelace, otras
    automatizaciones) mientras Lighting lo controla por la via directa.
"""

from __future__ import annotations

import logging
import threading
import time

import flask

import ha_mqtt
import ha_websocket
from lighting import presets, zone_store
from lighting.mqtt_lighting import MqttLightingZone
from lighting.zone_runner import ZoneRunner
from plugin_base import Plugin

log = logging.getLogger("lighting_plugin")

DEFAULT_REAPPLY_MINUTES = 5


class LightingPlugin(Plugin):
    slug = "lighting"
    name = "Lighting Orchestrator"
    version = "0.5.5"

    def __init__(self) -> None:
        self._runners: dict[str, ZoneRunner] = {}
        self._mqtt_zones: dict[str, MqttLightingZone] = {}
        self._ws = ha_websocket.HAWebSocketClient(self._on_entity_change)
        self._mqtt = ha_mqtt.HAMqttClient(client_id="home_orchestrator_lighting")
        # margen minimo casi nulo (no cero -- sigue haciendo falta un
        # pelin de coalescencia si varias entidades cambian a la vez, p.
        # ej. una zona entera de sensores en el mismo evento de HA) a
        # proposito: encender la luz debe sentirse inmediato, igual que
        # con Node-RED -- ver docstring de ReactiveTrigger.
        self._reactive = ha_websocket.ReactiveTrigger(self._run_reactive_cycle, min_interval_seconds=0.2)
        self._app = flask.Flask("lighting_plugin", template_folder="lighting_templates")
        # Registro GENERICO de "proveedores de actuadores" -- mismo
        # mecanismo que ya usa ClimatePlugin (ver su propio comentario):
        # cualquier plugin que ofrezca `light_handle` (Tuya hoy, otra
        # marca mañana) se registra aqui solo, sin que este fichero
        # necesite conocer nada especifico de esa marca.
        self._actuator_providers: dict[str, object] = {}
        self._register_routes()

    def register_actuator_provider(self, prefix: str, provider) -> None:
        """`provider` debe exponer `.light_handle(device_id, index) ->
        handle | None` y, si quiere aparecer en la referencia de luces de
        la interfaz, `.list_light_actuators() -> list[dict]`."""
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
        return provider.light_handle(device_id, index)

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

        @app.get("/api/light-actuators")
        def _list_light_actuators():
            """Actuadores de luz que OTROS plugins ofrecen (Tuya hoy,
            otra marca mañana) para control DIRECTO -- agregado de todos
            los proveedores registrados, mismo patron que Climate usa en
            `/api/actuators`. `ref` es lo que se escribe en `luces=...`
            de una regla para usar esta via en vez de un `light.*`."""
            out = []
            for prefix, provider in self._actuator_providers.items():
                lister = getattr(provider, "list_light_actuators", None)
                if lister is None:
                    continue
                try:
                    out.extend(lister())
                except Exception:
                    log.exception("Fallo listando actuadores de luz del proveedor '%s'", prefix)
            return flask.jsonify(out)

        @app.get("/api/room-presets")
        def _list_room_presets():
            """Presets recomendados de brillo/color por tipo de estancia
            (ver lighting/presets.py) -- solo un atajo de relleno rapido
            para el formulario de la interfaz, la zona nunca guarda una
            referencia al preset en si, solo los 4 numeros ya copiados."""
            return flask.jsonify(presets.list_presets())

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
                mqtt_zone = self._mqtt_zones.get(zone_id)
                if mqtt_zone:
                    mqtt_zone.publish_state(runner)
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
        self._mqtt.connect()

        zones = zone_store.load_zones()
        for zone in zones:
            self._start_zone(zone)

        log.info("Plugin Lighting arrancado con %d zona(s)", len(zones))

    def _start_zone(self, zone: dict) -> None:
        zone_id = zone["id"]
        cfg = zone["config"]
        state = zone.get("state") or None

        mqtt_zone = MqttLightingZone(self._mqtt, zone_id, cfg)
        runner = ZoneRunner(zone_id, cfg, self._ws, mqtt_zone=mqtt_zone, state=state, bridges=self)
        mqtt_zone.bind(runner)
        mqtt_zone.publish_discovery(
            min_color_temp_kelvin=float(cfg.get("min_color_temp_kelvin", 2200)),
            max_color_temp_kelvin=float(cfg.get("max_color_temp_kelvin", 5000)),
        )

        self._runners[zone_id] = runner
        self._mqtt_zones[zone_id] = mqtt_zone

        # Una decision inicial ya al arrancar la zona -- si no, el panel
        # se queda mostrando "sin evaluar todavia" hasta el primer evento
        # reactivo o hasta el primer ciclo periodico (que puede tardar
        # `reapply_minutes`). Solo falla en silencio si el WebSocket aun
        # no esta conectado (arranque en frio) -- el primer evento
        # reactivo o el primer ciclo periodico lo resuelven igualmente.
        try:
            runner.decide_and_act()
            zone_store.update_zone_state(zone_id, runner.to_persisted_state())
            mqtt_zone.publish_state(runner)
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
        mqtt_zone = self._mqtt_zones.pop(zone_id, None)
        if mqtt_zone:
            mqtt_zone.remove_discovery()
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
        # BUG REAL, confirmado por el usuario: el encendido al detectar
        # presencia tardaba 5-10s (con Node-RED era instantaneo). Causa
        # real: cada `ZoneRunner.decide_and_act()` pedia su PROPIA lectura
        # completa de HA (`ws.get_states()`) -- con 7 zonas, un solo
        # evento disparaba 7 lecturas completas seguidas por WebSocket.
        # Una unica lectura aqui, compartida por las 7 zonas del ciclo,
        # en vez de que cada una pida lo mismo por su cuenta.
        # Medicion real de tiempos -- a peticion expresa del usuario
        # (objetivo: menos de 1s de principio a fin), en vez de seguir
        # ajustando a ciegas. INFO, no DEBUG: es la unica forma de saber
        # de verdad donde se va el tiempo en produccion sin tener que
        # cronometrar a mano desde fuera cada vez que se sospecha una
        # regresion.
        cycle_start = time.monotonic()
        try:
            states = {s.get("entity_id"): s for s in self._ws.get_states() if s.get("entity_id")}
        except Exception:
            log.exception("Fallo leyendo estados de HA para el ciclo reactivo de Lighting")
            states = None
        states_elapsed = time.monotonic() - cycle_start
        # BUG REAL, confirmado por el usuario: incluso tras eliminar el
        # volcado completo de HA (`get_states()`), el ciclo seguia
        # tardando 1-3s -- `zone_store.update_zone_state` relee y
        # reescribe el fichero de config COMPLETO (compartido con
        # Battery/Climate/Tuya/TP-Link) en cada llamada, y aqui se
        # llamaba una vez POR ZONA (7 lecturas + 7 escrituras completas
        # de disco, en serie, por un solo evento). Se acumulan aqui y se
        # escriben de una sola vez al final (`update_zone_states`).
        pending_states: dict[str, dict] = {}
        for zone_id, runner in list(self._runners.items()):
            try:
                runner.handle_reactive_event(states)
                pending_states[zone_id] = runner.to_persisted_state()
                mqtt_zone = self._mqtt_zones.get(zone_id)
                if mqtt_zone:
                    mqtt_zone.publish_state(runner)
            except Exception:
                log.exception("Fallo en ciclo reactivo de zona lighting %s", zone_id)
        try:
            zone_store.update_zone_states(pending_states)
        except Exception:
            log.exception("Fallo guardando el estado de las zonas de Lighting tras el ciclo reactivo")
        total_elapsed = time.monotonic() - cycle_start
        log.info(
            "Ciclo reactivo de Lighting: %.3fs total (lectura de HA: %.3fs, %d zona(s))",
            total_elapsed, states_elapsed, len(self._runners),
        )

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
                mqtt_zone = self._mqtt_zones.get(zone_id)
                if mqtt_zone:
                    mqtt_zone.publish_state(runner)
            except Exception:
                log.exception("Fallo en reaplicacion periodica de zona lighting %s", zone_id)

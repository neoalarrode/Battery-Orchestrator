"""
Motor de decision de una zona de Lighting -- mismo espiritu que
`climate/zone_runner.py`: determinista, sin ningun modelo oculto, todo lo
que decide se puede explicar con la propia config de la zona.

Cada ciclo (reactivo, al cambiar un sensor de presencia o una condicion
de regla; o periodico, cada `reapply_minutes`, para que la curva de
color/brillo "viva" aunque nadie haya entrado ni salido de la zona) hace
lo mismo, en este orden:

  1. `_snapshot_states()`: UNA sola lectura de HA (`ws.get_states()`) para
     todo el ciclo -- evita N llamadas sueltas (una por sensor de
     presencia + una por condicion + una por luz) contra el WebSocket.
  2. `_is_occupied()`: OR de todos los sensores de presencia de la zona,
     comparando su estado contra `occupied_states` (config).
  3. Margen de apagado (`off_delay_seconds`): la zona no se considera
     "vacia de verdad" hasta que ha pasado ese margen desde la ULTIMA vez
     que hubo presencia -- evita apagar y encender en cada parpadeo de un
     sensor de movimiento.
  4. `rules.select_rule(...)`: que grupo de luces corresponde ahora mismo
     (primera regla cuyas condiciones se cumplen).
  5. Si la regla activa CAMBIO desde el ciclo anterior (o la zona acaba
     de pasar a ocupada), se apagan las luces de la regla ANTERIOR que no
     esten tambien en la nueva -- una unica vez, en la transicion. Fuera
     de una transicion nunca se tocan luces que el usuario haya podido
     encender/apagar a mano por su cuenta (ver `respect_manual_changes`
     mas abajo): esto es deliberado, apagar constantemente lo que un
     humano acaba de tocar a mano seria "pelearse" con el, no ayudarle.
  6. Se aplica el color/brillo de `schedule.value_at(...)` a las luces de
     la regla activa que sigan encendidas -- salvo las que el propio
     motor detecta "tocadas a mano" (ver `_detect_manual_overrides`).
"""

from __future__ import annotations

import logging
import time

from lighting import rules, schedule

log = logging.getLogger("lighting.zone_runner")

# Tolerancias para decidir si el estado REAL de una luz coincide con lo
# ultimo que le mandamos, o si alguien la ha tocado a mano por su cuenta
# (ver _detect_manual_overrides) -- HA redondea brillo 0-255<->porcentaje
# y algunos drivers (Tuya incluido) no devuelven exactamente lo pedido,
# asi que un margen estricto (una unidad) dispararia falsos positivos en
# cada ciclo.
BRIGHTNESS_TOLERANCE_PCT = 4
COLOR_TEMP_TOLERANCE_KELVIN = 150


class ZoneRunner:
    def __init__(self, zone_id: str, cfg: dict, ws, mqtt_zone=None, state: dict | None = None, bridges=None) -> None:
        self.zone_id = zone_id
        self.zone = cfg
        self.ws = ws
        self.mqtt = mqtt_zone  # reservado para exponer estado a HA mas adelante, no usado todavia
        # `bridges` (el propio LightingPlugin) resuelve refs
        # "tuya:<device_id>[:<indice>]" a un handle de control DIRECTO --
        # mismo patron que ZoneRunner.bridges de Climate. None en tests
        # aislados (sin bridges no se puede resolver ninguna ref con
        # prefijo, se tratan como si no existiera ese proveedor).
        self.bridges = bridges
        self._state = dict(state or {})

        # El texto de reglas se parsea UNA vez al arrancar la zona (se
        # reinicia en cada guardado de config, ver
        # LightingPlugin._update_zone: /stop+/start), no en cada ciclo --
        # mismo patron que `climate/zone_runner.py` con `presets_text`.
        # Un typo no debe tirar la zona entera: se registra y se sigue
        # sin reglas activas (nunca enciende nada hasta que se corrija,
        # pero la zona sigue viva y sigue apagando por falta de
        # presencia con normalidad). La curva de brillo/color NO se
        # parsea aqui -- no es texto, son 4 numeros ya validos en la
        # propia config (ver schedule.value_at).
        try:
            self._rules = rules.parse_rules_text(cfg.get("rules_text", ""))
        except ValueError:
            log.warning("Zona lighting %s: rules_text invalido, sin reglas activas", zone_id, exc_info=True)
            self._rules = []

        # Snapshot en vivo, recalculado cada ciclo -- lo que expone
        # `/api/zones` para pintar la interfaz.
        self.occupied: bool = bool(self._state.get("occupied", False))
        self.active_rule: str | None = self._state.get("active_rule")
        self.current_values: dict | None = None
        self.reason: str = "sin evaluar todavia"

    # ------------------------------------------------------------ estado -

    def to_persisted_state(self) -> dict:
        return dict(self._state)

    def watched_entities(self) -> set[str]:
        cfg = self.zone
        out = {e for e in (cfg.get("presence_entities") or []) if e}
        out |= rules.condition_entities(self._rules)
        return out

    def _snapshot_states(self) -> dict[str, dict]:
        """Una lectura de HA para todo el ciclo -- ver docstring del
        modulo. `get_states()` ya trae TODAS las entidades en una sola
        llamada al WebSocket, asi que construir el dict aqui es gratis
        comparado con `ws.get_state(x)` una vez por entidad."""
        try:
            return {s.get("entity_id"): s for s in self.ws.get_states() if s.get("entity_id")}
        except Exception:
            log.exception("Zona lighting %s: fallo leyendo estados de HA", self.zone_id)
            return {}

    # --------------------------------------------------------- ocupacion -

    def _is_occupied(self, states: dict[str, dict]) -> bool:
        cfg = self.zone
        presence = [e for e in (cfg.get("presence_entities") or []) if e]
        if not presence:
            return False
        occupied_states = cfg.get("occupied_states") or ["on"]
        return any((states.get(e) or {}).get("state") in occupied_states for e in presence)

    def _occupied_with_delay(self, raw_occupied: bool, now: float) -> bool:
        """Aplica el margen de gracia (`off_delay_seconds`) antes de
        considerar la zona vacia de verdad -- ver punto 3 del docstring."""
        if raw_occupied:
            self._state["last_occupied_ts"] = now
            return True
        last = self._state.get("last_occupied_ts")
        if last is None:
            return False
        delay = float(self.zone.get("off_delay_seconds", 120) or 0)
        return (now - last) < delay

    # -------------------------------------------------------------- luz --
    #
    # Cada "luz" de una regla es o bien un `light.*` de HA (via `self.ws`,
    # WebSocket) o bien una ref con prefijo `<proveedor>:<device_id>
    # [:indice]` (hoy solo `tuya:`, resuelta por `self.bridges` a un
    # handle de control DIRECTO en el mismo proceso -- ver
    # `LightingPlugin.resolve_bridge_handle`). Los metodos de aqui abajo
    # son el UNICO sitio que decide por cual via ir; el resto de
    # `decide_and_act` no sabe ni le importa cual es cual.

    def _is_bridge_ref(self, entity_id: str) -> bool:
        return self.bridges is not None and self.bridges.is_bridge_ref(entity_id)

    def _resolve_bridge_handle(self, entity_id: str):
        if self.bridges is None:
            return None
        try:
            return self.bridges.resolve_bridge_handle(entity_id)
        except Exception:
            log.exception("Zona lighting %s: fallo resolviendo bridge %s", self.zone_id, entity_id)
            return None

    def _current_light_values(self, states: dict[str, dict], entity_id: str) -> dict | None:
        """`{"on", "brightness_pct", "color_temp_kelvin"}` en la MISMA
        forma venga de donde venga -- HA o un bridge directo -- para que
        `_detect_manual_overrides` no tenga que saber cual es cual."""
        if self._is_bridge_ref(entity_id):
            handle = self._resolve_bridge_handle(entity_id)
            if handle is None or not handle.available:
                return None
            return {
                "on": handle.is_on,
                "brightness_pct": handle.brightness_pct,
                "color_temp_kelvin": handle.color_temp_kelvin,
            }
        st = states.get(entity_id) or {}
        attrs = st.get("attributes") or {}
        brightness = attrs.get("brightness")
        return {
            "on": st.get("state") == "on",
            "brightness_pct": round(brightness / 255 * 100) if brightness is not None else None,
            "color_temp_kelvin": attrs.get("color_temp_kelvin"),
        }

    def _is_on(self, states: dict[str, dict], entity_id: str) -> bool:
        vals = self._current_light_values(states, entity_id)
        return bool(vals and vals["on"])

    def _turn_off(self, entity_id: str) -> None:
        try:
            if self._is_bridge_ref(entity_id):
                handle = self._resolve_bridge_handle(entity_id)
                if handle is not None:
                    handle.turn_off()
                return
            self.ws.call_service("light", "turn_off", target={"entity_id": entity_id})
        except Exception:
            log.exception("Zona lighting %s: fallo apagando %s", self.zone_id, entity_id)

    def _apply_values(self, entity_id: str, values: dict | None, turning_on: bool, brightness_only: bool = False) -> None:
        """Enciende/ajusta segun la curva solar (`values`, puede ser None
        si `sun.sun` no esta disponible ahora mismo -- en ese caso solo
        se enciende, sin tocar color/brillo). Registra lo mandado en
        `_state["commanded"]` para poder detectar despues si alguien lo
        cambio a mano (ver `_detect_manual_overrides`). `brightness_only`
        (luz marcada «:solo_brillo» en la regla, ver rules.py) excluye
        esta luz en concreto del color/temperatura de color -- se ajusta
        el brillo igual que cualquier otra, simplemente nunca se le manda
        color."""
        brightness_pct = values.get("brightness_pct") if values else None
        color_temp_kelvin = None if brightness_only else (values.get("color_temp_kelvin") if values else None)
        try:
            if self._is_bridge_ref(entity_id):
                handle = self._resolve_bridge_handle(entity_id)
                if handle is None:
                    return
                handle.turn_on(brightness_pct=brightness_pct, color_temp_kelvin=color_temp_kelvin)
            else:
                service_data: dict = {}
                if brightness_pct is not None:
                    service_data["brightness_pct"] = brightness_pct
                if color_temp_kelvin is not None:
                    service_data["color_temp_kelvin"] = color_temp_kelvin
                transition = self.zone.get("transition_seconds")
                if transition is not None:
                    service_data["transition"] = float(transition)
                self.ws.call_service("light", "turn_on", service_data=service_data, target={"entity_id": entity_id})
        except Exception:
            log.exception("Zona lighting %s: fallo encendiendo/ajustando %s", self.zone_id, entity_id)
            return
        commanded = self._state.setdefault("commanded", {})
        # OJO: se guarda lo que de VERDAD se mando (brightness_pct/
        # color_temp_kelvin ya filtrados arriba por `brightness_only`),
        # NUNCA el `values` crudo de la curva -- si no, una luz «:solo_
        # brillo» quedaria con un color_temp_kelvin "esperado" en cache
        # que el dispositivo real nunca recibio, y `_detect_manual_
        # overrides` la marcaria como tocada a mano en el proximo ciclo
        # sin que nadie la haya tocado.
        commanded[entity_id] = {
            "brightness_pct": brightness_pct,
            "color_temp_kelvin": color_temp_kelvin,
            "ts": time.time(),
        }
        if turning_on:
            # entrada fresca en la zona (o cambio de regla): se considera
            # "mano limpia" de nuevo, cualquier marca de override anterior
            # de ESTA luz deja de aplicar.
            self._state.setdefault("manual_override", {}).pop(entity_id, None)

    def _detect_manual_overrides(self, states: dict[str, dict], entity_ids: set[str]) -> None:
        """Heuristica deliberadamente simple (mismo espiritu "sin caja
        negra" que el resto del proyecto, y el mismo problema que
        resuelve "Adaptive Lighting" de forma parecida): si el brillo o
        el color REAL de una luz que seguimos gestionando ya no coincide
        con lo ultimo que le mandamos, alguien la ha tocado a mano -- se
        marca y se deja de reajustar su color/brillo hasta la proxima vez
        que la zona vuelva a encenderla desde cero (ver `_apply_values`,
        `turning_on=True` limpia la marca). Si la luz esta APAGADA no se
        evalua nada (apagarla a mano no es "un override de color", es
        simplemente que la persona no la quiere encendida ahora)."""
        if not self.zone.get("respect_manual_changes", True):
            return
        commanded = self._state.get("commanded") or {}
        overrides = self._state.setdefault("manual_override", {})
        for entity_id in entity_ids:
            cmd = commanded.get(entity_id)
            vals = self._current_light_values(states, entity_id)
            if not cmd or not vals or not vals["on"]:
                continue
            mismatch = False
            # `cmd.get(...) is not None` -- NO solo "in cmd": desde que
            # `_apply_values` guarda siempre las dos claves (ver su
            # comentario), una luz «:solo_brillo» o sin lectura de sol
            # todavia tiene la clave con valor `None`, que no es
            # comparable (bug real que esto evita: `None - int` revienta).
            if cmd.get("brightness_pct") is not None and vals["brightness_pct"] is not None:
                if abs(vals["brightness_pct"] - cmd["brightness_pct"]) > BRIGHTNESS_TOLERANCE_PCT:
                    mismatch = True
            if not mismatch and cmd.get("color_temp_kelvin") is not None and vals["color_temp_kelvin"] is not None:
                if abs(vals["color_temp_kelvin"] - cmd["color_temp_kelvin"]) > COLOR_TEMP_TOLERANCE_KELVIN:
                    mismatch = True
            if mismatch:
                overrides[entity_id] = True

    # --------------------------------------------------------- decision --

    def decide_and_act(self) -> None:
        cfg = self.zone
        now = time.time()
        states = self._snapshot_states()

        raw_occupied = self._is_occupied(states)
        occupied = self._occupied_with_delay(raw_occupied, now)
        was_occupied = bool(self._state.get("occupied", False))
        self._state["occupied"] = occupied
        self.occupied = occupied

        all_zone_lights = rules.all_lights(self._rules)
        # deteccion de "tocado a mano" sobre TODAS las luces que la zona
        # gestiona, encendidas o no -- independiente de si hay presencia
        # ahora mismo, para no perder la marca si alguien la toca justo
        # cuando la zona se queda vacia.
        self._detect_manual_overrides(states, all_zone_lights)

        # `current_values` se calcula SIEMPRE, este ocupada o no la zona
        # -- es la curva solar en si (brillo/color que TOCARIA ahora
        # mismo segun la hora del dia), independiente de si hay alguien
        # para aplicarsela. Antes se dejaba en None sin presencia, lo que
        # desde fuera (interfaz, API) era indistinguible de "sun.sun no
        # disponible" -- ahora ambos casos se ven distintos: sin
        # presencia con sol legible sigue mostrando la previsualizacion
        # de lo que se encenderia si entrase alguien.
        self.current_values = schedule.value_at(cfg, states.get("sun.sun"))

        if not occupied:
            self.active_rule = None
            self._state["active_rule"] = None
            if cfg.get("auto_off", True):
                for entity_id in all_zone_lights:
                    if self._is_on(states, entity_id):
                        self._turn_off(entity_id)
                self.reason = "sin presencia -> apagado"
            else:
                self.reason = "sin presencia (apagado automatico desactivado)"
            return

        selected = rules.select_rule(self._rules, states)
        selected_name = selected.get("name") if selected else None
        selected_lights = set(selected.get("lights") or []) if selected else set()
        selected_brightness_only = set(selected.get("brightness_only") or []) if selected else set()

        transitioned = (not was_occupied) or (selected_name != self.active_rule)
        self.active_rule = selected_name
        self._state["active_rule"] = selected_name

        if transitioned and selected is not None:
            for entity_id in all_zone_lights - selected_lights:
                if self._is_on(states, entity_id):
                    self._turn_off(entity_id)

        values = self.current_values

        if selected is None:
            self.reason = "presencia detectada, ninguna regla coincide -- nada que encender"
            return

        # `auto_on` solo dispara el ENCENDIDO en la propia transicion
        # (entrada fresca en la zona, o cambio de regla activa) -- si el
        # usuario apaga a mano una luz de la regla activa mientras sigue
        # habiendo presencia, se respeta (no se vuelve a encender sola en
        # cada ciclo periodico, seria pelearse con quien la acaba de
        # apagar aposta). Las que ya estan encendidas SI se reajustan
        # (color/brillo de la curva) salvo que esten marcadas como
        # tocadas a mano.
        auto_on = cfg.get("auto_on", True)
        overrides = self._state.get("manual_override") or {}
        for entity_id in selected_lights:
            only_brightness = entity_id in selected_brightness_only
            if self._is_on(states, entity_id):
                if not overrides.get(entity_id):
                    self._apply_values(entity_id, values, turning_on=False, brightness_only=only_brightness)
            elif auto_on and transitioned:
                self._apply_values(entity_id, values, turning_on=True, brightness_only=only_brightness)

        self.reason = f"regla activa: {selected_name or '(sin nombre)'}"

    # -------------------------------------------------------- reactivo/periodico -

    def handle_reactive_event(self) -> None:
        self.decide_and_act()

    def handle_periodic_reapply(self) -> None:
        self.decide_and_act()

    # -------------------------------------------------- luz "dummy" (MQTT) -
    #
    # Ver lighting/mqtt_lighting.py -- una luz de conjunto por zona, para
    # HomeKit/Matter/Lovelace, en vez de exponer cada bombilla suelta.

    def _target_lights(self) -> tuple[set[str], set[str]]:
        """(luces objetivo, cuales de ellas son «:solo_brillo») para la
        luz dummy -- las de la regla activa si hay una ocupacion/regla
        resuelta ahora mismo, o TODAS las de la zona si no (zona vacia,
        o presencia real pero ninguna regla coincide): sin nada mas
        concreto que ofrecer, mejor representar el conjunto entero que no
        representar nada."""
        if self.occupied and self.active_rule:
            for rule in self._rules:
                if rule.get("name") == self.active_rule:
                    return set(rule.get("lights") or []), set(rule.get("brightness_only") or [])
        return rules.all_lights(self._rules), set()

    def group_state(self) -> dict:
        """Estado agregado para la luz dummy: ON si CUALQUIERA de las
        luces objetivo esta encendida ahora mismo; brillo/color = la
        curva solar ya calculada de la zona (`current_values`, ver
        decide_and_act -- se recalcula cada ciclo, este ocupada la zona
        o no)."""
        states = self._snapshot_states()
        target, _brightness_only = self._target_lights()
        on = any(self._is_on(states, e) for e in target)
        vals = self.current_values or {}
        return {
            "on": on,
            "brightness_pct": vals.get("brightness_pct") if on else None,
            "color_temp_kelvin": vals.get("color_temp_kelvin") if on else None,
        }

    def manual_command(self, on: bool, brightness_pct: float | None = None, color_temp_kelvin: float | None = None) -> None:
        """Comando desde la luz dummy (MQTT, HomeKit...) -- se reenvia TAL
        CUAL a las luces objetivo ahora mismo (ver `_target_lights`),
        respetando `:solo_brillo` por luz. No toca la logica de
        presencia/reglas -- es un override puntual, exactamente igual que
        si alguien hubiera tocado esas luces a mano por su cuenta (el
        propio `_detect_manual_overrides` ya se encarga de no pelearse
        con el en el siguiente ciclo si el valor no encaja con lo que la
        curva esperaria)."""
        target, brightness_only = self._target_lights()
        for entity_id in target:
            if on:
                values = {"brightness_pct": brightness_pct, "color_temp_kelvin": color_temp_kelvin}
                self._apply_values(entity_id, values, turning_on=True, brightness_only=entity_id in brightness_only)
            else:
                self._turn_off(entity_id)

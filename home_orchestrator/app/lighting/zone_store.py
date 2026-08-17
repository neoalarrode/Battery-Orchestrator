"""
Persistencia de las zonas de Lighting -- mismo fichero de config compartido
del nucleo (ver config_store.py de Battery), bajo su propio namespace
"plugins.lighting" (nunca pisa "plugins.battery" ni "plugins.climate").
Calcado deliberadamente de climate/zone_store.py -- mismo patron ya
probado en produccion, ninguna razon para inventar uno nuevo.

Cada zona tiene dos partes:
  - `zones[].config`: lo que declara el usuario (sensores de presencia,
    reglas condicionales, curva de brillo/color por hora...).
  - `zones[].state`: lo que el propio motor recuerda entre ciclos (regla
    activa, ultima vez que hubo presencia, que se le mando por ultimo a
    cada luz, que luces detecto "tocadas a mano"...).
"""

from __future__ import annotations

import uuid

import config_store
from lighting import schedule

PLUGIN_KEY = "lighting"


DEFAULT_ZONE_CONFIG = {
    "name": "",
    "presence_entities": [],
    "occupied_states": ["on", "home", "playing", "open"],
    "auto_on": True,
    "auto_off": True,
    "off_delay_seconds": 120,
    "respect_manual_changes": True,
    "transition_seconds": 2,
    "reapply_minutes": 5,
    # curva de brillo/color atada a la posicion del sol (sun.sun de HA),
    # NUNCA a una hora fija -- ver lighting/schedule.py. El usuario solo
    # declara los 4 extremos de los dos rangos.
    "min_brightness_pct": schedule.DEFAULT_MIN_BRIGHTNESS_PCT,
    "max_brightness_pct": schedule.DEFAULT_MAX_BRIGHTNESS_PCT,
    "min_color_temp_kelvin": schedule.DEFAULT_MIN_COLOR_TEMP_KELVIN,
    "max_color_temp_kelvin": schedule.DEFAULT_MAX_COLOR_TEMP_KELVIN,
    # Sensor de luz ambiente REAL (opcional) -- sube el brillo de la curva
    # solar por encima de lo que le tocaria segun la hora si la luz real
    # medida esta por debajo del objetivo (dia nublado, persiana bajada,
    # habitacion interior...). Sin sensor declarado, la curva sigue
    # dependiendo solo de la posicion del sol, igual que siempre. Ver
    # lighting/schedule.py:_lux_boosted_brightness_pct.
    "lux_sensor": "",
    "target_lux": schedule.DEFAULT_TARGET_LUX,
    # reglas condicionales, primera que coincide gana -- texto declarado
    # por el usuario, ver lighting/rules.py:parse_rules_text.
    "rules_text": "",
}


def _read_lighting_section() -> dict:
    return config_store.read_plugin_section(PLUGIN_KEY, {"zones": []})


def _write_lighting_section(section: dict) -> None:
    # El read-modify-write completo se hace dentro de config_store, bajo el
    # MISMO lock que el resto de escritores del fichero compartido -- antes este
    # modulo usaba un lock propio, distinto del de Battery/Tuya/Climate, con lo
    # que una escritura de otro plugin colada entre la lectura y la escritura de
    # aqui se descartaba en silencio. Ademas, el camino de "formato no
    # reconocido" reemplazaba el documento por uno vacio, tirando la config
    # entera si el fichero estaba en el formato plano antiguo (ver
    # config_store._as_namespaced).
    config_store.update_plugin_section(PLUGIN_KEY, section)


def load_zones() -> list[dict]:
    """Lista de zonas, cada una `{"id", "config", "state"}`."""
    with config_store.transaction():
        section = _read_lighting_section()
        zones = section.get("zones") or []
        for z in zones:
            merged = dict(DEFAULT_ZONE_CONFIG)
            merged.update(z.get("config") or {})
            z["config"] = merged
            z.setdefault("state", {})
        return zones


def save_zones(zones: list[dict]) -> None:
    with config_store.transaction():
        _write_lighting_section({"zones": zones})


def add_zone(config: dict) -> dict:
    with config_store.transaction():
        zones = load_zones()
        merged = dict(DEFAULT_ZONE_CONFIG)
        merged.update(config)
        zone = {"id": str(uuid.uuid4())[:8], "config": merged, "state": {}}
        zones.append(zone)
        save_zones(zones)
        return zone


def update_zone_config(zone_id: str, config: dict) -> dict | None:
    with config_store.transaction():
        zones = load_zones()
        for z in zones:
            if z["id"] == zone_id:
                z["config"].update(config)
                save_zones(zones)
                return z
        return None


def update_zone_state(zone_id: str, state: dict) -> None:
    with config_store.transaction():
        zones = load_zones()
        for z in zones:
            if z["id"] == zone_id:
                z["state"] = state
                save_zones(zones)
                return


def update_zone_states(states: dict[str, dict]) -> None:
    """Igual que `update_zone_state`, pero para VARIAS zonas de una
    tacada -- UN solo read-modify-write del fichero compartido, en vez de
    uno por zona. BUG REAL, confirmado por el usuario (el ciclo reactivo
    de Lighting seguia tardando 1-3s incluso despues de eliminar el
    volcado completo de HA por WebSocket): `LightingPlugin.
    _run_reactive_cycle` llamaba a `update_zone_state` una vez POR ZONA
    (7 en produccion) -- cada llamada relee y reescribe el fichero de
    config COMPLETO (compartido con Battery/Climate/Tuya/TP-Link) de
    principio a fin, asi que un solo evento de presencia disparaba 7
    lecturas + 7 escrituras completas de disco, en serie. Aqui se hace
    UNA sola vez para las 7."""
    if not states:
        return
    with config_store.transaction():
        zones = load_zones()
        changed = False
        for z in zones:
            new_state = states.get(z["id"])
            if new_state is not None:
                z["state"] = new_state
                changed = True
        if changed:
            save_zones(zones)


def delete_zone(zone_id: str) -> bool:
    with config_store.transaction():
        zones = load_zones()
        before = len(zones)
        zones = [z for z in zones if z["id"] != zone_id]
        save_zones(zones)
        return len(zones) < before

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

import threading
import uuid

import config_store
from lighting import schedule

PLUGIN_KEY = "lighting"

_lock = threading.RLock()

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
    # reglas condicionales, primera que coincide gana -- texto declarado
    # por el usuario, ver lighting/rules.py:parse_rules_text.
    "rules_text": "",
}


def _read_lighting_section() -> dict:
    raw = config_store._read_raw() or {}
    if not isinstance(raw.get("plugins"), dict):
        return {"zones": []}
    section = raw["plugins"].get(PLUGIN_KEY)
    if not isinstance(section, dict):
        return {"zones": []}
    return section


def _write_lighting_section(section: dict) -> None:
    with _lock:
        raw = config_store._read_raw()
        if not isinstance(raw, dict) or not isinstance(raw.get("plugins"), dict):
            raw = {"schema_version": config_store.SCHEMA_ROOT_VERSION, "core": {}, "plugins": {}}
        raw.setdefault("plugins", {})[PLUGIN_KEY] = section
        raw["schema_version"] = config_store.SCHEMA_ROOT_VERSION
        config_store._write_raw(raw)


def load_zones() -> list[dict]:
    """Lista de zonas, cada una `{"id", "config", "state"}`."""
    with _lock:
        section = _read_lighting_section()
        zones = section.get("zones") or []
        for z in zones:
            merged = dict(DEFAULT_ZONE_CONFIG)
            merged.update(z.get("config") or {})
            z["config"] = merged
            z.setdefault("state", {})
        return zones


def save_zones(zones: list[dict]) -> None:
    with _lock:
        _write_lighting_section({"zones": zones})


def add_zone(config: dict) -> dict:
    with _lock:
        zones = load_zones()
        merged = dict(DEFAULT_ZONE_CONFIG)
        merged.update(config)
        zone = {"id": str(uuid.uuid4())[:8], "config": merged, "state": {}}
        zones.append(zone)
        save_zones(zones)
        return zone


def update_zone_config(zone_id: str, config: dict) -> dict | None:
    with _lock:
        zones = load_zones()
        for z in zones:
            if z["id"] == zone_id:
                z["config"].update(config)
                save_zones(zones)
                return z
        return None


def update_zone_state(zone_id: str, state: dict) -> None:
    with _lock:
        zones = load_zones()
        for z in zones:
            if z["id"] == zone_id:
                z["state"] = state
                save_zones(zones)
                return


def delete_zone(zone_id: str) -> bool:
    with _lock:
        zones = load_zones()
        before = len(zones)
        zones = [z for z in zones if z["id"] != zone_id]
        save_zones(zones)
        return len(zones) < before

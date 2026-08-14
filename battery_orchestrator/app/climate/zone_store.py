"""
Persistencia de las zonas de Climate — mismo fichero de config compartido
del nucleo (ver config_store.py de Battery), bajo su propio namespace
"plugins.climate" (nunca pisa la seccion de Battery, "plugins.battery").

Cada zona tiene dos partes distintas guardadas por separado:
  - `zones[].config`: lo que declaras tu (actuadores, sensores, presets,
    consignas de seguridad...) — equivalente a lo que antes vivia en
    config_flow.py de Climate Orchestrator (ConfigEntry.data).
  - `zones[].state`: lo que el propio motor aprende/decide en caliente
    (preset activo, consignas manuales, aprendizajes de sobreimpulso...)
    — equivalente a lo que antes vivia en RestoreEntity. Se actualiza en
    cada `ZoneRunner.to_persisted_state()` tras un cambio que importe.
"""

from __future__ import annotations

import threading
import uuid

import config_store

PLUGIN_KEY = "climate"

_lock = threading.RLock()

DEFAULT_ZONE_CONFIG = {
    "name": "",
    "current_temp_sensor": "",
    "outdoor_temp_sensor": "",
    "humidity_sensor": "",
    "weather_entity": "",
    "heat_switches": [],
    "cool_switches": [],
    "climate_entities": [],
    "humidifier_entities": [],
    "presence_entities": [],
    "door_window_entities": [],
    "auto_window_detection": False,
    "presets_text": "",
    "presence_preset": "",
    "away_preset": "",
    "deadband": 0.3,
    "min_temp": 15.0,
    "max_temp": 30.0,
    "target_humidity": 45,
    "priority": "confort",
    "simulate": True,
    "min_on_seconds": 300,
    "min_off_seconds": 300,
    "tpi_cycle_minutes": 15,
    "max_power_w": 0,
    "home_power_sensor": "",
    "actuator_power": {},
    "history_days_for_inertia": 5,
    "forecast_refresh_minutes": 10,
    "dry_humidity_threshold": 65,
}


def _read_climate_section() -> dict:
    """Lee la seccion "plugins.climate" directa del fichero compartido —
    reusa la infraestructura de lectura de config_store (migracion de
    formato, etc.) sin depender de su forma de guardar el dict PLANO de
    Battery."""
    raw = config_store._read_raw() or {}
    if not isinstance(raw.get("plugins"), dict):
        return {"zones": []}
    section = raw["plugins"].get(PLUGIN_KEY)
    if not isinstance(section, dict):
        return {"zones": []}
    return section


def _write_climate_section(section: dict) -> None:
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
        section = _read_climate_section()
        zones = section.get("zones") or []
        # completar claves de config que falten (esquema nuevo)
        for z in zones:
            merged = dict(DEFAULT_ZONE_CONFIG)
            merged.update(z.get("config") or {})
            z["config"] = merged
            z.setdefault("state", {})
        return zones


def save_zones(zones: list[dict]) -> None:
    with _lock:
        _write_climate_section({"zones": zones})


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

"""
Persistencia de la configuracion del usuario (baterias, tarifa, origen de
PV, sensor de consumo). Todo editable desde la interfaz, nada hardcodeado.
Se guarda en un JSON dentro del directorio persistente del addon.
"""

from __future__ import annotations

import json
import os
import threading
import uuid

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/data/config.json")

_lock = threading.RLock()  # reentrante: load_config() llama a save_config() en el primer arranque

DEFAULT_CONFIG = {
    "batteries": [],
    "tariff": {
        "mode": "fixed",  # "fixed" | "pvpc_sensor"
        "punta_price": 0.173,
        "llano_price": 0.094,
        "valle_price": 0.075,
        "punta_periods": [[10, 14], [18, 22]],
        "llano_periods": [[8, 10], [14, 18], [22, 24]],
        "weekend_is_valle": True,
        "pvpc_sensor": "",
    },
    "pv_arrays": [],
    "current_pv_sensor": "",  # produccion solar INSTANTANEA real (W), corrige la hora actual del plan
    "load_sensor": "",  # consumo base YA SIN carga de baterias (p.ej. "consumo_instantaneo"); + solar + descarga de baterias = consumo real
    "general": {
        "horizon_hours": 30,
        "cycle_seconds": 60,
        "pv_refresh_seconds": 1800,
        "dry_run": True,
        "history_days_for_load": 21,
        "contracted_power_w": 0,
    },
}

DEFAULT_PV_ARRAY = {
    "mode": "entity",          # "entity" | "forecast_solar_api"
    "name": "",
    "entity_id": "",
    "api_key": "",
    "lat": 0.0,
    "lon": 0.0,
    "declination": 30,
    "azimuth": 0,
    "kwp": 1.0,
}


def load_config() -> dict:
    with _lock:
        if not os.path.exists(CONFIG_PATH):
            save_config(DEFAULT_CONFIG)
            return json.loads(json.dumps(DEFAULT_CONFIG))
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        # completar claves que falten (por si se actualiza el esquema)
        merged = json.loads(json.dumps(DEFAULT_CONFIG))
        _deep_merge(merged, cfg)
        return merged


def save_config(cfg: dict) -> None:
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with _lock:
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)


def _deep_merge(base: dict, override: dict) -> None:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def add_battery(cfg: dict, battery: dict) -> dict:
    battery = dict(battery)
    battery["id"] = battery.get("id") or str(uuid.uuid4())[:8]
    cfg["batteries"].append(battery)
    save_config(cfg)
    return battery


def update_battery(cfg: dict, battery_id: str, updates: dict) -> dict | None:
    for b in cfg["batteries"]:
        if b["id"] == battery_id:
            b.update(updates)
            save_config(cfg)
            return b
    return None


def delete_battery(cfg: dict, battery_id: str) -> bool:
    before = len(cfg["batteries"])
    cfg["batteries"] = [b for b in cfg["batteries"] if b["id"] != battery_id]
    save_config(cfg)
    return len(cfg["batteries"]) < before


def add_pv_array(cfg: dict, array: dict) -> dict:
    merged = dict(DEFAULT_PV_ARRAY)
    merged.update(array)
    merged["id"] = merged.get("id") or str(uuid.uuid4())[:8]
    cfg["pv_arrays"].append(merged)
    save_config(cfg)
    return merged


def update_pv_array(cfg: dict, array_id: str, updates: dict) -> dict | None:
    for a in cfg["pv_arrays"]:
        if a["id"] == array_id:
            a.update(updates)
            save_config(cfg)
            return a
    return None


def delete_pv_array(cfg: dict, array_id: str) -> bool:
    before = len(cfg["pv_arrays"])
    cfg["pv_arrays"] = [a for a in cfg["pv_arrays"] if a["id"] != array_id]
    save_config(cfg)
    return len(cfg["pv_arrays"]) < before

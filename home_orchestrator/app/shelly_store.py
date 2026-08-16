"""
Persistencia de los dispositivos Shelly dados de alta -- mismo patron que
govee_store.py/tplink_store.py: namespace propio ("plugins.shelly") en el
fichero de config compartido del nucleo. Sin seccion de cuenta -- la API
local de Shelly no exige credencial por defecto (Gen2 admite auth
opcional propia por dispositivo, no soportada todavia aqui -- ver
docstring de shelly/device_manager.py).
"""

from __future__ import annotations

import threading
import uuid

import config_store

PLUGIN_KEY = "shelly"

_lock = threading.RLock()

DEFAULT_DEVICE_CONFIG = {
    "name": "",
    "host": "",
    "expose_mqtt": False,
}


def _read_section() -> dict:
    raw = config_store._read_raw() or {}
    if not isinstance(raw.get("plugins"), dict):
        return {"devices": []}
    section = raw["plugins"].get(PLUGIN_KEY)
    return section if isinstance(section, dict) else {"devices": []}


def _write_section(section: dict) -> None:
    with _lock:
        raw = config_store._read_raw()
        if not isinstance(raw, dict) or not isinstance(raw.get("plugins"), dict):
            raw = {"schema_version": config_store.SCHEMA_ROOT_VERSION, "core": {}, "plugins": {}}
        raw.setdefault("plugins", {})[PLUGIN_KEY] = section
        raw["schema_version"] = config_store.SCHEMA_ROOT_VERSION
        config_store._write_raw(raw)


def load_devices() -> list[dict]:
    with _lock:
        section = _read_section()
        devices = section.get("devices") or []
        for d in devices:
            merged = dict(DEFAULT_DEVICE_CONFIG)
            merged.update(d.get("config") or {})
            d["config"] = merged
        return devices


def save_devices(devices: list[dict]) -> None:
    with _lock:
        section = _read_section()
        section["devices"] = devices
        _write_section(section)


def add_device(config: dict) -> dict:
    with _lock:
        devices = load_devices()
        merged = dict(DEFAULT_DEVICE_CONFIG)
        merged.update(config)
        device = {"id": str(uuid.uuid4())[:8], "config": merged}
        devices.append(device)
        save_devices(devices)
        return device


def update_device(device_id: str, config: dict) -> dict | None:
    with _lock:
        devices = load_devices()
        for d in devices:
            if d["id"] == device_id:
                d["config"].update(config)
                save_devices(devices)
                return d
        return None


def delete_device(device_id: str) -> bool:
    with _lock:
        devices = load_devices()
        before = len(devices)
        devices = [d for d in devices if d["id"] != device_id]
        save_devices(devices)
        return len(devices) < before

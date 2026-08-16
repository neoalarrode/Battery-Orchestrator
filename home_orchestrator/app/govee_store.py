"""
Persistencia de los dispositivos Govee dados de alta -- mismo patron que
tplink_store.py: namespace propio ("plugins.govee") en el fichero de
config compartido del nucleo. Sin seccion de cuenta -- el protocolo LAN
de Govee no usa ninguna credencial (a diferencia de TP-Link/Tapo), solo
la IP del dispositivo en la LAN.
"""

from __future__ import annotations

import threading
import uuid

import config_store

PLUGIN_KEY = "govee"

_lock = threading.RLock()

DEFAULT_DEVICE_CONFIG = {
    "name": "",
    "host": "",
    # Ingestion interna por Lighting y exposicion por MQTT NO son
    # excluyentes -- mismo criterio que `expose_mqtt` de Tuya/TP-Link.
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

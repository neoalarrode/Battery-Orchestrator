"""
Persistencia de los dispositivos Tuya dados de alta -- mismo patron que
climate/zone_store.py: namespace propio ("plugins.tuya") en el fichero de
config compartido del nucleo, nunca pisa la seccion de otro plugin.
"""

from __future__ import annotations

import threading
import uuid

import config_store

PLUGIN_KEY = "tuya"

_lock = threading.RLock()

DEFAULT_DEVICE_CONFIG = {
    "name": "",
    "device_id": "",
    "address": "",
    "local_key": "",
    "protocol_version": "3.3",
    "profile_yaml": "",
    # Ingestion interna por Climate y exposicion por MQTT NO son
    # excluyentes -- un mismo dispositivo puede estar a la vez consumido
    # por una zona y visible/controlable desde HA. `expose_mqtt` es solo
    # eso: si SE PUBLICA en HA, no si Climate puede usarlo (eso ya lo
    # decide la propia zona, referenciando este device_id).
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
        _write_section({"devices": devices})


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


# ------------------------------------------------------ cuenta Tuya Cloud -
# Solo se usa para VINCULAR (traer local_key + esquema de cada dispositivo
# al darlo de alta desde "Detectados", ver tuya_plugin.py) -- nunca en
# operacion normal, que sigue siendo 100% LAN. access_secret se guarda en
# el mismo config.json que ya guarda las credenciales EcoFlow de Energy --
# mismo nivel de sensibilidad, mismo sitio.
DEFAULT_ACCOUNT = {"region": "eu", "access_id": "", "access_secret": "", "uid": ""}


def load_account() -> dict:
    with _lock:
        section = _read_section()
        merged = dict(DEFAULT_ACCOUNT)
        merged.update(section.get("account") or {})
        return merged


def save_account(account: dict) -> None:
    with _lock:
        section = _read_section()
        merged = dict(DEFAULT_ACCOUNT)
        merged.update(section.get("account") or {})
        merged.update(account)
        section["account"] = merged
        _write_section(section)

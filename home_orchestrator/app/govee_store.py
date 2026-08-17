"""
Persistencia de los dispositivos Govee dados de alta -- mismo patron que
tplink_store.py: namespace propio ("plugins.govee") en el fichero de
config compartido del nucleo. Sin seccion de cuenta -- el protocolo LAN
de Govee no usa ninguna credencial (a diferencia de TP-Link/Tapo), solo
la IP del dispositivo en la LAN.
"""

from __future__ import annotations

import uuid

import config_store

PLUGIN_KEY = "govee"


DEFAULT_DEVICE_CONFIG = {
    "name": "",
    "host": "",
    # Ingestion interna por Lighting y exposicion por MQTT NO son
    # excluyentes -- mismo criterio que `expose_mqtt` de Tuya/TP-Link.
    "expose_mqtt": False,
}


def _read_section() -> dict:
    return config_store.read_plugin_section(PLUGIN_KEY, {"devices": []})


def _write_section(section: dict) -> None:
    # Ambos delegan ya en config_store, que hace el read-modify-write completo
    # bajo SU lock -- antes cada store usaba un lock PROPIO distinto, con lo
    # que una escritura de otro plugin colada entre la lectura y la escritura de
    # aqui se perdia en silencio. Y el camino de "formato no reconocido"
    # reemplazaba el documento por uno vacio, tirando la config entera cuando el
    # fichero estaba en el formato plano antiguo (ver config_store._as_namespaced).
    config_store.update_plugin_section(PLUGIN_KEY, section)


def load_devices() -> list[dict]:
    with config_store.transaction():
        section = _read_section()
        devices = section.get("devices") or []
        for d in devices:
            merged = dict(DEFAULT_DEVICE_CONFIG)
            merged.update(d.get("config") or {})
            d["config"] = merged
        return devices


def save_devices(devices: list[dict]) -> None:
    with config_store.transaction():
        section = _read_section()
        section["devices"] = devices
        _write_section(section)


def add_device(config: dict) -> dict:
    with config_store.transaction():
        devices = load_devices()
        merged = dict(DEFAULT_DEVICE_CONFIG)
        merged.update(config)
        device = {"id": str(uuid.uuid4())[:8], "config": merged}
        devices.append(device)
        save_devices(devices)
        return device


def update_device(device_id: str, config: dict) -> dict | None:
    with config_store.transaction():
        devices = load_devices()
        for d in devices:
            if d["id"] == device_id:
                d["config"].update(config)
                save_devices(devices)
                return d
        return None


def delete_device(device_id: str) -> bool:
    with config_store.transaction():
        devices = load_devices()
        before = len(devices)
        devices = [d for d in devices if d["id"] != device_id]
        save_devices(devices)
        return len(devices) < before

"""
Persistencia de los dispositivos TP-Link dados de alta -- mismo patron
que tuya/tuya_store.py: namespace propio ("plugins.tplink") en el
fichero de config compartido del nucleo, nunca pisa la seccion de otro
plugin.

A diferencia de Tuya (un `local_key` POR DISPOSITIVO), TP-Link usa una
UNICA cuenta compartida para todos los dispositivos Tapo/KLAP de la
vivienda -- exactamente el mismo modelo que usa Home Assistant (ver
`get_credentials(hass)` en su `config_flow.py`: una credencial de cuenta
para toda la integracion, no una por dispositivo). Los Kasa clasicos
(HS1xx/KP1xx, protocolo local sin cifrar) no necesitan credencial
ninguna -- `username`/`password` vacios sencillamente no se usan para
esos.
"""

from __future__ import annotations

import threading
import uuid

import config_store

PLUGIN_KEY = "tplink"

_lock = threading.RLock()

DEFAULT_DEVICE_CONFIG = {
    "name": "",
    "host": "",
    # Ingestion interna por Lighting y exposicion por MQTT NO son
    # excluyentes -- mismo criterio que `expose_mqtt` de Tuya.
    "expose_mqtt": False,
}

DEFAULT_ACCOUNT = {"username": "", "password": ""}


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
    # Lee la seccion actual y solo reemplaza "devices" -- NUNCA
    # sobrescribe la seccion entera, para no borrar "account" de paso
    # (bug real que ya se dio en Tuya con este mismo patron, ver el
    # comentario de tuya_store.py:save_devices -- corregido aqui desde
    # el principio).
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


# --------------------------------------------------- cuenta TP-Link/Tapo -
# Username/password de la cuenta TP-Link -- necesarios SOLO para el
# handshake local KLAP de dispositivos Tapo/Kasa nuevos (ver
# tplink/device_manager.py); un Kasa clasico (HS1xx/KP1xx) ignora esto
# por completo. Mismo nivel de sensibilidad que el local_key de Tuya o
# las credenciales EcoFlow de Energy -- mismo fichero, mismo sitio.

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

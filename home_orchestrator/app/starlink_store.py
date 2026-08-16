"""
Persistencia minima del plugin Starlink -- mismo patron que tplink_store.py
(namespace propio "plugins.starlink" en el fichero de config compartido
del nucleo). Un unico valor por ahora: la IP del router Starlink cuando
la direccion por defecto (192.168.1.1, ver core/routerEndpoint.ts del
proyecto original) no se alcanza -- coincide muy probablemente con la
del propio router de esta instalacion (misma LAN 192.168.1.0/24), ver
docstring de starlink_plugin.py.
"""

from __future__ import annotations

import threading

import config_store

PLUGIN_KEY = "starlink"

_lock = threading.RLock()

DEFAULT_CONFIG = {"router_ip": ""}


def _read_section() -> dict:
    raw = config_store._read_raw() or {}
    if not isinstance(raw.get("plugins"), dict):
        return dict(DEFAULT_CONFIG)
    section = raw["plugins"].get(PLUGIN_KEY)
    if not isinstance(section, dict):
        return dict(DEFAULT_CONFIG)
    merged = dict(DEFAULT_CONFIG)
    merged.update(section)
    return merged


def _write_section(section: dict) -> None:
    with _lock:
        raw = config_store._read_raw()
        if not isinstance(raw, dict) or not isinstance(raw.get("plugins"), dict):
            raw = {"schema_version": config_store.SCHEMA_ROOT_VERSION, "core": {}, "plugins": {}}
        raw.setdefault("plugins", {})[PLUGIN_KEY] = section
        raw["schema_version"] = config_store.SCHEMA_ROOT_VERSION
        config_store._write_raw(raw)


def load_config() -> dict:
    with _lock:
        return _read_section()


def save_router_ip(router_ip: str) -> dict:
    with _lock:
        section = _read_section()
        section["router_ip"] = (router_ip or "").strip()
        _write_section(section)
        return section

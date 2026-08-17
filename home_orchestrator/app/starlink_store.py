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

import config_store

PLUGIN_KEY = "starlink"


DEFAULT_CONFIG = {"router_ip": ""}


def _read_section() -> dict:
    merged = dict(DEFAULT_CONFIG)
    merged.update(config_store.read_plugin_section(PLUGIN_KEY, DEFAULT_CONFIG))
    return merged


def _write_section(section: dict) -> None:
    # Ver comentario homologo en govee_store: el read-modify-write completo se
    # hace ahora dentro de config_store, bajo el mismo lock que el resto de
    # escritores, y el formato plano antiguo se migra en vez de descartarse.
    config_store.update_plugin_section(PLUGIN_KEY, section)


def load_config() -> dict:
    with config_store.transaction():
        return _read_section()


def save_router_ip(router_ip: str) -> dict:
    with config_store.transaction():
        section = _read_section()
        section["router_ip"] = (router_ip or "").strip()
        _write_section(section)
        return section

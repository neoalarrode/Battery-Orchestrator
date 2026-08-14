"""
Cargador de plugins del nucleo Home Orchestrator.

De momento SOLO carga plugins de primera parte, registrados aqui mismo en
el codigo (`_BUILTIN_PLUGINS`) -- todavia NO descarga nada dinamicamente
desde `plugins.json` (el registro oficial en la raiz del repo). Antes de
construir la descarga real hace falta decidir como se verifica cada
plugin (version + checksum pinneados, como minimo) para no abrir la
puerta a ejecutar codigo sin verificar dentro de un proceso que ya tiene
credenciales reales (EcoFlow, HA, y ahora mismo tambien acceso SSH al
host durante el desarrollo). Cargar solo plugins de primera parte, ya
incluidos en este mismo repo, evita ese riesgo mientras tanto -- es la
base sobre la que construir la descarga real en una fase posterior, no el
diseño final.
"""

from __future__ import annotations

import logging

log = logging.getLogger("plugin_loader")

# Slug -> constructor. Registro de plugins de primera parte disponibles en
# este mismo repo (ver plugins.json en la raiz para la version/descripcion
# de cada uno) -- cuales de ellos se instancian de verdad lo decide
# config_store.get_installed_plugins() (seccion "core", ver tienda de
# plugins en la interfaz), no esta lista, que es solo el catalogo de lo
# que EXISTE en el codigo.
PLUGIN_REGISTRY = {}


def _battery():
    from battery_plugin import BatteryPlugin
    return BatteryPlugin()


def _climate():
    from climate_plugin import ClimatePlugin
    return ClimatePlugin()


PLUGIN_REGISTRY = {"battery": _battery, "climate": _climate}

# Metadatos para la tienda de plugins de la interfaz -- deliberadamente
# SIN instanciar nada (un plugin no instalado no debe crear su app Flask
# ni abrir ninguna conexion solo para aparecer listado). Mantener en
# sincronia a mano con plugins.json (raiz del repo) y con el `version` de
# cada Plugin -- mismo criterio que ya se sigue con el resto de números de
# versión duplicados (config.yaml, battery_plugin.py...).
PLUGIN_CATALOG = {
    "battery": {
        "name": "Energy Orchestrator",
        "description": "Baterías, solar y cargas diferibles — carga y descarga adaptativa por precio, sol y consumo real",
        "version": "0.11.62",
    },
    "climate": {
        "name": "Climate Orchestrator",
        "description": "Termostatos adaptativos por zona, expuestos como climate.* nativos de HA (HomeKit/Matter) vía MQTT Discovery",
        "version": "0.2.0",
    },
}


def list_catalog() -> list[dict]:
    import config_store

    installed = set(config_store.get_installed_plugins()) | REQUIRED_PLUGINS
    return [
        {
            "slug": slug,
            "name": meta["name"],
            "description": meta["description"],
            "version": meta["version"],
            "installed": slug in installed,
            "required": slug in REQUIRED_PLUGINS,
        }
        for slug, meta in PLUGIN_CATALOG.items()
    ]

# "battery" sirve la app raiz (ver core_app.py, ingress_port de
# config.yaml ya apunta ahi) -- quitarlo dejaria el nucleo sin nada que
# servir en "/". Se fuerza siempre instalado, la tienda no ofrece
# desinstalarlo (ver plugin-store en el frontend).
REQUIRED_PLUGINS = {"battery"}


def load_all_plugins() -> list:
    import config_store

    installed = set(config_store.get_installed_plugins()) | REQUIRED_PLUGINS
    plugins = []
    for slug, factory in PLUGIN_REGISTRY.items():
        if slug not in installed:
            log.info("Plugin '%s' no instalado -- no se carga", slug)
            continue
        plugins.append(factory())

    for p in plugins:
        log.info("Plugin cargado: %s v%s (%s)", p.name, p.version, p.slug)
    return plugins

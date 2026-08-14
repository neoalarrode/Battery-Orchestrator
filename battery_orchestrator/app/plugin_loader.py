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


def load_all_plugins() -> list:
    from battery_plugin import BatteryPlugin
    from climate_plugin import ClimatePlugin

    plugins = [BatteryPlugin(), ClimatePlugin()]
    for p in plugins:
        log.info("Plugin cargado: %s v%s (%s)", p.name, p.version, p.slug)
    return plugins

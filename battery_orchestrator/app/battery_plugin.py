"""
Envoltorio fino de Battery como plugin del nucleo Home Orchestrator.

Deliberadamente NO mueve ninguno de los modulos existentes (ha_client.py,
scheduler.py, battery_exec.py, ecoflow_*.py, config_store.py...) -- siguen
donde estaban, con sus imports de siempre, funcionando exactamente igual
que antes de esta fase. Este fichero solo expone `main.py` (que ya
concentraba toda la app Flask + el arranque de hilos) a traves del
contrato `Plugin`, para que el nucleo pueda cargarlo sin tener que conocer
ningun detalle interno de Battery. Riesgo de regresion: minimo -- es una
fachada sobre codigo que ya esta en produccion, no una reescritura.
"""

from __future__ import annotations

import main as battery_main
from plugin_base import Plugin


class BatteryPlugin(Plugin):
    slug = "battery"
    name = "Energy Orchestrator"
    version = "0.11.72"
    serves_root = True

    def flask_app(self):
        return battery_main.app

    def start_background_threads(self) -> None:
        battery_main.start_background_threads()

"""
Punto de entrada del nucleo Home Orchestrator (`run.sh` llama a este
fichero en vez de a `main.py` directamente, desde esta version).

Con un solo plugin instalado (Battery, el caso de hoy), el nucleo sirve
la app Flask de ese plugin tal cual -- fusionar varias apps Flask como
blueprints bajo una app compartida del nucleo es una decision de diseño
que no hace falta resolver todavia con un unico plugin real; se aborda
cuando Climate (u otro) sea el segundo plugin de verdad.
"""

from __future__ import annotations

import logging

import plugin_loader

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("core")


def main() -> None:
    plugins = plugin_loader.load_all_plugins()
    if not plugins:
        raise RuntimeError("No hay ningun plugin cargado -- nada que arrancar.")

    for p in plugins:
        p.start_background_threads()

    # Fase 1: exactamente un plugin (Battery). Se sirve su app Flask
    # directa, en el mismo puerto de siempre (8099, coherente con
    # ingress_port en config.yaml).
    primary = plugins[0]
    log.info("Home Orchestrator arrancando con el plugin '%s' v%s", primary.name, primary.version)
    primary.flask_app().run(host="0.0.0.0", port=8099, threaded=True)


if __name__ == "__main__":
    main()

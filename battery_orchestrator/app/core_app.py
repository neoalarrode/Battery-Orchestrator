"""
Punto de entrada del nucleo Home Orchestrator (`run.sh` llama a este
fichero en vez de a `main.py` directamente, desde esta version).

Con dos plugins instalados (Battery + Climate, desde esta version), el
nucleo fusiona sus apps Flask con `DispatcherMiddleware` de werkzeug:
Battery se sirve en la raiz (compatibilidad con `ingress_port` en
config.yaml, que ya apuntaba ahi antes de que existiera ningun otro
plugin) y cada plugin siguiente se monta bajo `/plugins/<slug>`.
"""

from __future__ import annotations

import logging

from werkzeug.middleware.dispatcher import DispatcherMiddleware
from werkzeug.serving import run_simple

import plugin_loader

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("core")


def main() -> None:
    plugins = plugin_loader.load_all_plugins()
    if not plugins:
        raise RuntimeError("No hay ningun plugin cargado -- nada que arrancar.")

    for p in plugins:
        p.start_background_threads()

    # "battery" siempre va en la raiz (ver plugin_loader.REQUIRED_PLUGINS) --
    # se busca por slug, no por posicion, para no depender del orden en que
    # plugin_loader haya devuelto la lista.
    primary = next(p for p in plugins if p.slug == "battery")
    rest = [p for p in plugins if p is not primary]
    log.info("Home Orchestrator arrancando con el plugin '%s' v%s en la raiz", primary.name, primary.version)

    mounts = {}
    for p in rest:
        log.info("Plugin '%s' v%s montado en /plugins/%s", p.name, p.version, p.slug)
        mounts[f"/plugins/{p.slug}"] = p.flask_app().wsgi_app

    app = primary.flask_app()
    if mounts:
        app.wsgi_app = DispatcherMiddleware(app.wsgi_app, mounts)

    run_simple("0.0.0.0", 8099, app, threaded=True)


if __name__ == "__main__":
    main()

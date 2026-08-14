"""
Punto de entrada del nucleo Home Orchestrator (`run.sh` llama a este
fichero en vez de a `main.py` directamente, desde esta version).

Quien sirve la raiz "/" depende de que este instalado, no de una
suposicion fija:
  - Si algun plugin instalado declara `serves_root = True` (hoy solo
    Energy) -- se sirve el SUYO en la raiz, con la tienda de plugins y la
    copia de seguridad (`core_shell.core_api_bp`) registradas DIRECTAMENTE
    sobre su misma app (mismo origen de siempre, cero cambio para quien ya
    tuviera Energy instalado).
  - Si NO hay ninguno (instalacion recien nacida, o Energy desinstalado)
    -- se sirve el catalogo/tienda del propio nucleo (`core_shell.
    build_shell_app()`) en la raiz, para que SIEMPRE haya algo que
    mostrar, nunca una pagina en blanco o un error.

El resto de plugins instalados (los que no sirven la raiz) se montan bajo
`/plugins/<slug>` con `DispatcherMiddleware`, igual que hasta ahora.
"""

from __future__ import annotations

import logging

from werkzeug.middleware.dispatcher import DispatcherMiddleware
from werkzeug.serving import run_simple

import core_shell
import plugin_loader

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("core")


def main() -> None:
    plugins = plugin_loader.load_all_plugins()

    for p in plugins:
        p.start_background_threads()

    primary = next((p for p in plugins if getattr(p, "serves_root", False)), None)
    rest = [p for p in plugins if p is not primary]

    if primary is not None:
        log.info("Home Orchestrator arrancando con el plugin '%s' v%s en la raiz", primary.name, primary.version)
        root_app = primary.flask_app()
        root_app.register_blueprint(core_shell.core_api_bp)
    else:
        log.info("Home Orchestrator arrancando SIN ningun plugin que sirva la raiz -- catalogo/tienda del nucleo")
        root_app = core_shell.build_shell_app()

    mounts = {}
    for p in rest:
        log.info("Plugin '%s' v%s montado en /plugins/%s", p.name, p.version, p.slug)
        mounts[f"/plugins/{p.slug}"] = p.flask_app().wsgi_app

    if mounts:
        root_app.wsgi_app = DispatcherMiddleware(root_app.wsgi_app, mounts)

    run_simple("0.0.0.0", 8099, root_app, threaded=True)


if __name__ == "__main__":
    main()

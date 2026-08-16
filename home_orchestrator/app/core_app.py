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
    by_slug = {p.slug: p for p in plugins}

    # Cualquier plugin cargado que ofrezca `climate_handle` (Tuya hoy,
    # otra marca mañana) se registra solo en Climate como proveedor de
    # actuadores -- no hace falta tocar este fichero ni climate_plugin.py
    # cuando llegue un tercero; basta con que el plugin nuevo exponga el
    # mismo contrato (ver TuyaPlugin.climate_handle/list_climate_actuators).
    climate_plugin = by_slug.get("climate")
    if climate_plugin is not None:
        for p in plugins:
            if p is climate_plugin or not hasattr(p, "climate_handle"):
                continue
            climate_plugin.register_actuator_provider(p.slug, p)

    # Mismo mecanismo, ahora para Lighting -- cualquier plugin cargado
    # que ofrezca `light_handle` (Tuya hoy, otra marca mañana) se
    # registra en Lighting sin que este fichero necesite conocer nada
    # especifico de esa marca.
    lighting_plugin = by_slug.get("lighting")
    if lighting_plugin is not None:
        for p in plugins:
            if p is lighting_plugin or not hasattr(p, "light_handle"):
                continue
            lighting_plugin.register_actuator_provider(p.slug, p)

    primary = next((p for p in plugins if getattr(p, "serves_root", False)), None)
    rest = [p for p in plugins if p is not primary]

    if primary is not None:
        log.info("Home Orchestrator arrancando con el plugin '%s' v%s en la raiz", primary.name, primary.version)
        root_app = primary.flask_app()
        root_app.register_blueprint(core_shell.core_api_bp)
        root_app.register_blueprint(core_shell.core_static_bp)
    else:
        log.info("Home Orchestrator arrancando SIN ningun plugin que sirva la raiz -- catalogo/tienda del nucleo")
        root_app = core_shell.build_shell_app()

    mounts = {}
    for p in rest:
        log.info("Plugin '%s' v%s montado en /plugins/%s", p.name, p.version, p.slug)
        mounts[f"/plugins/{p.slug}"] = p.flask_app().wsgi_app

    if mounts:
        root_app.wsgi_app = DispatcherMiddleware(root_app.wsgi_app, mounts)

    # BUG REAL, confirmado en produccion (crash-loop entero del addon):
    # `start_background_threads()` de Battery arranca ademas un SEGUNDO
    # servidor HTTP de verdad (el "wallpanel" de solo lectura, puerto
    # 8098, ver `_run_wallpanel_server` en main.py) sirviendo el MISMO
    # objeto Flask que un momento despues se convierte en `root_app`. Si
    # esto se llamaba ANTES de `register_blueprint` (como estaba aqui) y
    # una peticion cualquiera llegaba al wallpanel en ese hueco --
    # bastante mas probable cuantos mas plugins haya que cargar antes de
    # llegar aqui, ver el numero creciente de plugins de este addon --
    # Flask marca el app como "ya sirvio su primera peticion" y
    # `register_blueprint` revienta con `AssertionError`, tirando el
    # proceso entero abajo en un bucle de reinicio infinito. Arrancar los
    # hilos de fondo (wallpanel incluido) SOLO cuando el blueprint del
    # nucleo y el montaje de plugins ya estan completos elimina la
    # ventana de carrera por completo -- ninguna peticion puede llegar a
    # nada antes de que el app este totalmente armado.
    # BUG REAL, confirmado en produccion (crash-loop ENTERO del addon):
    # sin este try/except, un fallo en el arranque de hilos de fondo de
    # UN SOLO plugin (visto tal cual: `GoveeDeviceManager.start()` con
    # `OSError: [Errno 98] Address in use` al enlazar el puerto UDP 4002
    # -- otro proceso del host ya lo tenia tomado) tira el proceso ENTERO
    # abajo, con el, Energy/Climate/Lighting y el resto de plugins que sí
    # habian arrancado bien un instante antes -- un bucle de reinicio
    # infinito que nunca se recupera solo (el puerto sigue ocupado en el
    # siguiente intento). Mismo criterio de resiliencia que
    # `plugin_loader.load_all_plugins()` ya aplica a la CARGA de un
    # plugin ("se omite, el resto del nucleo sigue arrancando") --
    # faltaba aplicarlo tambien al ARRANQUE de sus hilos de fondo.
    for p in plugins:
        try:
            p.start_background_threads()
        except Exception:
            log.exception(
                "Plugin '%s' fallo arrancando sus hilos de fondo -- se omite, el resto "
                "del nucleo sigue arrancando. Puede quedarse sin funcionar hasta que se "
                "resuelva la causa (revisar los logs de arriba).",
                p.slug,
            )

    run_simple("0.0.0.0", 8099, root_app, threaded=True)


if __name__ == "__main__":
    main()

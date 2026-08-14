"""
Cargador de plugins del nucleo Home Orchestrator.

"battery" (Energy Orchestrator) es el nucleo -- sirve la app en la raiz
(ver core_app.py, `ingress_port` de config.yaml ya apunta ahi) y viene
siempre precargado en la imagen del addon; no tiene sentido "descargarlo"
por separado del propio nucleo que lo carga. El resto de plugins
(Climate, y los que vengan despues) SI son descargables de verdad: ver
`plugin_downloader.py` para el mecanismo (tag de git + sha256 pineados
aqui mismo, verificado antes de tocar disco) y `install_plugin()` mas
abajo, que es lo que llama la tienda de plugins de la interfaz.

Cuales de los plugins REGISTRADOS (existen en el codigo, descargados o
precargados) se instancian de verdad al arrancar lo decide
`config_store.get_installed_plugins()` (seccion "core") -- no el
registro en si, que es solo el catalogo de lo que EXISTE.
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger("plugin_loader")

# "battery" sirve la app raiz -- quitarlo dejaria el nucleo sin nada que
# servir en "/". Se fuerza siempre instalado; la tienda no ofrece
# desinstalarlo ni descargarlo (ya viene con el nucleo).
REQUIRED_PLUGINS = {"battery"}


def _prefer_downloaded(slug: str) -> None:
    """Si hay una version descargada de verdad de este plugin (ver
    plugin_downloader.py), se antepone a sys.path para que gane sobre
    cualquier copia que venga precargada en la imagen -- 'instalar desde
    la tienda' tiene que notarse de verdad, no ser un boton decorativo."""
    import plugin_downloader

    path = plugin_downloader.current_path(slug)
    if path and path not in sys.path:
        sys.path.insert(0, path)
        log.info("Plugin '%s': usando version descargada en %s", slug, path)


def _battery():
    from battery_plugin import BatteryPlugin
    return BatteryPlugin()


def _climate():
    _prefer_downloaded("climate")
    from climate_plugin import ClimatePlugin
    return ClimatePlugin()


# Slug -> constructor.
PLUGIN_REGISTRY = {"battery": _battery, "climate": _climate}

# Metadatos + procedencia verificada para la tienda de plugins de la
# interfaz -- deliberadamente SIN instanciar nada aqui (un plugin no
# instalado no debe crear su app Flask ni abrir ninguna conexion solo por
# aparecer listado). "tag"/"sha256" identifican exactamente que version
# de que tag del repo se descarga al instalar (ver plugin_downloader.py);
# "files" son las rutas (relativas a battery_orchestrator/app/ dentro del
# tarball de ese tag) que pertenecen a este plugin en concreto. Mantener
# todo esto en sincronia a mano con plugins.json (raiz del repo) y con el
# `version` de cada Plugin -- mismo criterio que el resto de números de
# version duplicados en este repo (config.yaml, battery_plugin.py...).
# "battery" no lleva tag/sha256/files: viene con el nucleo, no se descarga.
PLUGIN_CATALOG = {
    "battery": {
        "name": "Energy Orchestrator",
        "description": "Baterías, solar y cargas diferibles — carga y descarga adaptativa por precio, sol y consumo real",
        "version": "0.11.67",
        "downloadable": False,
    },
    "climate": {
        "name": "Climate Orchestrator",
        "description": "Termostatos adaptativos por zona, expuestos como climate.* nativos de HA (HomeKit/Matter) vía MQTT Discovery",
        "version": "0.2.0",  # version PROPIA del plugin (ClimatePlugin.version) -- distinta de "tag", que es la version del REPO de la que se descarga
        "downloadable": True,
        "tag": "v0.11.63",
        "sha256": "a1c57e3d7fc08156a084f6d9b5fe0cfc758f399572d7c74d4463c0ba2b021e60",
        "files": ["climate_plugin.py", "climate", "climate_templates"],
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
            "downloadable": meta.get("downloadable", False),
        }
        for slug, meta in PLUGIN_CATALOG.items()
    ]


def install_plugin(slug: str) -> None:
    """Descarga (si el plugin es descargable) y marca instalado. Lanza
    `plugin_downloader.PluginDownloadError` si la descarga o la
    verificacion fallan -- en ese caso no se marca nada como instalado."""
    import config_store

    meta = PLUGIN_CATALOG.get(slug)
    if meta is None:
        raise KeyError(f"plugin desconocido: {slug}")

    if meta.get("downloadable"):
        import plugin_downloader
        plugin_downloader.download_plugin(slug, meta["tag"], meta["sha256"], meta["files"])

    config_store.set_plugin_installed(slug, True)


def uninstall_plugin(slug: str, purge_files: bool = False) -> None:
    import config_store

    if slug in REQUIRED_PLUGINS:
        raise ValueError("este plugin es el nucleo, no se puede quitar")
    config_store.set_plugin_installed(slug, False)
    if purge_files and PLUGIN_CATALOG.get(slug, {}).get("downloadable"):
        import plugin_downloader
        plugin_downloader.remove_plugin_files(slug)


def load_all_plugins() -> list:
    """Un plugin OPCIONAL que falle al cargar (codigo no encontrado, error
    de importacion...) nunca debe tirar abajo el nucleo entero -- se
    registra el error y se sigue sin el, en vez de propagar la excepcion.
    Un REQUIRED_PLUGINS que falle si que revienta el arranque: sin el
    nucleo (Energy/"battery") no hay nada que servir en la raiz, no tiene
    sentido seguir a medias."""
    import config_store

    installed = set(config_store.get_installed_plugins()) | REQUIRED_PLUGINS
    plugins = []
    for slug, factory in PLUGIN_REGISTRY.items():
        if slug not in installed:
            log.info("Plugin '%s' no instalado -- no se carga", slug)
            continue
        try:
            plugins.append(factory())
        except Exception:
            if slug in REQUIRED_PLUGINS:
                raise
            log.exception(
                "Plugin '%s' estaba marcado como instalado pero fallo al cargar -- "
                "se omite, el resto del nucleo sigue arrancando. Puede que su codigo "
                "descargado no este disponible; reinstalalo desde la tienda.",
                slug,
            )

    for p in plugins:
        log.info("Plugin cargado: %s v%s (%s)", p.name, p.version, p.slug)
    return plugins

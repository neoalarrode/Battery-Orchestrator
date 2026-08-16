"""
Cargador de plugins del nucleo Home Orchestrator.

Ningun plugin viene obligado -- ni siquiera Energy. Una instalacion
recien nacida (o con todo desinstalado) simplemente sirve el catalogo del
propio nucleo en la raiz (ver core_shell.py) hasta que se instale algo.
Los dos plugins de primera parte de hoy (Energy, Climate) son
descargables de verdad: ver `plugin_downloader.py` para el mecanismo (tag
de git + sha256 pineados aqui mismo, verificado antes de tocar disco) y
`install_plugin()` mas abajo, que es lo que llama la tienda de plugins de
la interfaz.

Cuales de los plugins REGISTRADOS (existen en el codigo, descargados o
precargados) se instancian de verdad al arrancar lo decide
`config_store.get_installed_plugins()` (seccion "core") -- no el registro
en si, que es solo el catalogo de lo que EXISTE.
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger("plugin_loader")


def _prefer_downloaded(slug: str) -> None:
    """Si hay una version descargada de verdad de este plugin (ver
    plugin_downloader.py), se antepone a sys.path para que gane sobre
    cualquier copia que venga precargada en la imagen (si la hay) --
    'instalar desde la tienda' tiene que notarse de verdad, no ser un
    boton decorativo."""
    import plugin_downloader

    path = plugin_downloader.current_path(slug)
    if path and path not in sys.path:
        sys.path.insert(0, path)
        log.info("Plugin '%s': usando version descargada en %s", slug, path)


def _battery():
    _prefer_downloaded("battery")
    from battery_plugin import BatteryPlugin
    return BatteryPlugin()


def _climate():
    _prefer_downloaded("climate")
    from climate_plugin import ClimatePlugin
    return ClimatePlugin()


def _tuya():
    _prefer_downloaded("tuya")
    from tuya_plugin import TuyaPlugin
    return TuyaPlugin()


def _lighting():
    _prefer_downloaded("lighting")
    from lighting_plugin import LightingPlugin
    return LightingPlugin()


def _tplink():
    _prefer_downloaded("tplink")
    from tplink_plugin import TplinkPlugin
    return TplinkPlugin()


def _starlink():
    _prefer_downloaded("starlink")
    from starlink_plugin import StarlinkPlugin
    return StarlinkPlugin()


def _govee():
    _prefer_downloaded("govee")
    from govee_plugin import GoveePlugin
    return GoveePlugin()


def _shelly():
    _prefer_downloaded("shelly")
    from shelly_plugin import ShellyPlugin
    return ShellyPlugin()


# Slug -> constructor.
PLUGIN_REGISTRY = {
    "battery": _battery, "climate": _climate, "tuya": _tuya,
    "lighting": _lighting, "tplink": _tplink, "starlink": _starlink,
    "govee": _govee, "shelly": _shelly,
}

# Metadatos + procedencia verificada para la tienda de plugins de la
# interfaz -- deliberadamente SIN instanciar nada aqui (un plugin no
# instalado no debe crear su app Flask ni abrir ninguna conexion solo por
# aparecer listado). "tag"/"sha256" identifican exactamente que version
# de que tag del repo se descarga al instalar (ver plugin_downloader.py);
# "files" son las rutas (relativas a home_orchestrator/app/ dentro del
# tarball de ese tag) que pertenecen a este plugin en concreto. Mantener
# todo esto en sincronia a mano con plugins.json (raiz del repo) y con el
# `version` de cada Plugin -- mismo criterio que el resto de números de
# version duplicados en este repo (config.yaml, battery_plugin.py...).
#
# "tag" puede quedarse apuntando a una version del REPO mas antigua que
# la que se esta publicando ahora mismo, a proposito -- solo se actualiza
# (junto con "sha256", recalculado de verdad contra el tarball real) el
# dia que el CODIGO PROPIO de ese plugin cambie; publicar una version del
# addon que no toca un plugin no obliga a re-pinear su descarga.
PLUGIN_CATALOG = {
    "battery": {
        "name": "Energy Orchestrator",
        "description": "Baterías, solar y cargas diferibles — carga y descarga adaptativa por precio, sol y consumo real",
        "version": "0.11.86",
        "downloadable": True,
        "tag": "v0.43.0",
        "sha256": "0b84a08da4e74b4ba829eeef9b85fea80a68cfea1f788fade54fb74317453391",  # sha256 real del tarball de v0.43.0, verificado contra una descarga real antes de fijarlo aqui (bug de layout en movil: stat-grid + cabecera)
        "files": [
            "main.py", "battery_plugin.py", "battery_exec.py", "anomaly_store.py",
            "capacity_store.py", "climate_link.py", "deferrable_exec.py",
            "deferrable_scheduler.py", "deferrable_store.py", "ecoflow_ble.py",
            "ecoflow_cloud.py", "ecoflow_login.py", "forecast_store.py", "ha_client.py",
            "ha_statistics.py", "history_store.py", "lifetime_store.py", "pv_source.py",
            "savings_store.py", "scheduler.py", "solar_energy_store.py", "tariff_source.py",
            "templates",
        ],
    },
    "climate": {
        "name": "Climate Orchestrator",
        "description": "Termostatos adaptativos por zona, expuestos como climate.* nativos de HA (HomeKit/Matter) vía MQTT Discovery",
        "version": "0.4.9",  # version PROPIA del plugin (ClimatePlugin.version) -- distinta de "tag", que es la version del REPO de la que se descarga
        "downloadable": True,
        "tag": "v0.44.0",
        "sha256": "5d73ed53661c95ba2221bbfab91638707b6a652c453017c51df71ff9d9c31292",  # sha256 real del tarball de v0.44.0, verificado contra una descarga real antes de fijarlo aqui (rediseño de tarjetas de zona)
        "files": ["climate_plugin.py", "climate", "climate_templates"],
    },
    "tuya": {
        "name": "Tuya Orchestrator",
        "description": "Puente de ingesta para dispositivos Tuya-por-LAN — consumo interno por Climate y/o exposición opcional a HA por MQTT",
        "version": "0.4.7",
        "downloadable": True,
        "tag": "v0.39.0",
        "sha256": "7a588ad223aa72553c8982a41889de72f256ec6affaabac6fcd464c016e18e13",  # sha256 real del tarball de v0.39.0, verificado contra una descarga real antes de fijarlo aqui (rediseño: estetica de Dishylink)
        "files": ["tuya_plugin.py", "tuya", "tuya_templates"],
    },
    "lighting": {
        "name": "Lighting Orchestrator",
        "description": "Iluminación adaptativa por zona — color y brillo por hora, encendido/apagado por presencia y reglas condicionales (p.ej. TV encendida -> luces laterales en vez del techo)",
        "version": "0.7.12",
        "downloadable": True,
        "tag": "v0.45.0",
        "sha256": "77d86e8fbd682c4913123846bd85d94c4cca60031ff347ec08592b4522207924",  # sha256 real del tarball de v0.45.0, verificado contra una descarga real antes de fijarlo aqui (fader tipo Apple Home/Hue para brillo/color)
        "files": ["lighting_plugin.py", "lighting", "lighting_templates"],
    },
    "tplink": {
        "name": "TP-Link Orchestrator",
        "description": "Puente de ingesta para dispositivos TP-Link (Kasa/Tapo) vía python-kasa (misma librería que usa Home Assistant) — consumo interno por Lighting y/o exposición opcional a HA por MQTT",
        "version": "0.1.13",
        "downloadable": True,
        "tag": "v0.39.0",
        "sha256": "7a588ad223aa72553c8982a41889de72f256ec6affaabac6fcd464c016e18e13",  # sha256 real del tarball de v0.39.0, verificado contra una descarga real antes de fijarlo aqui (rediseño: estetica de Dishylink)
        "files": ["tplink_plugin.py", "tplink", "tplink_templates", "tplink_store.py"],
    },
    "starlink": {
        "name": "Starlink Orchestrator",
        "description": "Monitorización de tu Starlink (rendimiento, latencia, obstrucción, alineación, consumo) — build oficial de Dishylink, servido tal cual, con un proxy local al dish",
        "version": "0.3.1",
        "downloadable": True,
        "tag": "v0.33.0",
        "sha256": "546a54ad34ee4e34c84be20d3c06490d71afa6c3ba6942f3bbc21648fa26af90",  # sha256 real del tarball de v0.33.0, verificado contra una descarga real antes de fijarlo aqui (renombrado battery_orchestrator -> home_orchestrator, todas las descargas dependen de este tag ahora)
        "files": ["starlink_plugin.py", "starlink_store.py", "starlink_dist", "starlink_node"],
    },
    "govee": {
        "name": "Govee Orchestrator",
        "description": "Puente de ingesta para bombillas Govee — LAN API local del propio dispositivo (sin cuenta ni nube) — consumo interno por Lighting y/o exposición opcional a HA por MQTT",
        "version": "0.1.0",
        "downloadable": True,
        "tag": "v0.46.2",
        "sha256": "8b5d102654f878a7fb0103e501e31d371652f8f46ed87ead2b242fd2d5d01813",  # sha256 real del tarball de v0.46.2, verificado contra una descarga real antes de fijarlo aqui (HOTFIX: OSError sin atrapar tiraba el addon entero abajo)
        "files": ["govee_plugin.py", "govee", "govee_templates", "govee_store.py"],
    },
    "shelly": {
        "name": "Shelly Orchestrator",
        "description": "Puente de ingesta para dispositivos Shelly — API local del propio fabricante (Gen1 HTTP / Gen2+ RPC), sin cuenta ni nube — consumo interno por Lighting y/o exposición opcional a HA por MQTT",
        "version": "0.1.0",
        "downloadable": True,
        "tag": "v0.46.0",
        "sha256": "a27c33deae1236f67549e1510dc47d3cc1114e67f39a0e0aada93758f0f13d76",  # sha256 real del tarball de v0.46.0, verificado contra una descarga real antes de fijarlo aqui (primera version del plugin)
        "files": ["shelly_plugin.py", "shelly", "shelly_templates", "shelly_store.py"],
    },
}


def list_catalog() -> list[dict]:
    import config_store

    installed = set(config_store.get_installed_plugins())
    return [
        {
            "slug": slug,
            "name": meta["name"],
            "description": meta["description"],
            "version": meta["version"],
            "installed": slug in installed,
            "downloadable": meta.get("downloadable", False),
        }
        for slug, meta in PLUGIN_CATALOG.items()
    ]


def install_plugin(slug: str) -> None:
    """Descarga (si el plugin es descargable y su codigo no esta ya
    presente) y marca instalado. Lanza `plugin_downloader.
    PluginDownloadError` si la descarga o la verificacion fallan -- en
    ese caso no se marca nada como instalado."""
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

    config_store.set_plugin_installed(slug, False)
    if purge_files and PLUGIN_CATALOG.get(slug, {}).get("downloadable"):
        import plugin_downloader
        plugin_downloader.remove_plugin_files(slug)


def load_all_plugins() -> list:
    """Ningun plugin es obligatorio -- si uno falla al cargar (codigo no
    encontrado, error de importacion...) se registra el error y se sigue
    sin el. Una instalacion con CERO plugins cargados es un estado valido
    (ver core_app.py: sirve el catalogo del nucleo en ese caso), no un
    fallo de arranque."""
    import config_store

    installed = set(config_store.get_installed_plugins())
    plugins = []
    for slug, factory in PLUGIN_REGISTRY.items():
        if slug not in installed:
            log.info("Plugin '%s' no instalado -- no se carga", slug)
            continue
        try:
            plugins.append(factory())
        except Exception:
            log.exception(
                "Plugin '%s' estaba marcado como instalado pero fallo al cargar -- "
                "se omite, el resto del nucleo sigue arrancando. Puede que su codigo "
                "descargado no este disponible; reinstalalo desde la tienda.",
                slug,
            )

    for p in plugins:
        log.info("Plugin cargado: %s v%s (%s)", p.name, p.version, p.slug)
    return plugins

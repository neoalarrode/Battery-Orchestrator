"""
Persistencia de la configuracion del usuario (baterias, tarifa, origen de
PV, sensor de consumo). Todo editable desde la interfaz, nada hardcodeado.
Se guarda en un JSON dentro del directorio persistente del addon.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid

log = logging.getLogger("config_store")

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/data/config.json")

_lock = threading.RLock()  # reentrante: load_config() llama a save_config() en el primer arranque

# Nombre de este plugin dentro del fichero de config compartido por el
# nucleo de plugins (ver DOCS del nucleo / plugins.json en la raiz del
# repo) -- el fichero en disco pasa a tener namespace por plugin
# (`plugins.<slug>`) para que, el dia que haya mas de un plugin
# compartiendo instalacion, cada uno tenga su seccion propia sin pisarse.
# TODO EL RESTO de este modulo sigue trabajando con el dict PLANO de
# siempre (baterias/tarifa/...) -- el namespacing es un detalle de
# `load_config`/`save_config`, invisible para el resto de la app.
PLUGIN_KEY = "battery"
SCHEMA_ROOT_VERSION = 2

# Registro generico de entidades "de apoyo" (ver "tracked_entities" mas
# abajo): sensores que el usuario quiere que Energy conozca y clasifique
# por tipo de flujo, SIN que cada uno necesite su propio campo dedicado
# en la config (a diferencia de load_sensor/export_sensor/net_grid_sensor
# o current_sensor/power_sensor de cada array, que siguen siendo los que
# de verdad alimentan el motor de calculo). Es la pieza de "almacenamiento
# tipado + desplegable en Energy" pedida expresamente por el usuario --
# de momento es un registro consultable (guardar + clasificar + listar),
# la explotacion de cada tipo en el motor de calculo es incremental y se
# hace tipo a tipo segun haga falta, no todo de golpe aqui.
ENTITY_TYPES = {
    "load": "Carga / consumo",
    "grid_import": "Importado de red",
    "grid_export": "Exportado a red (vertido)",
    "solar_generation": "Generación solar",
    "battery_charge": "Carga de batería",
    "battery_discharge": "Descarga de batería",
    "deferrable_load": "Carga diferible",
    "other": "Otro",
}

DEFAULT_TRACKED_ENTITY = {
    "id": "",
    "entity_id": "",
    "type": "other",
    "label": "",  # opcional, nombre a mostrar en Energy -- si esta vacio se usa el entity_id
}

DEFAULT_CONFIG = {
    "batteries": [],
    "tracked_entities": [],
    "tariff": {
        "mode": "fixed",  # "fixed" | "pvpc_sensor"
        "punta_price": 0.173,
        "llano_price": 0.094,
        "valle_price": 0.075,
        "punta_periods": [[10, 14], [18, 22]],
        "llano_periods": [[8, 10], [14, 18], [22, 24]],
        "weekend_is_valle": True,
        "pvpc_sensor": "",
    },
    "pv_arrays": [],
    "deferrable_loads": [],
    "climate_orchestrator_zones": [],  # entity_id de las zonas de Climate Orchestrator (ver climate_link.py) — SOLO se rellena al pulsar "Buscar zonas" en la configuracion, nunca por sondeo automatico
    "climate_orchestrator_zones_discovered_at": None,  # ISO 8601 de la ultima vez que se pulso el boton, o None si nunca — solo informativo para la interfaz
    "load_sensor": "",  # modo "separate": consumo base YA SIN carga de baterias (p.ej. "consumo_instantaneo"); + solar + descarga de baterias = consumo real
    "load_sensor_mode": "separate",  # "separate" (load_sensor + export_sensor opcional) | "combined" (un unico net_grid_sensor con signo, alimenta tanto el flujo en vivo como la previsión historica)
    "export_sensor": "",  # modo "separate": potencia de vertido dedicada (siempre >= 0), opcional
    "net_grid_sensor": "",  # modo "combined": sensor unico con signo del punto de conexion a red EN BRUTO (+ importando, - vertiendo) — sustituye a load_sensor tanto en vivo como en la previsión
    # Credenciales de la cuenta EcoFlow, UNA sola para toda la instalacion
    # — las baterias EcoFlow (ver "source"/"ecoflow_mode" de cada bateria)
    # las reutilizan todas, no se repiten por bateria. Vacias = sin
    # baterias EcoFlow configuradas en ese modo.
    "ecoflow_access_key": "",   # modo cloud/hybrid — developer-eu.ecoflow.com (ver ecoflow_cloud.py)
    "ecoflow_secret_key": "",
    "ecoflow_user_id": "",      # modo bluetooth/hybrid — userId de la cuenta (no la contraseña), ver ecoflow_ble.py
    "general": {
        "horizon_hours": 48,  # menos de esto y, segun la hora del dia, el plan puede no llegar a ver la punta del dia siguiente y no cargar en la madrugada que toca (ver CHANGELOG v0.11.6)
        "cycle_seconds": 60,
        "pv_refresh_seconds": 1800,
        "dry_run": True,
        "history_days_for_load": 10,  # el recorder de HA por defecto solo guarda 10 dias; la app reintenta con menos si hace falta
        "contracted_power_w": 0,
        "priority_mode": "ahorro",  # "ahorro" | "autoconsumo" | "longevidad"
        "paced_charging": False,  # repartir la carga desde red en el tiempo disponible en vez de ir siempre al maximo (solo aplica con "ahorro" o "longevidad")
        "reserve_safety_margin_pct": 15,  # colchon extra (% de la capacidad util) sobre CUALQUIER objetivo de reserva -- protege contra fallos de la previsión de sol/consumo, sobre todo en bloques de valle largos (fin de semana) donde la reserva calculada puede ser minima. 0 = comportamiento de siempre, al filo. Ver scheduler.build_plan/_reserve_target.
        "language": "auto",  # "auto" (detecta el idioma del navegador) | "es" | "en" — se guarda como el idioma por defecto de esta instalacion
    },
}

DEFAULT_PV_ARRAY = {
    "mode": "entity",          # "entity" | "forecast_solar_api"
    "name": "",
    "entity_id": "",
    "api_key": "",
    "lat": 0.0,
    "lon": 0.0,
    "declination": 30,
    "azimuth": 0,
    "kwp": 1.0,
    "current_sensor": "",  # generacion INSTANTANEA real (W) de este array/string, corrige la hora actual del plan
    "installation_type": "ac_coupled",  # "ac_coupled" (necesita orden de carga por AC) | "hybrid" (conectado directo a una bateria, se autoconsume solo)
    # Alternativa/complemento a "current_sensor": este array/string tiene
    # su dato EN VIVO conectado a uno o varios puertos MPPT de una
    # bateria EcoFlow (Bluetooth/Hibrido/Cloud), en vez de (o ademas de)
    # un sensor de Home Assistant -- se suman si son varios puertos de la
    # MISMA zona/orientacion (p.ej. dos entradas MPPT del mismo tejado).
    # Una misma bateria puede tener varios de estos arrays a la vez, uno
    # por zona. "mode"/"entity_id"/API siguen decidiendo la previsión de
    # las horas FUTURAS (opcional, el usuario puede rellenarla o no) --
    # el vinculo EcoFlow solo aporta el dato de la hora actual. Cuando
    # hay puertos vinculados, installation_type se fuerza siempre a
    # "hybrid" (conectado directo a la bateria por definicion).
    "ecoflow_battery_id": "",     # id de la bateria (cfg["batteries"]) de la que cuelgan estos puertos
    "ecoflow_pv_channels": [],    # lista de "1".."4" -- que puerto(s) MPPT de esa bateria
    # Cuota de reparto en instalaciones de AUTOCONSUMO COMPARTIDO -- a
    # peticion expresa del usuario. 100 = instalacion propia normal (el
    # sensor/previsión de este array ya mide solo lo tuyo). En una
    # instalacion compartida (varios suministros repartiendose la MISMA
    # generacion), el sensor/previsión de este array puede estar midiendo
    # la instalacion COMPLETA -- aqui se declara que fraccion es
    # realmente tuya. Ese tipo de instalacion, ademas, no suele netear
    # nada FISICAMENTE antes de tu propio contador (a diferencia de un
    # panel propio de verdad): tu contador ve tu consumo BRUTO como si
    # viniera entero de red, y es esta app la que resta tu cuota real de
    # generacion para reconstruir lo que de verdad se importa/vierte --
    # ver pv_source.get_pv_forecast_total, donde se aplica el escalado
    # UNA vez, antes de sumar al resto de arrays.
    "self_consumption_share_pct": 100.0,
}

DEFAULT_DEFERRABLE_LOAD = {
    "name": "",
    "switch_entity": "",       # switch que la app enciende/apaga
    "power_sensor": "",        # opcional: sensor de potencia (W) para medir su consumo real y estimarlo solo
    "duration_hours": 1,       # cuantas horas seguidas necesita encendida
    "estimated_energy_wh": 0,  # 0 = usar la estimacion automatica por historico de activaciones (ver deferrable_store)
    "frequency": "daily",      # "once" (una vez y no se repite) | "daily" (una vez al dia) | "multiple_daily" (varias veces al dia)
    "runs_per_day": 2,         # solo se usa con "multiple_daily"
    "days_of_week": [],        # que dias programarla con "daily"/"multiple_daily": [] = todos los dias; si no, lista de 0=lunes..6=domingo (p.ej. lavadora solo lunes y sabado -> [0, 5])
    "interruptible": False,    # True: se puede apagar antes de tiempo si el excedente solar previsto desaparece (p.ej. un termo). False: se queda encendida toda su ventana pase lo que pase (p.ej. una lavadora, no se debe cortar a medio programa)
    "enabled": True,
    "done": False,             # solo relevante con frequency="once": ya se ejecuto una vez, no se vuelve a programar sola
}


def _is_namespaced(data) -> bool:
    return isinstance(data, dict) and isinstance(data.get("plugins"), dict)


def _read_raw() -> dict | None:
    if not os.path.exists(CONFIG_PATH):
        return None
    with open(CONFIG_PATH, encoding="utf-8") as f:
        content = f.read()
    if not content.strip():
        return None
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        if exc.msg == "Extra data":
            # El fichero tiene dos objetos JSON concatenados (escritura
            # interrumpida o doble volcado). Se recupera el primero,
            # que es el valido, y se sana el fichero en disco.
            try:
                obj, _ = json.JSONDecoder().raw_decode(content.lstrip())
                log.warning(
                    "config.json tenia datos extra (JSON corrupto) — recuperado el primer "
                    "objeto valido. El fichero se sanea ahora mismo."
                )
                _write_raw(obj)
                return obj
            except json.JSONDecodeError:
                pass
        raise


def _write_raw(root: dict) -> None:
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(root, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG_PATH)  # atomico en POSIX: nunca deja el fichero a medias


def load_config() -> dict:
    with _lock:
        raw = _read_raw()
        if raw is None:
            _write_raw({
                "schema_version": SCHEMA_ROOT_VERSION, "core": {},
                "plugins": {PLUGIN_KEY: json.loads(json.dumps(DEFAULT_CONFIG))},
            })
            return json.loads(json.dumps(DEFAULT_CONFIG))

        format_migrated = False
        if not _is_namespaced(raw):
            # Fichero de antes del nucleo de plugins: la config de Battery
            # estaba en la RAIZ del fichero, sin envolver. Se traslada tal
            # cual bajo "plugins.battery" -- nunca se pierde ni se toca
            # ningun valor, solo cambia donde vive dentro del JSON. Una
            # sola vez: en cuanto se guarda en el formato nuevo, los
            # siguientes arranques ya entran directos por la rama de abajo.
            log.info("Config en formato antiguo (plano, antes del nucleo de plugins) -- migrando a plugins.%s", PLUGIN_KEY)
            raw = {"schema_version": SCHEMA_ROOT_VERSION, "core": {}, "plugins": {PLUGIN_KEY: raw}}
            format_migrated = True

        battery_cfg = (raw.get("plugins") or {}).get(PLUGIN_KEY) or {}
        # completar claves que falten (por si se actualiza el esquema)
        merged = json.loads(json.dumps(DEFAULT_CONFIG))
        _deep_merge(merged, battery_cfg)
        schema_migrated = _migrate_legacy_pv_sensor(merged)
        schema_migrated = _migrate_legacy_export_sensor_mode(merged) or schema_migrated
        if format_migrated or schema_migrated:
            save_config(merged)
        return merged


def _migrate_legacy_pv_sensor(cfg: dict) -> bool:
    """
    Versiones anteriores tenian un unico "current_pv_sensor" global para
    toda la instalacion. Ahora cada array de "pv_arrays" lleva el suyo
    propio ("current_sensor"), para poder declarar varios strings/tejados
    sin tener que crear un sensor agregado en Home Assistant. Si solo hay
    un array declarado (el caso mas comun), se traslada solo. Con varios
    arrays no hay forma de adivinar a cual pertenecia, asi que se deja el
    campo viejo tal cual para que se reasigne a mano desde la interfaz.
    """
    legacy_sensor = cfg.get("current_pv_sensor")
    if not legacy_sensor:
        cfg.pop("current_pv_sensor", None)
        return False
    arrays = cfg.get("pv_arrays") or []
    if len(arrays) == 1 and not arrays[0].get("current_sensor"):
        arrays[0]["current_sensor"] = legacy_sensor
        del cfg["current_pv_sensor"]
        return True
    return False


def save_config(cfg: dict) -> None:
    """`cfg` sigue siendo el dict PLANO de siempre (baterias/tarifa/...) --
    todo el resto de la app sigue llamando a esta funcion exactamente igual
    que antes, sin enterarse del namespacing. Por debajo se guarda dentro
    de "plugins.battery", preservando lo que ya hubiera en "core" (o en
    otros plugins, el dia que compartan fichero) en vez de machacarlo."""
    with _lock:
        raw = _read_raw()
        if not _is_namespaced(raw):
            raw = {"schema_version": SCHEMA_ROOT_VERSION, "core": {}, "plugins": {}}
        raw.setdefault("plugins", {})[PLUGIN_KEY] = cfg
        raw["schema_version"] = SCHEMA_ROOT_VERSION
        _write_raw(raw)


# ---------------------------------------------------------- nucleo/plugins -
# Que plugins carga el nucleo al arrancar (ver plugin_loader.py) -- vive en
# "core", no bajo el namespace de ningun plugin concreto, porque decide
# sobre TODOS ellos por igual. Sin este campo (instalaciones de antes de
# la tienda de plugins) se asume que estan instalados todos los que ya
# traia el addon, para no desactivar nada en un arranque existente.
DEFAULT_INSTALLED_PLUGINS = ["battery", "climate"]


def get_installed_plugins() -> list[str]:
    with _lock:
        raw = _read_raw()
        if not _is_namespaced(raw):
            return list(DEFAULT_INSTALLED_PLUGINS)
        core = raw.get("core") or {}
        installed = core.get("installed_plugins")
        return list(installed) if installed is not None else list(DEFAULT_INSTALLED_PLUGINS)


def set_plugin_installed(slug: str, installed: bool) -> list[str]:
    with _lock:
        raw = _read_raw()
        if not _is_namespaced(raw):
            raw = {"schema_version": SCHEMA_ROOT_VERSION, "core": {}, "plugins": {}}
        raw.setdefault("core", {})
        current = raw["core"].get("installed_plugins")
        current = list(current) if current is not None else list(DEFAULT_INSTALLED_PLUGINS)
        if installed and slug not in current:
            current.append(slug)
        elif not installed and slug in current:
            current.remove(slug)
        raw["core"]["installed_plugins"] = current
        raw["schema_version"] = SCHEMA_ROOT_VERSION
        _write_raw(raw)
        return current


def _migrate_legacy_export_sensor_mode(cfg: dict) -> bool:
    """
    La v0.11.25 guardaba "export_sensor_mode" ("none"/"separate"/"combined")
    solo para el vertido, con `load_sensor` siempre por separado e
    independiente. Desde que el modo "combined" tambien alimenta la
    previsión (no solo el vertido), se unifico en un unico
    "load_sensor_mode" que gobierna los dos a la vez. Si una instalacion ya
    tenia guardado el campo viejo con "combined", se traslada tal cual
    (nunca se pierde la eleccion que ya habia hecho el usuario); "none" y
    "separate" no necesitan traslado, el nuevo default ya es "separate".
    """
    legacy_mode = cfg.pop("export_sensor_mode", None)
    if legacy_mode == "combined" and cfg.get("load_sensor_mode") != "combined":
        cfg["load_sensor_mode"] = "combined"
        return True
    return legacy_mode is not None


def _deep_merge(base: dict, override: dict) -> None:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def add_battery(cfg: dict, battery: dict) -> dict:
    battery = dict(battery)
    battery["id"] = battery.get("id") or str(uuid.uuid4())[:8]
    cfg["batteries"].append(battery)
    save_config(cfg)
    return battery


def update_battery(cfg: dict, battery_id: str, updates: dict) -> dict | None:
    for b in cfg["batteries"]:
        if b["id"] == battery_id:
            b.update(updates)
            save_config(cfg)
            return b
    return None


def delete_battery(cfg: dict, battery_id: str) -> bool:
    before = len(cfg["batteries"])
    cfg["batteries"] = [b for b in cfg["batteries"] if b["id"] != battery_id]
    save_config(cfg)
    return len(cfg["batteries"]) < before


def add_pv_array(cfg: dict, array: dict) -> dict:
    merged = dict(DEFAULT_PV_ARRAY)
    merged.update(array)
    merged["id"] = merged.get("id") or str(uuid.uuid4())[:8]
    cfg["pv_arrays"].append(merged)
    save_config(cfg)
    return merged


def update_pv_array(cfg: dict, array_id: str, updates: dict) -> dict | None:
    for a in cfg["pv_arrays"]:
        if a["id"] == array_id:
            a.update(updates)
            save_config(cfg)
            return a
    return None


def delete_pv_array(cfg: dict, array_id: str) -> bool:
    before = len(cfg["pv_arrays"])
    cfg["pv_arrays"] = [a for a in cfg["pv_arrays"] if a["id"] != array_id]
    save_config(cfg)
    return len(cfg["pv_arrays"]) < before


def add_tracked_entity(cfg: dict, entity: dict) -> dict:
    merged = dict(DEFAULT_TRACKED_ENTITY)
    merged.update(entity)
    merged["id"] = merged.get("id") or str(uuid.uuid4())[:8]
    if merged.get("type") not in ENTITY_TYPES:
        merged["type"] = "other"
    cfg.setdefault("tracked_entities", []).append(merged)
    save_config(cfg)
    return merged


def update_tracked_entity(cfg: dict, entity_id: str, updates: dict) -> dict | None:
    for e in cfg.get("tracked_entities", []):
        if e["id"] == entity_id:
            e.update(updates)
            if e.get("type") not in ENTITY_TYPES:
                e["type"] = "other"
            save_config(cfg)
            return e
    return None


def delete_tracked_entity(cfg: dict, entity_id: str) -> bool:
    before = len(cfg.get("tracked_entities", []))
    cfg["tracked_entities"] = [e for e in cfg.get("tracked_entities", []) if e["id"] != entity_id]
    save_config(cfg)
    return len(cfg["tracked_entities"]) < before


def add_deferrable_load(cfg: dict, load: dict) -> dict:
    merged = dict(DEFAULT_DEFERRABLE_LOAD)
    merged.update(load)
    merged["id"] = merged.get("id") or str(uuid.uuid4())[:8]
    cfg.setdefault("deferrable_loads", []).append(merged)
    save_config(cfg)
    return merged


def update_deferrable_load(cfg: dict, load_id: str, updates: dict) -> dict | None:
    for load in cfg.get("deferrable_loads", []):
        if load["id"] == load_id:
            load.update(updates)
            save_config(cfg)
            return load
    return None


def delete_deferrable_load(cfg: dict, load_id: str) -> bool:
    before = len(cfg.get("deferrable_loads", []))
    cfg["deferrable_loads"] = [d for d in cfg.get("deferrable_loads", []) if d["id"] != load_id]
    save_config(cfg)
    return len(cfg["deferrable_loads"]) < before

"""
Persistencia de los dispositivos Tuya dados de alta -- mismo patron que
climate/zone_store.py: namespace propio ("plugins.tuya") en el fichero de
config compartido del nucleo, nunca pisa la seccion de otro plugin.
"""

from __future__ import annotations

import uuid

import config_store

PLUGIN_KEY = "tuya"


DEFAULT_DEVICE_CONFIG = {
    "name": "",
    "device_id": "",
    "address": "",
    "local_key": "",
    "protocol_version": "3.3",
    "profile_yaml": "",
    # Ingestion interna por Climate y exposicion por MQTT NO son
    # excluyentes -- un mismo dispositivo puede estar a la vez consumido
    # por una zona y visible/controlable desde HA. `expose_mqtt` es solo
    # eso: si SE PUBLICA en HA, no si Climate puede usarlo (eso ya lo
    # decide la propia zona, referenciando este device_id).
    "expose_mqtt": False,
}


def _read_section() -> dict:
    return config_store.read_plugin_section(PLUGIN_KEY, {"devices": []})


def _write_section(section: dict) -> None:
    # El read-modify-write completo se hace dentro de config_store, bajo el
    # MISMO lock que el resto de escritores del fichero compartido -- antes cada
    # store usaba un lock propio distinto, con lo que una escritura de otro
    # plugin colada entre la lectura y la escritura de aqui se perdia en
    # silencio (un dispositivo guardado desaparecia sin mas). Ademas, el camino
    # de "formato no reconocido" reemplazaba el documento por uno vacio,
    # tirando la config entera si el fichero estaba en el formato plano antiguo
    # (ver config_store._as_namespaced).
    config_store.update_plugin_section(PLUGIN_KEY, section)


def load_devices() -> list[dict]:
    with config_store.transaction():
        section = _read_section()
        devices = section.get("devices") or []
        for d in devices:
            merged = dict(DEFAULT_DEVICE_CONFIG)
            merged.update(d.get("config") or {})
            d["config"] = merged
        return devices


def save_devices(devices: list[dict]) -> None:
    """Bug real, confirmado en produccion: esto escribia `{"devices":
    devices}` como seccion COMPLETA, borrando la cuenta Tuya guardada en
    la misma seccion cada vez que se añadia/editaba/borraba un
    dispositivo (`add_device`/`update_device`/`delete_device`, todos
    pasan por aqui) -- un usuario vinculaba la cuenta, resolvia UN
    dispositivo, lo añadia, y la cuenta desaparecia sin que nada lo
    avisase: el siguiente /resolve fallaba con "vincula primero una
    cuenta Tuya" aunque la interfaz siguiera mostrandola como vinculada
    (hasta el proximo refresco de /api/account). Fix: leer la seccion
    actual primero (igual que ya hacia save_account) y solo reemplazar
    la clave "devices", preservando cualquier otra cosa que viva en la
    misma seccion."""
    with config_store.transaction():
        section = _read_section()
        section["devices"] = devices
        _write_section(section)


def add_device(config: dict) -> dict:
    with config_store.transaction():
        devices = load_devices()
        merged = dict(DEFAULT_DEVICE_CONFIG)
        merged.update(config)
        device = {"id": str(uuid.uuid4())[:8], "config": merged}
        devices.append(device)
        save_devices(devices)
        return device


def update_device(device_id: str, config: dict) -> dict | None:
    with config_store.transaction():
        devices = load_devices()
        for d in devices:
            if d["id"] == device_id:
                d["config"].update(config)
                save_devices(devices)
                return d
        return None


def delete_device(device_id: str) -> bool:
    with config_store.transaction():
        devices = load_devices()
        before = len(devices)
        devices = [d for d in devices if d["id"] != device_id]
        save_devices(devices)
        return len(devices) < before


# ------------------------------------------------------ cuenta Tuya Cloud -
# Solo se usa para VINCULAR (traer local_key + esquema de cada dispositivo
# al darlo de alta desde "Detectados", ver tuya_plugin.py) -- nunca en
# operacion normal, que sigue siendo 100% LAN. access_secret se guarda en
# el mismo config.json que ya guarda las credenciales EcoFlow de Energy --
# mismo nivel de sensibilidad, mismo sitio.
DEFAULT_ACCOUNT = {"region": "eu", "access_id": "", "access_secret": "", "uid": ""}


def load_account() -> dict:
    with config_store.transaction():
        section = _read_section()
        merged = dict(DEFAULT_ACCOUNT)
        merged.update(section.get("account") or {})
        return merged


def save_account(account: dict) -> None:
    with config_store.transaction():
        section = _read_section()
        merged = dict(DEFAULT_ACCOUNT)
        merged.update(section.get("account") or {})
        merged.update(account)
        section["account"] = merged
        _write_section(section)

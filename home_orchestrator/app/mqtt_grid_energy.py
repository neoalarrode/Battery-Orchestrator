"""
Expone la energia acumulada importada/vertida (ver grid_energy_store.py)
a Home Assistant por MQTT Discovery -- dos `sensor.*` con `device_class:
energy` y `state_class: total_increasing`, el mismo contrato que
cualquier contador de energia nativo de HA (permite usarlos directos en
el panel de Energia de HA, utility_meter, etc. sin plantillas propias).

A diferencia del resto de sensores que expone este nucleo (potencia de
TP-Link, etc.), estos NO son opcionales por dispositivo -- Energy es el
UNICO plugin que los produce, asi que se publican siempre que Energy
este instalado (mismo criterio que "instalar el plugin ya implica querer
sus datos", no hace falta un toggle `expose_mqtt` extra para esto).
"""

from __future__ import annotations

import logging

log = logging.getLogger("mqtt_grid_energy")

DISCOVERY_PREFIX = "homeassistant"
NODE_ID = "home_orchestrator_energy"

_DEVICE_BLOCK = {
    "identifiers": [NODE_ID],
    "name": "Home Orchestrator — Energy",
    "manufacturer": "Home Orchestrator",
}


def _sensor_config(key: str, name: str) -> dict:
    base = f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/{key}"
    return {
        "topic": f"{base}/config",
        "state_topic": f"{base}/state",
        "payload": {
            "name": name,
            "unique_id": f"{NODE_ID}_{key}",
            "state_topic": f"{base}/state",
            "device_class": "energy",
            "state_class": "total_increasing",
            "unit_of_measurement": "kWh",
            "device": _DEVICE_BLOCK,
        },
    }


IMPORTED = _sensor_config("grid_imported", "Energía importada de red")
EXPORTED = _sensor_config("grid_exported", "Energía vertida a red")


def publish_discovery(mqtt_client) -> None:
    mqtt_client.publish(IMPORTED["topic"], IMPORTED["payload"], retain=True)
    mqtt_client.publish(EXPORTED["topic"], EXPORTED["payload"], retain=True)


def publish_state(mqtt_client, imported_kwh: float, exported_kwh: float) -> None:
    mqtt_client.publish(IMPORTED["state_topic"], round(imported_kwh, 3), retain=True)
    mqtt_client.publish(EXPORTED["state_topic"], round(exported_kwh, 3), retain=True)

"""
Cliente para el puente BLE — un custom_component GENERICO aparte en Home
Assistant (neoalarrode/Battery-Orchestrator-BLE-Bridge), no codigo de este
addon: Battery Orchestrator corre en su propio contenedor Docker sin
acceso a Bluetooth, asi que el habla BLE de verdad lo hace ese componente
(dentro del proceso de HA Core, unico sitio con acceso real al adaptador
Bluetooth y a los ESPHome BT Proxy) y aqui solo se le piden cosas por
SERVICIOS de HA — mismo patron que ya usa `climate_link.py` para
descubrir zonas de Climate Orchestrator, aqui aplicado a control real, no
solo lectura.

El puente es generico de marca (campo "brand" en cada servicio, hoy solo
existe "ecoflow"), pero ESTE modulo sigue siendo especifico de EcoFlow a
proposito — el mismo patron que ya usa `battery_exec.py` para despachar
por "ecoflow_mode": el dia que se sume otra marca, seria un modulo nuevo
tipo `<marca>_ble.py` con las mismas 4 funciones, no un cambio aqui.

Los 5 servicios del puente (ver su propio repositorio):
  battery_orchestrator_ble_bridge.discover
  battery_orchestrator_ble_bridge.get_state
  battery_orchestrator_ble_bridge.set_charging_task
  battery_orchestrator_ble_bridge.set_discharging_task
  battery_orchestrator_ble_bridge.disconnect
"""

from __future__ import annotations

import ha_client

DOMAIN = "battery_orchestrator_ble_bridge"
BRAND = "ecoflow"

# El pairing/handshake BLE puede tardar varios segundos de verdad (mas
# aun a traves de un ESPHome BT Proxy, un salto de red de mas frente a un
# adaptador local) — un timeout HTTP normal de la app se quedaria corto.
BLE_CALL_TIMEOUT_SECONDS = 40


def discover() -> list[dict] | None:
    """Dispositivos EcoFlow vistos por Bluetooth ahora mismo (sin conectar
    a ninguno) — `None` si el puente no esta instalado o no respondio."""
    resp = ha_client.call_service_with_response(
        DOMAIN, "discover", {"brand": BRAND}, timeout=BLE_CALL_TIMEOUT_SECONDS,
    )
    return resp.get("devices") if resp else None


def get_state(address: str, user_id: str) -> dict | None:
    """Conecta (si hace falta) y devuelve el ultimo estado conocido —
    `None` si el puente no responde o el dispositivo no esta al alcance,
    nunca un cero inventado."""
    return ha_client.call_service_with_response(
        DOMAIN, "get_state",
        {"brand": BRAND, "address": address, "credentials": {"user_id": user_id}},
        timeout=BLE_CALL_TIMEOUT_SECONDS,
    )


def set_charging_task(
    address: str, user_id: str,
    enable: bool | None = None, power_limit_w: float | None = None, target_soc: float | None = None,
) -> bool:
    extra = {"brand": BRAND, "address": address, "credentials": {"user_id": user_id}}
    if enable is not None:
        extra["enable"] = enable
    if power_limit_w is not None:
        extra["power_limit_w"] = power_limit_w
    if target_soc is not None:
        extra["target_soc"] = target_soc
    resp = ha_client.call_service_with_response(DOMAIN, "set_charging_task", extra, timeout=BLE_CALL_TIMEOUT_SECONDS)
    return bool(resp and resp.get("ok"))


def set_discharging_task(
    address: str, user_id: str, enable: bool | None = None, power_limit_w: float | None = None,
) -> bool:
    extra = {"brand": BRAND, "address": address, "credentials": {"user_id": user_id}}
    if enable is not None:
        extra["enable"] = enable
    if power_limit_w is not None:
        extra["power_limit_w"] = power_limit_w
    resp = ha_client.call_service_with_response(DOMAIN, "set_discharging_task", extra, timeout=BLE_CALL_TIMEOUT_SECONDS)
    return bool(resp and resp.get("ok"))

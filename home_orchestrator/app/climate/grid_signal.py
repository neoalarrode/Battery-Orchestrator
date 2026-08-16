"""
Lectura de la señal de red publicada por el plugin Battery de Home
Orchestrator (si esta instalado) — ver scheduler.py, seccion "Señal de
red" en su docstring.

Se lee por WebSocket (`ws.get_state`, ver ha_websocket.py) exactamente
igual que cualquier otra entidad de HA -- deliberado, aunque ambos
plugins puedan correr en el mismo proceso: mantiene a los dos plugins
DESACOPLADOS (si Battery no esta instalado, o no ha publicado nunca, esto
simplemente no encuentra la entidad y cae al comportamiento de siempre,
sin ninguna dependencia directa entre modulos de plugins distintos).

Entity_id FIJO, no configurable en ningun lado: Battery siempre publica en
"sensor.battery_orchestrator_grid_signal" (una unica instancia por
instalacion, igual que este plugin). Se detecta sola: si la entidad no
existe (Battery no instalado, o sin haber corrido su primer ciclo
todavia), `read()` devuelve todo a None y quien lo consuma
(scheduler.decide_action) ya sabe caer al comportamiento de siempre.
"""

from __future__ import annotations

GRID_SIGNAL_ENTITY_ID = "sensor.battery_orchestrator_grid_signal"


def read(ws) -> dict:
    """Devuelve {"tier", "solar_surplus_now_w", "home_power_sensor",
    "forecast"} — todo None (forecast: []) si Battery no esta instalado o
    no ha publicado nunca. "home_power_sensor" es el sensor general de
    consumo de la casa que Battery YA tiene declarado — aqui sirve para
    aprender solo el consumo real de cada actuador sin que el usuario
    tenga que declarar el mismo sensor otra vez en este plugin.

    "forecast" es la previsión horaria de precio/tramo/excedente solar que
    Battery YA calcula para si mismo (empezando por la hora actual, indice
    0) — se reutiliza tal cual para poder anticipar una hora punta antes
    de que llegue, en vez de reaccionar solo al excedente instantaneo.
    NUNCA se expone como atributo de la entidad climate.* de una zona: es
    una lista de hasta 48 elementos que cambiaria en cada publicacion de
    Battery, y grabarla entera en el recorder por cada zona en cada ciclo
    seria un derroche real — aqui se consume en memoria, nunca se
    persiste de mas."""
    try:
        state = ws.get_state(GRID_SIGNAL_ENTITY_ID)
    except Exception:
        state = None
    if state is None or state.get("state") in ("unknown", "unavailable"):
        return {"tier": None, "solar_surplus_now_w": None, "home_power_sensor": None, "forecast": []}
    attrs = state.get("attributes") or {}
    surplus = attrs.get("solar_surplus_now_w")
    try:
        surplus = float(surplus) if surplus is not None else None
    except (TypeError, ValueError):
        surplus = None
    forecast = attrs.get("forecast")
    if not isinstance(forecast, list):
        forecast = []
    return {
        "tier": attrs.get("tier"),
        "solar_surplus_now_w": surplus,
        "home_power_sensor": attrs.get("home_power_sensor") or None,
        "forecast": forecast,
    }

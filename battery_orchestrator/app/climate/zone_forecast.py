"""
Datos horarios (pasado real + futuro proyectado) para el grafico de 24h de
una zona de Climate Orchestrator -- mismo espiritu que el grafico de SOC de
Energy (ver templates/index.html:renderSocChart), pero de temperatura.

La mitad de FUTURO no es un horario guardado: es una proyeccion EN VIVO que
llama literalmente a `scheduler.decide_action` (la misma funcion que ya
decide de verdad en cada ciclo, ver zone_runner.py:decide_and_act) hora a
hora, avanzando la temperatura simulada con el mismo modelo de Newton
simple que ya usa `_anticipate` (ver scheduler.py) -- nunca una logica
paralela inventada solo para el grafico.

Que consigna (preset) usar en cada hora futura SI es una prediccion, a
peticion explicita del usuario: en vez de mantener fijo el preset activo
ahora mismo, cada hora futura resuelve que preset tocaria si la ocupacion
fuese la TIPICA de esa hora del dia, segun el patron real de los ultimos
`OCCUPANCY_HISTORY_DAYS` dias (ver `_hourly_occupancy_pct` y
ZoneRunner.preset_targets_for_occupancy) -- una media estadistica simple y
verificable a mano por el usuario contra su propio historico, no
aprendizaje automatico. El modo "manual" nunca se sustituye por esto (una
anulacion a mano vale para cualquier hora), y sin muestras suficientes esa
hora se cae al preset activo real de ahora, nunca se inventa una
ocupacion.

La sombra de "ocupacion" que pinta el grafico es la MISMA estadistica,
mostrada tal cual -- el usuario ve exactamente en que se basa la
proyeccion, nada oculto.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from . import grid_signal, occupancy, outdoor as outdoor_mod, scheduler
from .const import DEFAULT_DEADBAND, DEFAULT_MAX_TEMP, DEFAULT_MIN_TEMP
from .thermal_model import _climate_actuator_states, _history_for, _value_at_or_before

_LOGGER = logging.getLogger(__name__)

ACTUATOR_HISTORY_DAYS = 2  # solo hace falta cubrir `hours_back` -- nunca mas de 1-2 dias


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _bool_state(raw) -> bool | None:
    if raw in ("on", "home"):
        return True
    if raw in ("off", "not_home"):
        return False
    return None


def _bool_at_or_before(states: list, ts: datetime) -> bool | None:
    best = None
    for s in states:
        b = _bool_state(s.state)
        if b is None:
            continue
        if s.last_changed <= ts:
            best = b
        else:
            break
    return best


def _actuator_action_at(heat_hist, cool_hist, climate_heat_hist, climate_cool_hist, ts: datetime) -> str:
    if any(_bool_at_or_before(s, ts) for s in heat_hist) or any(_bool_at_or_before(s, ts) for s in climate_heat_hist):
        return "heat"
    if any(_bool_at_or_before(s, ts) for s in cool_hist) or any(_bool_at_or_before(s, ts) for s in climate_cool_hist):
        return "cool"
    return "idle"


def _historical_reason(action: str, occupied: bool | None) -> str:
    occ_note = "" if occupied is None else (" · zona ocupada" if occupied else " · zona sin presencia")
    label = {"heat": "calentando", "cool": "enfriando", "idle": "en reposo"}[action]
    return f"{label} (según histórico de actuadores){occ_note}"


def build_forecast(runner, hours_back: int = 24, hours_fwd: int = 24) -> list[dict]:
    ws = runner.ws
    bridges = runner.bridges
    zone = runner.zone
    now = _utcnow().replace(minute=0, second=0, microsecond=0)
    points: list[dict] = []

    current_temp_sensor = zone.get("current_temp_sensor")
    outdoor_sensor = zone.get("outdoor_temp_sensor")
    presence_entities = zone.get("presence_entities") or []
    heat_switches = zone.get("heat_switches") or []
    cool_switches = zone.get("cool_switches") or []
    climate_entities = zone.get("climate_entities") or []

    # ------------------------------------------------------------- pasado -
    indoor_hist = _history_for(ws, current_temp_sensor, ACTUATOR_HISTORY_DAYS, bridges=bridges) if current_temp_sensor else []
    outdoor_hist = _history_for(ws, outdoor_sensor, ACTUATOR_HISTORY_DAYS, bridges=bridges) if outdoor_sensor else []
    presence_hist = [_history_for(ws, e, ACTUATOR_HISTORY_DAYS, bridges=bridges) for e in presence_entities]
    heat_hist = [_history_for(ws, e, ACTUATOR_HISTORY_DAYS, bridges=bridges) for e in heat_switches]
    cool_hist = [_history_for(ws, e, ACTUATOR_HISTORY_DAYS, bridges=bridges) for e in cool_switches]
    climate_heat_hist = [_climate_actuator_states(ws, e, "heating", ACTUATOR_HISTORY_DAYS, bridges=bridges) for e in climate_entities]
    climate_cool_hist = [_climate_actuator_states(ws, e, "cooling", ACTUATOR_HISTORY_DAYS, bridges=bridges) for e in climate_entities]

    occupancy_by_hour = occupancy.hourly_occupancy_pct(ws, presence_entities, bridges)

    for i in range(-hours_back, 0):
        ts = now + timedelta(hours=i)
        indoor = _value_at_or_before(indoor_hist, ts)
        outdoor = _value_at_or_before(outdoor_hist, ts)
        occ_vals = [_bool_at_or_before(s, ts) for s in presence_hist]
        occ_known = [v for v in occ_vals if v is not None]
        occupied = any(occ_known) if occ_known else None
        action = _actuator_action_at(heat_hist, cool_hist, climate_heat_hist, climate_cool_hist, ts)
        points.append({
            "dt": ts.isoformat(), "historical": True,
            "indoor_temp": round(indoor, 1) if indoor is not None else None,
            "outdoor_temp": round(outdoor, 1) if outdoor is not None else None,
            "occupied": occupied,
            "occupied_pct": occupancy_by_hour.get(ts.astimezone().hour),
            "action": action, "target_temp": None,
            "reason": _historical_reason(action, occupied),
        })

    # ------------------------------------------------------------- futuro -
    weather_entity = zone.get("weather_entity", "")
    outdoor_forecast_full = outdoor_mod.get_outdoor_forecast(ws, zone, weather_entity, hours_fwd + 1)
    grid = grid_signal.read(ws)
    grid_forecast = grid.get("forecast") or []

    priority = zone.get("priority", "confort")
    deadband = float(zone.get("deadband", DEFAULT_DEADBAND))
    min_temp = float(zone.get("min_temp", DEFAULT_MIN_TEMP))
    max_temp = float(zone.get("max_temp", DEFAULT_MAX_TEMP))
    thermal = runner.thermal_model_snapshot()
    zone_power_w = runner.zone_estimated_power_w()
    temp = runner.current_temperature

    for i in range(1, hours_fwd + 1):
        ts = now + timedelta(hours=i)
        outdoor_now_i = outdoor_forecast_full[i] if i < len(outdoor_forecast_full) else (
            outdoor_forecast_full[-1] if outdoor_forecast_full else None
        )
        outdoor_rest = outdoor_forecast_full[i:] if i < len(outdoor_forecast_full) else []
        grid_i = grid_forecast[i] if i < len(grid_forecast) else None
        grid_rest = grid_forecast[i:] if i < len(grid_forecast) else []

        occ_pct = occupancy_by_hour.get(ts.astimezone().hour)
        occupied_likely = occupancy.likely(occ_pct)
        heat_target, cool_target, preset_name = runner.preset_targets_for_occupancy(occupied_likely)

        if runner.hvac_mode == "off":
            action, reason = "idle", "termostato apagado"
        elif temp is None:
            action, reason = "idle", "sensor de temperatura no disponible ahora mismo"
        elif heat_target is None and cool_target is None:
            action, reason = "idle", f"preset «{preset_name}»: sin consigna activa"
        else:
            action, reason = scheduler.decide_action(
                current_temp=temp, heat_target=heat_target, cool_target=cool_target,
                priority=priority, deadband=deadband, min_temp=min_temp, max_temp=max_temp,
                outdoor_now=outdoor_now_i, outdoor_forecast=outdoor_rest,
                heating_rate_deg_h=thermal.get("heating_rate_deg_h", 0.0) or 0.0,
                cooling_rate_deg_h=thermal.get("cooling_rate_deg_h", 0.0) or 0.0,
                idle_loss_coeff=thermal.get("idle_loss_coeff", 0.0) or 0.0,
                grid_tier=grid_i.get("tier") if grid_i else None,
                solar_surplus_now_w=grid_i.get("solar_surplus_w") if grid_i else None,
                zone_estimated_power_w=zone_power_w, grid_forecast=grid_rest,
            )
            reason = f"preset «{preset_name}» (patrón histórico): {reason}"

        if temp is not None:
            if action == "heat":
                temp = temp + (thermal.get("heating_rate_deg_h") or 0.0)
            elif action == "cool":
                temp = temp - (thermal.get("cooling_rate_deg_h") or 0.0)
            elif outdoor_now_i is not None:
                temp = temp + (thermal.get("idle_loss_coeff") or 0.0) * (outdoor_now_i - temp)

        target_now = heat_target if action == "heat" else cool_target if action == "cool" else (heat_target or cool_target)
        points.append({
            "dt": ts.isoformat(), "historical": False,
            "indoor_temp": round(temp, 1) if temp is not None else None,
            "outdoor_temp": round(outdoor_now_i, 1) if outdoor_now_i is not None else None,
            "occupied": occupied_likely,
            "occupied_pct": occ_pct,
            "action": action, "target_temp": round(target_now, 1) if target_now is not None else None,
            "reason": reason,
        })

    return points

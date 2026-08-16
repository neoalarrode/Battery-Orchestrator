"""
Previsión de temperatura exterior para el precalentamiento con antelacion
(prioridad "ahorro"). Prioriza, en este orden:

  1. Previsión horaria de una entidad `weather.*` ya existente en tu HA
     (AEMET, Met.no, OpenWeatherMap...), corregida en la hora actual con el
     sensor exterior propio de la zona si lo tiene declarado.
  2. Si no hay `weather.*` global, o no da previsión horaria: la media
     historica real de esa MISMA hora del dia en los ultimos dias, a partir
     del sensor exterior propio de la zona (nada de aprendizaje automatico
     opaco).
  3. Si tampoco hay sensor propio: temperatura constante (la actual, o un
     valor por defecto seguro) — mejor una previsión plana y honesta que
     inventar una curva.

Todo por WebSocket (ver ha_websocket.py) — `ws` se pasa explicito, nunca
un import global.
"""

from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta, timezone

_LOGGER = logging.getLogger(__name__)

MIN_SAMPLES_PER_HOUR = 3


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _start_iso(days: int) -> str:
    return (_utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_local_hour(epoch: float) -> int:
    # Sin zona horaria local propia disponible fuera de HA Core -- se usa
    # la hora del propio sistema del addon (el mismo TZ que ya usa el
    # resto de Home Orchestrator, ver main.py/scheduler.py de Battery).
    return datetime.fromtimestamp(epoch).hour


def weather_hourly_forecast(ws, weather_entity: str, horizon_hours: int) -> list[float] | None:
    if not weather_entity:
        return None
    try:
        response = ws.call_service(
            "weather", "get_forecasts", service_data={"type": "hourly"},
            target={"entity_id": weather_entity}, return_response=True,
        )
    except Exception:
        _LOGGER.debug("weather.get_forecasts no disponible para %s", weather_entity, exc_info=True)
        return None
    forecasts = (response or {}).get(weather_entity, {}).get("forecast", [])
    if not forecasts:
        return None
    temps = [f.get("temperature") for f in forecasts[:horizon_hours] if f.get("temperature") is not None]
    if not temps:
        return None
    if len(temps) < horizon_hours:
        temps += [temps[-1]] * (horizon_hours - len(temps))
    return temps[:horizon_hours]


def _hourly_average_sync(ws, entity_id: str, horizon_hours: int, days: int, default: float) -> list[float]:
    try:
        raw = ws.get_history(entity_id, _start_iso(days), with_attributes=False)
    except Exception:
        _LOGGER.debug("Sin historico (via WebSocket) de %s todavia", entity_id, exc_info=True)
        raw = []
    if not raw:
        return [default] * horizon_hours

    buckets: dict[int, list[float]] = {h: [] for h in range(24)}
    for point in raw:
        try:
            val = float(point["state"])
        except (ValueError, TypeError):
            continue
        if point.get("last_updated") is None:
            continue
        buckets[_to_local_hour(point["last_updated"])].append(val)

    hourly_avg: dict[int, float | None] = {}
    for h, vals in buckets.items():
        hourly_avg[h] = statistics.mean(vals) if len(vals) >= MIN_SAMPLES_PER_HOUR else None
    known = [v for v in hourly_avg.values() if v is not None]
    fallback = statistics.mean(known) if known else default
    for h in range(24):
        if hourly_avg[h] is None:
            hourly_avg[h] = fallback

    now_hour = datetime.now().hour
    return [hourly_avg[(now_hour + i) % 24] for i in range(horizon_hours)]


def get_outdoor_forecast(ws, zone: dict, weather_entity: str, horizon_hours: int) -> list[float]:
    outdoor_sensor = zone.get("outdoor_temp_sensor")
    default = 5.0 if zone.get("hvac_capability") != "cool" else 28.0

    forecast = weather_hourly_forecast(ws, weather_entity, horizon_hours)
    if forecast:
        if outdoor_sensor:
            try:
                state = ws.get_state(outdoor_sensor)
            except Exception:
                state = None
            if state is not None:
                try:
                    forecast[0] = float(state["state"])
                except (ValueError, TypeError, KeyError):
                    pass
        return forecast

    if outdoor_sensor:
        try:
            return _hourly_average_sync(ws, outdoor_sensor, horizon_hours, 14, default)
        except Exception:
            _LOGGER.debug("No se pudo calcular la previsión exterior por historico para %s", outdoor_sensor, exc_info=True)

    state = None
    if outdoor_sensor:
        try:
            state = ws.get_state(outdoor_sensor)
        except Exception:
            state = None
    try:
        current = float(state["state"]) if state else default
    except (ValueError, TypeError, KeyError):
        current = default
    return [current] * horizon_hours

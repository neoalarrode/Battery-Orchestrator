"""
Origen de la previsión solar. El usuario puede declarar VARIOS arrays
(distintas orientaciones/inclinaciones, o una instalación futura ampliada)
y se suman todos para dar la previsión total de la casa.

Cada array puede ser:
  - "entity": lee la previsión de un sensor de HA que ya la publique.
  - "forecast_solar_api": llama directamente a la API publica de
    Forecast.Solar. La URL base es fija (no es un secreto), la clave de
    API y los parametros de la instalacion los da el usuario.

La llamada a la API se cachea (por array) para no agotar la cuota gratuita
de peticiones/hora, independientemente de cada cuanto se ejecute el ciclo
de decision.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import requests

import ha_client

FORECAST_SOLAR_BASE = "https://api.forecast.solar"
TIMEOUT = 15

# cache por array_id: {"fetched_at": epoch, "watts": {timestamp_str: valor}}
_cache: dict[str, dict] = {}


def _fetch_raw(api_key: str, lat: float, lon: float, declination: float,
               azimuth: float, kwp: float) -> dict:
    key_segment = f"/{api_key}" if api_key else ""
    url = f"{FORECAST_SOLAR_BASE}{key_segment}/estimate/{lat}/{lon}/{declination}/{azimuth}/{kwp}"
    r = requests.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json().get("result", {}).get("watts", {})


def _hourly_from_watts(watts: dict, horizon_hours: int) -> list[float]:
    if not watts:
        return [0.0] * horizon_hours
    parsed = []
    for k, v in watts.items():
        try:
            parsed.append((datetime.fromisoformat(k.replace(" ", "T")), float(v)))
        except ValueError:
            continue
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    out = []
    for i in range(horizon_hours):
        slot_start = now + timedelta(hours=i)
        slot_end = slot_start + timedelta(hours=1)
        vals = [w for t, w in parsed if slot_start <= t < slot_end]
        out.append(sum(vals) / len(vals) if vals else 0.0)
    return out


def fetch_forecast_solar_api(array_id: str, api_key: str, lat: float, lon: float,
                              declination: float, azimuth: float, kwp: float,
                              horizon_hours: int, refresh_seconds: int = 1800) -> list[float]:
    """
    Devuelve la previsión horaria (W) para este array, usando cache: solo
    llama a la API si han pasado mas de `refresh_seconds` desde la ultima
    vez, para no agotar la cuota gratuita aunque el ciclo de decision se
    ejecute mucho mas a menudo.
    """
    now = time.time()
    cached = _cache.get(array_id)
    if cached is None or (now - cached["fetched_at"]) > refresh_seconds:
        try:
            watts = _fetch_raw(api_key, lat, lon, declination, azimuth, kwp)
            _cache[array_id] = {"fetched_at": now, "watts": watts}
        except (requests.RequestException, ValueError):
            if cached is None:
                return [0.0] * horizon_hours
            # si falla la llamada, seguir usando la cache anterior aunque este vencida
    return _hourly_from_watts(_cache[array_id]["watts"], horizon_hours)


def get_array_forecast(array: dict, horizon_hours: int, refresh_seconds: int) -> list[float]:
    mode = array.get("mode", "entity")
    if mode == "forecast_solar_api":
        return fetch_forecast_solar_api(
            array_id=array["id"],
            api_key=array.get("api_key", ""),
            lat=array.get("lat", 0),
            lon=array.get("lon", 0),
            declination=array.get("declination", 30),
            azimuth=array.get("azimuth", 0),
            kwp=array.get("kwp", 1),
            horizon_hours=horizon_hours,
            refresh_seconds=refresh_seconds,
        )
    entity_id = array.get("entity_id")
    if not entity_id:
        return [0.0] * horizon_hours
    return ha_client.pv_forecast_from_entity(entity_id, horizon_hours)


def get_pv_forecast_total(pv_arrays: list[dict], horizon_hours: int, refresh_seconds: int = 1800) -> list[float]:
    """Suma la previsión de todos los arrays declarados."""
    if not pv_arrays:
        return [0.0] * horizon_hours
    total = [0.0] * horizon_hours
    for array in pv_arrays:
        series = get_array_forecast(array, horizon_hours, refresh_seconds)
        for i in range(horizon_hours):
            total[i] += series[i] if i < len(series) else 0.0
    return total

"""
Origen de la previsión solar. El usuario puede declarar VARIOS arrays
(distintas orientaciones/inclinaciones, o una instalación futura ampliada)
y se suman todos para dar la previsión total de la casa.

Cada array puede ser:
  - "entity": lee la previsión de un sensor de HA que ya la publique.
  - "forecast_solar_api": llama directamente a la API publica de
    Forecast.Solar. La URL base es fija (no es un secreto), la clave de
    API y los parametros de la instalacion los da el usuario.

Y cada array declara ademas su "installation_type": "ac_coupled" (parte de
una instalacion de autoconsumo normal, necesita que la app mande una orden
de carga por AC para que una bateria aproveche su excedente) o "hybrid"
(paneles conectados directamente a una bateria con inversor integrado, que
absorbe su excedente sola sin ninguna orden). Es una propiedad de cada
panel/string, no de la bateria: una misma instalacion puede tener paneles
de los dos tipos a la vez.

La llamada a la API se cachea (por array) para no agotar la cuota gratuita
de peticiones/hora, independientemente de cada cuanto se ejecute el ciclo
de decision.

Ademas, si un array declara su sensor de generación instantánea
("current_sensor"), la previsión hora a hora se corrige con lo que ese
sensor ha generado REALMENTE de media a esa misma hora del día en los
últimos días: se toma el mínimo entre la previsión oficial y esa media
real. Así se prefiere el histórico real (que conoce sombras/obstáculos de
tu ubicación que ninguna previsión genérica capta) salvo que la previsión
oficial sea aún más baja para esa hora (señal de que se espera peor tiempo
de lo habitual). Sin sensor declarado, o mientras no tenga histórico
todavía (recién dado de alta), se usa la previsión oficial sin corregir.
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


def _historical_actual_forecast(current_sensor: str, horizon_hours: int, days: int = 21) -> tuple[list[float], list[bool]] | None:
    """
    Previsión basada en lo que este mismo array ha generado REALMENTE en el
    pasado (media por hora del dia de los ultimos `days` dias de su sensor de
    generación instantánea) — igual que ya hace la app para el consumo. Sirve
    para corregir el sesgo sistemático de la previsión "oficial" (API o
    sensor de HA), que no conoce tu ubicación real (sombras, obstáculos,
    orientación...).

    Devuelve (valores, fiable_por_hora): `fiable_por_hora[i]` es False para
    las horas donde todavia no hay suficiente muestra real (sensor recien
    declarado, o una franja horaria concreta con poco historico todavia) —
    en esas horas no hay nada fiable con que corregir. Devuelve None directo
    si no hay ningun historico en absoluto para este sensor.
    """
    if not ha_client.has_recent_history(current_sensor, days=1):
        return None
    return ha_client.hourly_average_forecast_with_reliability(current_sensor, horizon_hours, days=days, default=0.0)


def get_array_forecast(array: dict, horizon_hours: int, refresh_seconds: int) -> list[float]:
    mode = array.get("mode", "entity")
    if mode == "forecast_solar_api":
        official = fetch_forecast_solar_api(
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
    else:
        entity_id = array.get("entity_id")
        official = ha_client.pv_forecast_from_entity(entity_id, horizon_hours) if entity_id else [0.0] * horizon_hours

    current_sensor = array.get("current_sensor")
    if current_sensor:
        historical = _historical_actual_forecast(current_sensor, horizon_hours)
        if historical is not None:
            historical_actual, reliable = historical
            # Se hace mas caso a la media real por hora (conoce mejor tu
            # ubicación que cualquier previsión genérica) EXCEPTO cuando la
            # previsión oficial es menor: eso suele indicar que se espera
            # peor tiempo del habitual para esa hora (nubes), y ese matiz
            # día a día sí lo capta la previsión oficial y el histórico no.
            # Las horas sin suficiente muestra real todavia (`reliable`
            # False) se dejan tal cual con la previsión oficial, para no
            # arrastrar una media basada en un puñado de lecturas sueltas.
            return [
                min(historical_actual[i], official[i]) if reliable[i] else official[i]
                for i in range(horizon_hours)
            ]
    return official


def get_pv_forecast_total(
    pv_arrays: list[dict], horizon_hours: int, refresh_seconds: int = 1800,
    live_now_overrides: dict[str, float] | None = None,
) -> tuple[list[float], float | None, float]:
    """
    Suma la previsión de todos los arrays declarados, y corrige la hora
    ACTUAL (indice 0) con la generación real medida en cada array que
    tenga su propio sensor de generación instantánea declarado
    ("current_sensor") — asi puedes declarar varios strings/tejados sin
    tener que crear un sensor agregado aparte en Home Assistant: cada uno
    lleva su propio dato real, y aqui se suman igual que la previsión.

    `live_now_overrides` (opcional): {array_id: watts_ahora_mismo} para
    arrays cuyo dato en vivo no viene de un sensor de HA (`current_sensor`)
    sino de otra fuente que resuelve quien llama — hoy, un puerto MPPT de
    una bateria EcoFlow (ver `ecoflow_battery_id`/`ecoflow_pv_channel` en
    cada array y `_ecoflow_pv_channel_live_overrides` en main.py). Este
    modulo no sabe nada de EcoFlow a proposito, solo recibe el numero ya
    resuelto — mismo trato que `current_sensor`, con prioridad si los dos
    estuvieran declarados a la vez.

    Devuelve (forecast_w, pv_now_actual_w, hybrid_now_w):
      - forecast_w: la lista horaria total.
      - pv_now_actual_w: la generación real total ahora mismo — None si
        ningun array tiene dato en vivo (para que el llamante sepa que no
        hay dato real).
      - hybrid_now_w: cuanto de la hora actual viene de arrays marcados
        como "hybrid" (paneles conectados directamente a una bateria con
        inversor integrado) — esa energia ya se esta absorbiendo sola,
        sin que la app tenga que mandar ninguna orden de carga por AC, asi
        que el llamante debe descontarla de lo que decida mandar por AC.
    """
    if not pv_arrays:
        return [0.0] * horizon_hours, None, 0.0

    live_now_overrides = live_now_overrides or {}
    total = [0.0] * horizon_hours
    any_live = False
    hybrid_now = 0.0

    for array in pv_arrays:
        series = get_array_forecast(array, horizon_hours, refresh_seconds)
        live_value = live_now_overrides.get(array.get("id"))
        if live_value is None:
            current_sensor = array.get("current_sensor")
            if current_sensor and horizon_hours > 0:
                live_value = ha_client.get_numeric_state(current_sensor, default=None)
        if live_value is not None and horizon_hours > 0:
            series = [live_value] + list(series[1:])
            any_live = True
        for i in range(horizon_hours):
            total[i] += series[i] if i < len(series) else 0.0
        if horizon_hours > 0 and array.get("installation_type") == "hybrid" and series:
            hybrid_now += series[0]

    pv_now_actual = total[0] if any_live and horizon_hours > 0 else None
    return total, pv_now_actual, hybrid_now

"""
Cliente minimo para hablar con Home Assistant.

Dentro de un addon, HA Supervisor inyecta SUPERVISOR_TOKEN y el proxy
interno en http://supervisor/core/api/. Para desarrollo local fuera del
addon, se puede usar HA_URL + HA_TOKEN (token de larga duracion) en su lugar.
"""

from __future__ import annotations

import os
import statistics
from datetime import datetime, timedelta, timezone

import requests

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN")
if SUPERVISOR_TOKEN:
    BASE_URL = "http://supervisor/core/api"
    TOKEN = SUPERVISOR_TOKEN
else:
    BASE_URL = os.environ.get("HA_URL", "http://localhost:8123/api")
    TOKEN = os.environ.get("HA_TOKEN", "")

HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
TIMEOUT = 10


class HAError(Exception):
    pass


def get_state(entity_id: str):
    r = requests.get(f"{BASE_URL}/states/{entity_id}", headers=HEADERS, timeout=TIMEOUT)
    if r.status_code == 404:
        raise HAError(f"Entidad no encontrada: {entity_id}")
    r.raise_for_status()
    return r.json()


STALE_STATES = {"unavailable", "unknown", "none", ""}


def get_numeric_state(entity_id: str, default: float | None = 0.0) -> float | None:
    """
    Devuelve el valor numerico de una entidad. Si la entidad esta
    'unavailable'/'unknown' o no existe, devuelve `default` (que puede ser
    None para que el llamante decida saltarse esa entidad en vez de
    asumir un valor inventado).
    """
    try:
        s = get_state(entity_id)["state"]
        if s.strip().lower() in STALE_STATES:
            return default
        return float(s)
    except (HAError, ValueError, KeyError):
        return default


def call_service(domain: str, service: str, entity_id: str | None = None, extra: dict | None = None):
    payload = {}
    if entity_id:
        payload["entity_id"] = entity_id
    if extra:
        payload.update(extra)
    r = requests.post(f"{BASE_URL}/services/{domain}/{service}", headers=HEADERS, json=payload, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def turn_on(entity_id: str):
    domain = entity_id.split(".")[0]
    return call_service(domain, "turn_on", entity_id)


def turn_off(entity_id: str):
    domain = entity_id.split(".")[0]
    return call_service(domain, "turn_off", entity_id)


def set_number(entity_id: str, value: float):
    return call_service("number", "set_value", entity_id, {"value": value})


def publish_sensor(entity_id: str, state, attributes: dict | None = None):
    """Publica un sensor propio del orquestador en HA (para dashboards)."""
    payload = {"state": state, "attributes": attributes or {}}
    r = requests.post(f"{BASE_URL}/states/{entity_id}", headers=HEADERS, json=payload, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def get_history(entity_id: str, days: int) -> list[dict]:
    # OJO: la marca de tiempo va EMBEBIDA en la ruta de la URL (no en un
    # parametro de query), asi que tiene que ir "limpia". .isoformat() por
    # defecto produce algo como "...T21:58:03.123456+00:00": el "+" ahi
    # dentro rompe la ruta (se puede interpretar como espacio o generar una
    # fecha invalida) y HA devuelve una respuesta vacia sin avisar de error.
    # Formato limpio con sufijo "Z" (UTC) en su lugar.
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = requests.get(
        f"{BASE_URL}/history/period/{start}",
        headers=HEADERS,
        params={"filter_entity_id": entity_id, "minimal_response": "true"},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    return data[0] if data else []


def has_recent_history(entity_id: str, days: int = 1) -> bool:
    """
    Comprobacion barata: ¿hay algun punto de historico real para este sensor
    en los ultimos `days` dias? Se usa para saber si ya se puede calcular una
    media horaria de verdad (`hourly_average_forecast`) o si el sensor es
    demasiado nuevo y todavia no hay nada que promediar.
    """
    try:
        return bool(get_history(entity_id, days))
    except requests.RequestException:
        return False


def hourly_average_forecast(entity_id: str, horizon_hours: int, days: int = 21, default: float = 0.0) -> list[float]:
    """
    Previsión simple y explicable para CUALQUIER sensor numerico: para cada
    hora del horizonte, la media de esa MISMA hora-del-dia en los ultimos
    `days` dias de historico real. Nada de aprendizaje automatico opaco.

    Si `days` supera lo que tu Home Assistant realmente conserva (por
    defecto el recorder solo guarda 10 dias), reintenta con ventanas mas
    cortas antes de rendirse - asi no depende de que sepas/ajustes ese
    detalle de configuracion tuyo.
    """
    raw = get_history(entity_id, days)
    if not raw:
        for fallback_days in (10, 7, 3, 1):
            if fallback_days >= days:
                continue
            raw = get_history(entity_id, fallback_days)
            if raw:
                break
    if not raw:
        current = get_numeric_state(entity_id, default=default)
        return [current] * horizon_hours

    buckets: dict[int, list[float]] = {h: [] for h in range(24)}
    for point in raw:
        try:
            val = float(point["state"])
        except (KeyError, ValueError):
            continue
        ts = datetime.fromisoformat(point["last_changed"].replace("Z", "+00:00"))
        buckets[ts.astimezone().hour].append(val)

    hourly_avg = {}
    for h, vals in buckets.items():
        hourly_avg[h] = statistics.mean(vals) if vals else None

    known = [v for v in hourly_avg.values() if v is not None]
    fallback = statistics.mean(known) if known else default
    for h in range(24):
        if hourly_avg[h] is None:
            hourly_avg[h] = fallback

    now = datetime.now()
    return [hourly_avg[(now.hour + i) % 24] for i in range(horizon_hours)]


# alias retrocompatible
load_forecast_from_history = hourly_average_forecast


def true_load_forecast(base_consumption_sensor: str, solar_sensors: list[str],
                        battery_discharge_sensors: list[str],
                        horizon_hours: int, days: int = 21) -> list[float]:
    """
    Reconstruye el consumo REAL de la vivienda sumando el historico de cada
    componente por separado, hora a hora:

        consumo = consumo_base (red YA SIN la carga de baterias, p.ej.
                                 "consumo_instantaneo")
                + produccion_solar (de cada string/tejado declarado, sumados)
                + descarga_baterias (solo salida, sensores positivos tipo
                  "..._load_from_battery"; NO hace falta el de carga: al
                  restarse ya en el sensor base, los terminos de carga se
                  cancelan matematicamente)

    No hace falta un sensor nuevo en HA: se calcula aqui mismo a partir de
    sensores que ya existen y ya tienen historico acumulado.
    """
    total = hourly_average_forecast(base_consumption_sensor, horizon_hours, days, default=0.0)

    for ss in solar_sensors:
        if not ss:
            continue
        solar = hourly_average_forecast(ss, horizon_hours, days, default=0.0)
        total = [total[i] + solar[i] for i in range(horizon_hours)]

    for bs in battery_discharge_sensors:
        if not bs:
            continue
        batt = hourly_average_forecast(bs, horizon_hours, days, default=0.0)
        total = [total[i] + batt[i] for i in range(horizon_hours)]

    return total


def pv_forecast_from_entity(entity_id: str, horizon_hours: int) -> list[float]:
    """
    Lee la previsión solar desde un sensor de HA que exponga un atributo de
    tipo lista de pronosticos (forecast_solar, EMHASS p_pv_forecast, etc.)
    Se buscan claves de atributo habituales; si no se encuentra nada
    utilizable, se devuelve una lista de ceros (seguro, nunca inventa sol).
    """
    try:
        state = get_state(entity_id)
    except HAError:
        return [0.0] * horizon_hours

    attrs = state.get("attributes", {})
    for key in ("forecasts", "wh_hours", "watts", "forecast"):
        if key in attrs and isinstance(attrs[key], (list, dict)):
            series = attrs[key]
            if isinstance(series, dict):
                values = list(series.values())[:horizon_hours]
            else:
                values = [
                    item.get("p_pv_forecast") or item.get("value") or item.get("power")
                    for item in series[:horizon_hours]
                ]
            values = [float(v) for v in values if v is not None]
            if values:
                values += [0.0] * (horizon_hours - len(values))
                return values[:horizon_hours]

    # sin atributo de previsión util: usar el valor actual como estimacion
    # plana solo para la proxima hora, y 0 despues (mejor infravalorar que
    # inventar produccion que no va a existir)
    try:
        current = float(state["state"])
    except (ValueError, KeyError):
        current = 0.0
    return [current] + [0.0] * (horizon_hours - 1)

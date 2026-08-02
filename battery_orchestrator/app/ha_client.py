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


def call_service(domain: str, service: str, entity_id: str, extra: dict | None = None):
    payload = {"entity_id": entity_id}
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
    start = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    r = requests.get(
        f"{BASE_URL}/history/period/{start}",
        headers=HEADERS,
        params={"filter_entity_id": entity_id, "minimal_response": "true"},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    return data[0] if data else []


def load_forecast_from_history(entity_id: str, horizon_hours: int, days: int = 21) -> list[float]:
    """
    Previsión de consumo simple y explicable: para cada hora del horizonte,
    la media de esa MISMA hora-del-dia en los ultimos `days` dias de
    historico real. Nada de aprendizaje automatico opaco.
    """
    raw = get_history(entity_id, days)
    if not raw:
        # sin historico: devolver un valor plano conservador
        current = get_numeric_state(entity_id, default=300.0)
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

    # rellenar huecos con la media global si falta alguna hora
    known = [v for v in hourly_avg.values() if v is not None]
    fallback = statistics.mean(known) if known else 300.0
    for h in range(24):
        if hourly_avg[h] is None:
            hourly_avg[h] = fallback

    now = datetime.now()
    return [hourly_avg[(now.hour + i) % 24] for i in range(horizon_hours)]


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

"""
Historico ligero de decisiones ya ejecutadas (no previstas), para poder
mostrar la tabla completa del dia (00:00 a 00:00) mezclando lo que ya paso
con lo que queda por delante.

Una entrada por HORA de reloj (clave "YYYY-MM-DDTHH"): cada ciclo dentro de
esa hora sobreescribe la entrada con la ultima decision real tomada, asi
que al cerrar la hora queda registrado lo que de verdad se aplico.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta

HISTORY_PATH = os.environ.get("HISTORY_PATH", "/data/history.json")
MAX_AGE_HOURS = 24 * 8  # 8 dias: cubre la comparativa de "hoy vs media de los ultimos 7 dias"

_lock = threading.RLock()


def _hour_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H")


def _load() -> dict:
    with _lock:
        if not os.path.exists(HISTORY_PATH):
            return {}
        try:
            with open(HISTORY_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    with _lock:
        with open(HISTORY_PATH, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)


def record(now: datetime, entry: dict) -> None:
    """Guarda/actualiza la entrada de la hora actual con la decision real tomada."""
    data = _load()
    data[_hour_key(now)] = entry

    cutoff = now - timedelta(hours=MAX_AGE_HOURS)
    data = {k: v for k, v in data.items() if k >= _hour_key(cutoff)}

    _save(data)


def get_all() -> list[dict]:
    """Todo lo que quede retenido (hasta MAX_AGE_HOURS, 8 dias), ordenado
    por hora — para reconstruir un historico real en vez de un salto de
    golpe (ver ha_statistics.py)."""
    data = _load()
    return [v for k, v in sorted(data.items())]


def get_today(now: datetime) -> list[dict]:
    """Entradas ya ejecutadas de HOY (desde las 00:00 hasta la hora actual, sin incluirla)."""
    data = _load()
    today_prefix = now.strftime("%Y-%m-%d")
    entries = [v for k, v in sorted(data.items()) if k.startswith(today_prefix) and k < _hour_key(now)]
    return entries


def get_recent_days_consumption(now: datetime, days: int = 7) -> dict | None:
    """
    Compara el consumo acumulado de HOY (desde las 00:00 hasta ahora) con la
    media de los `days` dias anteriores, cada uno hasta la MISMA hora del
    dia — para que la comparacion sea justa (medio dia contra medio dia, no
    contra un dia entero). Se calcula solo a partir del propio historico ya
    guardado (campo "load_w" de cada hora), nada nuevo que pedir a HA.

    Devuelve None si no hay al menos un dia previo completo con el que
    comparar (instalacion recien estrenada).
    """
    data = _load()
    today_prefix = now.strftime("%Y-%m-%d")
    current_hour = now.hour

    by_date: dict[str, float] = {}
    for k, v in data.items():
        date_part, hour_part = k.split("T")
        if int(hour_part) >= current_hour:
            continue
        load_w = v.get("load_w")
        if load_w is None:
            continue
        by_date[date_part] = by_date.get(date_part, 0.0) + load_w / 1000.0  # Wh -> kWh (1 entrada = 1 hora)

    today_kwh = by_date.pop(today_prefix, 0.0)
    past_dates = sorted(by_date.keys())[-days:]
    if not past_dates:
        return None

    avg_kwh = sum(by_date[d] for d in past_dates) / len(past_dates)
    if avg_kwh <= 0:
        return None

    return {
        "today_kwh": round(today_kwh, 2),
        "avg_kwh": round(avg_kwh, 2),
        "days_compared": len(past_dates),
        "delta_pct": round(100 * (today_kwh - avg_kwh) / avg_kwh, 1),
    }

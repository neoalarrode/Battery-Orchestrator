"""
Guarda la primera prediccion de SOC agregado que hace el plan al empezar
cada hora, para poder comparar mas tarde — cuando esa hora termina —
cuanto se desvio la realidad de lo previsto. No es una alarma de fallo del
sistema: es un indicador honesto de cuanto se puede fiar uno de la
previsión de consumo/solar de esa hora en concreto (p.ej. si alguien
enciende un aparato que dispara el consumo muy por encima de lo previsto,
la desviacion sube y se nota en la interfaz).

La prediccion se guarda UNA SOLA VEZ por hora (la primera vez que se ve),
no se va actualizando cada ciclo — si se fuera actualizando, para el
final de la hora la "prediccion" ya habria convergido casi al valor real
y la comparacion perderia todo el sentido.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime

FORECAST_PATH = os.environ.get("FORECAST_PATH", "/data/forecast_accuracy.json")

_lock = threading.RLock()


def _hour_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H")


def _load() -> dict:
    with _lock:
        if not os.path.exists(FORECAST_PATH):
            return {}
        try:
            with open(FORECAST_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(FORECAST_PATH), exist_ok=True)
    with _lock:
        with open(FORECAST_PATH, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)


def record_and_compare(now: datetime, predicted_end_of_hour_soc_pct: float, actual_soc_pct_now: float) -> dict | None:
    """
    Llamar UNA vez por ciclo con la prediccion del plan para el final de
    ESTA hora y el SOC real medido ahora mismo.

    Si la hora ha cambiado desde la ultima llamada, la prediccion que
    habia guardada era la de la hora que ACABA de terminar: se compara
    contra el SOC real de ahora (la mejor foto disponible de como quedo)
    y el resultado se guarda como "el ultimo resultado conocido", que se
    queda fijo hasta que termine la proxima hora. Devuelve ese resultado
    ({hour, predicted_pct, actual_pct, deviation_pct}), o None si todavia
    no ha pasado ninguna hora completa desde que arranco el add-on.
    """
    data = _load()
    key = _hour_key(now)
    if data.get("current_hour") != key:
        prev_key = data.get("current_hour")
        prev_pred = data.get("predicted_pct")
        if prev_key is not None and prev_pred is not None:
            data["last_result"] = {
                "hour": prev_key,
                "predicted_pct": prev_pred,
                "actual_pct": round(actual_soc_pct_now, 1),
                "deviation_pct": round(actual_soc_pct_now - prev_pred, 1),
            }
        data["current_hour"] = key
        data["predicted_pct"] = round(predicted_end_of_hour_soc_pct, 1)
        _save(data)
    return data.get("last_result")

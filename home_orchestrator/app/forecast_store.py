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
        # Escritura ATOMICA (.tmp + os.replace) -- ver config_store._write_raw:
        # un corte a mitad de un `open(..., "w")` directo dejaba el fichero
        # truncado o con dos objetos JSON concatenados.
        tmp = FORECAST_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, FORECAST_PATH)


def record_and_compare(now: datetime, predicted_end_of_hour_soc_pct: float, actual_soc_pct_now: float) -> dict | None:
    """
    Llamar UNA vez por ciclo con la prediccion del plan para el final de
    ESTA hora y el SOC real medido ahora mismo.

    Si la hora ha cambiado desde la ultima llamada, la prediccion que
    habia guardada era la de la hora que ACABA de terminar: se compara
    contra el SOC real de ahora (la mejor foto disponible de como quedo)
    y el resultado se guarda como "el ultimo resultado conocido", que se
    queda fijo hasta que termine la proxima hora.

    Ademas de la desviacion en puntos de SOC (actual - predicho), se
    guarda "predicted_delta_pct": cuanto CAMBIO preveia el plan para esa
    hora (predicho - el SOC real que habia AL EMPEZARLA, guardado en su
    momento). Sirve para poder expresar la desviacion como un % de
    fiabilidad RELATIVO a lo que se esperaba que se moviera la bateria esa
    hora, en vez de una resta directa contra 100 — una desviacion de 3
    puntos es gravisima si solo se preveia mover 2, e insignificante si se
    preveia mover 25 (ver renderNextPunta en el frontend, que hace esa cuenta).

    Devuelve el ultimo resultado
    ({hour, predicted_pct, actual_pct, deviation_pct, predicted_delta_pct}),
    o None si todavia no ha pasado ninguna hora completa desde que arranco
    el add-on.
    """
    data = _load()
    key = _hour_key(now)
    if data.get("current_hour") != key:
        prev_key = data.get("current_hour")
        prev_pred = data.get("predicted_pct")
        prev_start = data.get("start_pct")
        if prev_key is not None and prev_pred is not None:
            predicted_delta = prev_pred - prev_start if prev_start is not None else 0.0
            data["last_result"] = {
                "hour": prev_key,
                "predicted_pct": prev_pred,
                "actual_pct": round(actual_soc_pct_now, 1),
                "deviation_pct": round(actual_soc_pct_now - prev_pred, 1),
                "predicted_delta_pct": round(predicted_delta, 1),
            }
        data["current_hour"] = key
        data["predicted_pct"] = round(predicted_end_of_hour_soc_pct, 1)
        # SOC real justo al empezar esta hora — referencia para calcular
        # cuanto preveia moverse la bateria (ver predicted_delta_pct arriba).
        data["start_pct"] = round(actual_soc_pct_now, 1)
        _save(data)
    return data.get("last_result")

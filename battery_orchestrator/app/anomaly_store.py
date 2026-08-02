"""
Detecta cuando el consumo real medido ahora mismo se dispara muy por
encima de lo que la previsión (media historica de esa hora del dia)
esperaba — señal de que algo se ha quedado encendido o hay un consumo
fuera de lo normal.

Nada de aprendizaje automatico: se compara el dato de ahora contra la
previsión ya existente, con un margen relativo Y un minimo absoluto (para
no disparar con bases de consumo pequeñas donde un 60% de mas son 30W sin
importancia), y se exige que se repita varios ciclos seguidos antes de
avisar — para no reaccionar a un pico de un instante — y varios ciclos
seguidos normales antes de desactivar la alerta.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime

ANOMALY_PATH = os.environ.get("ANOMALY_PATH", "/data/anomaly.json")

THRESHOLD_RATIO = 1.6   # el consumo real tiene que superar la previsión en un 60%...
THRESHOLD_MIN_W = 400   # ...Y superarla en al menos 400W, para no disparar con bases pequeñas
CONFIRM_CYCLES = 3      # ciclos seguidos por encima antes de confirmar la anomalia
CLEAR_CYCLES = 3        # ciclos seguidos normales antes de darla por resuelta

_lock = threading.RLock()


def _default() -> dict:
    return {
        "status": "ok", "since": None, "over_streak": 0, "under_streak": 0,
        "live_load_w": None, "expected_load_w": None,
    }


def _load() -> dict:
    with _lock:
        if not os.path.exists(ANOMALY_PATH):
            return _default()
        try:
            with open(ANOMALY_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return _default()


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(ANOMALY_PATH), exist_ok=True)
    with _lock:
        with open(ANOMALY_PATH, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)


def update(now: datetime, live_load_w: float, expected_load_w: float) -> dict:
    """
    Llamar una vez por ciclo con el consumo real medido ahora mismo y el
    esperado para esta hora. Devuelve el estado tras aplicar esta lectura,
    con "changed": True si el estado (ok/anomaly) acaba de cambiar en este
    ciclo — para saber cuando toca notificar en vez de repetir cada minuto.
    """
    data = _load()
    was_status = data["status"]

    is_over = (
        live_load_w > expected_load_w * THRESHOLD_RATIO
        and live_load_w - expected_load_w > THRESHOLD_MIN_W
    )

    if is_over:
        data["over_streak"] = data.get("over_streak", 0) + 1
        data["under_streak"] = 0
    else:
        data["under_streak"] = data.get("under_streak", 0) + 1
        data["over_streak"] = 0

    if data["status"] == "ok" and data["over_streak"] >= CONFIRM_CYCLES:
        data["status"] = "anomaly"
        data["since"] = now.isoformat()
    elif data["status"] == "anomaly" and data["under_streak"] >= CLEAR_CYCLES:
        data["status"] = "ok"
        data["since"] = None

    data["live_load_w"] = round(live_load_w)
    data["expected_load_w"] = round(expected_load_w)
    _save(data)

    return {**data, "changed": data["status"] != was_status}


def get_status() -> dict:
    data = _load()
    return {k: v for k, v in data.items() if k not in ("over_streak", "under_streak")}

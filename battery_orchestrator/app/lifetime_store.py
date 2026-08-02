"""
Contador de por vida (sin caducidad, a diferencia de history_store) de
energia cargada/descargada por cada bateria, para poder estimar su "salud"
aproximada: ciclos equivalentes = energia total movida / (2 x capacidad).

Es una estimacion, no una medicion de verdad (para eso haria falta un BMS
que reporte el dato) — pero es honesta: se explica de donde sale el numero
y desde cuando se cuenta, nada de caja negra.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime

LIFETIME_PATH = os.environ.get("LIFETIME_PATH", "/data/lifetime.json")

_lock = threading.RLock()


def _load() -> dict:
    with _lock:
        if not os.path.exists(LIFETIME_PATH):
            return {}
        try:
            with open(LIFETIME_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(LIFETIME_PATH), exist_ok=True)
    with _lock:
        with open(LIFETIME_PATH, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)


def accumulate(battery_id: str, battery_name: str, charged_wh: float, discharged_wh: float) -> None:
    """Suma la energia movida en este ciclo al acumulado de por vida de esa bateria."""
    if charged_wh <= 0 and discharged_wh <= 0:
        return
    data = _load()
    entry = data.get(battery_id) or {
        "name": battery_name,
        "since": datetime.now().isoformat(),
        "charged_wh": 0.0,
        "discharged_wh": 0.0,
    }
    entry["name"] = battery_name  # por si cambio el nombre
    entry["charged_wh"] += charged_wh
    entry["discharged_wh"] += discharged_wh
    data[battery_id] = entry
    _save(data)


def get_health(battery_id: str, capacity_wh: float) -> dict | None:
    data = _load()
    entry = data.get(battery_id)
    if not entry or capacity_wh <= 0:
        return None
    total_throughput_wh = entry["charged_wh"] + entry["discharged_wh"]
    # un "ciclo completo" mueve 2x la capacidad (una carga + una descarga completas)
    equivalent_cycles = total_throughput_wh / (2 * capacity_wh)
    return {
        "name": entry["name"],
        "since": entry["since"],
        "charged_kwh": round(entry["charged_wh"] / 1000, 2),
        "discharged_kwh": round(entry["discharged_wh"] / 1000, 2),
        "equivalent_cycles": round(equivalent_cycles, 1),
    }


def get_all_health(batteries: list[dict]) -> list[dict]:
    out = []
    for b in batteries:
        h = get_health(b["id"], float(b.get("capacity_wh", 0)))
        if h:
            out.append(h)
        else:
            out.append({
                "name": b["name"], "since": None, "charged_kwh": 0.0,
                "discharged_kwh": 0.0, "equivalent_cycles": 0.0,
            })
    return out

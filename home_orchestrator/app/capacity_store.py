"""
Estima la capacidad REAL de cada bateria (a diferencia de la capacidad
declarada por el usuario) observando cuanta energia hace falta para mover
su SOC un delta conocido durante cargas/descargas reales:

    capacidad_real_wh = energia_movida_wh / (delta_soc_pct / 100)

Un segmento (tramo continuo de carga o de descarga) se cierra y se
registra como observacion solo si el delta de SOC es suficientemente
grande (MIN_DELTA_PCT) para que el ruido de medida no domine el calculo.
La salud reportada es la mediana de las ultimas observaciones (mas
robusta que la media frente a una lectura rara), no un dato del BMS.
"""

from __future__ import annotations

import json
import os
import statistics
import threading

CAPACITY_PATH = os.environ.get("CAPACITY_PATH", "/data/capacity.json")

MIN_DELTA_PCT = 8.0    # ignorar segmentos demasiado cortos (ruido de medida)
MAX_OBSERVATIONS = 12  # ventana de observaciones recientes que se conservan

_lock = threading.RLock()


def _load() -> dict:
    with _lock:
        if not os.path.exists(CAPACITY_PATH):
            return {}
        try:
            with open(CAPACITY_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(CAPACITY_PATH), exist_ok=True)
    with _lock:
        with open(CAPACITY_PATH, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)


def _default_entry(name: str) -> dict:
    return {
        "name": name,
        "segment_action": None,
        "segment_start_soc": None,
        "segment_energy_wh": 0.0,
        "observations": [],
    }


def _close_segment(entry: dict, current_soc_pct: float) -> None:
    if entry["segment_action"] not in ("charge", "discharge") or entry["segment_start_soc"] is None:
        return
    delta = abs(current_soc_pct - entry["segment_start_soc"])
    if delta >= MIN_DELTA_PCT and entry["segment_energy_wh"] > 0:
        capacity_wh = entry["segment_energy_wh"] / (delta / 100)
        entry["observations"].append(round(capacity_wh, 1))
        entry["observations"] = entry["observations"][-MAX_OBSERVATIONS:]


def update(battery_id: str, battery_name: str, soc_pct: float | None,
           action: str | None, energy_wh_this_cycle: float,
           legacy_id: str | None = None) -> None:
    """
    Llamar una vez por ciclo con el SOC actual (%), la accion real de ESTA
    bateria ahora mismo ("charge" | "discharge" | None) y la energia (Wh,
    siempre positiva) movida en este ciclo. Cuando la accion cambia (o se
    para), se cierra el segmento anterior y, si el delta de SOC acumulado
    es suficiente, se registra como observacion.

    `battery_id` deberia ser una clave ESTABLE (ver `_stable_battery_key`
    en main.py) — si no hay entrada bajo esa clave pero SI la hay bajo
    `legacy_id` (el id de configuracion de antes de este cambio), se migra
    ese historico de observaciones en vez de perderlo.
    """
    if soc_pct is None:
        return
    data = _load()
    entry = data.get(battery_id)
    if entry is None and legacy_id and legacy_id in data:
        entry = data.pop(legacy_id)
    if entry is None:
        entry = _default_entry(battery_name)
    entry["name"] = battery_name

    if action != entry["segment_action"]:
        _close_segment(entry, soc_pct)
        entry["segment_action"] = action
        entry["segment_start_soc"] = soc_pct
        entry["segment_energy_wh"] = 0.0

    if action in ("charge", "discharge"):
        entry["segment_energy_wh"] += energy_wh_this_cycle

    data[battery_id] = entry
    _save(data)


def get_health(battery_id: str, declared_capacity_wh: float, display_id: str | None = None) -> dict | None:
    data = _load()
    entry = data.get(battery_id)
    if not entry or not entry["observations"] or declared_capacity_wh <= 0:
        return None
    real_capacity_wh = statistics.median(entry["observations"])
    health_pct = min(100.0, real_capacity_wh / declared_capacity_wh * 100)
    return {
        "id": display_id if display_id is not None else battery_id,
        "name": entry["name"],
        "declared_capacity_wh": declared_capacity_wh,
        "estimated_capacity_wh": round(real_capacity_wh),
        "health_pct": round(health_pct, 1),
        "observations": len(entry["observations"]),
    }


def _stable_key(b: dict) -> str:
    """Ver comentario homologo en lifetime_store.py / main.py."""
    if b.get("source") == "ecoflow":
        ident = b.get("ecoflow_sn") or b.get("ecoflow_ble_address")
        if ident:
            return f"ecoflow:{ident}"
    elif b.get("soc_sensor"):
        return f"ha:{b['soc_sensor']}"
    return b["id"]


def get_all_health(batteries: list[dict]) -> list[dict]:
    out = []
    for b in batteries:
        declared = float(b.get("capacity_wh", 0))
        h = get_health(_stable_key(b), declared, display_id=b["id"])
        if h:
            out.append(h)
        else:
            out.append({
                "id": b["id"], "name": b["name"], "declared_capacity_wh": declared,
                "estimated_capacity_wh": None, "health_pct": None, "observations": 0,
            })
    return out

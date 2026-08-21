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
        # Escritura ATOMICA (.tmp + os.replace) -- ver config_store._write_raw:
        # un corte a mitad de un `open(..., "w")` directo dejaba el fichero
        # truncado o con dos objetos JSON concatenados.
        tmp = LIFETIME_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, LIFETIME_PATH)


def accumulate(battery_id: str, battery_name: str, charged_wh: float, discharged_wh: float,
                legacy_id: str | None = None) -> None:
    """
    Suma la energia movida en este ciclo al acumulado de por vida de esa
    bateria. `battery_id` deberia ser una clave ESTABLE (ver
    `_stable_battery_key` en main.py), no el id de configuracion volatil
    — si no hay ninguna entrada todavia bajo esa clave pero SI la hay bajo
    `legacy_id` (el id de configuracion de antes de este cambio, o de una
    version anterior de la app), se migra ese historico en vez de
    empezar de cero, para no perder lo ya acumulado.
    """
    if charged_wh <= 0 and discharged_wh <= 0:
        return
    data = _load()
    entry = data.get(battery_id)
    if entry is None and legacy_id and legacy_id in data:
        entry = data.pop(legacy_id)
    if entry is None:
        entry = {
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


def rescale_to_aggregate(battery_ids: list[str], target_charged_wh: float,
                         target_discharged_wh: float) -> dict:
    """Reescala los acumulados de estas baterias para que su SUMA sea la
    indicada, conservando el reparto relativo que ya tenian.

    Necesario para deshacer una contabilizacion inflada sin tirar el historico:
    la potencia que reportan las baterias EcoFlow de un grupo enlazado es la
    del GRUPO (no la de cada unidad), asi que antes de la desduplicacion CADA
    bateria declarada sumaba el total del grupo -- con N unidades, el agregado
    salia Nx. Reescalar al total correcto arregla el agregado (que es lo que
    consume el Panel de Energia) sin inventar una atribucion por unidad que los
    datos no permiten conocer.

    Si todas las entradas estaban infladas por igual (el caso tipico), el
    reparto proporcional deja a cada una con su parte correspondiente."""
    with _lock:
        data = _load()
        cur_charged = sum(data.get(bid, {}).get("charged_wh", 0.0) for bid in battery_ids)
        cur_discharged = sum(data.get(bid, {}).get("discharged_wh", 0.0) for bid in battery_ids)
        for bid in battery_ids:
            entry = data.get(bid)
            if entry is None:
                continue
            if cur_charged > 0:
                entry["charged_wh"] = entry.get("charged_wh", 0.0) / cur_charged * max(0.0, target_charged_wh)
            if cur_discharged > 0:
                entry["discharged_wh"] = entry.get("discharged_wh", 0.0) / cur_discharged * max(0.0, target_discharged_wh)
            data[bid] = entry
        _save(data)
        return {
            "before": {"charged_wh": cur_charged, "discharged_wh": cur_discharged},
            "after": {"charged_wh": target_charged_wh, "discharged_wh": target_discharged_wh},
        }


def get_health(battery_id: str, capacity_wh: float, display_id: str | None = None) -> dict | None:
    data = _load()
    entry = data.get(battery_id)
    if not entry or capacity_wh <= 0:
        return None
    total_throughput_wh = entry["charged_wh"] + entry["discharged_wh"]
    # un "ciclo completo" mueve 2x la capacidad (una carga + una descarga completas)
    equivalent_cycles = total_throughput_wh / (2 * capacity_wh)
    return {
        "id": display_id if display_id is not None else battery_id,
        "name": entry["name"],
        "since": entry["since"],
        "charged_kwh": round(entry["charged_wh"] / 1000, 2),
        "discharged_kwh": round(entry["discharged_wh"] / 1000, 2),
        "equivalent_cycles": round(equivalent_cycles, 1),
    }


def _stable_key(b: dict) -> str:
    """Version dict-friendly del mismo criterio que `_stable_battery_key`
    en main.py — necesaria aqui porque `get_all_health` recibe la config
    cruda (`cfg["batteries"]`), no objetos `Battery`."""
    if b.get("source") == "ecoflow":
        ident = b.get("ecoflow_sn") or b.get("ecoflow_ble_address")
        if ident:
            return f"ecoflow:{ident}"
    elif b.get("soc_sensor"):
        return f"ha:{b['soc_sensor']}"
    return b["id"]


def get_aggregate_totals(battery_ids: list[str]) -> dict:
    """
    Suma charged_wh/discharged_wh de TODAS las baterias declaradas — para
    publicar sensores agregados aptos para el Panel de Energia oficial de
    HA (Ajustes -> Paneles -> Energia -> Baterias), que necesita un sensor
    de energia ACUMULADA (nunca decrece) por cada direccion, no uno por
    bateria por separado. Mismos numeros de por vida que ya alimentan
    "ciclos equivalentes" en `get_health` — ninguna cuenta nueva, solo se
    suman entre baterias.
    """
    data = _load()
    charged_wh = sum(data.get(bid, {}).get("charged_wh", 0.0) for bid in battery_ids)
    discharged_wh = sum(data.get(bid, {}).get("discharged_wh", 0.0) for bid in battery_ids)
    since_values = [data[bid]["since"] for bid in battery_ids if bid in data and data[bid].get("since")]
    return {
        "charged_wh": charged_wh,
        "discharged_wh": discharged_wh,
        "since": min(since_values) if since_values else None,
    }


def get_all_health(batteries: list[dict]) -> list[dict]:
    out = []
    for b in batteries:
        h = get_health(_stable_key(b), float(b.get("capacity_wh", 0)), display_id=b["id"])
        if h:
            out.append(h)
        else:
            out.append({
                "id": b["id"], "name": b["name"], "since": None, "charged_kwh": 0.0,
                "discharged_kwh": 0.0, "equivalent_cycles": 0.0,
            })
    return out

"""
Contador de por vida (sin caducidad) de energia solar total producida
por la casa (arrays con sensor de Home Assistant + puertos MPPT de
baterias EcoFlow, todo junto) -- a diferencia de lifetime_store.py (que
lleva la cuenta POR BATERIA, cargada/descargada), aqui hay una sola
magnitud: toda la solar generada, sin distinguir de que array vino.

Se integra en `_live_sensor_loop` (main.py) a partir de la potencia
solar en vivo (`_live_solar_now_w`), multiplicando por el tiempo real
transcurrido desde la ultima lectura -- no un intervalo fijo asumido,
para no arrastrar error si algun ciclo se salta o tarda mas de la
cuenta. Sirve para sensor.battery_orchestrator_solar_energy (kWh,
state_class total_increasing), el sensor que pide el Panel de Energia
oficial de HA para "Produccion de energia solar" (distinto del sensor
de potencia en W, que solo vale para "Energia de produccion solar" en
tiempo real, no para el acumulado).
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime

SOLAR_ENERGY_PATH = os.environ.get("SOLAR_ENERGY_PATH", "/data/solar_energy.json")

_lock = threading.RLock()


def _load() -> dict:
    with _lock:
        if not os.path.exists(SOLAR_ENERGY_PATH):
            return {"since": None, "wh": 0.0}
        try:
            with open(SOLAR_ENERGY_PATH) as f:
                data = json.load(f)
                data.setdefault("since", None)
                data.setdefault("wh", 0.0)
                return data
        except (json.JSONDecodeError, OSError):
            return {"since": None, "wh": 0.0}


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(SOLAR_ENERGY_PATH), exist_ok=True)
    with _lock:
        with open(SOLAR_ENERGY_PATH, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)


def accumulate(wh: float) -> None:
    """Suma `wh` (siempre >= 0, energia real movida desde la ultima
    lectura) al total de por vida."""
    if wh <= 0:
        return
    data = _load()
    if data["since"] is None:
        data["since"] = datetime.now().isoformat()
    data["wh"] += wh
    _save(data)


def get_total_wh() -> dict:
    """{"wh": ..., "since": ...} -- `since` es `None` si todavia no se
    ha registrado ninguna energia."""
    return _load()

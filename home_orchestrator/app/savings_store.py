"""
Registra, hora a hora, el ahorro real conseguido.

Se compara lo que de verdad se ha pagado (comprar a red lo que el consumo
directo necesite mas lo que se cargue de red en la bateria) contra lo que
se habria pagado SIN bateria (comprar directamente a red lo que el solar
no cubra, cada hora a su precio real). La diferencia acumulada es el
ahorro — positiva casi siempre en conjunto, aunque en horas de carga desde
red pueda ser momentaneamente negativa (esa energia se recupera despues,
al evitar comprar en punta).

Nada de estimaciones de "lo que podrias ahorrar": es literalmente
coste_sin_bateria - coste_real, con los mismos datos que usa el planificador.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime

SAVINGS_PATH = os.environ.get("SAVINGS_PATH", "/data/savings.json")
MAX_DAYS = 400  # poco mas de un año de historico diario, para no crecer sin limite

_lock = threading.RLock()


def _default() -> dict:
    return {"since": None, "total_real_eur": 0.0, "total_baseline_eur": 0.0, "days": {}}


def _load() -> dict:
    with _lock:
        if not os.path.exists(SAVINGS_PATH):
            return _default()
        try:
            with open(SAVINGS_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return _default()


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(SAVINGS_PATH), exist_ok=True)
    with _lock:
        # Escritura ATOMICA (.tmp + os.replace) -- ver config_store._write_raw:
        # un corte a mitad de un `open(..., "w")` directo dejaba el fichero
        # truncado o con dos objetos JSON concatenados.
        tmp = SAVINGS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, SAVINGS_PATH)


def record(now: datetime, real_cost_eur: float, baseline_cost_eur: float) -> None:
    data = _load()
    if data["since"] is None:
        data["since"] = now.isoformat()

    data["total_real_eur"] += real_cost_eur
    data["total_baseline_eur"] += baseline_cost_eur

    day_key = now.strftime("%Y-%m-%d")
    day = data["days"].get(day_key) or {"real_eur": 0.0, "baseline_eur": 0.0}
    day["real_eur"] += real_cost_eur
    day["baseline_eur"] += baseline_cost_eur
    data["days"][day_key] = day

    if len(data["days"]) > MAX_DAYS:
        for k in sorted(data["days"].keys())[: len(data["days"]) - MAX_DAYS]:
            del data["days"][k]

    _save(data)


def get_summary(now: datetime) -> dict:
    data = _load()
    today = data["days"].get(now.strftime("%Y-%m-%d")) or {"real_eur": 0.0, "baseline_eur": 0.0}
    return {
        "since": data["since"],
        "today_savings_eur": round(today["baseline_eur"] - today["real_eur"], 3),
        "total_savings_eur": round(data["total_baseline_eur"] - data["total_real_eur"], 3),
        "total_real_eur": round(data["total_real_eur"], 3),
        "total_baseline_eur": round(data["total_baseline_eur"], 3),
    }

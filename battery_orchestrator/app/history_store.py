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
MAX_AGE_HOURS = 72  # no hace falta guardar mas de un par de dias

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


def get_today(now: datetime) -> list[dict]:
    """Entradas ya ejecutadas de HOY (desde las 00:00 hasta la hora actual, sin incluirla)."""
    data = _load()
    today_prefix = now.strftime("%Y-%m-%d")
    entries = [v for k, v in sorted(data.items()) if k.startswith(today_prefix) and k < _hour_key(now)]
    return entries

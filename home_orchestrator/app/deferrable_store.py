"""
Estado en tiempo de ejecucion de las cargas diferibles — no es configuracion
del usuario (eso vive en config_store.py junto a baterias y paneles), es lo
que la app va decidiendo y midiendo sola:

  - "schedules": la ventana horaria decidida para cada carga, segun su
    frecuencia (ver deferrable_scheduler.py).
  - "sessions": mientras esta encendida, va acumulando la energia real
    medida por su sensor de potencia (si lo tiene); al apagarse, esa
    energia pasa a un historico corto que sirve para estimar el consumo
    tipico de la carga sin que el usuario tenga que indicarlo a mano.
    Tambien lleva la cuenta de ciclos seguidos sin excedente solar, para
    poder interrumpir antes de tiempo las cargas marcadas como
    interrumpibles sin reaccionar a un bache de un solo ciclo.
"""

from __future__ import annotations

import json
import os
import statistics
import threading
from datetime import datetime

DEFERRABLE_PATH = os.environ.get("DEFERRABLE_PATH", "/data/deferrable.json")
MAX_SESSIONS = 12  # activaciones pasadas guardadas, para la mediana de energia

# Mismo motivo y mismo valor que ENERGY_ACCUMULATE_MAX_GAP_SECONDS en main.py
# (energia de baterias): tope duro al integrar potencia * tiempo real
# transcurrido, para no inventar energia sobre un hueco largo sin ciclos
# (p.ej. tras un reinicio del addon con una sesion que se habia quedado
# "activa" en disco).
MAX_ACCUMULATE_GAP_SECONDS = 300

_lock = threading.RLock()


def _default_session() -> dict:
    return {
        "active_since": None, "active_energy_wh": 0.0, "no_surplus_streak": 0,
        # Marca de tiempo REAL del ultimo accumulate_session_energy() de
        # esta sesion — ver esa funcion. Necesaria para integrar potencia
        # * tiempo REALMENTE transcurrido entre ciclos, no un "cycle_hours"
        # nominal que asume que run_cycle() se ejecuta siempre cada
        # cycle_seconds exactos (no es cierto con el ciclo reactivo, ver
        # el comentario homologo en main.py sobre la energia de baterias
        # — este mismo fallo, ya corregido ahi, seguia sin corregir aqui).
        "last_accumulate_ts": None,
        # "history_wh"/"history_minutes" van EMPAREJADAS indice a indice: la
        # energia y la duracion de la misma activacion pasada — la duracion
        # sirve para que una carga NO interrumpible (p.ej. una lavadora) vea
        # crecer sola su ventana programada hasta cubrir lo que de verdad
        # tarda su programa, en vez de cortarla a medias por una duracion
        # configurada a mano demasiado corta. Una carga interrumpible
        # (p.ej. un termo, que enciende y apaga solo segun necesita calentar
        # mientras nuestro switch se lo permite) no depende de esto: puede
        # cortarse en cualquier momento sin problema.
        "history_wh": [], "history_minutes": [],
    }


def _default() -> dict:
    return {"schedules": {}, "sessions": {}}


def _load() -> dict:
    with _lock:
        if not os.path.exists(DEFERRABLE_PATH):
            return _default()
        try:
            with open(DEFERRABLE_PATH) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return _default()
        data.setdefault("schedules", {})
        data.setdefault("sessions", {})
        return data


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(DEFERRABLE_PATH), exist_ok=True)
    with _lock:
        with open(DEFERRABLE_PATH, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)


def get_schedule(load_id: str) -> dict | None:
    return _load()["schedules"].get(load_id)


def save_schedule(load_id: str, schedule: dict) -> None:
    data = _load()
    data["schedules"][load_id] = schedule
    _save(data)


def clear_load(load_id: str) -> None:
    """Al borrar una carga diferible desde la interfaz, limpiar tambien su estado guardado."""
    data = _load()
    data["schedules"].pop(load_id, None)
    data["sessions"].pop(load_id, None)
    _save(data)


def truncate_occurrence_now(load_id: str, now: datetime) -> None:
    """
    Corta en seco la ventana que este activa ahora mismo — interrupcion
    anticipada de una carga interrumpible cuando el excedente solar que la
    justificaba ya no esta. Su fin pasa a ser ahora mismo, asi que en el
    resto de ciclos deja de estar "en ventana" sin tener que recalcular
    todo el dia.
    """
    data = _load()
    schedule = data["schedules"].get(load_id)
    if not schedule:
        return
    for occ in schedule.get("occurrences", []):
        start, end = datetime.fromisoformat(occ["start"]), datetime.fromisoformat(occ["end"])
        if start <= now < end:
            occ["end"] = now.isoformat()
            occ["interrupted"] = True
    _save(data)


def record_no_surplus_streak(load_id: str, has_surplus: bool) -> int:
    data = _load()
    sess = data["sessions"].setdefault(load_id, _default_session())
    sess["no_surplus_streak"] = 0 if has_surplus else sess.get("no_surplus_streak", 0) + 1
    _save(data)
    return sess["no_surplus_streak"]


def record_session_start(load_id: str, now: datetime) -> None:
    data = _load()
    sess = data["sessions"].setdefault(load_id, _default_session())
    if sess.get("active_since") is None:
        sess["active_since"] = now.isoformat()
        sess["active_energy_wh"] = 0.0
        sess["last_accumulate_ts"] = now.isoformat()
    _save(data)


def accumulate_session_energy(load_id: str, power_w: float, now: datetime) -> None:
    """
    Integra potencia (W) * tiempo REAL transcurrido desde la ultima llamada
    (no un "cycle_hours" nominal fijo — con el ciclo reactivo, run_cycle()
    puede ejecutarse mucho mas a menudo que cycle_seconds, y multiplicar
    por el nominal en cada ejecucion reactiva contaba energia de mas cada
    vez; mismo fallo que ya se corrigio para la energia de baterias, ver
    el comentario homologo en main.py). El primer tick de una sesion nueva
    (last_accumulate_ts == active_since, puesto por record_session_start)
    integra 0 Wh -- correcto, todavia no ha pasado tiempo real.
    """
    data = _load()
    sess = data["sessions"].setdefault(load_id, _default_session())
    if sess.get("active_since") is None:
        return
    last_ts = sess.get("last_accumulate_ts")
    if last_ts is not None:
        elapsed_h = min((now - datetime.fromisoformat(last_ts)).total_seconds(), MAX_ACCUMULATE_GAP_SECONDS) / 3600
        if elapsed_h > 0:
            sess["active_energy_wh"] = sess.get("active_energy_wh", 0.0) + power_w * elapsed_h
    sess["last_accumulate_ts"] = now.isoformat()
    _save(data)


def end_session(load_id: str, now: datetime) -> float | None:
    """Cierra la sesion activa (si la habia) y devuelve la energia medida —
    None si no habia ninguna sesion en curso (nada que cerrar)."""
    data = _load()
    sess = data["sessions"].get(load_id)
    if not sess or sess.get("active_since") is None:
        return None
    energy = sess.get("active_energy_wh", 0.0)
    started = datetime.fromisoformat(sess["active_since"])
    duration_min = max(0.0, (now - started).total_seconds() / 60)
    sess["active_since"] = None
    sess["active_energy_wh"] = 0.0
    sess["no_surplus_streak"] = 0
    sess["last_accumulate_ts"] = None
    if energy > 1:  # ignora sesiones vacias/ruido de sensor
        history_wh = sess.setdefault("history_wh", [])
        history_min = sess.setdefault("history_minutes", [])
        history_wh.append(round(energy))
        history_min.append(round(duration_min, 1))
        sess["history_wh"] = history_wh[-MAX_SESSIONS:]
        sess["history_minutes"] = history_min[-MAX_SESSIONS:]
    _save(data)
    return energy


def get_estimated_energy_wh(load_id: str) -> float | None:
    """Mediana de las ultimas activaciones medidas — None si aun no hay ninguna."""
    history = _load()["sessions"].get(load_id, {}).get("history_wh", [])
    if not history:
        return None
    return statistics.median(history)


def get_estimated_duration_hours(load_id: str) -> float | None:
    """Mediana de la duracion real de las ultimas activaciones medidas
    (desde que se enciende hasta que se apaga) — None si aun no hay
    ninguna. Sirve para que las cargas no interrumpibles reserven una
    ventana que de verdad cubra lo que tarda su programa, sin depender de
    que el usuario acierte a mano cuanto dura."""
    history_min = _load()["sessions"].get(load_id, {}).get("history_minutes", [])
    if not history_min:
        return None
    return statistics.median(history_min) / 60


def get_session_info(load_id: str) -> dict:
    return _load()["sessions"].get(load_id, _default_session())

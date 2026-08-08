"""
Planificador de cargas diferibles: electrodomesticos con un enchufe/switch
controlable (lavadora, lavavajillas, termo electrico...) que no hace falta
que funcionen en un momento exacto, solo dentro de una ventana del dia.

Misma filosofia que scheduler.py: nada de programacion lineal ni parametros
ocultos, una regla simple y explicable por carga:

  1) Preferir la(s) hora(s) con excedente solar suficiente para cubrir su
     consumo estimado.
  2) Si no hay excedente suficiente en ninguna hora, la(s) hora(s) mas
     baratas disponibles (tipicamente valle).

La frecuencia la elige el usuario por carga (ver config_store.py):
  - "once": una unica vez dentro del horizonte de planificacion. Tras
    ejecutarse se marca "done" en su configuracion y no se vuelve a
    programar sola (el usuario puede "reprogramarla" desde la interfaz).
  - "daily": una vez cada dia, dentro de las horas que quedan de HOY.
  - "multiple_daily": varias veces cada dia (configurable), repartidas sin
    solaparse, cada una con la misma prioridad solar->barato.

Una vez una ventana ya ha EMPEZADO no se vuelve a recalcular (no tiene
sentido mover una carga que ya esta en marcha); las que aun no han
empezado si pueden recalcularse cada ciclo por si la previsión mejora.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import deferrable_store


def _avg(values: list[float], start: int, length: int) -> float:
    return sum(values[start:start + length]) / length


def _pick_blocks(surplus_w: list[float], prices: list[float], start_idx: int, end_idx: int,
                  duration: int, min_power_w: float, count: int,
                  blocked: set[int] | None = None) -> list[tuple[int, str]]:
    """
    Elige hasta `count` bloques de `duration` horas seguidas dentro de
    [start_idx, end_idx), sin solaparse entre si ni con las horas ya en
    `blocked`. Cada bloque: primero el de mayor excedente solar medio si
    llega al minimo necesario; si ninguno llega, el de menor precio medio.
    """
    blocked = set(blocked) if blocked else set()
    chosen: list[tuple[int, str]] = []

    for _ in range(count):
        candidates = [
            i for i in range(start_idx, end_idx - duration + 1)
            if not any((i + k) in blocked for k in range(duration))
        ]
        if not candidates:
            break

        best_solar = None
        for i in candidates:
            avg_surplus = _avg(surplus_w, i, duration)
            if avg_surplus >= min_power_w and (best_solar is None or avg_surplus > best_solar[0]):
                best_solar = (avg_surplus, i)

        if best_solar is not None:
            idx, mode = best_solar[1], "solar"
        else:
            best_cheap = None
            for i in candidates:
                avg_price = _avg(prices, i, duration)
                if best_cheap is None or avg_price < best_cheap[0]:
                    best_cheap = (avg_price, i)
            if best_cheap is None:
                break
            idx, mode = best_cheap[1], "cheap"

        chosen.append((idx, mode))
        blocked.update(range(idx, idx + duration))

    chosen.sort()
    return chosen


def _occurrence(plan_hours: list[datetime], idx: int, duration: int, mode: str, energy_wh: float) -> dict:
    start = plan_hours[idx]
    end = plan_hours[idx + duration - 1] + timedelta(hours=1)
    reason = (
        "excedente solar previsto suficiente" if mode == "solar"
        else "sin excedente solar suficiente: hora(s) mas barata(s) disponibles"
    )
    return {
        "start": start.isoformat(), "end": end.isoformat(),
        "mode": mode, "reason": reason, "energy_wh": round(energy_wh),
    }


def _hours_left_today(now: datetime, plan_hours: list[datetime]) -> int:
    today = now.date()
    for i, h in enumerate(plan_hours):
        if h.date() != today:
            return i
    return len(plan_hours)


def plan_for_load(load: dict, now: datetime, plan_hours: list[datetime],
                   pv_forecast_w: list[float], load_forecast_w: list[float],
                   charge_w_by_hour: list[float], charge_source_by_hour: list[str | None],
                   prices_by_hour: list[float]) -> dict | None:
    """
    Decide (o reutiliza, si ya estaba decidida y sigue vigente) la ventana
    horaria de esta carga diferible. Devuelve el "schedule" a usar por
    deferrable_exec.execute(), o None si no hay nada programado ahora mismo
    (frecuencia "once" ya ejecutada, o ninguna hora disponible que la cubra).
    """
    load_id = load["id"]
    horizon = len(plan_hours)
    configured_duration = max(1, int(load.get("duration_hours", 1)))
    frequency = load.get("frequency", "daily")

    # Cargas NO interrumpibles (p.ej. una lavadora: una vez arrancado su
    # programa no se debe cortar a medias) reservan, ademas de lo que el
    # usuario haya indicado a mano, lo que el propio historico de
    # activaciones diga que tarda de verdad su ciclo — asi la ventana
    # programada crece sola hasta cubrirlo aunque la duracion configurada
    # se quedara corta. Una carga interrumpible (p.ej. un termo, que
    # enciende y apaga solo segun necesita calentar mientras se lo permite
    # nuestro switch) no lo necesita: cortarla antes de tiempo no rompe nada.
    if not load.get("interruptible"):
        auto_duration_h = deferrable_store.get_estimated_duration_hours(load_id)
        if auto_duration_h:
            configured_duration = max(configured_duration, math.ceil(auto_duration_h))
    duration = max(1, min(horizon, configured_duration))

    manual_energy = float(load.get("estimated_energy_wh") or 0)
    auto_energy = deferrable_store.get_estimated_energy_wh(load_id)
    energy_wh = manual_energy or auto_energy or 500.0  # sin dato manual ni historico: estimacion de partida razonable
    min_power_w = energy_wh / duration

    # Excedente solar disponible para diferibles: lo que sobra de sol una
    # vez cubierto el consumo de la casa Y la carga de bateria que ya
    # tuviera planificada esa hora (para no competir por el mismo sol) —
    # si sobra despues de eso, es sol de verdad libre para esta carga.
    surplus_w = [
        max(0.0, pv_forecast_w[i] - load_forecast_w[i]
            - (charge_w_by_hour[i] if charge_source_by_hour[i] == "solar" else 0.0))
        for i in range(horizon)
    ]

    existing = deferrable_store.get_schedule(load_id)

    if frequency == "once":
        if load.get("done"):
            return None
        if existing and existing.get("occurrences"):
            # Ya hay una ocurrencia decidida — vigente (por empezar o en
            # curso) o recien terminada. En los DOS casos se reutiliza tal
            # cual, nunca se recalcula sola: si se recalculara justo al
            # terminar (comprobando si "end <= now"), pasaria ANTES de que
            # el resto del ciclo llegue a marcarla "done" (eso ocurre
            # despues, en deferrable_exec.execute()), y esa marca mira la
            # PRIMERA ocurrencia del schedule que haya en ese momento — si
            # aqui ya la hubieramos sustituido por una nueva, la original
            # nunca se marcaria "done" y la carga se re-programaria sin fin
            # en vez de ejecutarse una sola vez.
            return existing
        picks = _pick_blocks(surplus_w, prices_by_hour, 0, horizon, duration, min_power_w, count=1)
        if not picks:
            return None
        occ = _occurrence(plan_hours, picks[0][0], duration, picks[0][1], energy_wh)
        schedule = {"frequency": "once", "date": now.date().isoformat(), "occurrences": [occ]}
        deferrable_store.save_schedule(load_id, schedule)
        return schedule

    # "daily" / "multiple_daily": una decision por dia natural, sobre las
    # horas que quedan de HOY. Las ventanas que ya han empezado se
    # mantienen tal cual; solo se recalculan las que aun no han arrancado.
    # Si la carga tiene dias de la semana concretos (p.ej. una lavadora
    # solo lunes y sabado), los demas dias no se programa nada.
    days_of_week = load.get("days_of_week") or []
    if days_of_week and now.weekday() not in days_of_week:
        return None

    today = now.date().isoformat()
    count = 1 if frequency == "daily" else max(1, int(load.get("runs_per_day", 2)))
    hours_left_today = _hours_left_today(now, plan_hours)

    started = []
    if existing and existing.get("date") == today:
        started = [o for o in existing.get("occurrences", []) if datetime.fromisoformat(o["start"]) <= now]

    blocked = set()
    for occ in started:
        start_dt = datetime.fromisoformat(occ["start"])
        if start_dt not in plan_hours:
            # Ocurrencia de una hora que ya ha quedado fuera de la ventana
            # actual de `plan_hours` (p.ej. empezo a medianoche y el addon
            # no ha podido recalcular en horas, por un reinicio o un fallo
            # pasajero de HA) - no hay nada que bloquear para las horas de
            # HOY que quedan por delante, esa hora ya paso.
            continue
        idx = plan_hours.index(start_dt)
        blocked.update(range(idx, idx + duration))

    pending_count = count - len(started)
    new_picks = []
    if pending_count > 0 and hours_left_today > 0:
        new_picks = _pick_blocks(surplus_w, prices_by_hour, 0, hours_left_today, duration, min_power_w,
                                  pending_count, blocked=blocked)

    occurrences = started + [_occurrence(plan_hours, idx, duration, mode, energy_wh) for idx, mode in new_picks]
    if not occurrences:
        return existing if existing and existing.get("date") == today else None

    schedule = {"frequency": frequency, "date": today, "occurrences": occurrences}
    deferrable_store.save_schedule(load_id, schedule)
    return schedule

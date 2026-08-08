"""
Aplica la decision de las cargas diferibles (ver deferrable_scheduler.py):
enciende el switch si "ahora" cae dentro de alguna ventana programada, lo
apaga si no.

Ademas:
  - Integra la potencia de su sensor de consumo (si lo tiene) mientras esta
    encendida, para ir estimando cuanta energia gasta de verdad cada
    activacion — asi el usuario no tiene que indicarlo a mano si no quiere
    (ver deferrable_store.get_estimated_energy_wh).
  - Si esta marcada como "interrumpible" (p.ej. un termo electrico, que no
    pasa nada por pararlo a medias) y su ventana era por excedente solar,
    comprueba cada ciclo si ese excedente sigue existiendo de verdad; si
    desaparece varios ciclos seguidos, la apaga antes de tiempo. Las que NO
    son interrumpibles (p.ej. una lavadora, que no se debe cortar a medio
    programa) se quedan encendidas toda su ventana pase lo que pase.
  - Devuelve cuanta potencia se espera que esten consumiendo AHORA MISMO
    las cargas activas, para que el detector de anomalias de consumo no
    confunda una carga que la propia app acaba de encender con un consumo
    fuera de lo normal.
"""

from __future__ import annotations

from datetime import datetime

import deferrable_store
import ha_client

INTERRUPT_CONFIRM_CYCLES = 2  # ciclos seguidos sin excedente antes de cortar una carga interrumpible


def _in_any_window(now: datetime, occurrences: list[dict]) -> dict | None:
    for occ in occurrences:
        if datetime.fromisoformat(occ["start"]) <= now < datetime.fromisoformat(occ["end"]):
            return occ
    return None


def execute(loads: list[dict], schedules: dict[str, dict], now: datetime,
            cycle_hours: float, live_surplus_w: float, dry_run: bool = True):
    """
    `live_surplus_w`: excedente solar real medido ahora mismo (el mismo
    numero que usa el resto del ciclo), solo se usa para decidir si cortar
    antes de tiempo una carga interrumpible.

    Devuelve (log_lines, live_power_by_id, expected_power_now_w,
    just_completed_once_ids).
    """
    log_lines: list[str] = []
    live_power_by_id: dict[str, float] = {}
    expected_power_now_w = 0.0
    just_completed_once_ids: list[str] = []
    prefix = "[SIMULACION] " if dry_run else ""

    for load in loads:
        if not load.get("enabled", True):
            continue
        load_id, name, switch = load["id"], load["name"], load["switch_entity"]
        power_sensor = load.get("power_sensor")
        duration_hours = max(1, int(load.get("duration_hours", 1)))
        schedule = schedules.get(load_id)
        occurrences = schedule.get("occurrences", []) if schedule else []
        active_occ = _in_any_window(now, occurrences)

        if active_occ and active_occ["mode"] == "solar" and load.get("interruptible"):
            min_power_w = active_occ["energy_wh"] / duration_hours
            streak = deferrable_store.record_no_surplus_streak(load_id, live_surplus_w >= min_power_w)
            if streak >= INTERRUPT_CONFIRM_CYCLES:
                deferrable_store.truncate_occurrence_now(load_id, now)
                log_lines.append(f"{prefix}[{name}] interrumpida: el excedente solar previsto ya no esta disponible")
                active_occ = None

        if active_occ:
            expected_power_now_w += active_occ["energy_wh"] / duration_hours
            live_power_by_id[load_id] = ha_client.get_numeric_state(power_sensor, default=0.0) if power_sensor else 0.0
            deferrable_store.record_session_start(load_id, now)
            if power_sensor:
                deferrable_store.accumulate_session_energy(load_id, live_power_by_id[load_id] * cycle_hours)
            mode_txt = "excedente solar" if active_occ["mode"] == "solar" else "hora barata"
            window_txt = f"{active_occ['start'][11:16]}-{active_occ['end'][11:16]}"
            line = f"{prefix}[{name}] ENCENDIDA ({mode_txt}, ventana {window_txt})"
            if not dry_run:
                try:
                    ha_client.turn_on(switch)
                except Exception as e:
                    line += f" — AVISO: no se pudo encender en Home Assistant ({e})"
        else:
            energy = deferrable_store.end_session(load_id, now)
            if energy is not None and energy > 1:
                log_lines.append(f"{prefix}[{name}] sesion finalizada, ~{round(energy)}Wh medidos")
            if energy is None:
                # No habia ninguna ventana gestionada por la app activa para
                # esta carga (`end_session` solo devuelve energia si HABIA
                # una sesion que la propia app habia empezado). Si aun asi
                # el switch esta encendido, es que el usuario lo ha
                # encendido a mano fuera de su ventana programada - se
                # respeta, no se apaga: la app solo controla lo que ella
                # misma prendio, nunca un encendido manual ajeno.
                try:
                    manually_on = ha_client.get_state(switch)["state"] == "on"
                except Exception:
                    manually_on = False
                if manually_on:
                    line = f"{prefix}[{name}] encendida a mano fuera de ventana — no se toca"
                else:
                    line = f"{prefix}[{name}] apagada (fuera de ventana programada)"
            else:
                line = f"{prefix}[{name}] apagada (fuera de ventana programada)"
                if not dry_run:
                    try:
                        ha_client.turn_off(switch)
                    except Exception as e:
                        line += f" — AVISO: no se pudo apagar en Home Assistant ({e})"

        log_lines.append(line)

        if schedule and schedule.get("frequency") == "once" and not load.get("done") and occurrences:
            occ = occurrences[0]
            if datetime.fromisoformat(occ["end"]) <= now:
                just_completed_once_ids.append(load_id)

    return log_lines, live_power_by_id, expected_power_now_w, just_completed_once_ids

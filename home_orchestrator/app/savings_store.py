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


# Tope de cuanto tiempo "de golpe" se deja integrar en una sola vuelta -- si el
# addon estuvo parado un rato (reinicio, fallo...) no se quiere contar ese hueco
# entero como si hubiera habido el mismo consumo todo el tiempo. Mismo criterio
# y mismo valor que grid_energy_store.MAX_INTEGRATION_GAP_HOURS.
MAX_INTEGRATION_GAP_HOURS = 300 / 3600


def _default() -> dict:
    return {
        "since": None, "total_real_eur": 0.0, "total_baseline_eur": 0.0, "days": {},
        "last_update": None,
    }


def _load() -> dict:
    with _lock:
        if not os.path.exists(SAVINGS_PATH):
            return _default()
        try:
            with open(SAVINGS_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return _default()
        if not isinstance(data, dict):
            return _default()
        # Completar claves que falten en vez de indexar a ciegas: un fichero
        # escrito por una version anterior no tiene "last_update", y el resto
        # del modulo hacia `data["since"]`/`data["days"]` directo, que sobre un
        # esquema viejo o incompleto revienta con KeyError.
        merged = _default()
        merged.update(data)
        if not isinstance(merged.get("days"), dict):
            merged["days"] = {}
        return merged


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


def record(now: datetime, real_power_w: float, baseline_power_w: float,
           price_eur_kwh: float) -> None:
    """Integra el coste por rectangulo simple usando el tiempo REAL transcurrido
    desde la ultima llamada.

    BUG REAL (el ahorro acumulado se inflaba hasta ~12x): antes esta funcion
    recibia el coste YA multiplicado por el `cycle_seconds` NOMINAL de la
    config, pero `run_cycle` no se ejecuta solo cada `cycle_seconds` -- tambien
    lo dispara el ciclo reactivo (ver ha_websocket.ReactiveTrigger, con un suelo
    de 5s). Con el `cycle_seconds: 60` por defecto, un Home Assistant movido
    puede ejecutar el ciclo ~12 veces por minuto, y cada una sumaba una racion
    COMPLETA de una hora-fraccion de coste. Como solo se usaba `now` para la
    clave del dia, no habia forma de corregirse solo.

    Es exactamente el mismo fallo que este repo ya habia corregido para la
    energia de baterias (ver main.py), las cargas diferibles
    (deferrable_store), la red (grid_energy_store) y la solar -- el ahorro se
    quedo sin arreglar. Se recibe POTENCIA (W) y precio, no un coste ya
    multiplicado, para que la integracion la haga quien sabe cuanto tiempo ha
    pasado de verdad.

    La PRIMERA llamada tras un reinicio no integra nada (no hay "antes" con el
    que calcular un intervalo real), solo fija el punto de partida -- mismo
    criterio que grid_energy_store.accumulate."""
    with _lock:
        data = _load()
        if data.get("since") is None:
            data["since"] = now.isoformat()

        last_iso = data.get("last_update")
        dt_hours = 0.0
        if last_iso is not None:
            try:
                elapsed = (now - datetime.fromisoformat(last_iso)).total_seconds()
                dt_hours = max(0.0, elapsed) / 3600.0
                if dt_hours > MAX_INTEGRATION_GAP_HOURS:
                    dt_hours = 0.0  # hueco largo (addon parado): no se inventa consumo
            except ValueError:
                dt_hours = 0.0

        if dt_hours > 0.0:
            real_cost_eur = price_eur_kwh * (max(0.0, real_power_w) / 1000.0) * dt_hours
            baseline_cost_eur = price_eur_kwh * (max(0.0, baseline_power_w) / 1000.0) * dt_hours

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

        data["last_update"] = now.isoformat()
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

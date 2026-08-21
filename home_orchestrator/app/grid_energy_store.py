"""
Acumula, ciclo a ciclo, la energia importada y vertida a red -- kWh
integrados a partir de la potencia en vivo (`grid_total_w`/`vertido_w`,
ver run_cycle() en main.py) que ya se calcula cada ciclo. Mismo patron de
persistencia que savings_store.py: fichero JSON propio, se recupera solo
al reiniciar el addon (nunca se pierde el acumulado por un reinicio).

A peticion expresa del usuario: "crear y exponer un sensor de importacion
desde la red, vertido a la red (ambos acumulativos)" -- se exponen como
sensor.battery_orchestrator_grid_imported_energy/..._exported_energy con
device_class "energy" y state_class "total_increasing" (ver run_cycle()
en main.py, `_publish_sensor_throttled`): el mismo mecanismo YA PROBADO
que usa `sensor.battery_orchestrator_solar_energy` (REST directo a HA via
`ha_client.publish_sensor`, no MQTT Discovery -- mas simple, sin
conexion nueva que mantener, mismo patron de nombres). El mismo contrato
que un contador de verdad, solo sube, HA ya sabe calcular consumos por
periodo el solo a partir de esto -- listo para el Panel de Energia
oficial de HA (Configuracion -> Ajustes del panel de energia -> Red
electrica: consumo/vertido).
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime

STORE_PATH = os.environ.get("GRID_ENERGY_PATH", "/data/grid_energy.json")

# Hueco maximo entre dos llamadas que se integra como energia real -- un
# hueco mas largo (addon parado horas, reloj del sistema saltando...) se
# descarta ENTERO en vez de integrarlo, para no inflar el acumulado con
# una estimacion inventada sobre un intervalo que no se pudo medir de
# verdad. Mismo criterio de "nunca inventar dato" que el resto del repo.
MAX_INTEGRATION_GAP_HOURS = 2.0

_lock = threading.RLock()


def _default() -> dict:
    return {"imported_kwh": 0.0, "exported_kwh": 0.0, "last_update": None}


def _load() -> dict:
    with _lock:
        if not os.path.exists(STORE_PATH):
            return _default()
        try:
            with open(STORE_PATH) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return _default()
        merged = _default()
        merged.update(data)
        return merged


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(STORE_PATH), exist_ok=True)
    with _lock:
        # Escritura ATOMICA (.tmp + os.replace) -- ver config_store._write_raw:
        # un corte a mitad de un `open(..., "w")` directo dejaba el fichero
        # truncado o con dos objetos JSON concatenados.
        tmp = STORE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, STORE_PATH)


def accumulate(now: datetime, imported_w: float | None, exported_w: float | None) -> dict:
    """Integra por rectangulo simple usando el tiempo transcurrido desde
    la ULTIMA llamada real -- nunca un intervalo fijo asumido (`cycle_
    seconds`), para no arrastrar error si un ciclo tarda mas o llega por
    el disparador reactivo fuera de horario. La PRIMERA llamada tras un
    reinicio no integra nada (no hay "antes" con el que calcular un
    intervalo real), solo fija el punto de partida — mismo criterio que
    `_temp_ema`/EMAs del resto del repo con su primera lectura."""
    with _lock:
        data = _load()
        last_iso = data.get("last_update")
        if last_iso is not None:
            try:
                last = datetime.fromisoformat(last_iso)
                dt_hours = max(0.0, (now - last).total_seconds()) / 3600.0
                if dt_hours <= MAX_INTEGRATION_GAP_HOURS:
                    # Se acota el signo: estos dos acumulados se publican como
                    # `total_increasing` y HA interpreta un `total_increasing`
                    # que BAJA como un reset de contador (con el salto que eso
                    # mete en las graficas del Panel de Energia). `exported_w`
                    # sale del sensor CRUDO del usuario, y un medidor que
                    # reporte el vertido en negativo restaba del acumulado.
                    imported_w = max(0.0, imported_w or 0.0)
                    exported_w = max(0.0, exported_w or 0.0)
                    if imported_w:
                        data["imported_kwh"] += (imported_w / 1000.0) * dt_hours
                    if exported_w:
                        data["exported_kwh"] += (exported_w / 1000.0) * dt_hours
            except ValueError:
                pass
        data["last_update"] = now.isoformat()
        _save(data)
        return data


def set_totals(imported_kwh: float, exported_kwh: float, since: str | None = None) -> dict:
    """Fija los dos acumulados a valores concretos -- para dejarlos alineados
    con un historico recien reconstruido (ver `/api/energy/backfill_history`).

    Tambien reinicia `last_update`: la siguiente vuelta de `accumulate` solo
    fija el punto de partida sin integrar el hueco, para no sumar de golpe el
    tiempo que haya pasado durante la reconstruccion."""
    with _lock:
        data = _load()
        data["imported_kwh"] = max(0.0, float(imported_kwh))
        data["exported_kwh"] = max(0.0, float(exported_kwh))
        data["last_update"] = None
        if since is not None:
            data["since"] = since
        _save(data)
        return data


def totals() -> dict:
    return _load()

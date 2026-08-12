from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta

import requests
from flask import Flask, Response, jsonify, request, send_from_directory
from werkzeug.serving import make_server

import anomaly_store
import battery_exec
import capacity_store
import climate_link
import config_store
import deferrable_exec
import deferrable_scheduler
import deferrable_store
import forecast_store
import ha_client
import history_store
import lifetime_store
import pv_source
import savings_store
import scheduler
import tariff_source

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("battery_orchestrator")

app = Flask(__name__, static_folder="static", template_folder="templates")

# Puerto adicional, expuesto directamente por el add-on (ver "ports" en
# config.yaml), para poder ver el panel sin pasar por el Ingress de Home
# Assistant — pensado para dejarlo fijo en una tablet de pared con una app
# tipo WallPanel/Fully Kiosk. Es SOLO LECTURA: ni expone la configuracion
# (nombres de entidades, api key de Forecast.Solar...) ni permite forzar
# "Ejecutar ciclo ahora", porque a diferencia de Ingress no lleva delante
# la autenticacion de Home Assistant. El puerto normal (Ingress) sigue
# teniendo acceso completo como siempre.
WALLPANEL_PORT = int(os.environ.get("WALLPANEL_PORT", 8098))
WALLPANEL_ALLOWED_GET = {"/", "/api/status", "/api/live", "/api/savings", "/api/battery_health", "/api/anomaly"}


@app.before_request
def _restrict_wallpanel_port():
    if request.environ.get("SERVER_PORT") != str(WALLPANEL_PORT):
        return None  # peticion por Ingress (u otro puerto): sin restriccion
    if request.method == "GET" and request.path in WALLPANEL_ALLOWED_GET:
        return None
    return jsonify({
        "error": "No disponible desde el puerto de solo lectura (wallpanel). "
                 "Configura el add-on desde el panel lateral de Home Assistant.",
    }), 403


_state_lock = threading.Lock()
_last_status = {
    "last_run": None,
    "plan": [],
    "distribution": None,
    "log_lines": [],
    "skipped_batteries": [],
    "pv_now_actual": None,
    "current_soc_pct": None,
    "next_punta": None,
    "next_tariff_change": None,
    "energy_flow": None,
    "consumption_comparison": None,
    "anomaly": None,
    "deferrable_loads": [],
    "soc_forecast": None,
    "climate_orchestrator": None,
    "error": None,
}

ANOMALY_NOTIFICATION_ID = "battery_orchestrator_anomaly"

# Los sensores que este addon publica en HA (mas abajo) no necesitan
# actualizarse cada `cycle_seconds` (30-60s tipico) para ser utiles: ni el
# precio/tramo ni el estado cambian de verdad a ese ritmo. Publicarlos sin
# mas en cada ciclo escribe una fila nueva en el recorder de HA cada vez
# (aunque el valor no haya cambiado) y, en el caso de
# "sensor.battery_orchestrator_grid_signal", dispara una reevaluacion
# reactiva en CADA zona de Climate Orchestrator que lo escuche - en una
# instalacion con muchas entidades esto es carga real y evitable. Se
# publica como mucho cada PUBLISH_MIN_INTERVAL_SECONDS, salvo la PRIMERA
# vez (para no dejar al resto de HA sin dato ninguno mientras arranca).
PUBLISH_MIN_INTERVAL_SECONDS = 120
_last_published_at: dict[str, float] = {}


def _publish_sensor_throttled(entity_id: str, state, attributes: dict) -> None:
    now_ts = time.time()
    last = _last_published_at.get(entity_id)
    if last is not None and (now_ts - last) < PUBLISH_MIN_INTERVAL_SECONDS:
        return
    ha_client.publish_sensor(entity_id, state, attributes)
    _last_published_at[entity_id] = now_ts


def _battery_from_cfg(b: dict) -> battery_exec.Battery:
    return battery_exec.Battery(
        id=b["id"],
        name=b["name"],
        capacity_wh=float(b["capacity_wh"]),
        soc_sensor=b["soc_sensor"],
        charge_switch=b["charge_switch"],
        discharge_switch=b["discharge_switch"],
        max_charge_w=float(b.get("max_charge_w", 1200)),
        max_discharge_w=float(b.get("max_discharge_w", 1200)),
        min_soc_pct=float(b.get("min_soc_pct", 3)),
        max_soc_pct=float(b.get("max_soc_pct", 100)),
        charge_power_limit_entity=b.get("charge_power_limit_entity") or None,
        discharge_power_limit_entity=b.get("discharge_power_limit_entity") or None,
    )


def _battery_discharge_sensor(b: dict) -> str | None:
    """
    Sensor a usar para saber cuanto ha descargado (o esta descargando) esta
    bateria, coherente con su modo de sensor de potencia — misma logica que
    ya usa el calculo en vivo de `net_power_w` mas abajo. En modo "combined"
    el dato relevante esta en `net_power_sensor` (un unico sensor CON SIGNO,
    p.ej. carga positiva/descarga negativa); en ese modo `power_sensor`
    normalmente esta vacio, asi que usarlo siempre (como se hacia antes)
    dejaba fuera del todo la descarga de cualquier bateria en modo
    "combined" - ni con signo invertido ni sin el, directamente ausente.
    """
    mode = b.get("power_sensor_mode") or ("separate" if b.get("power_sensor") or b.get("charge_power_sensor") else "none")
    if mode == "combined":
        return b.get("net_power_sensor") or None
    return b.get("power_sensor") or None


def _live_battery_charge_discharge_w(batteries_cfg: list[dict]) -> tuple[float, float, bool]:
    """
    Carga y descarga TOTAL de todas las baterias AHORA MISMO, leido en vivo
    de HA (nunca de la previsión del planificador) — misma logica de
    power_sensor_mode que ya usa `/api/live` para cada bateria por separado,
    aqui sumada para reconstruir el flujo de energia real de la instalacion
    completa (ver `run_cycle`, "energy_flow"). Bateria sin ningun sensor de
    potencia declarado simplemente no aporta nada a la suma — nunca un cero
    inventado que esconda que falta ese dato.

    El tercer valor (`any_data`) distingue "ninguna bateria tiene sensor
    declarado" (no hay dato de verdad, 0.0 seria un cero inventado) de
    "las baterias SI tienen sensor y ahora mismo miden 0W" (0.0 real) —
    quien llama necesita saber cual de los dos casos es para decidir si
    cae a la previsión del planificador o no.
    """
    total_charge_w = 0.0
    total_discharge_w = 0.0
    any_data = False
    for b in batteries_cfg:
        mode = b.get("power_sensor_mode") or ("separate" if b.get("power_sensor") or b.get("charge_power_sensor") else "none")
        net_power = None
        if mode == "combined" and b.get("net_power_sensor"):
            net_power = ha_client.get_numeric_state(b.get("net_power_sensor"), default=None)
        elif mode == "separate":
            charge = (
                ha_client.get_numeric_state(b.get("charge_power_sensor"), default=None)
                if b.get("charge_power_sensor") else None
            )
            power = ha_client.get_numeric_state(b.get("power_sensor"), default=None) if b.get("power_sensor") else None
            if charge is not None or power is not None:
                net_power = abs(charge or 0.0) - abs(power or 0.0)
        if net_power is None:
            continue
        any_data = True
        if net_power > 0:
            total_charge_w += net_power
        else:
            total_discharge_w += abs(net_power)
    return total_charge_w, total_discharge_w, any_data


def _live_export_w(cfg: dict, known_net_grid_w: float | None = None) -> float | None:
    """
    Vertido a red AHORA MISMO, leido en vivo — deriva del mismo modo
    "separado vs unificado" que gobierna "Consumo de la casa"
    (`load_sensor_mode`). Nunca cuenta como "consumo de la casa" ni afecta
    a `contracted_power_w`/grid_w, porque el excedente vertido no pasa por
    la linea contratada — es puramente informativo.

    `known_net_grid_w`: si quien llama ya ha leido `net_grid_sensor` este
    mismo ciclo (para reconstruir el consumo, ver run_cycle/api_live), se
    pasa aqui para no volver a pedirselo a HA por lo mismo.

    `None` significa "no hay dato de verdad" (sin sensor declarado, o el
    sensor no responde ahora mismo) — nunca un cero inventado; quien llama
    debe tratarlo como "vertido no disponible", no como "0W de verdad".
    """
    mode = cfg.get("load_sensor_mode") or "separate"
    if mode == "combined" and cfg.get("net_grid_sensor"):
        net = known_net_grid_w if known_net_grid_w is not None else ha_client.get_numeric_state(cfg.get("net_grid_sensor"), default=None)
        return max(0.0, -net) if net is not None else None
    if cfg.get("export_sensor"):
        return ha_client.get_numeric_state(cfg.get("export_sensor"), default=None)
    return None


def run_cycle():
    """Un ciclo completo: leer estado, planificar, repartir, ejecutar."""
    cfg = config_store.load_config()
    batteries_cfg = cfg["batteries"]
    dry_run = bool(cfg["general"]["dry_run"])
    cycle_hours = cfg["general"]["cycle_seconds"] / 3600

    if not batteries_cfg:
        with _state_lock:
            _last_status.update(last_run=datetime.now().isoformat(),
                                 error="No hay baterias configuradas todavia.")
        return

    batteries = [_battery_from_cfg(b) for b in batteries_cfg]
    horizon = int(cfg["general"]["horizon_hours"])

    now = datetime.now()
    prices_tiers = tariff_source.get_prices_tiers(cfg["tariff"], now, horizon)

    # Previsión solar: se suman todos los arrays declarados, y la hora
    # ACTUAL (indice 0) se corrige con la generación real medida en cada
    # array que tenga su propio sensor instantáneo declarado — asi no hace
    # falta un sensor agregado en HA para tener varios strings/tejados.
    pv_forecast, pv_now_actual, hybrid_pv_now_w = pv_source.get_pv_forecast_total(
        cfg["pv_arrays"], horizon, refresh_seconds=cfg["general"]["pv_refresh_seconds"]
    )

    # Consumo real = consumo base (ya sin carga de baterias) + solar (de
    # cada array con sensor instantáneo) + descarga de baterias. Si no hay
    # ningun sensor de baterias/solar declarado, es simplemente el consumo
    # base (funciona igual, solo menos preciso en las horas en que la
    # bateria o el sol cubren gran parte del consumo).
    #
    # Modo "combined" (ver "Consumo de la casa" en Configuración): en vez
    # de un sensor ya neteado, se parte del medidor de red EN BRUTO con
    # signo (`net_grid_sensor`) y se reconstruye el consumo con el balance
    # fisico del panel (sol + red neta + descarga − carga) — mismo criterio
    # tanto para la previsión historica (`true_load_forecast_from_grid`)
    # como para la lectura en vivo, ver mas abajo. La carga/descarga en
    # vivo de baterias se necesita para ese balance, asi que se calcula
    # AQUI (antes de lo habitual) y se reutiliza mas abajo en el flujo de
    # energia "ahora mismo" en vez de volver a pedirla a HA.
    load_sensor = cfg.get("load_sensor")
    load_sensor_mode = cfg.get("load_sensor_mode") or "separate"
    net_grid_sensor = cfg.get("net_grid_sensor")
    history_days = cfg["general"]["history_days_for_load"]
    solar_sensors_for_load = [a.get("current_sensor") for a in cfg["pv_arrays"] if a.get("current_sensor")]
    live_charge_w, live_discharge_w, live_battery_data_ok = _live_battery_charge_discharge_w(batteries_cfg)
    net_grid_now_w = None  # solo se rellena en modo "combined"; se reutiliza mas abajo para el vertido

    if load_sensor_mode == "combined" and net_grid_sensor:
        load_forecast = ha_client.true_load_forecast_from_grid(
            net_grid_sensor, solar_sensors_for_load, batteries_cfg, horizon, days=history_days
        )
        net_grid_now_w = ha_client.get_numeric_state(net_grid_sensor, default=None)
        if net_grid_now_w is not None and pv_now_actual is not None and live_battery_data_ok:
            live_base_load_w = max(0.0, pv_now_actual + net_grid_now_w + live_discharge_w - live_charge_w)
        else:
            live_base_load_w = None
    elif load_sensor:
        battery_discharge_sensors = [s for s in (_battery_discharge_sensor(b) for b in batteries_cfg) if s]
        load_forecast = ha_client.true_load_forecast(
            load_sensor, solar_sensors_for_load, battery_discharge_sensors, horizon, days=history_days
        )
        # Lectura en vivo del consumo base, UNA sola vez por ciclo — se
        # reutiliza tanto para decidir si cortar antes de tiempo una carga
        # diferible interrumpible como para la deteccion de anomalias mas
        # abajo, en vez de pedirsela dos veces a Home Assistant.
        live_base_load_w = ha_client.get_numeric_state(load_sensor, default=None)
    else:
        load_forecast = [300.0] * horizon
        live_base_load_w = None

    # Climate Orchestrator, si esta instalado: la lista de zonas NUNCA se
    # descubre sola aqui (ver climate_link.py) — se guarda en config.json
    # cuando el usuario pulsa "Buscar zonas" en la configuracion
    # (`/api/climate/discover`), y este ciclo solo lee su potencia
    # AHORA MISMO a partir de esa lista ya conocida. Sin zonas guardadas
    # (Climate Orchestrator no instalado, o boton nunca pulsado), esto
    # devuelve {"total_w": 0.0, "zones": []} sin pedir nada a HA.
    climate_live = climate_link.read_live_power_w(cfg.get("climate_orchestrator_zones") or [])

    # Señal para quien quiera coordinarse con el precio/sol de la casa (hoy,
    # Climate Orchestrator) SIN que haga falta declarar nada a mano en
    # ningun lado: entity_id fijo, siempre el mismo, para que se pueda
    # encontrar sola. DELIBERADAMENTE aqui, ANTES de la comprobacion de
    # baterias que sigue: precio/sol no dependen de que las baterias
    # respondan, y esta señal se queda "pegada" (nadie la vuelve a publicar)
    # si se calcula despues de un `return` anticipado por baterias caidas —
    # justo el caso mas tipico de que haga falta (un hipo de conectividad
    # tras reiniciar HA, por ejemplo), asi que no puede depender de ellas.
    try:
        contracted_power_w = float(cfg["general"].get("contracted_power_w") or 0)
        price_now, tier_now = prices_tiers[0]
        solar_surplus_now = max(0.0, pv_forecast[0] - load_forecast[0])
        headroom_w = max(0.0, contracted_power_w - load_forecast[0]) if contracted_power_w else None
        forecast = [
            {
                "dt": (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=i)).isoformat(),
                "price": prices_tiers[i][0], "tier": prices_tiers[i][1],
                "solar_surplus_w": round(max(0.0, pv_forecast[i] - load_forecast[i])),
            }
            for i in range(horizon)
        ]
        _publish_sensor_throttled(
            "sensor.battery_orchestrator_grid_signal",
            price_now,
            {
                "unit_of_measurement": "EUR/kWh",
                "tier": tier_now,
                "solar_surplus_now_w": round(solar_surplus_now),
                "contracted_headroom_w": round(headroom_w) if headroom_w is not None else None,
                "forecast": forecast,
                # Sensor general de consumo de la casa YA declarado aqui
                # (ver "Consumo de la casa" en Configuración) — se publica
                # para que Climate Orchestrator, si esta instalado, pueda
                # APRENDER solo el consumo de sus actuadores (correlacion
                # contra este mismo sensor, ver su power_model.py) sin que
                # el usuario tenga que declarar el mismo sensor otra vez en
                # ese otro proyecto. Nunca una estimacion inventada por
                # ningun lado: si no hay load_sensor declarado aqui
                # tampoco, esto va vacio y Climate Orchestrator sigue
                # pidiendo su propio sensor a mano, como hoy.
                "home_power_sensor": load_sensor or None,
                "friendly_name": "Battery Orchestrator Grid Signal",
            },
        )
    except Exception as e:  # no tumbar el ciclo si HA no responde
        log.warning(f"No se pudo publicar la señal de red: {e}")

    # Baterias con sensor de SOC caido no cuentan para la planificacion
    # agregada de esta pasada (se excluyen tambien de la ejecucion real
    # en battery_exec.plan_distribution).
    socs = {b.id: b.read_soc_pct() for b in batteries}
    usable_batteries = [b for b in batteries if socs[b.id] is not None]
    skipped = [b.name for b in batteries if socs[b.id] is None]
    if skipped:
        log.warning(f"Baterias omitidas este ciclo (sensor SOC no disponible): {', '.join(skipped)}")

    total_capacity_wh = sum(b.capacity_wh for b in usable_batteries)
    current_soc_wh = sum(socs[b.id] / 100 * b.capacity_wh for b in usable_batteries)
    # SOC real AHORA MISMO, medido — distinto de hp.soc_wh del plan, que es
    # una PROYECCION de como quedara el SOC al final de esta hora si se
    # carga/descarga al ritmo decidido (el plan trabaja en pasos de una
    # hora). Mezclarlos hacia mostrar un "SOC agregado" que salta muy por
    # encima del real mientras se esta cargando.
    current_soc_pct = round(100 * current_soc_wh / total_capacity_wh, 1) if total_capacity_wh else 0
    min_soc_wh = sum(b.min_soc_pct / 100 * b.capacity_wh for b in usable_batteries)
    max_charge_w = sum(b.max_charge_w for b in usable_batteries)
    max_discharge_w = sum(b.max_discharge_w for b in usable_batteries)
    # techo real de carga: si alguna bateria tiene un SOC maximo declarado
    # por debajo del 100% (habitual para alargar vida util), el objetivo
    # de reserva tiene que respetarlo, no apuntar al 100% nominal.
    max_usable_wh = sum(b.max_soc_pct / 100 * b.capacity_wh for b in usable_batteries)

    if not usable_batteries:
        with _state_lock:
            _last_status.update(last_run=datetime.now().isoformat(),
                                 error="Ninguna bateria tiene el sensor de SOC disponible ahora mismo.")
        return

    # Prioridad elegida por el usuario: "ahorro" es el comportamiento de
    # siempre (carga tambien desde red si hace falta); "autoconsumo" solo
    # carga con excedente solar, nunca desde red aunque este barata;
    # "longevidad" es como "ahorro" pero sin apurar el SOC objetivo mas
    # alla del 90%. La carga sostenida (reparto de potencia en el tiempo
    # disponible en vez de siempre al maximo) es un interruptor aparte,
    # disponible tanto en "ahorro" como en "longevidad" — en "autoconsumo"
    # no aplica porque ahi nunca se carga desde red.
    priority_mode = cfg["general"].get("priority_mode", "ahorro")
    allow_grid_charging = priority_mode != "autoconsumo"
    paced_charging = bool(cfg["general"].get("paced_charging", False)) and allow_grid_charging
    effective_max_usable_wh = max_usable_wh
    if priority_mode == "longevidad" and total_capacity_wh:
        effective_max_usable_wh = min(max_usable_wh, total_capacity_wh * 0.90)

    plan, reserve_wh = scheduler.build_plan(
        now=now,
        pv_forecast_w=pv_forecast,
        load_forecast_w=load_forecast,
        current_soc_wh=current_soc_wh,
        total_capacity_wh=total_capacity_wh,
        max_charge_w=max_charge_w,
        max_discharge_w=max_discharge_w,
        min_soc_wh=min_soc_wh,
        prices_tiers=prices_tiers,
        contracted_power_w=float(cfg["general"].get("contracted_power_w") or 0),
        max_usable_wh=effective_max_usable_wh,
        allow_grid_charging=allow_grid_charging,
        paced_charging=paced_charging,
    )

    # Cargas diferibles: se planifican con el mismo plan hora a hora que
    # acaba de calcular el motor de baterias (asi saben en que horas la
    # bateria ya se va a quedar con el excedente solar), y se aplican ya
    # mismo si "ahora" cae dentro de alguna ventana decidida.
    deferrable_loads_cfg = cfg.get("deferrable_loads", [])
    deferrable_log_lines: list[str] = []
    deferrable_live_power: dict[str, float] = {}
    deferrable_expected_now_w = 0.0
    deferrable_schedules: dict[str, dict] = {}
    if deferrable_loads_cfg:
        plan_hours = [hp.dt for hp in plan]
        charge_w_by_hour = [hp.charge_w for hp in plan]
        charge_source_by_hour = [hp.charge_source for hp in plan]
        prices_by_hour = [hp.price for hp in plan]

        for load in deferrable_loads_cfg:
            if not load.get("enabled", True):
                continue
            try:
                schedule = deferrable_scheduler.plan_for_load(
                    load, now, plan_hours, pv_forecast, load_forecast,
                    charge_w_by_hour, charge_source_by_hour, prices_by_hour,
                )
            except Exception:
                # Un fallo al planificar UNA carga diferible no debe tumbar
                # el resto del ciclo: la decision de carga/descarga de las
                # baterias (lo importante) va DESPUES de este bloque y tiene
                # que seguir ejecutandose pase lo que pase aqui.
                log.exception(f"Fallo al planificar la carga diferible '{load.get('name', load.get('id'))}'")
                schedule = None
            if schedule:
                deferrable_schedules[load["id"]] = schedule

        live_pv_for_deferrable = pv_now_actual if pv_now_actual is not None else pv_forecast[0]
        live_surplus_w = (
            max(0.0, live_pv_for_deferrable - live_base_load_w) if live_base_load_w is not None
            else max(0.0, plan[0].pv_w - plan[0].load_w)
        )

        deferrable_log_lines, deferrable_live_power, deferrable_expected_now_w, just_done_once = deferrable_exec.execute(
            deferrable_loads_cfg, deferrable_schedules, now, cycle_hours,
            live_surplus_w=live_surplus_w, dry_run=dry_run,
        )
        for line in deferrable_log_lines:
            log.info(line)
        for load_id in just_done_once:
            config_store.update_deferrable_load(cfg, load_id, {"done": True})

    now_hp = plan[0]

    # Precision de la previsión: la primera vez que se ve esta hora se
    # guarda que SOC agregado predice el plan para el final de la misma;
    # cuando la hora cambie, se compara esa prediccion contra el SOC real
    # medido — asi se puede saber si lo que ha pasado se parece a lo
    # previsto o no (p.ej. un consumo inesperado que dispare muy por
    # encima de la previsión de esa hora), en vez de solo mirar cuanta
    # reserva hay acumulada. Ver forecast_store.py.
    predicted_end_of_hour_pct = round(100 * now_hp.soc_wh / total_capacity_wh, 1) if total_capacity_wh else current_soc_pct
    try:
        soc_forecast = forecast_store.record_and_compare(now, predicted_end_of_hour_pct, current_soc_pct)
    except Exception as e:
        log.warning(f"No se pudo actualizar la precision de la previsión: {e}")
        soc_forecast = None

    pv_surplus_now = max(0.0, now_hp.pv_w - now_hp.load_w)
    # Lo que ya se esta autoconsumiendo directo (paneles "hybrid" conectados
    # a una bateria con inversor integrado) no hace falta volver a mandarlo
    # por AC — se descuenta de la carga que SI hay que ordenar por AC.
    ac_charge_w = now_hp.charge_w
    if now_hp.charge_source == "solar":
        ac_charge_w = max(0.0, now_hp.charge_w - hybrid_pv_now_w)
    distribution = battery_exec.plan_distribution(
        batteries, ac_charge_w, now_hp.discharge_w, pv_surplus_w=pv_surplus_now
    )
    log_lines = battery_exec.execute(batteries, distribution, dry_run=dry_run)

    for line in log_lines:
        log.info(line)
    log.info(f"Hora actual: {now_hp.tier} ({now_hp.price} EUR/kWh) - {now_hp.reason}")

    # Cuenta atras a la proxima punta: reserve_wh ya es el objetivo real
    # que usa el planificador ahora mismo (cortado en el proximo valle,
    # punta + llano), el mismo numero que decide cuanto cargar de verdad
    # — ya no hace falta duplicar la cuenta con una version aparte "para
    # mostrar".
    next_punta = None
    next_punta_idx = next((i for i, hp in enumerate(plan) if hp.tier == "punta"), None)
    if next_punta_idx is not None:
        next_punta = {
            "hours_until": next_punta_idx,
            "dt": plan[next_punta_idx].dt.isoformat(),
            "reserve_target_wh": round(reserve_wh),
            "current_soc_wh": round(current_soc_wh),
            "reserve_pct": round(min(100.0, 100 * current_soc_wh / reserve_wh), 1) if reserve_wh else 100.0,
        }

    # Cuenta atras al proximo CAMBIO DE TRAMO (sea cual sea, no solo a
    # punta) — util para saber cuanto queda del precio actual.
    next_tariff_change = None
    for i in range(1, len(plan)):
        if plan[i].tier != now_hp.tier:
            next_tariff_change = {"hours_until": i, "dt": plan[i].dt.isoformat(), "tier": plan[i].tier}
            break

    # Flujo de energia AHORA MISMO, para el diagrama de "Estado actual" Y
    # el medidor de potencia contratada — CRITICO que sean datos EN VIVO,
    # no la previsión del planificador (`now_hp` es la media histórica de
    # esta hora, no lo que está pasando de verdad este segundo): si el
    # margen de potencia contratada se calculase con la previsión en vez
    # de con la carga real (p.ej. una lavadora encendida a mano que la
    # previsión no podía saber), puede parecer que sobra margen cuando en
    # realidad no lo hay — justo el caso que este medidor existe para
    # evitar. Se usa el dato en vivo siempre que existe, con la previsión
    # como red de seguridad SOLO si el sensor no responde ahora mismo
    # (mismo patrón "vivo con fallback a previsión" que ya usa pv_source.py
    # para la hora actual) — nunca al revés. `live_charge_w`/
    # `live_discharge_w`/`live_battery_data_ok` ya se calcularon mas arriba
    # (los necesitaba el modo "combined" del consumo), se reutilizan tal
    # cual en vez de volver a pedirlos a HA.
    flow_pv_w = pv_now_actual if pv_now_actual is not None else now_hp.pv_w
    flow_load_w = live_base_load_w if live_base_load_w is not None else now_hp.load_w
    flow_charge_w = live_charge_w if live_battery_data_ok else now_hp.charge_w
    flow_discharge_w = live_discharge_w if live_battery_data_ok else now_hp.discharge_w
    # La FUENTE de la carga (solar vs red) no se puede medir en vivo con un
    # sensor generico — es una decision que ya tomó el planificador este
    # mismo ciclo (`now_hp.charge_source`) y que `battery_exec.execute` ya
    # aplicó de verdad; se reutiliza esa atribución tal cual, solo con el
    # VATIAJE corregido a la lectura en vivo.
    solar_to_casa_w = min(flow_pv_w, flow_load_w)
    solar_to_batt_w = flow_charge_w if now_hp.charge_source == "solar" else 0.0
    grid_to_batt_w = flow_charge_w if now_hp.charge_source == "grid" else 0.0
    batt_to_casa_w = flow_discharge_w
    grid_to_casa_w = max(0.0, flow_load_w - solar_to_casa_w - batt_to_casa_w)
    grid_total_w = grid_to_casa_w + grid_to_batt_w
    energy_needed_now_w = flow_load_w + flow_charge_w
    autoconsumo_pct = 100.0
    if energy_needed_now_w > 0:
        autoconsumo_pct = max(0.0, min(100.0, 100.0 * (1 - grid_total_w / energy_needed_now_w)))
    energy_flow = {
        # TODOS estos en vivo (ver flow_pv_w/flow_load_w/flow_charge_w/
        # flow_discharge_w mas arriba) — antes usaban `now_hp` (la
        # previsión del planificador para esta hora), que no es lo mismo
        # que "ahora mismo" de verdad.
        "solar_w": round(flow_pv_w),
        "load_w": round(flow_load_w),
        "solar_to_casa_w": round(solar_to_casa_w),
        "solar_to_batt_w": round(solar_to_batt_w),
        "batt_to_casa_w": round(batt_to_casa_w),
        "battery_net_w": round(flow_charge_w - flow_discharge_w),
        "grid_w": round(grid_total_w),
        "autoconsumo_pct": round(autoconsumo_pct, 1),
        # Va aqui (y no solo en /api/config) para que el medidor de potencia
        # contratada funcione tambien desde el puerto wallpanel, que no
        # tiene acceso a la configuracion completa.
        "contracted_power_w": float(cfg["general"].get("contracted_power_w") or 0),
        # Vertido a red — puramente informativo, ver `_live_export_w`: NO
        # cuenta en `load_w`/`grid_w` ni en el margen de potencia contratada,
        # justo porque el excedente vertido no pasa por esa linea. `None`
        # si no hay sensor de vertido declarado (no un 0 inventado).
        "vertido_w": (lambda v: round(v) if v is not None else None)(_live_export_w(cfg, known_net_grid_w=net_grid_now_w)),
    }

    # Energia (Wh) movida en este ciclo, por bateria. En carga usamos la
    # potencia real repartida a cada una; en descarga cada bateria se
    # autogestiona (no la repartimos de verdad), asi que aqui SOLO para
    # llevar la cuenta se estima proporcional a su potencia maxima de
    # descarga entre las que estan activas — es una estimacion, no una
    # medicion exacta de lo que ha hecho cada una.
    by_id = {b.id: b for b in batteries}
    per_battery_energy: dict[str, tuple[str | None, float]] = {b.id: (None, 0.0) for b in batteries}
    if distribution["action"] == "charge":
        for entry in distribution["per_battery"]:
            wh = entry["power_w"] * cycle_hours
            if wh > 0:
                per_battery_energy[entry["id"]] = ("charge", wh)
    elif distribution["action"] == "discharge":
        enabled = [e for e in distribution["per_battery"] if e.get("enabled")]
        total_max_discharge = sum(by_id[e["id"]].max_discharge_w for e in enabled) or 1
        for e in enabled:
            share = by_id[e["id"]].max_discharge_w / total_max_discharge
            wh = now_hp.discharge_w * share * cycle_hours
            if wh > 0:
                per_battery_energy[e["id"]] = ("discharge", wh)

    # Acumular energia de por vida (para "ciclos equivalentes") y
    # alimentar la estimacion de capacidad real (para la "salud" por
    # comparacion con la capacidad declarada). Ver capacity_store.py.
    for b in batteries:
        action, wh = per_battery_energy[b.id]
        if action == "charge":
            lifetime_store.accumulate(b.id, b.name, charged_wh=wh, discharged_wh=0)
        elif action == "discharge":
            lifetime_store.accumulate(b.id, b.name, charged_wh=0, discharged_wh=wh)
        capacity_store.update(b.id, b.name, socs.get(b.id), action, wh)

    # Registrar la decision REAL de esta hora en el historico (se
    # sobreescribe con cada ciclo hasta que la hora termine, quedando la
    # ultima decision tomada como "lo que paso" esa hora).
    try:
        history_store.record(now, {
            "dt": now.replace(minute=0, second=0, microsecond=0).isoformat(),
            "price": now_hp.price, "tier": now_hp.tier,
            "pv_w": round(now_hp.pv_w), "load_w": round(now_hp.load_w),
            "charge_w": round(now_hp.charge_w), "discharge_w": round(now_hp.discharge_w),
            "soc_pct": current_soc_pct,
            "reason": now_hp.reason,
        })
    except Exception as e:
        log.warning(f"No se pudo guardar el historico: {e}")

    consumption_comparison = None
    try:
        consumption_comparison = history_store.get_recent_days_consumption(now, days=7)
    except Exception as e:
        log.warning(f"No se pudo calcular la comparativa de consumo: {e}")

    # Ahorro real: coste de lo que se ha comprado de verdad a red (consumo
    # directo que el solar no cubre, mas lo que se cargue de red en la
    # bateria) frente al coste SIN bateria (comprar directamente a red lo
    # que el solar no cubra). Mismos numeros que usa el planificador, sin
    # inventar nada nuevo.
    try:
        grid_bought_w = max(0.0, now_hp.load_w - now_hp.pv_w - now_hp.discharge_w)
        if now_hp.charge_source == "grid":
            grid_bought_w += now_hp.charge_w
        real_cost_eur = now_hp.price * (grid_bought_w / 1000) * cycle_hours
        baseline_deficit_w = max(0.0, now_hp.load_w - now_hp.pv_w)
        baseline_cost_eur = now_hp.price * (baseline_deficit_w / 1000) * cycle_hours
        savings_store.record(now, real_cost_eur, baseline_cost_eur)
    except Exception as e:
        log.warning(f"No se pudo actualizar el ahorro acumulado: {e}")

    # Deteccion de anomalias de consumo: compara el consumo real medido
    # AHORA MISMO (no la previsión) contra lo que la previsión historica
    # esperaba para esta hora. Solo se puede calcular si hay sensor de
    # consumo configurado. A lo esperado se le suma el consumo estimado de
    # las cargas diferibles que la propia app tiene encendidas ahora mismo
    # (deferrable_expected_now_w) y el de las zonas de Climate Orchestrator
    # activas (climate_live) — asi no se confunde una lavadora que ACABAMOS
    # de encender nosotros mismos, o la calefaccion trabajando de verdad un
    # dia atipico de frio, con un consumo fuera de lo normal.
    anomaly = None
    has_live_load_data = live_base_load_w is not None and (load_sensor_mode == "combined" or load_sensor)
    if has_live_load_data:
        try:
            if load_sensor_mode == "combined":
                # live_base_load_w ya es el consumo TOTAL reconstruido
                # (sol + red neta + descarga − carga, ver mas arriba) — a
                # diferencia del modo "separate", donde el sensor de
                # consumo excluye sol/descarga y hay que sumarlos aqui.
                live_load_w = live_base_load_w
            else:
                live_pv = pv_now_actual if pv_now_actual is not None else pv_forecast[0]
                live_discharge = sum(
                    abs(ha_client.get_numeric_state(_battery_discharge_sensor(b), default=0.0) or 0.0)
                    for b in batteries_cfg if _battery_discharge_sensor(b)
                )
                live_load_w = live_base_load_w + live_pv + live_discharge
            expected_load_w = load_forecast[0] + deferrable_expected_now_w + climate_live["total_w"]
            anomaly = anomaly_store.update(now, live_load_w, expected_load_w)
            if anomaly["changed"]:
                if anomaly["status"] == "anomaly":
                    ha_client.call_service("persistent_notification", "create", extra={
                        "notification_id": ANOMALY_NOTIFICATION_ID,
                        "title": "Battery Orchestrator: consumo anómalo",
                        "message": (
                            f"Consumo real ~{anomaly['live_load_w']}W, muy por encima de lo "
                            f"esperado para esta hora (~{anomaly['expected_load_w']}W)."
                        ),
                    })
                    log.warning(f"Anomalia de consumo detectada: {anomaly['live_load_w']}W vs {anomaly['expected_load_w']}W esperados")
                else:
                    ha_client.call_service("persistent_notification", "dismiss", extra={
                        "notification_id": ANOMALY_NOTIFICATION_ID,
                    })
                    log.info("Anomalia de consumo resuelta")
        except Exception as e:
            log.warning(f"No se pudo comprobar la anomalia de consumo: {e}")
    if anomaly is None:
        anomaly = anomaly_store.get_status()

    try:
        _publish_sensor_throttled(
            "sensor.battery_orchestrator_status",
            now_hp.reason,
            {
                "tramo": now_hp.tier,
                "precio": now_hp.price,
                "carga_w": now_hp.charge_w,
                "descarga_w": now_hp.discharge_w,
                "soc_total_pct": current_soc_pct,
                "dry_run": dry_run,
                "pv_actual_w": pv_now_actual,
                "baterias_omitidas": skipped,
                "friendly_name": "Battery Orchestrator",
            },
        )
    except Exception as e:  # no tumbar el ciclo si HA no responde
        log.warning(f"No se pudo publicar el sensor de estado: {e}")

    # Tabla completa del dia: lo que YA paso hoy (del historico, real) +
    # lo previsto desde ahora en adelante (el plan recien calculado).
    today_history = [{**entry, "historical": True} for entry in history_store.get_today(now)]
    future_plan = [
        {
            "dt": hp.dt.isoformat(), "price": hp.price, "tier": hp.tier,
            "pv_w": round(hp.pv_w), "load_w": round(hp.load_w),
            "charge_w": round(hp.charge_w), "discharge_w": round(hp.discharge_w),
            "soc_pct": round(100 * hp.soc_wh / total_capacity_wh, 1) if total_capacity_wh else 0,
            "reason": hp.reason, "historical": False,
        }
        for hp in plan
    ]

    # Estado de cada carga diferible para el dashboard: lo REAL (potencia
    # que esta consumiendo ahora, si tiene sensor) junto con lo PROGRAMADO
    # (la ventana decidida, y por que). SIN el entity_id del switch: este
    # endpoint (/api/status) es uno de los accesibles desde el wallpanel
    # de solo lectura (sin autenticacion de HA delante), y el frontend no
    # lo necesita de aqui - la ficha de configuracion (que si lo muestra)
    # lee de /api/config, que el wallpanel tiene bloqueado.
    deferrable_status = [
        {
            "id": load["id"], "name": load["name"], "enabled": load.get("enabled", True),
            "interruptible": load.get("interruptible", False),
            "frequency": load.get("frequency", "daily"),
            "schedule": deferrable_schedules.get(load["id"]),
            "live_power_w": deferrable_live_power.get(load["id"]),
            "auto_estimated_energy_wh": deferrable_store.get_estimated_energy_wh(load["id"]),
            "auto_estimated_duration_hours": deferrable_store.get_estimated_duration_hours(load["id"]),
        }
        for load in deferrable_loads_cfg
    ]

    with _state_lock:
        _last_status.update(
            last_run=datetime.now().isoformat(),
            plan=today_history + future_plan,
            distribution=distribution,
            log_lines=log_lines + deferrable_log_lines,
            skipped_batteries=skipped,
            pv_now_actual=pv_now_actual,
            current_soc_pct=current_soc_pct,
            next_punta=next_punta,
            next_tariff_change=next_tariff_change,
            energy_flow=energy_flow,
            consumption_comparison=consumption_comparison,
            anomaly=anomaly,
            deferrable_loads=deferrable_status,
            soc_forecast=soc_forecast,
            climate_orchestrator=climate_live,
            error=None,
        )


def background_loop():
    while True:
        try:
            run_cycle()
        except Exception:
            log.exception("Fallo en el ciclo de planificacion")
            with _state_lock:
                _last_status["error"] = "Error en el ultimo ciclo, revisa los logs del addon."
        cfg = config_store.load_config()
        time.sleep(max(15, int(cfg["general"]["cycle_seconds"])))


# ---------------------------------------------------------------- API ----

@app.get("/api/config")
def api_get_config():
    return jsonify(config_store.load_config())


@app.post("/api/config")
def api_save_config():
    cfg = request.get_json(force=True)
    config_store.save_config(cfg)
    return jsonify(cfg)


@app.get("/api/config/export")
def api_export_config():
    cfg = config_store.load_config()
    body = json.dumps(cfg, indent=2, ensure_ascii=False)
    return Response(
        body, mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=battery_orchestrator_config.json"},
    )


@app.post("/api/config/import")
def api_import_config():
    cfg = request.get_json(force=True)
    required_keys = {"batteries", "tariff", "pv_arrays", "general"}
    if not isinstance(cfg, dict) or not required_keys.issubset(cfg.keys()):
        return jsonify({"error": "El archivo no tiene el formato esperado de configuración."}), 400
    config_store.save_config(cfg)
    return jsonify(cfg)


@app.post("/api/batteries")
def api_add_battery():
    cfg = config_store.load_config()
    battery = config_store.add_battery(cfg, request.get_json(force=True))
    return jsonify(battery), 201


@app.put("/api/batteries/<battery_id>")
def api_update_battery(battery_id):
    cfg = config_store.load_config()
    updated = config_store.update_battery(cfg, battery_id, request.get_json(force=True))
    if updated is None:
        return jsonify({"error": "no encontrada"}), 404
    return jsonify(updated)


@app.delete("/api/batteries/<battery_id>")
def api_delete_battery(battery_id):
    cfg = config_store.load_config()
    ok = config_store.delete_battery(cfg, battery_id)
    return jsonify({"deleted": ok})


@app.post("/api/pv_arrays")
def api_add_pv_array():
    cfg = config_store.load_config()
    array = config_store.add_pv_array(cfg, request.get_json(force=True))
    return jsonify(array), 201


@app.put("/api/pv_arrays/<array_id>")
def api_update_pv_array(array_id):
    cfg = config_store.load_config()
    updated = config_store.update_pv_array(cfg, array_id, request.get_json(force=True))
    if updated is None:
        return jsonify({"error": "no encontrado"}), 404
    return jsonify(updated)


@app.delete("/api/pv_arrays/<array_id>")
def api_delete_pv_array(array_id):
    cfg = config_store.load_config()
    ok = config_store.delete_pv_array(cfg, array_id)
    return jsonify({"deleted": ok})


@app.post("/api/deferrable_loads")
def api_add_deferrable_load():
    cfg = config_store.load_config()
    load = config_store.add_deferrable_load(cfg, request.get_json(force=True))
    return jsonify(load), 201


@app.put("/api/deferrable_loads/<load_id>")
def api_update_deferrable_load(load_id):
    cfg = config_store.load_config()
    updated = config_store.update_deferrable_load(cfg, load_id, request.get_json(force=True))
    if updated is None:
        return jsonify({"error": "no encontrada"}), 404
    return jsonify(updated)


@app.delete("/api/deferrable_loads/<load_id>")
def api_delete_deferrable_load(load_id):
    cfg = config_store.load_config()
    ok = config_store.delete_deferrable_load(cfg, load_id)
    if ok:
        deferrable_store.clear_load(load_id)
    return jsonify({"deleted": ok})


@app.post("/api/deferrable_loads/<load_id>/reschedule")
def api_reschedule_deferrable_load(load_id):
    """Solo relevante para frequency="once" ya ejecutada: la vuelve a
    dejar pendiente de programar, sin tocar el resto de su configuracion."""
    cfg = config_store.load_config()
    updated = config_store.update_deferrable_load(cfg, load_id, {"done": False})
    if updated is None:
        return jsonify({"error": "no encontrada"}), 404
    deferrable_store.clear_load(load_id)
    return jsonify(updated)


@app.get("/api/status")
def api_status():
    with _state_lock:
        return jsonify(_last_status)


@app.get("/api/live")
def api_live():
    """
    Lectura RAPIDA de solo lectura: nada de previsión, planificacion ni
    ejecucion, solo el estado medido en Home Assistant AHORA MISMO. Pensada
    para que el dashboard refresque los numeros "en vivo" cada pocos
    segundos sin esperar al proximo ciclo completo de optimizacion (que es
    mas lento y solo se relanza cada `cycle_seconds`).
    """
    cfg = config_store.load_config()

    battery_live = []
    total_capacity_wh, current_soc_wh = 0.0, 0.0
    # Suma en vivo de carga/descarga de TODAS las baterias, reutilizando el
    # mismo `net_power` que ya se calcula por bateria mas abajo en este
    # mismo bucle (nunca una segunda ronda de llamadas a HA por lo mismo)
    # — hace falta para "energy_flow" al final de esta funcion, ver ahi.
    live_charge_w = 0.0
    live_discharge_w = 0.0
    live_battery_data_ok = False
    for b in cfg["batteries"]:
        soc = ha_client.get_numeric_state(b["soc_sensor"], default=None)
        power = ha_client.get_numeric_state(b.get("power_sensor"), default=None) if b.get("power_sensor") else None

        # net_power_w: potencia CON SIGNO (positiva cargando, negativa
        # descargando), pensada para poder ver en vivo tambien la carga,
        # no solo la descarga (que es lo unico que da power_sensor). Se
        # calcula segun el modo que haya elegido el usuario para esta
        # bateria — "combined" (un sensor con signo ya de por si) o
        # "separate" (dos sensores, cada uno siempre positivo o cero).
        # Instalaciones de antes de que existiera este desplegable no
        # tienen "power_sensor_mode" guardado: se tratan como "separate"
        # con solo el de descarga relleno, que es exactamente su
        # comportamiento de siempre (no se pierde nada al actualizar).
        mode = b.get("power_sensor_mode") or ("separate" if b.get("power_sensor") or b.get("charge_power_sensor") else "none")
        net_power = None
        if mode == "combined" and b.get("net_power_sensor"):
            net_power = ha_client.get_numeric_state(b.get("net_power_sensor"), default=None)
        elif mode == "separate":
            charge = (
                ha_client.get_numeric_state(b.get("charge_power_sensor"), default=None)
                if b.get("charge_power_sensor") else None
            )
            if charge is not None or power is not None:
                net_power = abs(charge or 0.0) - abs(power or 0.0)

        battery_live.append({"id": b["id"], "name": b["name"], "soc_pct": soc, "power_w": power, "net_power_w": net_power})
        if net_power is not None:
            live_battery_data_ok = True
            if net_power > 0:
                live_charge_w += net_power
            else:
                live_discharge_w += abs(net_power)
        if soc is not None:
            cap = float(b.get("capacity_wh", 0))
            total_capacity_wh += cap
            current_soc_wh += soc / 100 * cap
    current_soc_pct = round(100 * current_soc_wh / total_capacity_wh, 1) if total_capacity_wh else None

    pv_sensors = [a.get("current_sensor") for a in cfg["pv_arrays"] if a.get("current_sensor")]
    pv_now_w = None
    if pv_sensors:
        vals = [v for v in (ha_client.get_numeric_state(s, default=None) for s in pv_sensors) if v is not None]
        pv_now_w = round(sum(vals)) if vals else None

    # Modo "combined" (ver "Consumo de la casa" en Configuración): sin
    # sensor de consumo ya neteado, se reconstruye con el balance fisico
    # del panel — sol + red neta (con signo) + descarga − carga — a partir
    # del medidor de red EN BRUTO, mismo criterio que usa run_cycle.
    load_sensor_mode = cfg.get("load_sensor_mode") or "separate"
    net_grid_sensor = cfg.get("net_grid_sensor")
    net_grid_now_w = None
    if load_sensor_mode == "combined" and net_grid_sensor:
        net_grid_now_w = ha_client.get_numeric_state(net_grid_sensor, default=None)
        load_now_w = None
        if net_grid_now_w is not None and pv_now_w is not None and live_battery_data_ok:
            load_now_w = max(0.0, pv_now_w + net_grid_now_w + live_discharge_w - live_charge_w)
    else:
        load_sensor = cfg.get("load_sensor")
        load_now_w = ha_client.get_numeric_state(load_sensor, default=None) if load_sensor else None

    # Flujo de energia y margen de potencia contratada, calculados AQUI
    # (no en run_cycle) para que se refresquen cada vez que se pide
    # /api/live — cada 5s desde el dashboard (ver refreshLive en
    # index.html), sin esperar al proximo ciclo completo de optimizacion
    # (`cycle_seconds`, hasta 60s). La atribucion solar/red de la carga de
    # baterias tambien se calcula en vivo aqui (si el excedente solar
    # AHORA MISMO cubre lo que se esta cargando, se atribuye a solar; el
    # resto a red) en vez de depender de la decision que tomo el
    # planificador en su ultimo ciclo — mas preciso y sin ninguna
    # dependencia del ciclo.
    energy_flow_live = None
    if load_now_w is not None:
        solar_w = pv_now_w or 0.0
        solar_to_casa_w = min(solar_w, load_now_w)
        solar_surplus_w = max(0.0, solar_w - load_now_w)
        charge_w = live_charge_w if live_battery_data_ok else 0.0
        discharge_w = live_discharge_w if live_battery_data_ok else 0.0
        solar_to_batt_w = min(solar_surplus_w, charge_w)
        grid_to_batt_w = max(0.0, charge_w - solar_to_batt_w)
        batt_to_casa_w = discharge_w
        grid_to_casa_w = max(0.0, load_now_w - solar_to_casa_w - batt_to_casa_w)
        grid_total_w = grid_to_casa_w + grid_to_batt_w
        energy_needed_w = load_now_w + charge_w
        autoconsumo_pct = 100.0
        if energy_needed_w > 0:
            autoconsumo_pct = max(0.0, min(100.0, 100.0 * (1 - grid_total_w / energy_needed_w)))
        energy_flow_live = {
            "solar_w": round(solar_w),
            "load_w": round(load_now_w),
            "solar_to_casa_w": round(solar_to_casa_w),
            "solar_to_batt_w": round(solar_to_batt_w),
            "batt_to_casa_w": round(batt_to_casa_w),
            "battery_net_w": round(charge_w - discharge_w),
            "grid_w": round(grid_total_w),
            "autoconsumo_pct": round(autoconsumo_pct, 1),
            "contracted_power_w": float(cfg["general"].get("contracted_power_w") or 0),
            # Ver `_live_export_w` / comentario homologo en run_cycle: solo
            # informativo, no forma parte de load_w/grid_w ni del margen.
            "vertido_w": (lambda v: round(v) if v is not None else None)(_live_export_w(cfg, known_net_grid_w=net_grid_now_w)),
        }

    deferrable_live = []
    for load in cfg.get("deferrable_loads", []):
        try:
            switch_state = ha_client.get_state(load["switch_entity"])["state"]
        except (ha_client.HAError, requests.RequestException):
            switch_state = None
        power_sensor = load.get("power_sensor")
        power = ha_client.get_numeric_state(power_sensor, default=None) if power_sensor else None
        deferrable_live.append({
            "id": load["id"], "name": load["name"],
            "switch_state": switch_state, "power_w": power,
            "schedule": deferrable_store.get_schedule(load["id"]),
        })

    return jsonify({
        "now": datetime.now().isoformat(),
        "batteries": battery_live,
        "current_soc_pct": current_soc_pct,
        "pv_now_w": pv_now_w,
        "load_now_w": load_now_w,
        "deferrable_loads": deferrable_live,
        "energy_flow": energy_flow_live,
    })


@app.get("/api/battery_health")
def api_battery_health():
    cfg = config_store.load_config()
    # Cruzado por id, NO por nombre: dos baterias pueden compartir nombre, o
    # una puede haberse renombrado, y en ambos casos cruzar por nombre
    # atribuiria la salud/ciclos de una bateria a otra distinta.
    cycles = {h["id"]: h for h in lifetime_store.get_all_health(cfg["batteries"])}
    capacity = capacity_store.get_all_health(cfg["batteries"])
    combined = []
    for c in capacity:
        cyc = cycles.get(c["id"], {})
        combined.append({
            **c,
            "equivalent_cycles": cyc.get("equivalent_cycles", 0.0),
            "charged_kwh": cyc.get("charged_kwh", 0.0),
            "discharged_kwh": cyc.get("discharged_kwh", 0.0),
            "since": cyc.get("since"),
        })
    return jsonify(combined)


@app.get("/api/savings")
def api_savings():
    return jsonify(savings_store.get_summary(datetime.now()))


@app.get("/api/anomaly")
def api_anomaly():
    return jsonify(anomaly_store.get_status())


@app.post("/api/run_now")
def api_run_now():
    try:
        run_cycle()
    except Exception:
        # El detalle completo (tipo de excepcion, traceback) va solo al log
        # del servidor: no se devuelve al cliente para no exponer rutas de
        # ficheros, nombres de sensores internos, etc. via la respuesta.
        log.exception("Fallo al forzar ciclo")
        return jsonify({"error": "No se pudo forzar el ciclo, revisa el log del addon"}), 500
    with _state_lock:
        return jsonify(_last_status)


@app.post("/api/climate/discover")
def api_climate_discover():
    """
    Boton "Buscar zonas de Climate Orchestrator" en la configuracion — el
    UNICO sitio desde donde se llama a `climate_link.discover_zone_ids()`
    en toda la app. Guarda el resultado en config.json
    (`climate_orchestrator_zones`) para que `run_cycle()` lo use tal cual
    en cada ciclo sin volver a descubrir nada por su cuenta (ver
    climate_link.py). Sin Climate Orchestrator instalado, esto
    simplemente guarda una lista vacia — no es un error, es el resultado
    correcto de "no hay nada que encontrar".
    """
    try:
        zone_ids = climate_link.discover_zone_ids()
    except Exception:
        log.exception("Fallo al buscar zonas de Climate Orchestrator")
        return jsonify({"error": "No se pudo buscar zonas, revisa el log del addon"}), 500
    cfg = config_store.load_config()
    cfg["climate_orchestrator_zones"] = zone_ids
    cfg["climate_orchestrator_zones_discovered_at"] = datetime.now().isoformat()
    config_store.save_config(cfg)
    return jsonify({"zones": zone_ids, "count": len(zone_ids)})


@app.get("/")
def index():
    return send_from_directory("templates", "index.html")


@app.get("/<path:path>")
def static_files(path):
    return send_from_directory("static", path)


def _run_wallpanel_server():
    try:
        server = make_server("0.0.0.0", WALLPANEL_PORT, app, threaded=True)
        log.info(f"Panel de solo lectura (wallpanel) escuchando en el puerto {WALLPANEL_PORT}")
        server.serve_forever()
    except OSError as e:
        log.warning(f"No se pudo abrir el puerto wallpanel ({WALLPANEL_PORT}): {e}")


if __name__ == "__main__":
    t = threading.Thread(target=background_loop, daemon=True)
    t.start()
    wp = threading.Thread(target=_run_wallpanel_server, daemon=True)
    wp.start()
    app.run(host="0.0.0.0", port=8099, threaded=True)

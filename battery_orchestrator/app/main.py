from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime

from flask import Flask, Response, jsonify, request, send_from_directory

import anomaly_store
import battery_exec
import capacity_store
import config_store
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
    "error": None,
}

ANOMALY_NOTIFICATION_ID = "battery_orchestrator_anomaly"


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


def run_cycle():
    """Un ciclo completo: leer estado, planificar, repartir, ejecutar."""
    cfg = config_store.load_config()
    batteries_cfg = cfg["batteries"]

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
    load_sensor = cfg.get("load_sensor")
    history_days = cfg["general"]["history_days_for_load"]

    if load_sensor:
        battery_discharge_sensors = [b.get("power_sensor") for b in batteries_cfg if b.get("power_sensor")]
        solar_sensors_for_load = [a.get("current_sensor") for a in cfg["pv_arrays"] if a.get("current_sensor")]
        load_forecast = ha_client.true_load_forecast(
            load_sensor, solar_sensors_for_load, battery_discharge_sensors, horizon, days=history_days
        )
    else:
        load_forecast = [300.0] * horizon

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

    now_hp = plan[0]
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
    dry_run = bool(cfg["general"]["dry_run"])
    log_lines = battery_exec.execute(batteries, distribution, dry_run=dry_run)

    for line in log_lines:
        log.info(line)
    log.info(f"Hora actual: {now_hp.tier} ({now_hp.price} EUR/kWh) - {now_hp.reason}")

    # Cuenta atras a la proxima punta: la propia reserva (reserve_wh) que
    # acaba de calcular el planificador es el numero real que se esta
    # usando para decidir, asi que se reutiliza tal cual en vez de volver
    # a calcularlo aparte.
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

    # Flujo de energia ahora mismo, para el diagrama de "Estado actual" —
    # los mismos numeros que ya usa el planificador, solo reordenados para
    # mostrar de donde sale la potencia y a donde va.
    solar_to_casa_w = min(now_hp.pv_w, now_hp.load_w)
    solar_to_batt_w = now_hp.charge_w if now_hp.charge_source == "solar" else 0.0
    grid_to_batt_w = now_hp.charge_w if now_hp.charge_source == "grid" else 0.0
    batt_to_casa_w = now_hp.discharge_w
    grid_to_casa_w = max(0.0, now_hp.load_w - solar_to_casa_w - batt_to_casa_w)
    grid_total_w = grid_to_casa_w + grid_to_batt_w
    energy_needed_now_w = now_hp.load_w + now_hp.charge_w
    autoconsumo_pct = 100.0
    if energy_needed_now_w > 0:
        autoconsumo_pct = max(0.0, min(100.0, 100.0 * (1 - grid_total_w / energy_needed_now_w)))
    energy_flow = {
        "solar_w": round(now_hp.pv_w),
        "load_w": round(now_hp.load_w),
        "solar_to_casa_w": round(solar_to_casa_w),
        "solar_to_batt_w": round(solar_to_batt_w),
        "batt_to_casa_w": round(batt_to_casa_w),
        "battery_net_w": round(now_hp.charge_w - now_hp.discharge_w),
        "grid_w": round(grid_total_w),
        "autoconsumo_pct": round(autoconsumo_pct, 1),
    }

    # Energia (Wh) movida en este ciclo, por bateria. En carga usamos la
    # potencia real repartida a cada una; en descarga cada bateria se
    # autogestiona (no la repartimos de verdad), asi que aqui SOLO para
    # llevar la cuenta se estima proporcional a su potencia maxima de
    # descarga entre las que estan activas — es una estimacion, no una
    # medicion exacta de lo que ha hecho cada una.
    cycle_hours = cfg["general"]["cycle_seconds"] / 3600
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
    # consumo configurado.
    anomaly = None
    if load_sensor:
        try:
            live_base = ha_client.get_numeric_state(load_sensor, default=None)
            if live_base is not None:
                live_pv = pv_now_actual if pv_now_actual is not None else pv_forecast[0]
                live_discharge = sum(
                    ha_client.get_numeric_state(b.get("power_sensor"), default=0.0) or 0.0
                    for b in batteries_cfg if b.get("power_sensor")
                )
                live_load_w = live_base + live_pv + live_discharge
                anomaly = anomaly_store.update(now, live_load_w, load_forecast[0])
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
        ha_client.publish_sensor(
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

    with _state_lock:
        _last_status.update(
            last_run=datetime.now().isoformat(),
            plan=today_history + future_plan,
            distribution=distribution,
            log_lines=log_lines,
            skipped_batteries=skipped,
            pv_now_actual=pv_now_actual,
            current_soc_pct=current_soc_pct,
            next_punta=next_punta,
            next_tariff_change=next_tariff_change,
            energy_flow=energy_flow,
            consumption_comparison=consumption_comparison,
            anomaly=anomaly,
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


@app.get("/api/status")
def api_status():
    with _state_lock:
        return jsonify(_last_status)


@app.get("/api/battery_health")
def api_battery_health():
    cfg = config_store.load_config()
    cycles = {h["name"]: h for h in lifetime_store.get_all_health(cfg["batteries"])}
    capacity = capacity_store.get_all_health(cfg["batteries"])
    combined = []
    for c in capacity:
        cyc = cycles.get(c["name"], {})
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
    except Exception as e:
        log.exception("Fallo al forzar ciclo")
        return jsonify({"error": str(e)}), 500
    with _state_lock:
        return jsonify(_last_status)


@app.get("/")
def index():
    return send_from_directory("templates", "index.html")


@app.get("/<path:path>")
def static_files(path):
    return send_from_directory("static", path)


if __name__ == "__main__":
    t = threading.Thread(target=background_loop, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=8099, threaded=True)

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from flask import Flask, jsonify, request, send_from_directory

import battery_exec
import config_store
import ha_client
import pv_source
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
    "error": None,
}


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

    pv_forecast = pv_source.get_pv_forecast_total(
        cfg["pv_arrays"], horizon, refresh_seconds=cfg["general"]["pv_refresh_seconds"]
    )

    # Correccion en tiempo real: para la hora ACTUAL (indice 0) usamos la
    # produccion solar real medida ahora mismo en vez de la previsión, si
    # hay un sensor configurado. Las horas futuras siguen siendo previsión
    # (todavia no existe un "dato real" de algo que no ha pasado).
    current_pv_sensor = cfg.get("current_pv_sensor")
    pv_now_actual = None
    if current_pv_sensor:
        pv_now_actual = ha_client.get_numeric_state(current_pv_sensor, default=None)
        if pv_now_actual is not None and pv_forecast:
            pv_forecast[0] = pv_now_actual

    # Consumo real = consumo base (ya sin carga de baterias) + solar + descarga
    # de baterias. Si no hay ningun sensor de baterias declarado, es
    # simplemente el consumo base (funciona igual, solo menos preciso en
    # las horas en que la bateria cubre gran parte del consumo).
    load_sensor = cfg.get("load_sensor")
    history_days = cfg["general"]["history_days_for_load"]

    if load_sensor:
        battery_discharge_sensors = [b.get("power_sensor") for b in batteries_cfg if b.get("power_sensor")]
        solar_sensor_for_load = cfg.get("current_pv_sensor") or None
        load_forecast = ha_client.true_load_forecast(
            load_sensor, solar_sensor_for_load, battery_discharge_sensors, horizon, days=history_days
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

    plan = scheduler.build_plan(
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
        max_usable_wh=max_usable_wh,
    )

    now_hp = plan[0]
    pv_surplus_now = max(0.0, now_hp.pv_w - now_hp.load_w)
    distribution = battery_exec.plan_distribution(
        batteries, now_hp.charge_w, now_hp.discharge_w, pv_surplus_w=pv_surplus_now
    )
    dry_run = bool(cfg["general"]["dry_run"])
    log_lines = battery_exec.execute(batteries, distribution, dry_run=dry_run)

    for line in log_lines:
        log.info(line)
    log.info(f"Hora actual: {now_hp.tier} ({now_hp.price} EUR/kWh) - {now_hp.reason}")

    try:
        ha_client.publish_sensor(
            "sensor.battery_orchestrator_status",
            now_hp.reason,
            {
                "tramo": now_hp.tier,
                "precio": now_hp.price,
                "carga_w": now_hp.charge_w,
                "descarga_w": now_hp.discharge_w,
                "soc_total_pct": round(100 * current_soc_wh / total_capacity_wh, 1) if total_capacity_wh else 0,
                "dry_run": dry_run,
                "pv_actual_w": pv_now_actual,
                "baterias_omitidas": skipped,
                "friendly_name": "Battery Orchestrator",
            },
        )
    except Exception as e:  # no tumbar el ciclo si HA no responde
        log.warning(f"No se pudo publicar el sensor de estado: {e}")

    with _state_lock:
        _last_status.update(
            last_run=datetime.now().isoformat(),
            plan=[
                {
                    "dt": hp.dt.isoformat(), "price": hp.price, "tier": hp.tier,
                    "pv_w": round(hp.pv_w), "load_w": round(hp.load_w),
                    "charge_w": round(hp.charge_w), "discharge_w": round(hp.discharge_w),
                    "soc_pct": round(100 * hp.soc_wh / total_capacity_wh, 1) if total_capacity_wh else 0,
                    "reason": hp.reason,
                }
                for hp in plan
            ],
            distribution=distribution,
            log_lines=log_lines,
            skipped_batteries=skipped,
            pv_now_actual=pv_now_actual,
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

"""
Cliente minimo para hablar con Home Assistant.

Dentro de un addon, HA Supervisor inyecta SUPERVISOR_TOKEN y el proxy
interno en http://supervisor/core/api/. Para desarrollo local fuera del
addon, se puede usar HA_URL + HA_TOKEN (token de larga duracion) en su lugar.
"""

from __future__ import annotations

import logging
import os
import statistics
import time
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger("ha_client")

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN")
if SUPERVISOR_TOKEN:
    BASE_URL = "http://supervisor/core/api"
    TOKEN = SUPERVISOR_TOKEN
else:
    BASE_URL = os.environ.get("HA_URL", "http://localhost:8123/api")
    TOKEN = os.environ.get("HA_TOKEN", "")

HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
TIMEOUT = 10


class HAError(Exception):
    pass


def get_state(entity_id: str):
    r = requests.get(f"{BASE_URL}/states/{entity_id}", headers=HEADERS, timeout=TIMEOUT)
    if r.status_code == 404:
        raise HAError(f"Entidad no encontrada: {entity_id}")
    r.raise_for_status()
    return r.json()


def get_all_states() -> list[dict]:
    """
    Todos los estados de HA de una vez (para descubrir entidades por
    atributo, p.ej. las zonas de Climate Orchestrator - ver
    climate_link.py - en vez de tener que declararlas una a una a mano).
    Lista vacia si HA no responde, nunca propaga la excepcion: quien
    descubre algo a partir de esto ya sabe tratar "no hay nada todavia"
    igual que "no se pudo preguntar ahora".
    """
    try:
        r = requests.get(f"{BASE_URL}/states", headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except requests.RequestException:
        return []


def render_template(template: str) -> str | None:
    """
    Pide a HA que renderice una plantilla Jinja2 EL MISMO (POST
    /api/template) — HA solo serializa lo que la plantilla pida, nunca el
    volcado completo de /api/states. Se usa para descubrir las zonas de
    Climate Orchestrator filtrando por dominio "climate" DENTRO de HA en
    vez de traerse las ~2000+ entidades de toda la instalacion para
    filtrarlas aqui (ver climate_link.py) - mismo resultado, fraccion del
    coste, tanto de red como de CPU/memoria en el lado de HA Core.
    Devuelve None si HA no responde (quien lo use ya sabe caer a "no hay
    nada todavia", igual que con `get_all_states`).
    """
    try:
        r = requests.post(f"{BASE_URL}/template", headers=HEADERS, json={"template": template}, timeout=TIMEOUT)
        r.raise_for_status()
        return r.text
    except requests.RequestException:
        return None


STALE_STATES = {"unavailable", "unknown", "none", ""}


def get_numeric_state(entity_id: str, default: float | None = 0.0) -> float | None:
    """
    Devuelve el valor numerico de una entidad. Si la entidad esta
    'unavailable'/'unknown' o no existe, devuelve `default` (que puede ser
    None para que el llamante decida saltarse esa entidad en vez de
    asumir un valor inventado). Un fallo de red/HA pasajero (timeout, 502/503
    del Supervisor) tambien cae a `default` en vez de tumbar el ciclo entero
    de planificacion - mejor una hora con un dato por defecto que ninguna
    orden de carga/descarga hasta que HA vuelva a responder.
    """
    try:
        s = get_state(entity_id)["state"]
        if s.strip().lower() in STALE_STATES:
            return default
        return float(s)
    except (HAError, ValueError, KeyError, requests.RequestException):
        return default


def call_service(
    domain: str, service: str, entity_id: str | None = None, extra: dict | None = None,
    timeout: float = TIMEOUT, return_response: bool = False,
):
    payload = {}
    if entity_id:
        payload["entity_id"] = entity_id
    if extra:
        payload.update(extra)
    url = f"{BASE_URL}/services/{domain}/{service}"
    if return_response:
        # Query param que expone HA para servicios que declaran datos de
        # respuesta (SupportsResponse.OPTIONAL/ONLY) — sin esto la llamada
        # funciona igual pero el resultado nunca trae "service_response".
        url += "?return_response"
    r = requests.post(url, headers=HEADERS, json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()


def call_service_with_response(
    domain: str, service: str, extra: dict | None = None, timeout: float = TIMEOUT,
) -> dict | None:
    """
    Para servicios de terceros (p.ej. el puente BLE de EcoFlow, ver
    ecoflow_ble.py) que devuelven datos de verdad, no solo cambian
    entidades — nunca lanza por un fallo de red/HA, devuelve `None` para
    que quien llame lo trate igual que "sin dato todavia" en vez de tumbar
    el ciclo entero.
    """
    try:
        result = call_service(domain, service, extra=extra, timeout=timeout, return_response=True)
    except (HAError, requests.RequestException) as e:
        log.warning(f"Fallo al llamar al servicio {domain}.{service}: {e}")
        return None
    return result.get("service_response") if isinstance(result, dict) else None


def turn_on(entity_id: str):
    domain = entity_id.split(".")[0]
    return call_service(domain, "turn_on", entity_id)


def turn_off(entity_id: str):
    domain = entity_id.split(".")[0]
    return call_service(domain, "turn_off", entity_id)


def set_number(entity_id: str, value: float):
    return call_service("number", "set_value", entity_id, {"value": value})


def publish_sensor(entity_id: str, state, attributes: dict | None = None):
    """Publica un sensor propio del orquestador en HA (para dashboards)."""
    payload = {"state": state, "attributes": attributes or {}}
    r = requests.post(f"{BASE_URL}/states/{entity_id}", headers=HEADERS, json=payload, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def get_history(entity_id: str, days: int) -> list[dict]:
    # OJO: la marca de tiempo va EMBEBIDA en la ruta de la URL (no en un
    # parametro de query), asi que tiene que ir "limpia". .isoformat() por
    # defecto produce algo como "...T21:58:03.123456+00:00": el "+" ahi
    # dentro rompe la ruta (se puede interpretar como espacio o generar una
    # fecha invalida) y HA devuelve una respuesta vacia sin avisar de error.
    # Formato limpio con sufijo "Z" (UTC) en su lugar.
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = requests.get(
        f"{BASE_URL}/history/period/{start}",
        headers=HEADERS,
        params={"filter_entity_id": entity_id, "minimal_response": "true"},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    return data[0] if data else []


# Con menos muestras reales que esto en una franja horaria concreta, esa
# franja no se considera fiable todavia (una lectura suelta -p.ej. una nube
# pasajera, o un sensor recien dado de alta que solo ha visto esa hora una
# vez- no debe fijar la media de toda la franja) y se rellena como si no
# hubiera dato, en vez de arrastrar ese ruido a la previsión.
MIN_SAMPLES_PER_HOUR = 3


def _safe_get_history(entity_id: str, days: int) -> list[dict]:
    """
    Igual que `get_history`, pero absorbe fallos de red/HA (timeouts, 502/503
    del Supervisor mientras arranca o se reinicia HA...) devolviendo lista
    vacia en vez de propagar la excepcion - un ciclo de planificacion entero
    no debe abortar (dejando la bateria sin ninguna orden) solo porque UNA
    llamada de historico haya fallado de forma pasajera.
    """
    try:
        return get_history(entity_id, days)
    except requests.RequestException:
        return []


_has_history_cache: dict[tuple, tuple[float, bool]] = {}
HAS_HISTORY_CACHE_SECONDS = 1800  # 30 min


def has_recent_history(entity_id: str, days: int = 1) -> bool:
    """
    Comprobacion de si hay algun punto de historico real para este sensor
    en los ultimos `days` dias. Se usa para saber si merece la pena intentar
    calcular una media horaria (`hourly_average_forecast`) o si el sensor es
    demasiado nuevo y todavia no hay nada que promediar. OJO: esto NO
    garantiza que cada hora tenga suficiente muestra - eso lo decide
    `hourly_average_forecast_with_reliability` franja a franja.

    Cacheada `HAS_HISTORY_CACHE_SECONDS`: "¿tiene ya historico?" no puede
    cambiar mas que de False a True (nunca al reves, en uso normal), asi
    que no hace falta volver a pedir el historico entero de un sensor -
    potencialmente con muchisimos puntos si reporta muy a menudo, como un
    sensor de potencia solar - en cada ciclo de 30-60s solo para esta
    comprobacion booleana. Sin cache, esto llegaba a pedir el historico
    completo del sensor solar decenas de miles de veces al dia.
    """
    cache_key = (entity_id, days)
    now_ts = time.time()
    cached = _has_history_cache.get(cache_key)
    if cached is not None and (now_ts - cached[0]) < HAS_HISTORY_CACHE_SECONDS:
        return cached[1]
    result = bool(_safe_get_history(entity_id, days))
    _has_history_cache[cache_key] = (now_ts, result)
    return result


# Cuanto se reutiliza la media por hora-del-dia ya calculada antes de
# volver a pedir el historico a HA. Estas medias apenas cambian de un ciclo
# a otro (se basan en dias enteros de historico); pedirlas enteras cada
# `cycle_seconds` (tipicamente 30-60s) es puro peso extra sobre el recorder
# de HA sin ganar nada en precision. Se cachea SOLO la parte cara (pedir y
# recorrer el historico), nunca el resultado ya alineado a "ahora" - la
# alineacion cambia cada hora y tiene que calcularse fresca siempre, o un
# resultado cacheado se quedaria "atrasado" una hora justo al cruzar el
# limite entre dos horas dentro de la ventana de cache.
_HISTORY_CACHE_SECONDS = 900  # 15 min
_hourly_avg_cache: dict[tuple, tuple[float, dict[int, float], dict[int, bool]]] = {}


def _hourly_avg_by_hour_of_day(
    entity_id: str, days: int, default: float, abs_values: bool, sign_filter: str | None = None
) -> tuple[dict[int, float], dict[int, bool]]:
    cache_key = (entity_id, days, default, abs_values, sign_filter)
    cached = _hourly_avg_cache.get(cache_key)
    now_ts = time.time()
    if cached is not None and (now_ts - cached[0]) < _HISTORY_CACHE_SECONDS:
        return cached[1], cached[2]

    raw = _safe_get_history(entity_id, days)
    if not raw:
        for fallback_days in (10, 7, 3, 1):
            if fallback_days >= days:
                continue
            raw = _safe_get_history(entity_id, fallback_days)
            if raw:
                break
    if not raw:
        current = get_numeric_state(entity_id, default=default)
        if abs_values and current is not None:
            current = abs(current)
        hourly_avg = {h: current for h in range(24)}
        reliable_by_hour = {h: False for h in range(24)}
        _hourly_avg_cache[cache_key] = (now_ts, hourly_avg, reliable_by_hour)
        return hourly_avg, reliable_by_hour

    buckets: dict[int, list[float]] = {h: [] for h in range(24)}
    for point in raw:
        try:
            val = float(point["state"])
        except (KeyError, ValueError):
            continue
        # sign_filter separa un sensor bidireccional con signo en sus dos
        # mitades (p.ej. un "net_power_sensor" de bateria: positivo=carga,
        # negativo=descarga) para poder promediar cada una POR SEPARADO —
        # necesario para reconstruir consumo desde un sensor de red en
        # bruto (ver `true_load_forecast_from_grid`), donde la carga tiene
        # que RESTARSE y la descarga SUMARSE, cosa que un abs_values() a
        # secas no puede distinguir. Tiene prioridad sobre abs_values.
        if sign_filter == "positive":
            if val <= 0:
                continue
        elif sign_filter == "negative":
            if val >= 0:
                continue
            val = abs(val)
        elif abs_values:
            val = abs(val)
        ts = datetime.fromisoformat(point["last_changed"].replace("Z", "+00:00"))
        buckets[ts.astimezone().hour].append(val)

    hourly_avg: dict[int, float | None] = {}
    reliable_by_hour: dict[int, bool] = {}
    for h, vals in buckets.items():
        reliable_by_hour[h] = len(vals) >= MIN_SAMPLES_PER_HOUR
        hourly_avg[h] = statistics.mean(vals) if reliable_by_hour[h] else None

    known = [v for v in hourly_avg.values() if v is not None]
    fallback = statistics.mean(known) if known else default
    for h in range(24):
        if hourly_avg[h] is None:
            hourly_avg[h] = fallback

    _hourly_avg_cache[cache_key] = (now_ts, hourly_avg, reliable_by_hour)
    return hourly_avg, reliable_by_hour


def hourly_average_forecast_with_reliability(
    entity_id: str, horizon_hours: int, days: int = 21, default: float = 0.0, abs_values: bool = False,
    sign_filter: str | None = None,
) -> tuple[list[float], list[bool]]:
    """
    Igual que `hourly_average_forecast`, pero ademas devuelve, hora a hora,
    si ese valor viene de suficiente historico real (>= MIN_SAMPLES_PER_HOUR
    muestras en esa franja horaria) o si es un relleno (media de las horas
    que si tienen muestra suficiente, o el valor actual si no hay historico
    en absoluto). Sirve para que quien consuma esto sepa en que horas puede
    fiarse del historico y en cuales todavia no.

    `abs_values=True` aplica valor absoluto a CADA MUESTRA antes de
    promediar (no a la media ya calculada) - imprescindible para sensores
    de potencia bidireccionales con signo (p.ej. carga positiva/descarga
    negativa en un mismo sensor): promediar primero y aplicar abs() despues
    deja que las muestras positivas y negativas de una misma franja horaria
    se CANCELEN entre si, escondiendo el verdadero movimiento de energia.

    La parte cara (pedir el historico a HA y recorrerlo) se cachea
    `_HISTORY_CACHE_SECONDS`; la alineacion al horizonte desde la hora
    ACTUAL se recalcula siempre al vuelo, nunca desde cache.
    """
    hourly_avg, reliable_by_hour = _hourly_avg_by_hour_of_day(entity_id, days, default, abs_values, sign_filter)
    now = datetime.now()
    values = [hourly_avg[(now.hour + i) % 24] for i in range(horizon_hours)]
    reliable = [reliable_by_hour[(now.hour + i) % 24] for i in range(horizon_hours)]
    return values, reliable


def hourly_average_forecast(
    entity_id: str, horizon_hours: int, days: int = 21, default: float = 0.0, abs_values: bool = False,
    sign_filter: str | None = None,
) -> list[float]:
    """
    Previsión simple y explicable para CUALQUIER sensor numerico: para cada
    hora del horizonte, la media de esa MISMA hora-del-dia en los ultimos
    `days` dias de historico real. Nada de aprendizaje automatico opaco.

    Si `days` supera lo que tu Home Assistant realmente conserva (por
    defecto el recorder solo guarda 10 dias), reintenta con ventanas mas
    cortas antes de rendirse - asi no depende de que sepas/ajustes ese
    detalle de configuracion tuyo.

    `sign_filter` ("positive" | "negative" | None): para sensores
    bidireccionales con signo, promedia SOLO la mitad de muestras que
    cumple el signo pedido (ver `_hourly_avg_by_hour_of_day`) — necesario
    cuando carga y descarga comparten el mismo sensor y hay que tratarlas
    por separado (ver `true_load_forecast_from_grid`).
    """
    values, _ = hourly_average_forecast_with_reliability(entity_id, horizon_hours, days, default, abs_values, sign_filter)
    return values


# alias retrocompatible
load_forecast_from_history = hourly_average_forecast


def true_load_forecast(base_consumption_sensor: str, solar_sensors: list[str],
                        battery_discharge_sensors: list[str],
                        horizon_hours: int, days: int = 21) -> list[float]:
    """
    Reconstruye el consumo REAL de la vivienda sumando el historico de cada
    componente por separado, hora a hora:

        consumo = consumo_base (red YA SIN la carga de baterias, p.ej.
                                 "consumo_instantaneo")
                + produccion_solar (de cada string/tejado declarado, sumados)
                + descarga_baterias (solo salida, sensores positivos tipo
                  "..._load_from_battery"; NO hace falta el de carga: al
                  restarse ya en el sensor base, los terminos de carga se
                  cancelan matematicamente)

    El sensor de descarga de cada bateria se toma en valor absoluto MUESTRA A
    MUESTRA (no sobre la media ya calculada): algunos modelos usan un unico
    sensor bidireccional (carga positiva/descarga negativa - el mismo caso ya
    detectado y corregido en el calculo en vivo, ver `net_power_w` en
    main.py). Si una franja horaria mezcla muestras de carga y descarga de
    distintos dias (p.ej. unos dias todavia cargando a esa hora, otros ya
    descargando), promediar primero y aplicar abs() despues deja que esas
    muestras se CANCELEN entre si y el resultado se hunda cerca de cero
    aunque hubiera bastante movimiento de energia real. Por eso el abs() se
    aplica antes de promediar.

    No hace falta un sensor nuevo en HA: se calcula aqui mismo a partir de
    sensores que ya existen y ya tienen historico acumulado.
    """
    total = hourly_average_forecast(base_consumption_sensor, horizon_hours, days, default=0.0)

    for ss in solar_sensors:
        if not ss:
            continue
        solar = hourly_average_forecast(ss, horizon_hours, days, default=0.0)
        total = [total[i] + solar[i] for i in range(horizon_hours)]

    for bs in battery_discharge_sensors:
        if not bs:
            continue
        batt = hourly_average_forecast(bs, horizon_hours, days, default=0.0, abs_values=True)
        total = [total[i] + batt[i] for i in range(horizon_hours)]

    return total


def true_load_forecast_from_grid(net_grid_sensor: str, solar_sensors: list[str],
                                  batteries_cfg: list[dict], horizon_hours: int, days: int = 21,
                                  ecoflow_discharge_sensor: str | None = None,
                                  ecoflow_charge_sensor: str | None = None) -> list[float]:
    """
    Igual que `true_load_forecast`, pero para el modo "unificado" del
    sensor de consumo (ver "Consumo de la casa" en Configuración): en vez
    de un sensor que YA reste la carga de las baterías, aquí se parte del
    medidor de red EN BRUTO del punto de conexión (con signo: positivo
    importando, negativo vertiendo) — balance de potencia en el panel:

        consumo = produccion_solar + red_neta (con signo) + descarga_baterias
                  - carga_baterias

    A diferencia de `true_load_forecast`, aquí SÍ hace falta la carga de
    cada batería por separado (positiva), porque el sensor de red en bruto
    no la excluye como sí hace un "consumo_instantaneo" ya neteado — sin
    restarla, cada carga se contaría dos veces como si fuera consumo de la
    casa.

    Para baterías en modo "combined" (un único `net_power_sensor` con
    signo, carga positiva/descarga negativa), la carga y la descarga se
    extraen del MISMO sensor pero promediando cada mitad por separado
    (`sign_filter`, ver `hourly_average_forecast`) — promediar el sensor
    entero y aplicar abs() después cancelaría carga y descarga entre sí
    dentro de la misma franja horaria.
    """
    total = hourly_average_forecast(net_grid_sensor, horizon_hours, days, default=0.0)

    for ss in solar_sensors:
        if not ss:
            continue
        solar = hourly_average_forecast(ss, horizon_hours, days, default=0.0)
        total = [total[i] + solar[i] for i in range(horizon_hours)]

    for b in batteries_cfg:
        mode = b.get("power_sensor_mode") or ("separate" if b.get("power_sensor") or b.get("charge_power_sensor") else "none")
        if mode == "combined" and b.get("net_power_sensor"):
            sensor = b.get("net_power_sensor")
            discharge = hourly_average_forecast(sensor, horizon_hours, days, default=0.0, sign_filter="negative")
            charge = hourly_average_forecast(sensor, horizon_hours, days, default=0.0, sign_filter="positive")
            total = [total[i] + discharge[i] - charge[i] for i in range(horizon_hours)]
        elif mode == "separate":
            if b.get("power_sensor"):
                discharge = hourly_average_forecast(b.get("power_sensor"), horizon_hours, days, default=0.0, abs_values=True)
                total = [total[i] + discharge[i] for i in range(horizon_hours)]
            if b.get("charge_power_sensor"):
                charge = hourly_average_forecast(b.get("charge_power_sensor"), horizon_hours, days, default=0.0, abs_values=True)
                total = [total[i] - charge[i] for i in range(horizon_hours)]

    # Baterias EcoFlow: no tienen sensor de HA propio (bucle de arriba las
    # ignora, `power_sensor`/`net_power_sensor` vienen vacios a proposito),
    # asi que su descarga/carga se suma aqui a partir del agregado que
    # publica el propio addon (ver _live_sensor_loop en main.py) — sin
    # esto, cualquier consumo que una bateria EcoFlow cubriera sola
    # desaparece de la reconstruccion del historico.
    if ecoflow_discharge_sensor:
        discharge = hourly_average_forecast(ecoflow_discharge_sensor, horizon_hours, days, default=0.0)
        total = [total[i] + discharge[i] for i in range(horizon_hours)]
    if ecoflow_charge_sensor:
        charge = hourly_average_forecast(ecoflow_charge_sensor, horizon_hours, days, default=0.0)
        total = [total[i] - charge[i] for i in range(horizon_hours)]

    # El consumo real nunca es negativo — un resultado negativo aqui solo
    # puede venir de ruido de medida entre sensores independientes (p.ej.
    # relojes/franjas ligeramente desalineados entre el sensor de red y el
    # de bateria), nunca de una situacion real.
    return [max(0.0, v) for v in total]


def pv_forecast_from_entity(entity_id: str, horizon_hours: int) -> list[float]:
    """
    Lee la previsión solar desde un sensor de HA que exponga un atributo de
    tipo lista de pronosticos (forecast_solar, EMHASS p_pv_forecast, etc.)
    Se buscan claves de atributo habituales; si no se encuentra nada
    utilizable, se devuelve una lista de ceros (seguro, nunca inventa sol).
    """
    try:
        state = get_state(entity_id)
    except (HAError, requests.RequestException):
        return [0.0] * horizon_hours

    attrs = state.get("attributes", {})
    for key in ("forecasts", "wh_hours", "watts", "forecast"):
        if key in attrs and isinstance(attrs[key], (list, dict)):
            series = attrs[key]
            if isinstance(series, dict):
                values = list(series.values())[:horizon_hours]
            else:
                values = [
                    item.get("p_pv_forecast") or item.get("value") or item.get("power")
                    for item in series[:horizon_hours]
                ]
            values = [float(v) for v in values if v is not None]
            if values:
                values += [0.0] * (horizon_hours - len(values))
                return values[:horizon_hours]

    # sin atributo de previsión util: usar el valor actual como estimacion
    # plana solo para la proxima hora, y 0 despues (mejor infravalorar que
    # inventar produccion que no va a existir)
    try:
        current = float(state["state"])
    except (ValueError, KeyError):
        current = 0.0
    return [current] + [0.0] * (horizon_hours - 1)

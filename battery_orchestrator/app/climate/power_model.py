"""
Aprendizaje de la potencia TIPICA de un actuador CONCRETO (no de toda la
zona: una misma zona puede tener un aire acondicionado sin forma de
medir su consumo real y un radiador con su propio sensor, cada uno con
su propia fuente — ver CONF_ACTUATOR_POWER en const.py), para cuando ese
actuador en concreto no tiene ni sensor propio ni un valor fijo
declarado. Util sobre todo para equipos donde no se puede instrumentar
el consumo real, como un aire acondicionado con maquina exterior
COMPARTIDA entre varias interiores.

Igual que thermal_model.py: nada de aprendizaje automatico opaco. Se
busca en el historico de un sensor de potencia GENERAL de la vivienda
(CONF_HOME_POWER_SENSOR) el salto medido justo antes/despues de cada vez
que este actuador concreto cambia de estado, y se toma la MEDIANA de
esos saltos — un numero que se puede explicar en una frase: "de media,
este actuador sube el consumo de la vivienda en 1200W al encenderse".
Sin historico suficiente, `reliable=False` — climate.py se encarga de
caer entonces a la potencia estimada fija declarada para ese actuador
(ver CONF_ACTUATOR_POWER en const.py) si la hay, o a nada.

MEJORA CLAVE para el caso de maquina exterior compartida: antes de
aceptar una transicion como muestra valida, se comprueba si ALGUNA OTRA
zona Climate Orchestrator (sus propios switches/climate.*/humidifier.*
declarados) ya estaba activa en ese mismo instante — si es asi, esa
transicion se descarta (no se puede aislar limpiamente que parte del
salto es de esta zona y cual de la otra). Solo se cuentan transiciones
"limpias": ningun otro equipo conocido cambiando ni ya encendido en la
ventana de muestreo. Esto es justo lo que hace falta para un split
multi-interior con un unico compresor: si dos interiores comparten
maquina exterior, un salto medido mientras la otra ya estaba en marcha
no dice nada fiable sobre el consumo de la primera.

Sigue siendo una CORRELACION, no una medida directa — `reliable=False`
con pocas muestras limpias, nunca se presenta como un dato medido de
verdad.
"""

from __future__ import annotations

import logging
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime, timedelta, timezone

_LOGGER = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _start_iso(days: int) -> str:
    return (_utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_dt(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc)

MIN_RUN_MINUTES = 3            # la potencia cambia al instante, no hace falta un tramo largo como en thermal_model
MIN_VALID_SAMPLES = 4          # transiciones LIMPIAS minimas antes de fiarse de la mediana
SAMPLE_WINDOW_MINUTES = 2      # cuanto se promedia el sensor de potencia justo antes/despues de la transicion
MIN_DELTA_W = 10               # saltos menores se descartan como ruido de medida, no un cambio real

# `_active_intervals` escanea el historico de TODOS los actuadores de
# TODAS las demas zonas — el mismo escaneo, repetido entero, cada vez que
# CUALQUIER zona necesita aprender su potencia. Con varias zonas, aunque
# cada una ya reparte CUANDO recalcula (ver climate.py `_zone_stagger_
# seconds`), pueden seguir coincidiendo dentro de una franja de varios
# minutos — y el resultado ("que tramos estuvo encendido cada actuador
# ajeno en los ultimos N dias") no cambia de un minuto para otro. Se
# cachea a nivel de proceso (compartido entre TODAS las zonas, no por
# zona) para no repetir el mismo escaneo caro varias veces seguidas.
_active_intervals_cache: dict[tuple, tuple[float, list]] = {}
ACTIVE_INTERVALS_CACHE_SECONDS = 1800  # 30 min


class _SyntheticState:
    __slots__ = ("state", "last_changed")

    def __init__(self, state: str, last_changed: datetime) -> None:
        self.state = state
        self.last_changed = last_changed


# LIMITE DURO, no negociable — mismo motivo y mismo valor que en
# thermal_model.py (ver ahi para el razonamiento completo): esta funcion
# TAMBIEN se usa para leer el sensor general de potencia de la casa
# (home_power_sensor), que suele ser de los sensores mas parlanchines de
# toda la instalacion.
MAX_STATES_PER_ENTITY = 5000


def _cap_states(states: list) -> list:
    if len(states) <= MAX_STATES_PER_ENTITY:
        return states
    step = len(states) / MAX_STATES_PER_ENTITY
    return [states[int(i * step)] for i in range(MAX_STATES_PER_ENTITY)]


def _history_for(ws, entity_id: str, days: int) -> list:
    try:
        raw = ws.get_history(entity_id, _start_iso(days), with_attributes=False)
    except Exception:
        _LOGGER.debug("Sin historico (via WebSocket) de %s todavia", entity_id, exc_info=True)
        raw = []
    points = [_SyntheticState(p["state"], _to_dt(p["last_updated"])) for p in raw if p.get("last_updated") is not None]
    return _cap_states(points)


def _state_runs(states: list) -> list[tuple[datetime, datetime, str]]:
    runs = []
    for i, s in enumerate(states):
        if s.state not in ("on", "off"):
            continue
        start = s.last_changed
        end = states[i + 1].last_changed if i + 1 < len(states) else _utcnow()
        runs.append((start, end, s.state))
    return runs


def _climate_on_off_runs(ws, entity_id: str, days: int) -> list[tuple[datetime, datetime, str]]:
    """Traduce el historico de un climate.* a on/off via su `hvac_action`
    (heating/cooling frente al resto) — mismo truco que thermal_model.py."""
    try:
        raw = ws.get_history(entity_id, _start_iso(days), with_attributes=True)
    except Exception:
        _LOGGER.debug("Sin historico (via WebSocket) de %s todavia", entity_id, exc_info=True)
        raw = []
    raw = _cap_states([p for p in raw if p.get("last_updated") is not None])
    synthetic = [
        _SyntheticState(
            "on" if (p.get("attributes") or {}).get("hvac_action") in ("heating", "cooling") else "off",
            _to_dt(p["last_updated"]),
        )
        for p in raw
    ]
    return _state_runs(synthetic)


def _entity_runs(ws, entity_id: str, days: int) -> list[tuple[datetime, datetime, str]]:
    if entity_id.startswith("climate."):
        return _climate_on_off_runs(ws, entity_id, days)
    return _state_runs(_history_for(ws, entity_id, days))


def _other_zone_entities(zones: list[dict], this_zone_id: str) -> list[str]:
    """Actuadores declarados en TODAS las demas zonas Climate — para poder
    descartar transiciones contaminadas por otro equipo compartiendo la
    misma maquina exterior. `zones`: config de TODAS las zonas de este
    plugin (ver config_store), pasada explicita por quien llama en vez de
    leerse de `hass.config_entries` (ya no hay config_entries fuera de
    HA Core) — el plugin de Climate mantiene su propia lista."""
    others: list[str] = []
    for zone in zones:
        if zone.get("id") == this_zone_id:
            continue
        others.extend(zone.get("heat_switches") or [])
        others.extend(zone.get("cool_switches") or [])
        others.extend(zone.get("climate_entities") or [])
        others.extend(zone.get("humidifier_entities") or [])
    return others


def _active_intervals(ws, entities: list[str], days: int) -> list[tuple[datetime, datetime]]:
    """Tramos (inicio, fin) en los que CUALQUIERA de estas entidades
    estuvo encendida/calentando/enfriando — la union de todas, para
    comprobar solapamiento rapido despues."""
    intervals = []
    for entity_id in entities:
        for run_start, run_end, state in _entity_runs(ws, entity_id, days):
            if state == "on":
                intervals.append((run_start, run_end))
    return intervals


def _cached_active_intervals(ws, entities: list[str], days: int) -> list[tuple[datetime, datetime]]:
    """Version cacheada de `_active_intervals` (ver ACTIVE_INTERVALS_CACHE_
    SECONDS arriba) — la clave es solo el conjunto de entidades (no start/
    end, que cambian en cada llamada por segundos): dentro de la ventana de
    cache, se asume que "hace cuanto que paso cada N dias" no varia lo
    suficiente como para justificar repetir el escaneo."""
    if not entities:
        return []
    key = tuple(sorted(entities))
    now_ts = time.time()
    cached = _active_intervals_cache.get(key)
    if cached is not None and (now_ts - cached[0]) < ACTIVE_INTERVALS_CACHE_SECONDS:
        return cached[1]
    result = _active_intervals(ws, entities, days)
    _active_intervals_cache[key] = (now_ts, result)
    return result


def _overlaps(intervals: list[tuple[datetime, datetime]], t0: datetime, t1: datetime) -> bool:
    return any(a < t1 and b > t0 for a, b in intervals)


def _avg_around(power_states: list, center_ts: datetime, before: bool, window_minutes: float) -> float | None:
    vals = []
    for s in power_states:
        try:
            v = float(s.state)
        except (TypeError, ValueError):
            continue
        delta_min = (center_ts - s.last_changed).total_seconds() / 60 if before \
            else (s.last_changed - center_ts).total_seconds() / 60
        if 0 <= delta_min <= window_minutes:
            vals.append(v)
    return statistics.mean(vals) if vals else None


_EMPTY_ENTRY = {"learned_power_w": None, "reliable": False, "samples_used": 0, "samples_discarded_other_zone": 0}


def _learn_one_entity(ws, entity_id: str, days: int, other_intervals: list, power_states: list) -> dict:
    model = dict(_EMPTY_ENTRY)
    runs = _entity_runs(ws, entity_id, days)
    if not runs:
        return model

    deltas: list[float] = []
    discarded_other_zone = 0
    for run_start, run_end, _state in runs:
        dur_min = (run_end - run_start).total_seconds() / 60
        if dur_min < MIN_RUN_MINUTES:
            continue

        window_start = run_start - timedelta(minutes=SAMPLE_WINDOW_MINUTES)
        window_end = run_start + timedelta(minutes=SAMPLE_WINDOW_MINUTES)
        if other_intervals and _overlaps(other_intervals, window_start, window_end):
            discarded_other_zone += 1
            continue

        before = _avg_around(power_states, run_start, before=True, window_minutes=SAMPLE_WINDOW_MINUTES)
        after = _avg_around(power_states, run_start, before=False, window_minutes=SAMPLE_WINDOW_MINUTES)
        if before is None or after is None:
            continue
        delta = abs(after - before)
        if delta >= MIN_DELTA_W:
            deltas.append(delta)

    model["samples_discarded_other_zone"] = discarded_other_zone
    if len(deltas) < MIN_VALID_SAMPLES:
        return model

    model["learned_power_w"] = round(statistics.median(deltas), 0)
    model["reliable"] = True
    model["samples_used"] = len(deltas)
    return model


def _compute_power_model_sync(ws, entities: list[str], zone_id: str, zones: list[dict],
                               home_power_sensor: str, days: int) -> dict:
    """Aprende, para CADA entidad de `entities` (las de esta zona sin
    sensor propio ni valor fijo declarado), su salto tipico de potencia —
    ver `_learn_one_entity`. Las demas zonas se consultan UNA vez y se
    reutilizan para todas las entidades de esta zona (mas barato que
    repetir la consulta por cada una)."""
    result: dict[str, dict] = {}
    if not entities or not home_power_sensor:
        return result

    other_entities = _other_zone_entities(zones, zone_id)
    other_intervals = _cached_active_intervals(ws, other_entities, days) if other_entities else []
    power_states = _history_for(ws, home_power_sensor, days)

    for entity_id in entities:
        result[entity_id] = _learn_one_entity(ws, entity_id, days, other_intervals, power_states)
    return result


POWER_MODEL_COMPUTE_TIMEOUT_SECONDS = 60  # limite duro de espera, mismo motivo que thermal_model.py

_power_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="power_model")


def get_power_model(
    ws, entities: list[str], zone_id: str, zones: list[dict], home_power_sensor: str, days: int,
    fallback: dict | None = None,
) -> dict:
    """Consulta el historico por WebSocket en su propio hilo. Devuelve
    {entity_id: {"learned_power_w", "reliable", "samples_used",
    "samples_discarded_other_zone"}} — solo para las entidades pedidas
    (las que de verdad necesitan aprenderse). `ws`: instancia de
    ha_websocket.HAWebSocketClient ya conectada. `zones`: config de TODAS
    las zonas de este plugin (para `_other_zone_entities`).

    Timeout de POWER_MODEL_COMPUTE_TIMEOUT_SECONDS: mismo motivo que en
    thermal_model.py — un calculo que tarda demasiado deja de bloquear a
    esta zona, aunque el hilo siga corriendo por detras hasta terminar
    solo.

    `fallback`: que devolver si falla o se agota el tiempo — quien llama
    puede pasar aqui el modelo actual, para no perder un aprendizaje ya
    fiable por un timeout puntual (mismo razonamiento en
    thermal_model.get_model)."""
    if not entities or not home_power_sensor:
        return {}
    future = _power_executor.submit(_compute_power_model_sync, ws, entities, zone_id, zones, home_power_sensor, days)
    try:
        return future.result(timeout=POWER_MODEL_COMPUTE_TIMEOUT_SECONDS)
    except FutureTimeoutError:
        _LOGGER.warning(
            "Aprendizaje de potencia de %s actuadores superó %ss, se sigue con lo ya aprendido",
            len(entities), POWER_MODEL_COMPUTE_TIMEOUT_SECONDS,
        )
    except Exception:
        _LOGGER.debug("No se pudo aprender la potencia de %s actuadores todavia", len(entities), exc_info=True)
    return fallback if fallback is not None else {}

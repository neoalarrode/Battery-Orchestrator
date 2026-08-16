"""
Aprende la inercia termica REAL de una zona a partir del propio historico
de Home Assistant (via el recorder): cuantos grados por hora sube/baja la
temperatura con el actuador encendido, y cuantos pierde/gana por hora con
el actuador apagado en funcion de la diferencia con el exterior.

Nada de machine learning ni de un solver: se buscan tramos continuos en el
historico donde un actuador estuvo en un mismo estado (encendido/apagado)
al menos `MIN_RUN_MINUTES`, se calcula la pendiente real de temperatura de
cada tramo, y se toma la MEDIANA de todos los tramos validos de TODOS los
actuadores de ese lado juntos (robusta frente a un tramo suelto, p.ej. una
ventana abierta, y frente a tener varios actuadores heterogeneos para el
mismo lado). El resultado es un numero que se puede explicar en una
frase: "de media, esta zona sube 0.9°C por hora calentando".

Funciona con los dos tipos de actuador declarados en una zona (ver
const.py — ya no hay "actuator_mode", una zona puede tener switches Y
climate.* delegados a la vez, cualquier combinacion):

  - Switches (`heat_switches`/`cool_switches`): se usa directamente su
    estado on/off del historico — se sabe con certeza cuando estuvo
    actuando, porque lo enciende/apaga esta misma integracion.
  - climate.* delegados (`climate_entities`): tambien se puede aprender,
    a partir del atributo `hvac_action` de SU PROPIO historico (heating/
    cooling frente a idle/off/otro) — la mayoria de integraciones de
    climate lo publican. Solo se usa la parte de un climate.* que de
    verdad soporta ese lado (se comprueba su `hvac_modes` en vivo). Si
    una entidad concreta nunca reporta `hvac_action`, sencillamente no se
    encuentran tramos validos para ella y no aporta nada — nunca se
    inventa una cifra.
"""

from __future__ import annotations

import logging
import statistics
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime, timedelta, timezone

from .const import (
    DEFAULT_COOLING_RATE_DEG_H,
    DEFAULT_HEATING_RATE_DEG_H,
    DEFAULT_IDLE_LOSS_COEFF,
)

# Todo lo que hable con HA (estados, historico, ordenes) pasa por la MISMA
# conexion WebSocket persistente (ver ha_websocket.py) -- nunca por REST
# aparte. `ws` (una instancia de ha_websocket.HAWebSocketClient) se pasa
# explicito a cada funcion que lo necesita, en vez de importar un global,
# para no acoplar este modulo a main.py.


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _start_iso(days: int) -> str:
    return (_utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_dt(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc)

_LOGGER = logging.getLogger(__name__)

MIN_RUN_MINUTES = 20
MIN_VALID_RUNS = 3

# LIMITE DURO, no negociable: cuantos puntos como MAXIMO se procesan de
# UNA entidad en UNA sola consulta de historico, pase lo que pase. Sin
# esto, un sensor muy parlanchin (reporta cada pocos segundos) sobre una
# ventana de varios dias puede traer decenas o cientos de miles de
# objetos State a la vez dentro de un unico hilo del executor — memoria
# real, de golpe, sea cual sea la potencia de la maquina (confirmado en
# produccion: el mismo patron de cuelgues intermitentes de HA Core
# persistia igual tras migrar de una RPi5 a un i7 de 8 nucleos). Los
# demas throttles de este modulo (recalculo espaciado, reparto por zona,
# cache compartida — ver climate.py) reducen CUANTO A MENUDO se hace este
# trabajo; este limite acota CUANTO PESA como maximo cada vez que se
# hace, sin excepcion, independientemente de cuantas zonas haya o cuanto
# reporte un sensor. Si se supera, se toma un submuestreo UNIFORME (no
# solo los puntos mas recientes, para no sesgar la estadistica hacia una
# parte concreta del dia) — los tramos que se calculan a partir de esto
# (ver `_state_runs`) siguen siendo estadisticamente validos con menos
# puntos, solo pierden algo de resolucion temporal en tramos muy cortos.
MAX_STATES_PER_ENTITY = 5000


def _cap_states(states: list) -> list:
    if len(states) <= MAX_STATES_PER_ENTITY:
        return states
    step = len(states) / MAX_STATES_PER_ENTITY
    return [states[int(i * step)] for i in range(MAX_STATES_PER_ENTITY)]


_Run = tuple[datetime, datetime, str]


class _SyntheticState:
    """Un estado on/off minimo (mismo shape que necesita `_state_runs`),
    para poder tratar un climate.* delegado exactamente igual que un
    switch propio una vez traducido su `hvac_action` — ver
    `_climate_actuator_states`."""

    __slots__ = ("state", "last_changed")

    def __init__(self, state: str, last_changed: datetime) -> None:
        self.state = state
        self.last_changed = last_changed


def _state_runs(states: list) -> list[_Run]:
    """Tramos continuos (inicio, fin, estado) de una entidad on/off, a
    partir de la lista de estados que devuelve el recorder (ordenada por
    tiempo, una entrada por cambio de estado real)."""
    runs = []
    for i, s in enumerate(states):
        if s.state not in ("on", "off"):
            continue
        start = s.last_changed
        end = states[i + 1].last_changed if i + 1 < len(states) else _utcnow()
        runs.append((start, end, s.state))
    return runs


def _value_at_or_before(states: list, ts: datetime) -> float | None:
    best = None
    for s in states:
        try:
            val = float(s.state)
        except (ValueError, TypeError):
            continue
        if s.last_changed <= ts:
            best = val
        else:
            break
    return best


def _learn_rate(temp_states: list, runs: list[_Run]) -> tuple[float | None, int]:
    slopes = []
    for start, end, state in runs:
        if state != "on":
            continue
        duration_h = (end - start).total_seconds() / 3600
        if duration_h * 60 < MIN_RUN_MINUTES:
            continue
        t0 = _value_at_or_before(temp_states, start)
        t1 = _value_at_or_before(temp_states, end)
        if t0 is None or t1 is None:
            continue
        slope = (t1 - t0) / duration_h
        if 0.05 <= abs(slope) <= 5.0:
            slopes.append(abs(slope))
    if len(slopes) < MIN_VALID_RUNS:
        return None, len(slopes)
    return statistics.median(slopes), len(slopes)


def _learn_idle_loss_coeff(temp_states: list, runs: list[_Run], outdoor_states: list) -> tuple[float | None, int]:
    coeffs = []
    for start, end, state in runs:
        if state != "off":
            continue
        duration_h = (end - start).total_seconds() / 3600
        if duration_h * 60 < MIN_RUN_MINUTES:
            continue
        t0, t1 = _value_at_or_before(temp_states, start), _value_at_or_before(temp_states, end)
        out0, out1 = _value_at_or_before(outdoor_states, start), _value_at_or_before(outdoor_states, end)
        if None in (t0, t1, out0, out1):
            continue
        avg_delta = ((out0 - t0) + (out1 - t1)) / 2
        if abs(avg_delta) < 1.0:
            continue
        slope = (t1 - t0) / duration_h
        coeff = slope / avg_delta
        if 0.0 <= coeff <= 0.6:
            coeffs.append(coeff)
    if len(coeffs) < MIN_VALID_RUNS:
        return None, len(coeffs)
    return statistics.median(coeffs), len(coeffs)


def _history_for(ws, entity_id: str, days: int, bridges=None) -> list:
    """Historico de una entidad simple (switch, sensor): una entrada por
    cambio de SU `state`, sin atributos (mas barato). Acotado con
    `_cap_states` (ver MAX_STATES_PER_ENTITY arriba) — limite duro, no
    solo para sensores parlanchines conocidos.

    Si `entity_id` es de OTRO plugin (Tuya u otra marca, ver
    ClimatePlugin.is_bridge_ref/get_history) el historico se pide a ese
    plugin en vez de a HA -- un actuador consumido internamente nunca pasa
    por el recorder de HA, asi que no tiene historico ahi que consultar."""
    if bridges is not None and bridges.is_bridge_ref(entity_id):
        try:
            raw = bridges.get_history(entity_id, days)
        except Exception:
            _LOGGER.debug("Sin historico propio de %s todavia", entity_id, exc_info=True)
            raw = []
    else:
        try:
            raw = ws.get_history(entity_id, _start_iso(days), with_attributes=False)
        except Exception:
            _LOGGER.debug("Sin historico (via WebSocket) de %s todavia", entity_id, exc_info=True)
            raw = []
    points = [_SyntheticState(p["state"], _to_dt(p["last_updated"])) for p in raw if p.get("last_updated") is not None]
    return _cap_states(points)


def _climate_actuator_states(ws, entity_id: str, wanted_action: str, days: int, bridges=None) -> list:
    """Traduce el historico de un climate.* delegado a la misma forma
    on/off que un switch, usando su atributo `hvac_action` (heating/
    cooling/idle/off/fan/drying): "on" mientras coincide con
    `wanted_action` ("heating" o "cooling"), "off" el resto del tiempo.
    Hace falta el historico CON atributos (mas caro que `_history_for`,
    por eso es una funcion aparte) porque `hvac_action` es un atributo,
    no el `state` de la entidad (que es el hvac_mode: heat/cool/off/...).
    Acotado con `_cap_states` ANTES de traducir — con atributos completos
    por punto, esta es la consulta mas cara de las dos, el limite duro
    importa mas aqui todavia.

    Un delegado de OTRO plugin (Tuya u otra marca) no tiene `hvac_action`
    que traducir -- su propio historico ya viene en la misma forma on/off
    (ver TuyaDeviceManager.get_actuator_history, que usa el switch_dp del
    dispositivo como señal de "actuando"), asi que se devuelve tal cual."""
    if bridges is not None and bridges.is_bridge_ref(entity_id):
        return _history_for(ws, entity_id, days, bridges=bridges)
    try:
        raw = ws.get_history(entity_id, _start_iso(days), with_attributes=True)
    except Exception:
        _LOGGER.debug("Sin historico (via WebSocket) de %s todavia", entity_id, exc_info=True)
        raw = []
    raw = _cap_states([p for p in raw if p.get("last_updated") is not None])
    synthetic = []
    for p in raw:
        action = (p.get("attributes") or {}).get("hvac_action")
        synthetic.append(_SyntheticState("on" if action == wanted_action else "off", _to_dt(p["last_updated"])))
    return synthetic


def _runs_for_side(ws, zone: dict, side: str, wanted_action: str, days: int, bridges=None) -> list[_Run]:
    """Tramos on/off combinados de TODOS los actuadores de un lado
    ("heat" o "cool"): sus switches dedicados, mas cualquier climate.*
    delegado que soporte ese modo de verdad (comprobado en vivo contra
    sus `hvac_modes` — nunca una declaracion nuestra, ver const.py).
    Concatenar tramos de fuentes distintas es seguro: `_learn_rate`/
    `_learn_idle_loss_coeff` no asumen ningun orden cronologico global.

    Un delegado de otro plugin (Tuya u otra marca) no tiene un
    `hvac_modes` que consultar por WebSocket -- se asume heat+cool
    disponibles (mismo par minimo que ya usa ZoneRunner._get_state para
    controlarlo, ver zone_runner.py)."""
    runs: list[_Run] = []
    for sw in zone.get(f"{side}_switches") or []:
        runs.extend(_state_runs(_history_for(ws, sw, days, bridges=bridges)))
    for entity_id in zone.get("climate_entities") or []:
        if bridges is not None and bridges.is_bridge_ref(entity_id):
            supported = ["heat", "cool"]
        else:
            try:
                state = ws.get_state(entity_id)
            except Exception:
                state = None
            supported = ((state or {}).get("attributes") or {}).get("hvac_modes") or []
        if side in supported:
            runs.extend(_state_runs(_climate_actuator_states(ws, entity_id, wanted_action, days, bridges=bridges)))
    return runs


def _compute_model_sync(ws, zone: dict, days: int, bridges=None) -> dict:
    model = {
        "heating_rate_deg_h": DEFAULT_HEATING_RATE_DEG_H,
        "cooling_rate_deg_h": DEFAULT_COOLING_RATE_DEG_H,
        "idle_loss_coeff": DEFAULT_IDLE_LOSS_COEFF,
        "reliable": False,
        "runs_used": 0,
    }

    if not zone.get("current_temp_sensor"):
        return model

    temp_states = _history_for(ws, zone["current_temp_sensor"], days, bridges=bridges)
    if not temp_states:
        return model

    runs_used = 0

    heat_runs = _runs_for_side(ws, zone, "heat", "heating", days, bridges=bridges)
    if heat_runs:
        rate, n = _learn_rate(temp_states, heat_runs)
        if rate is not None:
            model["heating_rate_deg_h"] = rate
            runs_used += n

    cool_runs = _runs_for_side(ws, zone, "cool", "cooling", days, bridges=bridges)
    if cool_runs:
        rate, n = _learn_rate(temp_states, cool_runs)
        if rate is not None:
            model["cooling_rate_deg_h"] = rate
            runs_used += n

    outdoor_sensor = zone.get("outdoor_temp_sensor")
    # Tramos "apagado" de AMBOS lados juntos (calor y frio) — la perdida
    # pasiva de la zona no depende de cual de los dos la calento/enfrio
    # por ultima vez, cualquier tramo con el actuador correspondiente
    # apagado sirve igual. Iba con "or" (heat_runs or cool_runs): al ser
    # listas, eso escogia SOLO heat_runs en cuanto tuviera aunque fuera un
    # tramo (aunque cool_runs tuviera muchos mas y mejores), descartando
    # de raiz la mitad de las muestras disponibles sin motivo -- menos
    # datos para `idle_loss_coeff`, que alimenta directamente la
    # anticipacion (`scheduler._anticipate`): una estimacion peor ahi
    # dispara la anticipacion demasiado pronto o demasiado tarde, ninguna
    # de las dos eficiente.
    idle_runs = heat_runs + cool_runs
    if outdoor_sensor and idle_runs:
        outdoor_states = _history_for(ws, outdoor_sensor, days, bridges=bridges)
        coeff, n = _learn_idle_loss_coeff(temp_states, idle_runs, outdoor_states)
        if coeff is not None:
            model["idle_loss_coeff"] = coeff
            runs_used += n

    model["runs_used"] = runs_used
    model["reliable"] = runs_used >= MIN_VALID_RUNS
    return model


MODEL_COMPUTE_TIMEOUT_SECONDS = 60  # limite duro de espera — ver mas abajo


_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="thermal_model")


def get_model(ws, zone: dict, days: int, fallback: dict | None = None, bridges=None) -> dict:
    """Consulta el historico por WebSocket en su propio hilo (nunca
    bloquea el hilo que llama mas de MODEL_COMPUTE_TIMEOUT_SECONDS — una
    consulta de varios dias puede tardar). `ws`: instancia de
    ha_websocket.HAWebSocketClient ya conectada.

    Si por lo que sea (HA bajo carga, red lenta, lo que sea) esto tarda
    mas de lo razonable, esta zona deja de ESPERAR en vez de quedarse
    colgada indefinidamente. El hilo sigue corriendo por detras hasta que
    termine solo (Python no puede matar un hilo a la fuerza), pero ya no
    bloquea a nadie que dependa de este resultado — protege la RESPUESTA,
    no el trabajo de fondo en si (para eso esta MAX_STATES_PER_ENTITY, que
    acota cuanto hay que procesar de entrada).

    `fallback`: que devolver si falla o se agota el tiempo — por defecto
    (sin declarar) los valores de fabrica sin fiabilidad, pero quien llama
    puede pasar aqui el modelo actual: un timeout puntual no debe tirar a
    la basura un modelo que YA era fiable de una vez anterior, solo
    significa "esta vez no ha dado tiempo a refrescarlo", no "hay que
    olvidar lo aprendido hasta ahora"."""
    default_fallback = {
        "heating_rate_deg_h": DEFAULT_HEATING_RATE_DEG_H,
        "cooling_rate_deg_h": DEFAULT_COOLING_RATE_DEG_H,
        "idle_loss_coeff": DEFAULT_IDLE_LOSS_COEFF,
        "reliable": False,
        "runs_used": 0,
    }
    fallback = fallback if fallback is not None else default_fallback
    future = _executor.submit(_compute_model_sync, ws, zone, days, bridges)
    try:
        return future.result(timeout=MODEL_COMPUTE_TIMEOUT_SECONDS)
    except FutureTimeoutError:
        _LOGGER.warning(
            "Calculo de inercia termica de %s superó %ss, se sigue con el ultimo modelo conocido",
            zone.get("name"), MODEL_COMPUTE_TIMEOUT_SECONDS,
        )
    except Exception:
        _LOGGER.debug("No se pudo calcular la inercia termica de %s todavia", zone.get("name"), exc_info=True)
    return fallback

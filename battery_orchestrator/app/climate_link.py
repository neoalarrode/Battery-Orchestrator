"""
Enlace automatico con Climate Orchestrator (si esta instalado) — sin
configuracion manual en ningun lado, por convencion sobre una entidad
normal de Home Assistant:

  - Cada zona de Climate Orchestrator es una entidad climate.* que YA
    expone, entre sus atributos, "zone_power_w" (la potencia que esa
    zona esta consumiendo ahora mismo) — no hace falta que Climate
    Orchestrator publique nada nuevo aparte, solo que marque esa misma
    entidad con "climate_orchestrator_zone: true" para poder encontrarla.
    Aqui se DESCUBRE el conjunto de zonas preguntando /api/states una vez
    y filtrando por ese atributo — nunca hace falta que el usuario
    declare "que zonas tiene" en ningun sitio.
  - Sin Climate Orchestrator instalado (o sin ninguna zona marcada
    todavia), la busqueda simplemente no encuentra nada: se cae al mismo
    comportamiento que sin este modulo en absoluto, nunca un error.

El descubrimiento (la LISTA de zonas) se cachea unos minutos - las zonas
no aparecen/desaparecen tan rapido como para justificar re-preguntar
/api/states ENTERO en cada ciclo de 30-60s. Esto es imprescindible, no
solo una optimizacion: /api/states devuelve TODAS las entidades de la
instalacion (miles, en una casa con muchos dispositivos) serializadas
entera en cada respuesta - pedirla en cada ciclo machaca a HA Core con
una carga real e innecesaria cada 30-60s sin parar, y puede llegar a
notarse como lentitud/perdida de red intermitente en el propio HA (visto
en produccion). La LECTURA de "zone_power_w" de esas zonas SI se pide
fresca en cada ciclo (es la potencia AHORA MISMO, no una media
historica), pero UNA A UNA (/api/states/<entity_id>, barata: solo esa
entidad), nunca repitiendo el volcado completo.
"""

from __future__ import annotations

import time

import ha_client

DISCOVERY_CACHE_SECONDS = 300  # 5 min
ZONE_MARKER_ATTR = "climate_orchestrator_zone"


ACTIVE_HVAC_ACTIONS = {"heating", "cooling"}


def _entry_from_state(state: dict) -> dict:
    """
    OJO con "zone_power_w" ausente/null: Climate Orchestrator lo deja asi
    en DOS casos muy distintos - la zona esta parada (0W, de verdad) O la
    zona esta calentando/enfriando de verdad pero ninguno de sus
    actuadores tiene sensor, potencia aprendida ni estimada declarada
    (consumo real DESCONOCIDO, no cero). Tratar los dos como 0W (lo que
    hacia esto antes) esconde justo el caso que mas importa: una zona
    tirando de verdad de la red sin que el detector de anomalias de
    Battery Orchestrator se entere. Se distinguen con "hvac_action"
    (atributo estandar de cualquier climate.*, no hace falta que Climate
    Orchestrator publique nada mas): activa+sin dato -> "unknown": True,
    nunca se sustituye por un 0 inventado.
    """
    attrs = state.get("attributes", {})
    power = attrs.get("zone_power_w")
    try:
        power = float(power) if power is not None else None
    except (TypeError, ValueError):
        power = None

    active = attrs.get("hvac_action") in ACTIVE_HVAC_ACTIONS
    unknown = active and power is None

    return {
        "entity_id": state["entity_id"],
        "name": attrs.get("friendly_name") or state["entity_id"],
        "power_w": 0.0 if power is None else power,
        "unknown": unknown,
    }


_discovery_cache: tuple[float, list[str]] | None = None  # (timestamp, [entity_id, ...])


def _discover_zone_ids() -> list[str]:
    """
    SOLO esta funcion pide /api/states entero (el volcado caro de toda la
    instalacion) - y solo cuando la cache ha caducado (cada
    DISCOVERY_CACHE_SECONDS, 5 min), nunca en cada ciclo.
    """
    global _discovery_cache
    now_ts = time.time()
    if _discovery_cache is not None and (now_ts - _discovery_cache[0]) < DISCOVERY_CACHE_SECONDS:
        return _discovery_cache[1]
    all_states = ha_client.get_all_states()
    ids = [s["entity_id"] for s in all_states if s.get("attributes", {}).get(ZONE_MARKER_ATTR)]
    _discovery_cache = (now_ts, ids)
    return ids


def read_live_power_w() -> dict:
    """
    Descubre las zonas de Climate Orchestrator (cacheado, ver
    `_discover_zone_ids`) y lee su "zone_power_w" AHORA MISMO (siempre
    fresco, nunca cacheado) - pero pidiendo cada zona SUELTA
    (/api/states/<entity_id>), nunca el volcado completo de la instalacion
    otra vez.

    Devuelve {"total_w", "zones": [{"name","power_w","unknown"}, ...],
    "unknown_zone_names": [...]}. "total_w" SOLO suma zonas con dato
    real (nunca se inventa un numero para una zona activa sin sensor
    declarado - ver `_entry_from_state`); las zonas "unknown" se listan
    aparte para poder avisar de que su consumo no se esta teniendo en
    cuenta, en vez de fingir que es cero. "zones": [] y "total_w": 0.0 si
    no hay ninguna zona detectada (Climate Orchestrator no instalado, o
    sin ninguna marcada todavia) - nunca None, quien lo usa lo suma
    directo sin comprobar nada.
    """
    zone_ids = _discover_zone_ids()
    if not zone_ids:
        return {"total_w": 0.0, "zones": [], "unknown_zone_names": []}

    detail = []
    unknown_names = []
    total = 0.0
    for entity_id in zone_ids:
        try:
            state = ha_client.get_state(entity_id)
        except Exception:
            continue
        entry = _entry_from_state(state)
        detail.append({"name": entry["name"], "power_w": entry["power_w"], "unknown": entry["unknown"]})
        if entry["unknown"]:
            unknown_names.append(entry["name"])
        else:
            total += entry["power_w"]
    return {"total_w": total, "zones": detail, "unknown_zone_names": unknown_names}

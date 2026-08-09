"""
Enlace automatico con Climate Orchestrator (si esta instalado) — sin
configuracion manual en ningun lado, por convencion sobre una entidad
normal de Home Assistant:

  - Cada zona de Climate Orchestrator es una entidad climate.* que YA
    expone, entre sus atributos, "zone_power_w" (la potencia que esa
    zona esta consumiendo ahora mismo) — no hace falta que Climate
    Orchestrator publique nada nuevo aparte. Aqui se DESCUBRE el
    conjunto de zonas pidiendole a HA, EXPRESAMENTE, "que entidades
    pertenecen a la integracion climate_orchestrator" (funcion nativa de
    plantillas `integration_entities()`, ver `_DISCOVERY_TEMPLATE` mas
    abajo) — no un rastreo por dominio "climate" entero (que traeria
    tambien cualquier otro termostato instalado, de cualquier otra
    integracion) ni por un atributo que haya que confiar en que nadie
    mas reutilice por casualidad: va derecho al registro de entidades de
    HA, la fuente de verdad de "esto es de Climate Orchestrator o no lo
    es". Nunca hace falta que el usuario declare "que zonas tiene" en
    ningun sitio.
  - Sin Climate Orchestrator instalado (o sin ninguna zona todavia), la
    busqueda simplemente no encuentra nada: se cae al mismo
    comportamiento que sin este modulo en absoluto, nunca un error.

El descubrimiento (la LISTA de zonas) se cachea unos minutos - las zonas
no aparecen/desaparecen tan rapido como para justificar re-preguntar por
ellas en cada ciclo de 30-60s. Y aun cacheado cada 5 min, NUNCA se pide
/api/states ENTERO (el volcado de TODAS las entidades de la instalacion,
miles en una casa con muchos dispositivos): se usa la API de plantillas
de HA (POST /api/template, ver `ha_client.render_template`) para que sea
el propio HA Core quien resuelva la pertenencia a la integracion ANTES
de contestar - la respuesta es solo los pocos entity_id que hacen falta,
nunca miles de entidades serializadas para descartarlas aqui. Esto es
imprescindible, no solo una optimizacion: incluso una vez cada 5 min, un
volcado completo repetido sin parar machaca a HA Core con una carga real
e innecesaria, y puede llegar a notarse como lentitud/perdida de red
intermitente en el propio HA (visto en produccion). La LECTURA de
"zone_power_w" de esas zonas SI se pide fresca en cada ciclo (es la
potencia AHORA MISMO, no una media historica), pero UNA A UNA
(/api/states/<entity_id>, barata: solo esa entidad), nunca repitiendo el
volcado completo ni la plantilla (esa solo hace falta para la LISTA, que
ya esta cacheada). Si la plantilla fallase por lo que sea, la red de
seguridad (`get_all_states` + el atributo "climate_orchestrator_zone")
SI sigue usando ese atributo — de ahi que Climate Orchestrator lo siga
marcando, aunque el camino normal ya no dependa de el.
"""

from __future__ import annotations

import json
import time

import ha_client

DISCOVERY_CACHE_SECONDS = 300  # 5 min
ZONE_MARKER_ATTR = "climate_orchestrator_zone"  # se sigue usando como comprobacion extra, ver mas abajo
CLIMATE_ORCHESTRATOR_DOMAIN = "climate_orchestrator"

# Jinja2, renderizada POR HA (no aqui): `integration_entities()` es una
# funcion NATIVA de las plantillas de HA que consulta directamente el
# registro de entidades y devuelve solo las que pertenecen EXPRESAMENTE
# a la integracion "climate_orchestrator" - a diferencia de filtrar por
# el dominio "climate" entero (que traeria de vuelta CUALQUIER
# termostato instalado, de cualquier integracion), esto va derecho a
# "solo lo que Climate Orchestrator ha creado", sin heuristicas.
# Se filtra ademas a las que empiezan por "climate." porque esa misma
# integracion tambien registra alguna entidad number.* (ver number.py de
# ese proyecto) que no es una zona y no debe entrar en el descubrimiento.
_DISCOVERY_TEMPLATE = (
    f"{{{{ integration_entities('{CLIMATE_ORCHESTRATOR_DOMAIN}') "
    "| select('match', '^climate\\\\.') | list | tojson }}"
)


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
    Pide la LISTA de zonas via la API de plantillas (filtrada por HA,
    ver `_DISCOVERY_TEMPLATE` arriba) - y solo cuando la cache ha
    caducado (cada DISCOVERY_CACHE_SECONDS, 5 min), nunca en cada ciclo.

    Si la plantilla falla por lo que sea (HA muy antiguo, permiso
    denegado, respuesta rara) cae al volcado completo de /api/states como
    red de seguridad - MUCHO mas caro, pero solo ocurre en ese caso raro,
    nunca en el camino normal, y sigue sin fallar el descubrimiento.
    """
    global _discovery_cache
    now_ts = time.time()
    if _discovery_cache is not None and (now_ts - _discovery_cache[0]) < DISCOVERY_CACHE_SECONDS:
        return _discovery_cache[1]

    ids: list[str] | None = None
    rendered = ha_client.render_template(_DISCOVERY_TEMPLATE)
    if rendered is not None:
        try:
            parsed = json.loads(rendered)
            if isinstance(parsed, list):
                ids = [e for e in parsed if isinstance(e, str)]
        except (json.JSONDecodeError, TypeError):
            ids = None

    if ids is None:
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

"""
Enlace con Climate Orchestrator (si esta instalado) — sin sondeo
automatico: el usuario pulsa "Buscar zonas" en la configuracion (ver
`/api/climate/discover` en main.py) y esa lista de entity_id se guarda
en config.json (`climate_orchestrator_zones`); a partir de ahi la app
NUNCA vuelve a preguntar "que zonas hay" por su cuenta, ni por sondeo
periodico ni cacheado — solo cuando el usuario lo pide expresamente. Esto
es a proposito, no una limitacion: incluso un descubrimiento cacheado
cada 5 min es una carga real y evitable sobre HA Core (se vio en
produccion, RPi5), y las zonas de una instalacion no cambian con
frecuencia como para justificar preguntarlo solo.

  - Cada zona de Climate Orchestrator es una entidad climate.* que YA
    expone, entre sus atributos, "zone_power_w" (la potencia que esa
    zona esta consumiendo ahora mismo) — no hace falta que Climate
    Orchestrator publique nada nuevo aparte.
  - El DESCUBRIMIENTO (`discover_zone_ids`, solo se llama al pulsar el
    boton) le pide a HA, EXPRESAMENTE, "que entidades pertenecen a la
    integracion climate_orchestrator" (funcion nativa de plantillas
    `integration_entities()`, ver `_DISCOVERY_TEMPLATE` mas abajo) — no
    un rastreo por dominio "climate" entero (que traeria tambien
    cualquier otro termostato instalado, de cualquier otra integracion):
    va derecho al registro de entidades de HA, la fuente de verdad de
    "esto es de Climate Orchestrator o no lo es".
  - La LECTURA de "zone_power_w" (`read_live_power_w`, SI se llama en
    cada ciclo) nunca vuelve a descubrir nada: recibe la lista YA
    guardada como parametro y solo pide, UNA A UNA, el estado fresco de
    cada entidad ya conocida (/api/states/<entity_id>, barata: solo esa
    entidad) — nunca un volcado ni una plantilla en el camino normal.
  - Sin Climate Orchestrator instalado (o sin haber pulsado nunca el
    boton), la lista guardada esta vacia: se cae al mismo comportamiento
    que sin este modulo en absoluto, nunca un error.
"""

from __future__ import annotations

import json

import ha_client

ZONE_MARKER_ATTR = "climate_orchestrator_zone"  # red de seguridad del descubrimiento manual, ver discover_zone_ids
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


def discover_zone_ids() -> list[str]:
    """
    Pide la LISTA de zonas via la API de plantillas (filtrada por HA, ver
    `_DISCOVERY_TEMPLATE` arriba) — de UN SOLO USO, llamada SOLO desde
    `/api/climate/discover` cuando el usuario pulsa el boton en la
    configuracion. Nunca se llama sola, ni con temporizador ni con cache:
    quien quiera una lista fresca tiene que pedirla expresamente.

    Si la plantilla falla por lo que sea (HA muy antiguo, permiso
    denegado, respuesta rara) cae al volcado completo de /api/states como
    red de seguridad — MUCHO mas caro, pero se paga solo esta vez (una
    pulsacion de boton), nunca en el camino normal de cada ciclo.
    """
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

    return ids


def read_live_power_w(zone_ids: list[str]) -> dict:
    """
    Lee "zone_power_w" AHORA MISMO (siempre fresco, nunca cacheado) de
    las zonas YA CONOCIDAS (`zone_ids`, la lista guardada en config.json
    tras pulsar "Buscar zonas" — ver `discover_zone_ids` y
    `/api/climate/discover`) — pidiendo cada zona SUELTA
    (/api/states/<entity_id>, barata: solo esa entidad). NUNCA descubre
    nada por su cuenta: si `zone_ids` esta vacia (Climate Orchestrator no
    instalado, o el usuario todavia no ha pulsado el boton), esto no pide
    nada a HA en absoluto.

    Devuelve {"total_w", "zones": [{"name","power_w","unknown"}, ...],
    "unknown_zone_names": [...]}. "total_w" SOLO suma zonas con dato
    real (nunca se inventa un numero para una zona activa sin sensor
    declarado - ver `_entry_from_state`); las zonas "unknown" se listan
    aparte para poder avisar de que su consumo no se esta teniendo en
    cuenta, en vez de fingir que es cero. "zones": [] y "total_w": 0.0 si
    `zone_ids` esta vacia — nunca None, quien lo usa lo suma directo sin
    comprobar nada.
    """
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

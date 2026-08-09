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
/api/states entero en cada ciclo de 30-60s. La LECTURA de "zone_power_w"
de esas zonas, en cambio, nunca se cachea: se pide fresca en cada ciclo
(es la potencia AHORA MISMO, no una media historica) - y ademas viene
GRATIS en la misma respuesta de /api/states del descubrimiento cuando
este no esta en cache, asi que no cuesta ninguna peticion de mas.
"""

from __future__ import annotations

import time

import ha_client

DISCOVERY_CACHE_SECONDS = 300  # 5 min
ZONE_MARKER_ATTR = "climate_orchestrator_zone"


def _entry_from_state(state: dict) -> dict:
    attrs = state.get("attributes", {})
    power = attrs.get("zone_power_w")
    try:
        power = float(power) if power is not None else 0.0
    except (TypeError, ValueError):
        power = 0.0
    return {
        "entity_id": state["entity_id"],
        "name": attrs.get("friendly_name") or state["entity_id"],
        "power_w": power,
    }


_discovery_cache: tuple[float, list[str]] | None = None  # (timestamp, [entity_id, ...])


def _discover_zone_ids(all_states: list[dict]) -> list[str]:
    global _discovery_cache
    now_ts = time.time()
    if _discovery_cache is not None and (now_ts - _discovery_cache[0]) < DISCOVERY_CACHE_SECONDS:
        return _discovery_cache[1]
    ids = [s["entity_id"] for s in all_states if s.get("attributes", {}).get(ZONE_MARKER_ATTR)]
    _discovery_cache = (now_ts, ids)
    return ids


def read_live_power_w() -> dict:
    """
    Descubre las zonas de Climate Orchestrator (cacheado) y lee su
    "zone_power_w" AHORA MISMO (siempre fresco, nunca cacheado).

    Devuelve {"total_w": float, "zones": [{"name", "power_w"}, ...]} — con
    "zones": [] si no hay ninguna zona detectada (Climate Orchestrator no
    instalado, o sin ninguna marcada todavia); "total_w" siempre 0.0 en
    ese caso, nunca None (quien lo usa lo suma directo sin comprobar nada).
    """
    all_states = ha_client.get_all_states()
    zone_ids = _discover_zone_ids(all_states)
    if not zone_ids:
        return {"total_w": 0.0, "zones": []}

    # Si el descubrimiento vino de cache (no de esta misma llamada a
    # /api/states), los ids conocidos pueden no estar en `all_states` -
    # se piden sueltos solo esos, nunca los 300+ estados de una casa entera
    # de nuevo si ya los tenemos.
    by_id = {s["entity_id"]: s for s in all_states}
    detail = []
    total = 0.0
    for entity_id in zone_ids:
        state = by_id.get(entity_id)
        if state is None:
            try:
                state = ha_client.get_state(entity_id)
            except Exception:
                continue
        entry = _entry_from_state(state)
        detail.append({"name": entry["name"], "power_w": entry["power_w"]})
        total += entry["power_w"]
    return {"total_w": total, "zones": detail}

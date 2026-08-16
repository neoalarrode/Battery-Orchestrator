"""
Patron historico de ocupacion de una zona, por hora del dia -- estadistica
simple y verificable a mano (media de los ultimos `HISTORY_DAYS` dias),
NUNCA aprendizaje automatico: cada punto es "en que % de los dias, a esta
hora en punto, alguno de los `presence_entities` de la zona estuvo en
'on'/'home' segun el recorder de HA" -- el usuario puede reproducir el
mismo numero el mismo mirando el historico.

Usado en DOS sitios, a proposito con el mismo dato (nunca dos fuentes que
puedan discrepar):
  - zone_forecast.py: pinta la sombra de ocupacion del grafico de 24h Y
    decide que preset proyectar en cada hora futura.
  - scheduler.py (via zone_runner.py:decide_and_act): anticipa la
    consigna de confort ANTES de que la zona se ocupe de verdad, si el
    patron dice que esta hora suele estar vacia pero la siguiente no (ver
    `_occupancy_anticipate` en scheduler.py) -- el equivalente, para
    ocupacion, de lo que `_anticipate` ya hace para la previsión
    meteorologica.

Sin sensores de presencia declarados, o sin muestras suficientes en una
hora concreta (zona/sensor recien añadido), esa hora devuelve None -- se
prefiere no anticipar nada antes que inventar un patron que no esta.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .thermal_model import _history_for

HISTORY_DAYS = 14
MIN_SAMPLES_PER_HOUR = 3
LIKELY_THRESHOLD_PCT = 50.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _bool_state(raw) -> bool | None:
    if raw in ("on", "home"):
        return True
    if raw in ("off", "not_home"):
        return False
    return None


def _bool_at_or_before(states: list, ts: datetime) -> bool | None:
    best = None
    for s in states:
        b = _bool_state(s.state)
        if b is None:
            continue
        if s.last_changed <= ts:
            best = b
        else:
            break
    return best


def hourly_occupancy_pct(ws, presence_entities: list[str], bridges) -> dict[int, float | None]:
    """{0..23: pct 0..100 | None}."""
    if not presence_entities:
        return {h: None for h in range(24)}

    per_entity_states = [_history_for(ws, e, HISTORY_DAYS, bridges=bridges) for e in presence_entities]
    now = _utcnow()
    buckets: dict[int, list[bool]] = {h: [] for h in range(24)}
    cursor = (now - timedelta(days=HISTORY_DAYS)).replace(minute=0, second=0, microsecond=0)
    while cursor <= now:
        vals = [_bool_at_or_before(states, cursor) for states in per_entity_states]
        known = [v for v in vals if v is not None]
        if known:
            buckets[cursor.astimezone().hour].append(any(known))
        cursor += timedelta(hours=1)

    out: dict[int, float | None] = {}
    for h, vals in buckets.items():
        out[h] = round(100 * sum(vals) / len(vals)) if len(vals) >= MIN_SAMPLES_PER_HOUR else None
    return out


def likely(pct: float | None) -> bool | None:
    return None if pct is None else pct >= LIKELY_THRESHOLD_PCT


def forecast_likely(occupancy_by_hour: dict[int, float | None], start_hour: int, hours: int) -> list[bool | None]:
    """[proxima hora, +2h, ...] -- lecturas directas del diccionario ya
    calculado (barato: sin ninguna consulta a HA), para usar en cada
    decision reactiva sin recalcular el historico en el hot path."""
    return [likely(occupancy_by_hour.get((start_hour + i) % 24)) for i in range(1, hours + 1)]

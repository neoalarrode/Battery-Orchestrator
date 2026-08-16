"""
Curva de brillo/color atada a la POSICION REAL DEL SOL -- nunca a una
hora fija tecleada por el usuario (un horario fijo se desincroniza solo
con las estaciones: amanece y anochece antes en diciembre que en junio).

Puerto directo del calculo de "Adaptive Lighting" (integracion de
referencia de HA, github.com/basnijholt/adaptive-lighting,
`color_and_brightness.py`: `SunEvents.sun_position`/`SunLightSettings.
brightness_pct`/`color_temp_kelvin`) -- MISMA formula, pero leyendo los
4 eventos del dia (amanecer/atardecer/mediodia solar/medianoche solar)
directamente de los atributos `next_rising`/`next_setting`/`next_noon`/
`next_midnight` que la propia entidad nucleo `sun.sun` de HA YA calcula
(verificado contra la instancia real: existen los 4, ver CHANGELOG) en
vez de depender de la libreria `astral` que usa el original -- evita
añadir una dependencia nueva a la imagen del addon por una zona que
puede que nadie instale. El "evento anterior" de cada uno (`sun.sun` solo
da el PROXIMO) se aproxima restando 24h al proximo -- el desfase dia a
dia de una salida/puesta de sol real es de segundos a un par de minutos,
irrelevante para una curva de iluminacion.

`sun_position` queda en [-1, 1]: negativo de noche (mas negativo cuanto
mas lejos del amanecer/atardecer, hasta -1 en la medianoche solar),
positivo de dia (hasta +1 en el mediodia solar) -- una curva de coseno a
trozos, no un tramo recto, para que la transicion se sienta suave al
entrar/salir del crepusculo en vez de un cambio de pendiente brusco.

Brillo: CORREGIDO a peticion expresa del usuario -- el modo "default" del
original (a pleno dia se queda fijo en el maximo, toda la variacion pasa
por la noche) no es lo que se queria aqui. En su lugar, el brillo sube
desde el amanecer hasta el maximo en el mediodia solar y vuelve a bajar
hasta el atardecer -- la MISMA forma que ya usaba el color (ver
`_color_temp_kelvin` mas abajo, formula identica con brillo en vez de
temperatura de color), aplicada tambien al brillo. De noche se queda fijo
en el minimo (no tiene sentido que "suba" de madrugada sin que nadie lo
vea) -- solo el tramo de dia (`sun_position > 0`) sigue la curva.
"""

from __future__ import annotations

from datetime import datetime, timezone

DEFAULT_MIN_BRIGHTNESS_PCT = 25
DEFAULT_MAX_BRIGHTNESS_PCT = 100
DEFAULT_MIN_COLOR_TEMP_KELVIN = 2200
DEFAULT_MAX_COLOR_TEMP_KELVIN = 5000

_EVENT_ATTRS = (
    ("rising", "next_rising"),
    ("setting", "next_setting"),
    ("noon", "next_noon"),
    ("midnight", "next_midnight"),
)


def _parse_iso(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _sun_position(attrs: dict, now: datetime) -> float | None:
    """Puerto de `SunEvents.sun_position` -- ver docstring del modulo."""
    events: list[tuple[str, float]] = []
    for name, attr in _EVENT_ATTRS:
        dt = _parse_iso(attrs.get(attr))
        if dt is None:
            return None  # sun.sun sin los atributos esperados -- no se puede calcular
        ts = dt.timestamp()
        events.append((name, ts))
        events.append((name, ts - 24 * 3600))  # aproximacion del evento "anterior"

    events.sort(key=lambda e: e[1])
    now_ts = now.timestamp()
    idx = 0
    while idx < len(events) and events[idx][1] <= now_ts:
        idx += 1
    if idx == 0 or idx >= len(events):
        return None  # sin margen antes/despues -- deberia ser imposible con 8 eventos que cubren +-24h
    (_, prev_ts), (next_event, next_ts) = events[idx - 1], events[idx]

    h, x = (prev_ts, next_ts) if next_event in ("setting", "rising") else (next_ts, prev_ts)
    if h == x:
        return None
    k = 1.0 if next_event in ("setting", "noon") else -1.0
    return k * (1 - ((now_ts - h) / (h - x)) ** 2)


def _brightness_pct(sun_position: float, min_b: float, max_b: float) -> float:
    """Sube desde el amanecer (min_b) hasta el mediodia solar (max_b) y
    vuelve a bajar hacia el atardecer -- misma forma que
    `_color_temp_kelvin`, aplicada al brillo. De noche se queda fijo en
    el minimo."""
    if sun_position > 0:
        return (max_b - min_b) * sun_position + min_b
    return min_b


def _color_temp_kelvin(sun_position: float, min_k: float, max_k: float) -> int:
    if sun_position > 0:
        ct = (max_k - min_k) * sun_position + min_k
    else:
        ct = min_k
    return 5 * round(ct / 5)  # redondeo a multiplos de 5, igual que el original


def value_at(cfg: dict, sun_state: dict | None, now: datetime | None = None) -> dict | None:
    """`sun_state` = `ws.get_states()` ya filtrado a `sun.sun` (ver
    ZoneRunner._snapshot_states). Devuelve `{"brightness_pct",
    "color_temp_kelvin"}`, o `None` si `sun.sun` no esta disponible o le
    faltan atributos ahora mismo (HA arrancando, integracion de sol
    deshabilitada...) -- en ese caso la zona sigue gestionando
    encendido/apagado con normalidad, solo se queda sin ajustar
    color/brillo hasta que vuelva a haber lectura."""
    attrs = (sun_state or {}).get("attributes") or {}
    position = _sun_position(attrs, now or datetime.now(timezone.utc))
    if position is None:
        return None

    min_b = float(cfg.get("min_brightness_pct", DEFAULT_MIN_BRIGHTNESS_PCT))
    max_b = float(cfg.get("max_brightness_pct", DEFAULT_MAX_BRIGHTNESS_PCT))
    min_k = float(cfg.get("min_color_temp_kelvin", DEFAULT_MIN_COLOR_TEMP_KELVIN))
    max_k = float(cfg.get("max_color_temp_kelvin", DEFAULT_MAX_COLOR_TEMP_KELVIN))

    return {
        "brightness_pct": round(_brightness_pct(position, min_b, max_b)),
        "color_temp_kelvin": _color_temp_kelvin(position, min_k, max_k),
        "sun_position": round(position, 3),
    }

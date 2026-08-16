"""
Dos formas de saber "cuanto cuesta la luz esta hora", elegibles desde la
interfaz. El motor de planificacion (scheduler.py) no sabe ni le importa
cual se esta usando: solo recibe una lista de (precio, tramo) por hora.

  - "fixed": tarifa con precios fijos por tramo (punta/llano/valle), como
    la 2.0TD española. El usuario declara los 3 precios y los horarios.

  - "pvpc_sensor": precio dinamico leido de un sensor de HA (p.ej. la
    integracion PVPC/ESIOS). Como el precio varia hora a hora sin tramos
    fijos, el "tramo" se calcula por posicion relativa dentro de las 24h
    del dia (el tercio mas caro = punta, el mas barato = valle, el medio
    = llano) para que el mismo algoritmo de reserva/carga/descarga funcione
    igual en los dos modos.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import requests

import ha_client


@dataclass
class FixedTariffConfig:
    punta_price: float = 0.173
    llano_price: float = 0.094
    valle_price: float = 0.075
    punta_periods: list = field(default_factory=lambda: [(10, 14), (18, 22)])
    llano_periods: list = field(default_factory=lambda: [(8, 10), (14, 18), (22, 24)])
    weekend_is_valle: bool = True


def _fixed_price_for_hour(dt: datetime, cfg: FixedTariffConfig) -> tuple[float, str]:
    if cfg.weekend_is_valle and dt.weekday() >= 5:
        return cfg.valle_price, "valle"
    h = dt.hour
    for start, end in cfg.punta_periods:
        if start <= h < end:
            return cfg.punta_price, "punta"
    for start, end in cfg.llano_periods:
        if start <= h < end:
            return cfg.llano_price, "llano"
    return cfg.valle_price, "valle"


def fixed_tariff_prices(now: datetime, horizon_hours: int, cfg: FixedTariffConfig) -> list[tuple[float, str]]:
    hours = [now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=i) for i in range(horizon_hours)]
    return [_fixed_price_for_hour(h, cfg) for h in hours]


def _read_pvpc_hourly_prices(entity_id: str) -> dict[datetime, float]:
    """
    Intenta leer el precio por hora desde los atributos habituales de las
    integraciones PVPC de HA (varian segun version/integracion). Se
    prueban varios formatos conocidos; si no se reconoce ninguno, se
    devuelve solo el precio actual repetido.
    """
    state = ha_client.get_state(entity_id)
    attrs = state.get("attributes", {})
    prices: dict[datetime, float] = {}

    # Formato tipo "price_00h".."price_23h" (integracion pvpc_hourly_pricing clasica)
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    for h in range(24):
        key = f"price_{h:02d}h"
        if key in attrs and attrs[key] is not None:
            prices[today + timedelta(hours=h)] = float(attrs[key])

    # Formato tipo lista de pronosticos con "date"/"price" o "value"
    for key in ("forecasts", "raw_today", "raw_tomorrow", "prices"):
        series = attrs.get(key)
        if isinstance(series, list):
            for item in series:
                try:
                    dt = datetime.fromisoformat(str(item.get("datetime") or item.get("date")).replace("Z", "+00:00"))
                    val = item.get("price") if item.get("price") is not None else item.get("value")
                    if val is not None:
                        prices[dt.replace(tzinfo=None)] = float(val)
                except (TypeError, ValueError, AttributeError):
                    continue

    return prices


def pvpc_sensor_prices(entity_id: str, now: datetime, horizon_hours: int) -> list[tuple[float, str]]:
    hours = [now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=i) for i in range(horizon_hours)]

    try:
        hourly = _read_pvpc_hourly_prices(entity_id)
    except (ha_client.HAError, requests.RequestException):
        # Sensor no encontrado, o fallo de red/HA pasajero (502/503 del
        # Supervisor, timeout...) - se cae al precio actual repetido en vez
        # de tumbar el ciclo entero de planificacion.
        hourly = {}

    if not hourly:
        # sin datos por hora: usar el estado actual como precio plano
        current = ha_client.get_numeric_state(entity_id, default=0.15)
        prices = [current] * horizon_hours
    else:
        fallback = sum(hourly.values()) / len(hourly)
        prices = [hourly.get(h, fallback) for h in hours]

    # tramos por posicion relativa dentro de las horas conocidas del horizonte:
    # tercio mas barato = valle, tercio mas caro = punta, resto = llano
    sorted_prices = sorted(prices)
    n = len(sorted_prices)
    valle_cut = sorted_prices[max(0, n // 3 - 1)]
    punta_cut = sorted_prices[min(n - 1, (2 * n) // 3)]

    def tier_for(p: float) -> str:
        if p <= valle_cut:
            return "valle"
        if p >= punta_cut:
            return "punta"
        return "llano"

    return [(p, tier_for(p)) for p in prices]


def get_prices_tiers(tariff_cfg: dict, now: datetime, horizon_hours: int) -> list[tuple[float, str]]:
    if tariff_cfg.get("mode") == "pvpc_sensor":
        entity_id = tariff_cfg.get("pvpc_sensor")
        if entity_id:
            return pvpc_sensor_prices(entity_id, now, horizon_hours)
        # sin sensor configurado todavia: no inventar precio, tratar todo como llano
        return [(0.15, "llano")] * horizon_hours

    cfg = FixedTariffConfig(
        punta_price=tariff_cfg["punta_price"],
        llano_price=tariff_cfg["llano_price"],
        valle_price=tariff_cfg["valle_price"],
        punta_periods=[tuple(p) for p in tariff_cfg["punta_periods"]],
        llano_periods=[tuple(p) for p in tariff_cfg["llano_periods"]],
        weekend_is_valle=tariff_cfg["weekend_is_valle"],
    )
    return fixed_tariff_prices(now, horizon_hours, cfg)

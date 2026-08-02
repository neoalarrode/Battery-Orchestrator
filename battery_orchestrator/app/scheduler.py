"""
Motor de planificacion de carga/descarga de baterias.

Nada de programacion lineal ni parametros ocultos: dos pasadas simples,
explicables y deterministas.

  PASADA A (hacia atras): cuanta energia hace falta reservar para cubrir
  las horas caras (punta) que quedan por delante, dado el consumo previsto
  y la produccion solar prevista.

  PASADA B (hacia adelante): simula hora a hora. Carga siempre gratis con
  excedente solar. Carga desde red SOLO en horas valle y SOLO lo que falte
  para llegar a la reserva calculada en la pasada A (nunca de mas). Descarga
  en horas punta (y llano si sobra) para cubrir el deficit previsto.

El resultado es un plan hora a hora, mas la accion concreta a ejecutar YA
en la hora actual.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class HourPlan:
    dt: datetime
    price: float
    tier: str
    pv_w: float
    load_w: float
    charge_w: float = 0.0
    discharge_w: float = 0.0
    soc_wh: float = 0.0
    reason: str = ""


def build_plan(
    now: datetime,
    pv_forecast_w: list[float],
    load_forecast_w: list[float],
    current_soc_wh: float,
    total_capacity_wh: float,
    max_charge_w: float,
    max_discharge_w: float,
    min_soc_wh: float,
    prices_tiers: list[tuple[float, str]],
    contracted_power_w: float = 0,
) -> list[HourPlan]:
    """
    pv_forecast_w / load_forecast_w: listas de potencia media (W) para cada
    una de las proximas horas, empezando por la hora actual (indice 0).
    Ambas listas deben tener la misma longitud (el horizonte, tipicamente 24-36h).

    prices_tiers: lista de (precio EUR/kWh, tramo) para cada hora del mismo
    horizonte, ya calculada por el modulo `tariff_source` (tarifa fija o
    PVPC dinamica) — a este motor le da igual de donde vengan los precios.

    contracted_power_w: potencia contratada de la vivienda (0 = sin limite).
    Solo se aplica a la carga desde RED (en valle) — la carga con excedente
    solar no consume potencia contratada porque no tira de red.
    """
    horizon = len(pv_forecast_w)
    hours = [now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=i) for i in range(horizon)]

    deficit_w = [max(0.0, load_forecast_w[i] - pv_forecast_w[i]) for i in range(horizon)]
    surplus_w = [max(0.0, pv_forecast_w[i] - load_forecast_w[i]) for i in range(horizon)]

    # --- PASADA A: cuanta energia reservar para las horas punta que quedan ---
    # (si no hay horas punta con deficit, tambien miramos llano como segunda prioridad)
    punta_need_wh = sum(deficit_w[i] for i in range(horizon) if prices_tiers[i][1] == "punta")
    llano_need_wh = sum(deficit_w[i] for i in range(horizon) if prices_tiers[i][1] == "llano")

    usable_capacity_wh = total_capacity_wh - min_soc_wh
    # Reserva objetivo: cubrir punta primero, luego llano si cabe, limitado a
    # lo que la bateria puede fisicamente almacenar.
    reserve_wh = min(punta_need_wh, usable_capacity_wh)
    reserve_wh += min(llano_need_wh, max(0.0, usable_capacity_wh - reserve_wh))

    # --- PASADA B: simulacion hacia adelante ---
    plan: list[HourPlan] = []
    soc = current_soc_wh

    for i in range(horizon):
        price, tier = prices_tiers[i]
        hp = HourPlan(dt=hours[i], price=price, tier=tier, pv_w=pv_forecast_w[i], load_w=load_forecast_w[i])

        # 1) Carga gratis con excedente solar, siempre.
        if surplus_w[i] > 0:
            headroom = total_capacity_wh - soc
            charge = min(surplus_w[i], max_charge_w, headroom)
            if charge > 0:
                soc += charge
                hp.charge_w = charge
                hp.reason = "carga con excedente solar"

        # 2) Carga desde red SOLO en valle y SOLO si aun no llegamos a la reserva.
        #    Respetando la potencia contratada: no cargar mas de lo que deja
        #    libre el consumo previsto de la casa en esa hora.
        elif tier == "valle" and soc < reserve_wh:
            headroom = min(total_capacity_wh - soc, reserve_wh - soc)
            charge_limit = max_charge_w
            if contracted_power_w > 0:
                grid_headroom = max(0.0, contracted_power_w - load_forecast_w[i])
                charge_limit = min(charge_limit, grid_headroom)
            charge = min(charge_limit, headroom)
            if charge > 0:
                soc += charge
                hp.charge_w = charge
                hp.reason = f"carga en valle (objetivo reserva {reserve_wh/1000:.2f} kWh)"
            elif charge_limit <= 0:
                hp.reason = "sin carga: al limite de potencia contratada"

        # 3) Descarga en punta/llano para cubrir el deficit previsto.
        elif deficit_w[i] > 0 and tier in ("punta", "llano"):
            available = max(0.0, soc - min_soc_wh)
            discharge = min(deficit_w[i], max_discharge_w, available)
            if discharge > 0:
                soc -= discharge
                hp.discharge_w = discharge
                hp.reason = f"descarga para cubrir consumo en {tier}"

        if not hp.reason:
            hp.reason = "sin accion (no compensa)"

        hp.soc_wh = soc
        plan.append(hp)

    return plan


if __name__ == "__main__":
    # Prueba rapida con datos simulados: un domingo por la tarde, poca
    # bateria, punta manana laborable.
    from tariff_source import FixedTariffConfig, fixed_tariff_prices

    now = datetime(2026, 8, 2, 18, 0)  # domingo 18:00, como en la conversacion real
    horizon = 24

    # Consumo tipico: base ~250W, pico tarde 15-17h laborable no incluido aqui.
    load = [340, 782, 489, 552, 372, 283, 259, 246, 225, 267, 226, 212, 192, 195,
            359, 553, 790, 933, 689, 554, 438, 1373, 2465, 1012][:horizon]
    pv = [298, 76, 26, 4, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
          9, 45, 171, 303, 397, 421, 390, 404, 409, 292][:horizon]

    cfg = FixedTariffConfig()
    prices_tiers = fixed_tariff_prices(now, horizon, cfg)
    plan = build_plan(
        now=now,
        pv_forecast_w=pv,
        load_forecast_w=load,
        current_soc_wh=0.50 * 9600,
        total_capacity_wh=9600,
        max_charge_w=1200,
        max_discharge_w=1200,
        min_soc_wh=0.03 * 9600,
        prices_tiers=prices_tiers,
    )

    print(f"{'Hora':>16} {'Tramo':>6} {'Precio':>7} {'PV':>6} {'Carga':>7} {'Carga_W':>8} {'Desc_W':>7} {'SOC%':>6}  Motivo")
    for hp in plan:
        print(f"{hp.dt.strftime('%a %d %H:%M'):>16} {hp.tier:>6} {hp.price:>7.3f} {hp.pv_w:>6.0f} "
              f"{hp.load_w:>7.0f} {hp.charge_w:>8.0f} {hp.discharge_w:>7.0f} {100*hp.soc_wh/9600:>5.1f}%  {hp.reason}")

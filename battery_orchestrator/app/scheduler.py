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

# Margen sobre la potencia sostenida calculada (carga_pausada): la prevision
# de consumo/solar puede fallar un poco, asi que apuntamos a llegar con un
# 20% de mas potencia de la estrictamente necesaria, no al filo.
PACED_CHARGE_SAFETY_MARGIN = 1.2


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
    charge_source: str | None = None  # "solar" | "grid" | None — de donde sale la carga de esta hora


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
    max_usable_wh: float | None = None,
    allow_grid_charging: bool = True,
    paced_charging: bool = False,
) -> tuple[list[HourPlan], float]:
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

    max_usable_wh: techo real de carga (p.ej. si tus baterias tienen un
    SOC maximo declarado por debajo del 100% nominal, como 97%, para
    alargar su vida util). Si no se indica, se usa total_capacity_wh
    (100% nominal). total_capacity_wh se sigue usando tal cual para
    calcular el % de SOC en el plan.

    allow_grid_charging: si es False, la bateria SOLO carga con excedente
    solar — nunca desde red, ni en valle ni en emergencia de llano (modo
    "autoconsumo"). Si es True (por defecto), tambien carga desde red en
    valle (y en llano de emergencia) lo justo para cubrir la punta que
    quede por delante (modo "ahorro").

    paced_charging: si es True, la carga deliberada desde red (valle y
    emergencia en llano) NO va siempre a maxima potencia: se reparte a lo
    largo de las horas que quedan hasta que la bateria vaya a hacer falta
    de verdad (la proxima hora, sea llano o punta, en la que se prevea
    consumo por encima de la produccion solar), con un margen de seguridad.
    Menos calor/estres en la bateria a cambio de, a veces, no acabar de
    cargar tan rapido. Si el tiempo se echa encima, la potencia calculada
    sube sola (mismo calculo, menos horas) hasta el maximo si hace falta —
    no es una rama de emergencia aparte, es el mismo numero.

    Devuelve (plan, reserve_wh): el plan hora a hora, y el nivel de SOC
    absoluto (Wh) que el motor esta intentando alcanzar ahora mismo para
    cubrir punta/llano futuros — util para mostrar "cuanto falta para la
    reserva" en la interfaz sin duplicar esta cuenta en otro sitio.
    """
    horizon = len(pv_forecast_w)
    hours = [now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=i) for i in range(horizon)]
    ceiling_wh = max_usable_wh if max_usable_wh is not None else total_capacity_wh

    deficit_w = [max(0.0, load_forecast_w[i] - pv_forecast_w[i]) for i in range(horizon)]
    surplus_w = [max(0.0, pv_forecast_w[i] - load_forecast_w[i]) for i in range(horizon)]

    # --- PASADA A: cuanta energia reservar para las horas punta que quedan ---
    # (si no hay horas punta con deficit, tambien miramos llano como segunda prioridad)
    punta_need_wh = sum(deficit_w[i] for i in range(horizon) if prices_tiers[i][1] == "punta")
    llano_need_wh = sum(deficit_w[i] for i in range(horizon) if prices_tiers[i][1] == "llano")

    usable_capacity_wh = ceiling_wh - min_soc_wh
    # Cuanta ENERGIA hace falta acumular (cubrir punta primero, luego llano
    # si cabe), limitado a lo que la bateria puede fisicamente entregar.
    energy_needed_wh = min(punta_need_wh, usable_capacity_wh)
    energy_needed_wh += min(llano_need_wh, max(0.0, usable_capacity_wh - energy_needed_wh))
    # Convertido a NIVEL ABSOLUTO de SOC (no un delta): el suelo minimo mas
    # la energia que hace falta, sin superar nunca el techo real declarado.
    # (antes esto se comparaba mal contra el SOC absoluto y el objetivo
    # quedaba siempre min_soc_wh por debajo del techo real, p.ej. un 97%
    # aunque el techo configurado fuera 100%)
    reserve_wh = min(ceiling_wh, min_soc_wh + energy_needed_wh)

    # Cuanto deficit de PUNTA queda por delante desde cada hora i (sin
    # incluir la propia hora i si es punta, esa la cubre la rama 3 directamente).
    # Sirve para que la descarga en LLANO nunca se coma battery que hace
    # falta reservar para una punta posterior.
    future_punta_after = [0.0] * (horizon + 1)
    for i in range(horizon - 1, -1, -1):
        extra = deficit_w[i] if prices_tiers[i][1] == "punta" else 0.0
        future_punta_after[i] = future_punta_after[i + 1] + extra
    # future_punta_after[i] = deficit total en punta desde la hora i (inclusive) en adelante

    # Para la carga sostenida (paced_charging): en que hora, de aqui en
    # adelante, va a hacer falta de verdad la bateria por primera vez — sea
    # llano o punta, nos da igual el tramo, solo que haya consumo por
    # encima del solar previsto (en valle nunca se descarga, se paga barato
    # directo de red, asi que no cuenta como "necesidad").
    needs_battery = [prices_tiers[i][1] != "valle" and deficit_w[i] > 0 for i in range(horizon)]
    next_need_idx: list[int | None] = [None] * (horizon + 1)
    for i in range(horizon - 1, -1, -1):
        next_need_idx[i] = i if needs_battery[i] else next_need_idx[i + 1]

    def _paced_charge_limit(i: int, soc_now: float, target_wh: float) -> float:
        """Potencia sostenida que reparte lo que falta hasta que la bateria
        haga falta por primera vez. Si esa necesidad es AHORA MISMO (o no
        se ve ninguna en el horizonte), no hay margen que repartir: maxima
        potencia disponible, sin rodeos."""
        deadline = next_need_idx[i]
        if deadline is None:
            return max_charge_w
        hours_remaining = deadline - i
        if hours_remaining <= 0:
            return max_charge_w
        energy_needed_wh = max(0.0, target_wh - soc_now)
        return min(max_charge_w, (energy_needed_wh / hours_remaining) * PACED_CHARGE_SAFETY_MARGIN)

    # --- PASADA B: simulacion hacia adelante ---
    plan: list[HourPlan] = []
    soc = current_soc_wh

    for i in range(horizon):
        price, tier = prices_tiers[i]
        hp = HourPlan(dt=hours[i], price=price, tier=tier, pv_w=pv_forecast_w[i], load_w=load_forecast_w[i])

        # 1) Carga gratis con excedente solar, siempre.
        if surplus_w[i] > 0:
            headroom = ceiling_wh - soc
            charge = min(surplus_w[i], max_charge_w, headroom)
            if charge > 0:
                soc += charge
                hp.charge_w = charge
                hp.charge_source = "solar"
                hp.reason = "carga con excedente solar"

        # 2) Carga desde red en VALLE, oportunista, hasta la reserva completa
        #    (punta + llano que quepa). Respetando la potencia contratada.
        #    Se salta por completo en modo "autoconsumo" (allow_grid_charging=False).
        elif allow_grid_charging and tier == "valle" and soc < reserve_wh:
            headroom = min(ceiling_wh - soc, reserve_wh - soc)
            charge_limit = _paced_charge_limit(i, soc, reserve_wh) if paced_charging else max_charge_w
            if contracted_power_w > 0:
                grid_headroom = max(0.0, contracted_power_w - load_forecast_w[i])
                charge_limit = min(charge_limit, grid_headroom)
            charge = min(charge_limit, headroom)
            if charge > 0:
                soc += charge
                hp.charge_w = charge
                hp.charge_source = "grid"
                if paced_charging and charge_limit < max_charge_w:
                    hp.reason = f"carga sostenida en valle (objetivo reserva {reserve_wh/1000:.2f} kWh)"
                else:
                    hp.reason = f"carga en valle (objetivo reserva {reserve_wh/1000:.2f} kWh)"
            elif charge_limit <= 0:
                hp.reason = "sin carga: al limite de potencia contratada"

        # 2b) Carga de EMERGENCIA en LLANO: si con lo que hay no va a llegar
        #     a cubrir toda la punta que queda por delante, compensa cargar
        #     en llano aunque sea mas caro que valle — sigue siendo mas
        #     barato que dejar esa punta sin cubrir (llano < punta siempre).
        #     Solo carga lo justo para tapar ese hueco, no la reserva completa.
        #     Tambien se salta en modo "autoconsumo".
        elif allow_grid_charging and tier == "llano" and soc < min_soc_wh + future_punta_after[i]:
            target = min(ceiling_wh, min_soc_wh + future_punta_after[i])
            headroom = min(ceiling_wh - soc, target - soc)
            charge_limit = _paced_charge_limit(i, soc, target) if paced_charging else max_charge_w
            if contracted_power_w > 0:
                grid_headroom = max(0.0, contracted_power_w - load_forecast_w[i])
                charge_limit = min(charge_limit, grid_headroom)
            charge = min(charge_limit, headroom)
            if charge > 0:
                soc += charge
                hp.charge_w = charge
                hp.charge_source = "grid"
                if paced_charging and charge_limit < max_charge_w:
                    hp.reason = "carga sostenida en llano (no llegaba a cubrir la punta que queda)"
                else:
                    hp.reason = "carga en llano (no llegaba a cubrir la punta que queda)"
            elif charge_limit <= 0:
                hp.reason = "sin carga: al limite de potencia contratada"

        # 3) Descarga en PUNTA: siempre prioritaria, cubre el deficit previsto.
        elif deficit_w[i] > 0 and tier == "punta":
            available = max(0.0, soc - min_soc_wh)
            discharge = min(deficit_w[i], max_discharge_w, available)
            if discharge > 0:
                soc -= discharge
                hp.discharge_w = discharge
                hp.reason = "descarga para cubrir consumo en punta"

        # 4) Descarga en LLANO: solo con lo que sobre por encima de lo que
        #    haga falta reservar para TODA la punta que quede por delante.
        elif deficit_w[i] > 0 and tier == "llano":
            reserved_for_future_punta = future_punta_after[i + 1]
            available = max(0.0, soc - min_soc_wh - reserved_for_future_punta)
            discharge = min(deficit_w[i], max_discharge_w, available)
            if discharge > 0:
                soc -= discharge
                hp.discharge_w = discharge
                hp.reason = "descarga para cubrir consumo en llano"
            else:
                hp.reason = "sin descargar en llano: reservado para punta posterior"

        if not hp.reason:
            hp.reason = "sin accion (no compensa)"

        hp.soc_wh = soc
        plan.append(hp)

    return plan, reserve_wh


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
    plan, reserve_wh = build_plan(
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

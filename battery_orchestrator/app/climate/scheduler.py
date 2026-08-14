"""
Motor de decision de una zona.

Es CONTINUO, no un plan de dia completo — a diferencia de la primera
version de este proyecto (y de Battery Orchestrator), aqui no hay
horario que anticipar: los presets se activan por presencia REAL (ver
presets.py) o a mano, nunca por una franja horaria prevista de antemano.

Pero eliminar el horario NO significa eliminar la anticipacion: el
sistema SI debe adelantarse a cambios de temperatura exterior previstos
— es mas eficiente (y mas comodo) ir ajustando la zona de forma sostenida
mientras todavia hay margen, que esperar a que se salga de rango y tener
que actuar a maxima potencia de golpe. La diferencia con la version
anterior es DE DONDE viene esa anticipacion: antes venia de un horario
(¿cuando sube el nivel programado?), ahora viene UNICAMENTE de la
previsión meteorologica exterior (dato observable, nunca una prediccion
de presencia — eso seria una caja negra, y solo se usa medido en directo).

Nada de programacion lineal: una funcion, unas pocas ramas, cada una con
su motivo en texto plano.

  1. Limites de seguridad de la zona (min_temp/max_temp, ver const.py):
     SIEMPRE se respetan, pase lo que pase con el preset activo, la
     presencia o el modo manual — el "no me importa que no haya nadie,
     nunca por debajo de 12°C en invierno / nunca por encima de 30°C en
     verano" que se pidio explicitamente.

  2. Reactivo: si la zona YA esta fuera de rango del preset activo, actua
     ya. El margen de esta comprobacion es la histéresis declarada en
     prioridad "confort", o un margen mas ancho en "ahorro" (ver
     `_ahorro_extra_margin`, se estrecha si la previsión exterior empeora
     en las proximas horas O si el precio/sol de la red no acompañan
     ahora mismo — ver mas abajo, "señal de red").

  3. Banco de confort (SOLO en "ahorro"): si hay excedente solar disponible
     AHORA MISMO que cubre lo que esta zona necesitaria, y la zona todavia
     tiene margen hasta el techo/suelo de seguridad, adelanta la
     actuacion — usa la inercia termica del edificio como un deposito de
     confort gratis antes de que llegue una hora cara (ver
     `_opportunistic_preheat`).

  4. Anticipatorio (aplica en "confort" Y "ahorro" por igual — no es una
     cuestion de ahorro, es evitar el golpe de "esperar y luego a tope"):
     si la zona esta DENTRO de rango ahora mismo pero, proyectando su
     deriva pasiva con la previsión exterior y la inercia termica
     aprendida, se preve que se salga de rango en las proximas horas y ya
     no de tiempo a recuperarlo empezando mas tarde, se empieza a actuar
     YA, de forma sostenida — en vez de esperar a que la zona ya se haya
     salido de rango y tener que compensarlo de golpe.

  5. Prioridad "manual": nunca decide, deja el control a la anulacion
     manual o al preset fijado a mano.

Señal de red (opcional, Battery Orchestrator): si ese addon esta
instalado, publica una unica entidad con entity_id FIJO
("sensor.battery_orchestrator_grid_signal") — se detecta sola, sin
declarar nada en la configuracion de esta integracion. Sin ella (no
instalado, o sin haber corrido su primer ciclo todavia), todos los
parametros de red llegan a None/0 y el comportamiento es EXACTAMENTE el
de antes de que existiera esta seccion: nada de lo de aqui es obligatorio.
"""

from __future__ import annotations

AHORRO_MAX_MARGIN_DEG = 1.5       # cuanto mas ancho puede llegar a ser el margen de "ahorro" frente a "confort", en el mejor de los casos
AHORRO_MARGIN_SENSITIVITY = 0.5   # cuantos °C se recorta ese margen extra por cada °C que empeore la previsión en el horizonte de aviso
AHORRO_LOOKAHEAD_HOURS = 3        # cuantas horas de previsión exterior se miran para juzgar la tendencia (ahorro)
ANTICIPATE_LOOKAHEAD_HOURS = 3    # cuantas horas de previsión exterior se miran para anticipar una salida de rango (confort Y ahorro)
REFERENCE_RATE_DEG_H = 1.0        # tasa de referencia (°C/h) a partir de la cual una zona se considera "rapida" y se le da margen completo
PRICE_ANTICIPATE_LOOKAHEAD_HOURS = 4  # cuantas horas del pronostico de red (Battery Orchestrator) se miran para anticipar una hora punta

# `idle_loss_coeff` (thermal_model.py, aprendido del historico real) es
# la CAPACIDAD DE RETENCION de la zona: cuantos grados por hora se acerca
# a la temperatura exterior con todo apagado, por cada grado de
# diferencia — 0.0 = retiene perfectamente (no aprendido, o aislamiento
# excelente), hasta MAX_IDLE_LOSS_COEFF (el mismo tope que usa
# thermal_model.py para descartar medidas invalidas) = pierde rapido. Ya
# se usaba para decidir CUANDO empezar a anticipar (`_anticipate`); ahora
# tambien decide CUANTO merece la pena banquear en el preheat/preenfriado
# oportunista (`_opportunistic_preheat`/`_price_anticipation_preheat`):
# una zona que retiene bien aprovecha un margen extra completo (el calor/
# frio banqueado dura hasta la hora cara); una que pierde rapido no
# compensa tanto banquear — ese margen se escaparia solo antes de que
# haga falta.
MAX_IDLE_LOSS_COEFF = 0.6


def _retention_factor(idle_loss_coeff: float | None) -> float:
    """1.0 (retencion perfecta conocida, o sin dato todavia — mismo
    comportamiento que antes de existir esto, no penaliza a una zona
    recien creada) hasta 0.0 (perdida maxima observable, MAX_IDLE_LOSS_
    COEFF). Multiplica el margen extra de preheat/preenfriado — ver
    llamadas en `_opportunistic_preheat`/`_price_anticipation_preheat`."""
    if idle_loss_coeff is None:
        return 1.0
    return max(0.0, min(1.0, 1 - idle_loss_coeff / MAX_IDLE_LOSS_COEFF))


def retention_label(idle_loss_coeff: float | None) -> str:
    """Etiqueta legible de la capacidad de retencion, para mostrar en el
    dashboard (ver climate.py extra_state_attributes) — nunca una caja
    negra, el numero crudo (`idle_loss_coeff`) tambien se expone al lado."""
    if idle_loss_coeff is None:
        return "sin datos todavía"
    factor = _retention_factor(idle_loss_coeff)
    if factor >= 0.75:
        return "buena"
    if factor >= 0.4:
        return "media"
    return "baja"

# TPI (Time Proportional Integral) — inspirado en versatile_thermostat:
# en vez de un simple on/off, un switch recibe un porcentaje de tiempo
# encendido DENTRO de cada ciclo (ver `tpi_on_percent` y
# `ClimateOrchestratorZone._tpi_desired_on` en climate.py), proporcional
# a cuanto falta para la consigna — mas suave, menos golpes de
# encendido/apagado, mas eficiente. Solo aplica a switches: un climate.*
# delegado ya tiene su propio control interno, no le hace falta esto.
# Coeficientes fijos por ahora (no configurables) — TPI_COEF_INT pesa el
# error de temperatura INTERIOR, TPI_COEF_EXT un empuje extra por lo fria
# u caliente que este fuera (ayuda a arrancar antes en dias extremos).
TPI_COEF_INT = 0.4
TPI_COEF_EXT = 0.05


def decide_action(
    current_temp: float,
    heat_target: float | None,
    cool_target: float | None,
    priority: str,
    deadband: float,
    min_temp: float,
    max_temp: float,
    outdoor_now: float | None,
    outdoor_forecast: list[float],
    heating_rate_deg_h: float,
    cooling_rate_deg_h: float,
    idle_loss_coeff: float,
    grid_tier: str | None = None,
    solar_surplus_now_w: float | None = None,
    zone_estimated_power_w: float | None = None,
    grid_forecast: list[dict] | None = None,
) -> tuple[str, str]:
    """Devuelve (accion, motivo). `accion` es "heat" | "cool" | "idle".

    `heat_target`/`cool_target`: las DOS consignas del preset activo
    (ver presets.py) — una zona de calor y frio ("Auto", el unico modo
    dual que expone climate.py, para que sea compatible con el System
    Mode estandar de Matter) tiene las dos a la vez, calienta si baja de
    `heat_target` y enfria si sube de `cool_target`; una zona de un solo
    sentido solo trae rellena la que le corresponde (la otra es None).

    `outdoor_forecast`: previsión horaria empezando por la hora actual
    (indice 0), o lista vacia si no hay ninguna fuente declarada — ver
    outdoor.py. Sin previsión disponible, el motor sigue funcionando
    (reactivo puro, sin anticipacion ni ensanche de margen), nunca falla.

    `grid_tier`/`solar_surplus_now_w`/`zone_estimated_power_w`/
    `grid_forecast`: señal de Battery Orchestrator, si esta instalado (ver
    modulo grid_signal.py) — None/[] si no hay addon, o si no ha
    reportado nunca. Solo se usan en prioridad "ahorro"; sin ellos,
    "ahorro" se comporta exactamente igual que antes de que existiera
    esta integracion (solo meteo exterior).
    """
    heating = heat_target is not None
    cooling = cool_target is not None

    if heating and current_temp < min_temp:
        return "heat", f"por debajo del mínimo de seguridad de la zona ({min_temp:.1f}°C)"
    if cooling and current_temp > max_temp:
        return "cool", f"por encima del máximo de seguridad de la zona ({max_temp:.1f}°C)"

    if priority == "manual":
        return "idle", "modo manual: sin gestión automática"

    # Red de seguridad EN VIVO: `parse_presets` (presets.py) ya rechaza
    # esto al declarar un preset, pero las consignas number.* son
    # editables en caliente desde Lovelace o una automatizacion — si
    # alguien deja la de calor igual o por encima de la de frio DESPUES
    # de creada la zona, sin esto ninguna temperatura real dejaria a la
    # zona tranquila: siempre "hace falta calor" o "hace falta frio" a la
    # vez, el peor derroche posible (o luchando entre si, o ciclando sin
    # parar). Mejor idle con el motivo claro que dejar que el motor
    # persiga dos consignas imposibles.
    if heating and cooling and heat_target >= cool_target:
        return "idle", (
            f"consignas inválidas: calor ({heat_target:.1f}°C) no es menor que frío ({cool_target:.1f}°C) — "
            "revisa las entidades number.* del preset activo"
        )

    heat_deadband = cool_deadband = deadband
    heat_note = cool_note = ""
    if priority == "ahorro":
        if heating:
            extra, why = _ahorro_extra_margin(True, outdoor_now, outdoor_forecast, heating_rate_deg_h,
                                               grid_tier, solar_surplus_now_w, zone_estimated_power_w)
            heat_deadband = deadband + extra
            heat_note = f" ({why})"
        if cooling:
            extra, why = _ahorro_extra_margin(False, outdoor_now, outdoor_forecast, cooling_rate_deg_h,
                                               grid_tier, solar_surplus_now_w, zone_estimated_power_w)
            cool_deadband = deadband + extra
            cool_note = f" ({why})"

    if heating and current_temp < heat_target - heat_deadband:
        return "heat", f"calentando hacia {heat_target:.1f}°C{heat_note}"
    if cooling and current_temp > cool_target + cool_deadband:
        return "cool", f"enfriando hacia {cool_target:.1f}°C{cool_note}"

    if priority == "ahorro":
        if heating:
            action, reason = _opportunistic_preheat(True, current_temp, heat_target, deadband,
                                                      solar_surplus_now_w, zone_estimated_power_w,
                                                      max_temp, min_temp, idle_loss_coeff)
            if action != "idle":
                return action, reason
            action, reason = _price_anticipation_preheat(True, current_temp, heat_target, deadband,
                                                           grid_tier, grid_forecast, zone_estimated_power_w,
                                                           max_temp, min_temp, idle_loss_coeff)
            if action != "idle":
                return action, reason
        if cooling:
            action, reason = _opportunistic_preheat(False, current_temp, cool_target, deadband,
                                                      solar_surplus_now_w, zone_estimated_power_w,
                                                      max_temp, min_temp, idle_loss_coeff)
            if action != "idle":
                return action, reason
            action, reason = _price_anticipation_preheat(False, current_temp, cool_target, deadband,
                                                           grid_tier, grid_forecast, zone_estimated_power_w,
                                                           max_temp, min_temp, idle_loss_coeff)
            if action != "idle":
                return action, reason

    if heating:
        action, reason = _anticipate(True, current_temp, heat_target, deadband, outdoor_forecast, idle_loss_coeff, heating_rate_deg_h)
        if action != "idle":
            return action, reason
    if cooling:
        action, reason = _anticipate(False, current_temp, cool_target, deadband, outdoor_forecast, idle_loss_coeff, cooling_rate_deg_h)
        if action != "idle":
            return action, reason

    parts = []
    if heating:
        parts.append(f"calor {heat_target:.1f}°C (±{heat_deadband:.1f}){heat_note}")
    if cooling:
        parts.append(f"frío {cool_target:.1f}°C (±{cool_deadband:.1f}){cool_note}")
    return "idle", f"dentro de rango: {', '.join(parts) if parts else 'sin consigna activa'}"


def _economic_factor(grid_tier: str | None, solar_surplus_now_w: float | None,
                      zone_power_w: float | None) -> tuple[float, str]:
    """Multiplicador (0..1) sobre el margen de "ahorro" segun el precio/sol
    de la red AHORA MISMO — ver grid_signal.py. 1.0 = no recorta nada
    (sin señal de Battery Orchestrator, o excedente solar de sobra para
    cubrir la zona: tan barato como pueda ser, margen completo). Recorta
    mas cuanto mas cara sea la hora sin sol que la cubra — igual filosofia
    que el resto de factores de `_ahorro_extra_margin`: solo puede
    RECORTAR el margen maximo, nunca ampliarlo por su cuenta."""
    if grid_tier is None:
        return 1.0, ""
    if solar_surplus_now_w and zone_power_w and solar_surplus_now_w >= zone_power_w:
        return 1.0, "excedente solar cubre la zona: margen completo"
    if grid_tier == "valle":
        return 0.8, "hora valle: margen amplio"
    if grid_tier == "llano":
        return 0.4, "hora llano: margen moderado"
    return 0.0, "hora punta sin sol suficiente: margen mínimo"


def _ahorro_extra_margin(heating: bool, outdoor_now: float | None, outdoor_forecast: list[float],
                          rate_deg_h: float, grid_tier: str | None = None,
                          solar_surplus_now_w: float | None = None,
                          zone_power_w: float | None = None) -> tuple[float, str]:
    """Cuantos °C de mas se le puede dar de margen a la histéresis en
    prioridad "ahorro", y por que. Combina TRES factores independientes,
    cada uno limitando el margen por su cuenta (nunca lo amplian, solo lo
    recortan desde el maximo):

      - Tendencia exterior: si la previsión de las proximas
        `AHORRO_LOOKAHEAD_HOURS` horas empeora (mas frio en calefaccion,
        mas calor en refrigeracion), se recorta el margen proporcionalmente
        — mejor no confiar en un margen ancho si el exterior va a jugar en
        contra.
      - Velocidad real de la zona (inercia termica aprendida): una zona
        lenta no se puede permitir tanto margen como una rapida, porque
        tarda mas en recuperar terreno si hace falta.
      - Precio/sol AHORA MISMO (ver `_economic_factor`) — señal opcional
        de Battery Orchestrator, si esta instalado.
    """
    if outdoor_now is None or not outdoor_forecast:
        base_max = AHORRO_MAX_MARGIN_DEG * 0.5
        trend_note = "sin previsión exterior: margen moderado"
    else:
        lookahead = outdoor_forecast[:AHORRO_LOOKAHEAD_HOURS] or [outdoor_now]
        trend = lookahead[-1] - outdoor_now
        worsening = max(0.0, -trend) if heating else max(0.0, trend)
        base_max = max(0.0, AHORRO_MAX_MARGIN_DEG - worsening * AHORRO_MARGIN_SENSITIVITY)
        trend_note = "previsión exterior estable" if worsening < 0.5 else "la previsión exterior empeora, margen recortado"

    responsiveness = max(0.0, min(1.0, (rate_deg_h or 0.0) / REFERENCE_RATE_DEG_H))
    economic_mult, economic_note = _economic_factor(grid_tier, solar_surplus_now_w, zone_power_w)
    extra = base_max * responsiveness * economic_mult

    note = trend_note
    if responsiveness < 0.5:
        note = f"{note}, zona lenta ({rate_deg_h:.1f}°C/h)"
    if economic_note:
        note = f"{note}, {economic_note}"
    return extra, note


def _opportunistic_preheat(heating: bool, current_temp: float, target_temp: float, deadband: float,
                            solar_surplus_now_w: float | None, zone_power_w: float | None,
                            max_temp: float, min_temp: float, idle_loss_coeff: float | None = None) -> tuple[str, str]:
    """Banco de confort: si hay excedente solar AHORA MISMO que cubre lo
    que esta zona necesitaria, adelanta la actuacion hasta `deadband`
    extra sobre la consigna — usa la inercia termica del edificio como un
    deposito de confort gratis antes de que llegue una hora cara, en vez
    de esperar a necesitarlo de verdad y tener que pagarlo. Nunca cruza
    `max_temp`/`min_temp` (ya comprobados antes que nada mas en
    `decide_action`).

    El margen extra REAL se escala por `_retention_factor(idle_loss_coeff)`
    — una zona que retiene bien el calor/frio (aislamiento real, aprendido
    del historico, ver thermal_model.py) aprovecha el deadband completo,
    porque lo banqueado dura; una que pierde rapido banquea menos, porque
    ese margen se escaparia solo antes de que haga falta de verdad. Sin
    dato de retencion todavia (zona nueva), se comporta igual que antes
    (deadband completo).

    Sin dato de excedente solar o de potencia estimada de la zona, no
    hace nada — nunca inventa una oportunidad que no se puede confirmar."""
    if not solar_surplus_now_w or not zone_power_w or solar_surplus_now_w < zone_power_w:
        return "idle", ""
    boost = deadband * _retention_factor(idle_loss_coeff)
    retention_note = f", retención {retention_label(idle_loss_coeff)}" if idle_loss_coeff else ""
    if heating:
        boost_target = min(target_temp + boost, max_temp)
        if current_temp < boost_target:
            return "heat", f"excedente solar disponible ahora: pre-climatizando antes de la próxima hora cara{retention_note}"
    else:
        boost_target = max(target_temp - boost, min_temp)
        if current_temp > boost_target:
            return "cool", f"excedente solar disponible ahora: pre-climatizando antes de la próxima hora cara{retention_note}"
    return "idle", ""


def _price_anticipation_preheat(heating: bool, current_temp: float, target_temp: float, deadband: float,
                                 grid_tier: str | None, grid_forecast: list[dict] | None,
                                 zone_power_w: float | None, max_temp: float, min_temp: float,
                                 idle_loss_coeff: float | None = None) -> tuple[str, str]:
    """Banco de confort por PRECIO: a diferencia de `_opportunistic_preheat`
    (que solo mira el excedente solar AHORA MISMO), esta mira el
    PRONOSTICO que Battery Orchestrator ya calcula para si mismo (ver
    grid_signal.py) — si alguna de las proximas `PRICE_ANTICIPATE_LOOKAHEAD_HOURS`
    horas sera "punta" sin excedente solar que la cubra, y la hora ACTUAL
    todavia no lo es, adelanta la actuacion usando la inercia termica del
    edificio como deposito de confort gratis mientras es barato, en vez
    de esperar a que llegue la hora cara para notarlo.

    A proposito NO mira la hora actual (indice 0 del pronostico, ya
    cubierto por `grid_tier`): si la hora punta YA es ahora, esto no debe
    disparar (seria "precalentar" en plena hora cara) — ese caso ya lo
    cubre el margen recortado de `_economic_factor` en la rama reactiva.

    Mismo escalado por retencion que `_opportunistic_preheat` (ver ahi) —
    nunca cruza max_temp/min_temp. Sin pronostico (Battery Orchestrator
    no instalado, sin datos todavia, o zona sin potencia estimada/
    aprendida), no hace nada — nunca inventa una hora punta que no esta
    confirmada en el pronostico."""
    if not grid_forecast or not zone_power_w or grid_tier == "punta":
        return "idle", ""
    upcoming_expensive = any(
        h.get("tier") == "punta" and (h.get("solar_surplus_w") or 0) < zone_power_w
        for h in grid_forecast[1:PRICE_ANTICIPATE_LOOKAHEAD_HOURS + 1]
    )
    if not upcoming_expensive:
        return "idle", ""
    boost = deadband * _retention_factor(idle_loss_coeff)
    retention_note = f", retención {retention_label(idle_loss_coeff)}" if idle_loss_coeff else ""
    if heating:
        boost_target = min(target_temp + boost, max_temp)
        if current_temp < boost_target:
            return "heat", f"hora punta próxima sin sol suficiente: pre-climatizando ahora que es más barato{retention_note}"
    else:
        boost_target = max(target_temp - boost, min_temp)
        if current_temp > boost_target:
            return "cool", f"hora punta próxima sin sol suficiente: pre-climatizando ahora que es más barato{retention_note}"
    return "idle", ""


def _anticipate(heating: bool, current_temp: float, target_temp: float, deadband: float,
                 outdoor_forecast: list[float], idle_loss_coeff: float, rate_deg_h: float) -> tuple[str, str]:
    """Proyecta la deriva PASIVA de la zona (sin actuar) durante
    `ANTICIPATE_LOOKAHEAD_HOURS`, hora a hora, con el mismo modelo de
    Newton simple que usa el aprendizaje de inercia (ver
    thermal_model.py): cada hora, la temperatura se acerca a la exterior
    prevista esa hora en proporcion a `idle_loss_coeff`. Si en algun punto
    de esa proyeccion la zona cruzaria el umbral de confort, y el tiempo
    que queda hasta ese cruce no basta para recuperarlo actuando a la
    velocidad real conocida de la zona si se empezase justo entonces,
    arranca YA — de forma sostenida, no de golpe cuando ya sea tarde.

    Sin previsión exterior (`outdoor_forecast` vacia) o sin tasa de
    actuacion conocida, no se anticipa nada: se cae al comportamiento
    puramente reactivo, nunca se inventa una previsión."""
    if not outdoor_forecast or not rate_deg_h or rate_deg_h <= 0:
        return "idle", ""

    threshold = (target_temp - deadband) if heating else (target_temp + deadband)
    temp = current_temp
    for hours_ahead, outdoor_h in enumerate(outdoor_forecast[:ANTICIPATE_LOOKAHEAD_HOURS], start=1):
        temp = temp + idle_loss_coeff * (outdoor_h - temp)
        crossed = (temp < threshold) if heating else (temp > threshold)
        if not crossed:
            continue
        gap = abs(target_temp - temp)
        recover_hours = gap / rate_deg_h
        # Comparacion con el valor FRACCIONARIO de `recover_hours`, sin
        # redondear hacia arriba a un minimo de 1h: una zona muy rapida
        # (recover_hours << 1) no necesita anticipacion aunque el cruce
        # este a "solo" 1h vista — el propio tramo reactivo (mas arriba)
        # ya llega a tiempo de sobra cuando de verdad haga falta.
        if hours_ahead <= recover_hours:
            action = "heat" if heating else "cool"
            return action, (
                f"anticipando: la previsión exterior sacaría la zona de rango en ~{hours_ahead}h; "
                f"empieza ya de forma sostenida para no tener que actuar de golpe"
            )
        break  # se sale de rango en el horizonte, pero todavia hay tiempo de sobra antes de tener que actuar

    return "idle", ""


def tpi_on_percent(current_temp: float, target_temp: float, outdoor_now: float | None, heating: bool) -> float:
    """Duty-cycle proporcional (0..1) para un switch en modo TPI — ver
    TPI_COEF_INT/TPI_COEF_EXT arriba. `heating=True` calienta (pesa mas
    encendido cuanto mas frio respecto a la consigna); `heating=False`
    enfria (al reves). Sin dato exterior, ese termino simplemente no
    aporta nada (no se inventa un valor)."""
    outdoor = outdoor_now if outdoor_now is not None else current_temp
    if heating:
        error_int = target_temp - current_temp
        error_ext = target_temp - outdoor
    else:
        error_int = current_temp - target_temp
        error_ext = outdoor - target_temp
    on_percent = TPI_COEF_INT * error_int + TPI_COEF_EXT * error_ext
    return max(0.0, min(1.0, on_percent))

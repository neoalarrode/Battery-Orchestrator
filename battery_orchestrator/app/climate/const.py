"""Constantes de Climate Orchestrator.

Una entrada de configuracion (ConfigEntry) = UNA zona. Se añaden tantas
zonas como habitaciones se quieran gestionar repitiendo "+ Añadir
integración" — mismo patrón que versatile_thermostat, y el que HA
recomienda para integraciones con varias instancias independientes.
"""

from __future__ import annotations

DOMAIN = "climate_orchestrator"

# --------------------------------------------------------------- claves ----

# NADA de "actuator_mode" ni de declarar capacidad de calor/frio a mano:
# se listan los actuadores que de verdad tiene la zona, y todo lo demas
# (que puede hacer cada uno, que hvac_modes expone la entidad final)
# se DEDUCE de ahi en climate.py — nunca una redundancia que el usuario
# tenga que mantener sincronizada el mismo a mano.
#
#   - "climate_entities": lista de climate.* YA EXISTENTES en los que
#     delegar (una valvula termostatica, un aire acondicionado con su
#     propia electronica...), tantos como se quiera. Cada uno se gobierna
#     por SUS PROPIOS hvac_modes nativos (leidos en vivo del propio
#     climate.*, no declarados aqui) — si soporta "heat", se activa en
#     "heat" cuando toca calentar; si soporta "cool", en "cool" cuando
#     toca enfriar; si soporta los dos (un equipo reversible de verdad),
#     se le manda el que corresponda cada vez, una unica orden, nunca dos
#     que se pisen. Un radiador con valvula termostatica y un aire
#     acondicionado conviven en la misma lista sin declarar nada mas.
#   - "heat_switches"/"cool_switches": listas de switch.* — a diferencia
#     de un climate.*, un switch no puede autodeclarar para que sirve, asi
#     que aqui si hace falta decir de que lado es cada uno. Tantos como se
#     quiera por lado (p.ej. dos radiadores en switches separados que
#     deben encenderse juntos).
#
# La capacidad final de la zona (heat/cool/heat_cool, y por tanto que
# hvac_modes expone el climate.* de Climate Orchestrator a Home
# Assistant/Matter/HomeKit) se calcula sola a partir de lo declarado aqui
# — ver `ClimateOrchestratorZone._compute_capability()` en climate.py.
CONF_CLIMATE_ENTITIES = "climate_entities"
CONF_HEAT_SWITCHES = "heat_switches"
CONF_COOL_SWITCHES = "cool_switches"
CONF_CURRENT_TEMP_SENSOR = "current_temp_sensor"
CONF_HUMIDITY_SENSOR = "humidity_sensor"
CONF_OUTDOOR_TEMP_SENSOR = "outdoor_temp_sensor"
CONF_WEATHER_ENTITY = "weather_entity"

# Humidificacion: entidades humidifier.* YA EXISTENTES en las que delegar
# (el humidificador fisico) — mismo espiritu que climate_entities, se
# detecta su presencia, nunca hace falta declarar "que sabe hacer" a
# mano. Funcion NATIVA de esta zona (ClimateEntityFeature.TARGET_HUMIDITY
# en el propio climate.* de la zona — target_humidity ajustable desde la
# misma tarjeta, como la temperatura), integrada en el funcionamiento
# normal de la zona (activa siempre que la zona no este apagada ni en
# pausa por puerta/ventana, sea cual sea el hvac_mode concreto — Auto,
# calor, frio... "integrada en Auto" en el sentido de "parte del
# funcionamiento automatico", no exclusiva de ese modo). Consigna UNICA
# por zona (no por preset, a diferencia de calor/frio): se trata como un
# limite mas, configurable en "Configurar" — ver CONF_MIN_TEMP/
# CONF_MAX_TEMP — aunque tambien se puede ajustar al vuelo desde la
# tarjeta del termostato, igual que la temperatura.
CONF_HUMIDIFIER_ENTITIES = "humidifier_entities"
CONF_TARGET_HUMIDITY = "target_humidity"
DEFAULT_TARGET_HUMIDITY = 45.0
DEFAULT_MIN_HUMIDITY = 20.0
DEFAULT_MAX_HUMIDITY = 80.0

# Presets con nombre en vez de horario (ver presets.py: se elimino la
# franja horaria fija a proposito — no sabe si hay alguien de verdad en
# la habitacion). "Nombre: temperatura, Nombre: temperatura..." declarado
# como texto libre, para poder tener tantos presets como se quiera
# ("Confort", "Fiesta", "Vacaciones"...) sin una lista con "+ añadir" a
# medida en el asistente. `presence_preset`/`away_preset` designan cuales
# de esos nombres usar automaticamente segun la presencia FISICA real
# (ver CONF_PRESENCE_ENTITIES mas abajo) cuando el preset activo es
# presets.PRESET_AUTO — el modo por defecto.
CONF_PRESETS_TEXT = "presets_text"
CONF_PRESENCE_PRESET = "presence_preset"
CONF_AWAY_PRESET = "away_preset"

CONF_DEADBAND = "deadband"

# Techo/suelo de seguridad de la zona: SIEMPRE se respetan, pase lo que
# pase con el preset activo o la presencia — "nunca por debajo de 12°C en
# invierno aunque no haya nadie, nunca por encima de 30°C en verano". No
# son solo limites informativos del selector de temperatura: scheduler.py
# los aplica como accion obligatoria si se cruzan.
CONF_MIN_TEMP = "min_temp"
CONF_MAX_TEMP = "max_temp"

CONF_MIN_ON_SECONDS = "min_on_seconds"
CONF_MIN_OFF_SECONDS = "min_off_seconds"
CONF_PRIORITY = "priority"                     # "confort" | "ahorro" | "manual"

# TPI (ver scheduler.py `tpi_on_percent`): duracion del ciclo dentro del
# cual un switch se enciende un % proporcional en vez de simplemente
# on/off — cuanto mas corto, mas fino el control pero mas ciclos de
# encendido/apagado (siempre respetando CONF_MIN_ON_SECONDS/
# CONF_MIN_OFF_SECONDS por debajo). Solo afecta a switches; un climate.*
# delegado ya tiene su propio control interno.
CONF_TPI_CYCLE_MINUTES = "tpi_cycle_minutes"
DEFAULT_TPI_CYCLE_MINUTES = 15

# Reposo INTELIGENTE — sin interruptor propio: coexiste solo con el modo
# mas automatico que tenga la zona (Auto en una con calor y frio de
# verdad; el unico modo que le queda a una de un solo sentido, que ya es
# "lo mas automatico" que puede ofrecer — ver la llamada en
# `_async_decide_and_act`, climate.py). En vez de simplemente apagar
# cuando ya se esta dentro de margen (ni hace falta calor ni frio), un
# climate.* delegado que TAMBIEN sepa deshumidificar o solo ventilar
# (detectado en vivo, ver `_compute_capability` en climate.py — nunca
# declarado a mano) puede aprovecharse. Deshumidificar tiene prioridad
# sobre ventilar: responde a un problema medido (humedad alta), no solo a
# comodidad. Ninguno de los dos decide nunca una temperatura ni sustituye
# a calor/frio cuando de verdad hacen falta, ni cambia el hvac_mode de la
# zona — solo la orden que recibe el delegado mientras esta en reposo. El
# umbral de humedad SI es configurable por zona, igual que cualquier otro
# limite (ver CONF_MIN_TEMP/CONF_MAX_TEMP) — nunca un numero fijo en el
# codigo.
CONF_DRY_HUMIDITY_THRESHOLD = "dry_humidity_threshold"
DEFAULT_DRY_HUMIDITY_THRESHOLD = 65.0

# Sensores de PRESENCIA FISICA de la propia zona (PIR, mmWave, radar de
# presencia...) — "¿hay alguien AHORA MISMO en esta habitacion?", no "¿esta
# alguien en casa?". Un binary_sensor de ocupacion/movimiento de la
# habitacion es la señal principal pensada aqui; person.*/device_tracker.*
# tambien se aceptan como señal adicional (utiles sobre todo para saber
# que NADIE esta en toda la casa), pero no son el caso de uso principal.
CONF_PRESENCE_ENTITIES = "presence_entities"
CONF_DOOR_WINDOW_ENTITIES = "door_window_entities"
CONF_HISTORY_DAYS_FOR_INERTIA = "history_days_for_inertia"
CONF_FORECAST_REFRESH_MINUTES = "forecast_refresh_minutes"
CONF_SIMULATE = "simulate"                     # modo simulacion: calcula y muestra, nunca actua de verdad

# Deteccion de ventana/puerta abierta SIN sensor dedicado (ver
# window_algorithm.py) — RESPALDO opcional (desactivado por defecto, no
# cambia nada en una zona existente hasta que se active a proposito) de
# CONF_DOOR_WINDOW_ENTITIES, nunca un sustituto: analiza la pendiente del
# sensor exterior y pausa la zona igual que un sensor real si detecta una
# caida/subida de temperatura anomala en contra de lo que se pide.
# Desactivado por defecto porque, a diferencia del resto de reglas de
# esta integracion, es una inferencia (no un dato medido directo) — puede
# dar algun falso positivo con corrientes de aire fuertes.
CONF_AUTO_WINDOW_DETECTION = "auto_window_detection"

# Consumo electrico — POR ACTUADOR, no por zona: una misma zona puede
# tener un aire acondicionado (maquina exterior compartida, imposible de
# instrumentar) y un radiador electrico con su propio sensor de consumo,
# y cada uno necesita su propia fuente. `actuator_power` (ver
# CONF_ACTUATOR_POWER) es un dict {entity_id: {"sensor": sensor.* opcional,
# "estimated_w": potencia fija opcional}} — se rellena desde un paso
# dinamico del asistente/"Configurar", uno por cada actuador ya declarado.
# Para el que no tenga ni sensor propio ni valor fijo, y si hay un
# CONF_HOME_POWER_SENSOR general de la vivienda declarado, se intenta
# APRENDER su consumo tipico (ver power_model.py) correlacionando sus
# transiciones on/off con el salto visto en ese sensor general —
# descartando muestras contaminadas por otras zonas activas a la vez.
# Ver `_zone_power_w`/`_async_refresh_forecast` en climate.py.
CONF_ACTUATOR_POWER = "actuator_power"
CONF_HOME_POWER_SENSOR = "home_power_sensor"

# CONF_MAX_POWER_W (opcional, a nivel de ZONA): activa una prevencion
# simple de sobrecarga — si la zona ya esta al limite (o por encima) de
# esa potencia (sumando lo que se sepa de cada actuador), no se arrancan
# NUEVOS actuadores hasta que haya margen — lo que ya estuviera encendido
# no se corta de golpe por esto, solo se evita sumar mas.
CONF_MAX_POWER_W = "max_power_w"
DEFAULT_MAX_POWER_W = 0.0  # 0 = sin limite (la prevencion de sobrecarga se desactiva)

DEFAULT_DEADBAND = 0.3
DEFAULT_MIN_TEMP = 15.0
DEFAULT_MAX_TEMP = 30.0
DEFAULT_MIN_ON_SECONDS = 300
DEFAULT_MIN_OFF_SECONDS = 300
DEFAULT_HISTORY_DAYS_FOR_INERTIA = 5  # antes 14: cada dia de mas es historico REAL que HA tiene que traer entero a memoria en una sola consulta antes de poder procesarlo (ver MAX_STATES_PER_ENTITY en thermal_model.py/power_model.py) — con MIN_VALID_RUNS=3 tramos de MIN_RUN_MINUTES=20 cada uno, 5 dias basta de sobra para una zona con uso normal; configurable si hace falta mas
DEFAULT_FORECAST_REFRESH_MINUTES = 10
DEFAULT_OUTDOOR_HORIZON_HOURS = 6  # ya no hace falta un horizonte largo (sin horario que anticipar): solo AHORRO_LOOKAHEAD_HOURS de scheduler.py + margen

# Inercia termica: valores conservadores por defecto hasta que haya
# historico suficiente (ver thermal_model.py) — nunca un numero inventado
# como si fuera real, siempre marcado con `reliable=False` hasta entonces.
DEFAULT_HEATING_RATE_DEG_H = 0.9
DEFAULT_COOLING_RATE_DEG_H = 1.2
DEFAULT_IDLE_LOSS_COEFF = 0.08

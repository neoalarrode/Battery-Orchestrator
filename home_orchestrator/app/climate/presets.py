"""
Presets con nombre, en vez de un horario fijo.

Por que se elimino el horario: una franja "07:00-23:00 = confort" no sabe
si hay alguien de verdad en la habitacion — asume una rutina fija. Los
presets, combinados con presencia REAL medida ahora mismo (nunca prevista,
seria una caja negra — ver climate.py), se adaptan a lo que de verdad esta
pasando: si vuelves antes o tarde, la zona reacciona al instante en vez de
esperar a la hora programada.

Cada preset lleva DOS consignas independientes — calor ("invierno") y
frio ("verano") — no una sola, para poder decir "nunca por debajo de 21°C
en invierno, nunca por encima de 25°C en verano" dentro del MISMO preset
"Confort", sin duplicar presets por estacion. En una zona de un solo
sentido (solo calor o solo frio) basta con declarar el lado que aplica.

Estas consignas NO se leen de aqui en directo durante la decision de cada
ciclo: `parse_presets` solo se usa para SEMBRAR las entidades number.* la
primera vez que se crea la zona (ver number.py) — a partir de ahi el
valor vivo de esas entidades manda, para poder ajustarlas desde Lovelace o
una automatizacion sin volver a "Configurar". Por eso este modulo ya no
expone la temperatura resuelta de un preset, solo su NOMBRE activo — el
valor lo busca climate.py en las entidades number.* correspondientes.

`PRESET_AUTO` es el modo por defecto: deja que Climate Orchestrator elija
solo entre el preset "con presencia" y el "sin presencia" segun la
presencia real. Elegir CUALQUIER OTRO preset a mano (termostato, voz,
Google Home, un puente Matter/HomeKit) es una eleccion PERSISTENTE que se
queda fijada hasta que vuelvas a poner "Automatico" tu mismo.

`PRESET_MANUAL` es un preset especial mas: no lo declaras tu en
`presets_text` (como "Confort" o "Ausente"), lo activa SOLO climate.py
cuando ajustas la temperatura directamente desde la tarjeta del
termostato en vez de elegir un preset — a diferencia de la version
anterior de esto (una anulacion TEMPORAL de un par de horas), pasar a
"Manual" es tan persistente como cualquier otro preset: se queda con la
temperatura que hayas puesto hasta que tu mismo cambies a otro preset o a
"Automatico". Su valor no vive en una entidad number.* (no tiene sentido,
lo pones tu directo en el termostato) — climate.py lo guarda como su
propio estado, restaurado tras un reinicio igual que el resto.
"""

from __future__ import annotations

PRESET_AUTO = "Automático"
PRESET_MANUAL = "Manual"


def parse_presets(text: str) -> list[dict]:
    """Convierte el texto declarado en el asistente en una lista de
    presets. Cada preset es "Nombre: calor/frio" (dos consignas) o
    "Nombre: temperatura" (una sola, valida para el lado que corresponda
    en zonas de un solo sentido). Ejemplo: "Confort: 21/25, Ausente:
    17/28" o, en una zona solo de calor, "Confort: 21, Ausente: 17".
    Lanza ValueError con un mensaje legible si el texto no tiene el
    formato esperado."""
    presets: list[dict] = []
    seen = set()
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            raise ValueError(f"«{chunk}» no tiene el formato «Nombre: temperatura»")
        name, temps_str = chunk.split(":", 1)
        name = name.strip()
        if not name or name in (PRESET_AUTO, PRESET_MANUAL):
            raise ValueError(f"«{name}» no es un nombre de preset valido")
        if name in seen:
            raise ValueError(f"el preset «{name}» esta repetido")
        seen.add(name)

        temps_str = temps_str.strip()
        if "/" in temps_str:
            heat_str, cool_str = temps_str.split("/", 1)
            try:
                heat_temp = float(heat_str.strip())
                cool_temp = float(cool_str.strip())
            except ValueError as e:
                raise ValueError(f"«{temps_str}» no es un par valido «calor/frio» para «{name}»") from e
            # Con calor >= frio (p.ej. "25/21" en vez de "21/25", el orden
            # invertido) la zona en Auto no tendria NINGUNA temperatura
            # que la deje tranquila: por debajo de 25 "hace falta calor",
            # por encima de 21 "hace falta frio" — las dos cosas a la vez,
            # siempre, sin importar la temperatura real. En la practica
            # eso es o bien calor y frio luchando entre si sin parar (el
            # peor derroche posible, ver `_async_execute`/mutual
            # exclusion), o un ciclado constante entre los dos — nunca un
            # estado estable. Se corta aqui, en vez de dejar que la zona
            # lo sufra en produccion.
            if heat_temp >= cool_temp:
                raise ValueError(
                    f"«{name}»: la consigna de calor ({heat_temp}°C) tiene que ser menor que la de frío "
                    f"({cool_temp}°C) — si no, Auto no encontraría nunca una temperatura que no pidiera las dos "
                    "cosas a la vez"
                )
        else:
            try:
                heat_temp = cool_temp = float(temps_str)
            except ValueError as e:
                raise ValueError(f"«{temps_str}» no es una temperatura valida para «{name}»") from e

        presets.append({"name": name, "heat_temp": heat_temp, "cool_temp": cool_temp})
    if not presets:
        raise ValueError("declara al menos un preset")
    return presets


def resolve_active_preset_name(preset_mode: str, preset_names: list[str], presence_preset: str,
                                away_preset: str, presence_now: bool | None) -> tuple[str, str]:
    """Devuelve (nombre_del_preset_activo, motivo) — la TEMPERATURA de ese
    preset se busca aparte, en las entidades number.* que lo respaldan
    (ver climate.py: `_preset_value`).

    `presence_now`: True/False si hay lectura fiable de los sensores de
    presencia FISICA declarados (ver climate.py — pensados para ser
    sensores de presencia de la propia habitacion, tipo PIR o mmWave, no
    solo "en casa"), None si no hay ninguno declarado o ninguno da un dato
    fiable ahora mismo.
    """
    if preset_mode == PRESET_MANUAL:
        return PRESET_MANUAL, "modo manual: temperatura fijada a mano"

    if preset_mode != PRESET_AUTO and preset_mode in preset_names:
        return preset_mode, f"preset «{preset_mode}» fijado a mano"

    if presence_now is None:
        return away_preset, "automático sin sensor de presencia fiable: usando el preset de ausencia"
    if presence_now:
        return presence_preset, "automático: presencia detectada en la zona"
    return away_preset, "automático: sin presencia en la zona"

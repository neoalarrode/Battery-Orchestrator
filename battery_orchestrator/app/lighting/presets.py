"""
Presets recomendados de brillo/color por tipo de estancia -- mismo
espiritu que climate/presets.py: un atajo de relleno rapido en el
formulario, NUNCA una referencia que la zona guarde. Elegir "Cocina" solo
copia estos 4 numeros a los campos min/max_brightness_pct y
min/max_color_temp_kelvin de la zona en ese momento -- a partir de ahi son
numeros normales, tan editables como si el usuario los hubiera tecleado a
mano (ver docstring de lighting/schedule.py: la zona solo sabe leer esos
4 numeros, nunca sabe ni le importa de que preset vinieron, si vinieron
de alguno).

Valores pensados como punto de partida razonable, no como verdad
absoluta -- criterio general:
  - Estancias de trabajo/tarea (cocina, despacho, baño, pasillo/entrada,
    escalera): brillo minimo mas alto (nunca demasiado tenue, hace falta
    ver bien), color mas frio en el extremo superior (mas alerta).
  - Estancias de descanso (salon, dormitorio, patio de noche): brillo
    minimo mucho mas bajo (pueden quedarse casi apagadas de ambiente),
    color mas calido en los dos extremos.
  - Dormitorio en particular: nunca demasiado brillante ni demasiado
    frio ni de dia -- apoya la higiene de sueño en vez de perseguir el
    maximo posible como el resto.
  - Escalera: el minimo mas alto de todos -- es la unica estancia donde
    "demasiado tenue" es un riesgo de seguridad, no solo de confort.
"""

from __future__ import annotations

ROOM_TYPE_PRESETS: dict[str, dict] = {
    "Cocina": {
        "min_brightness_pct": 40, "max_brightness_pct": 100,
        "min_color_temp_kelvin": 2400, "max_color_temp_kelvin": 5000,
    },
    "Salón": {
        "min_brightness_pct": 15, "max_brightness_pct": 90,
        "min_color_temp_kelvin": 2200, "max_color_temp_kelvin": 4500,
    },
    "Dormitorio": {
        "min_brightness_pct": 10, "max_brightness_pct": 70,
        "min_color_temp_kelvin": 2000, "max_color_temp_kelvin": 3500,
    },
    "Baño": {
        "min_brightness_pct": 35, "max_brightness_pct": 100,
        "min_color_temp_kelvin": 2400, "max_color_temp_kelvin": 4500,
    },
    "Despacho": {
        "min_brightness_pct": 30, "max_brightness_pct": 100,
        "min_color_temp_kelvin": 2700, "max_color_temp_kelvin": 5000,
    },
    "Pasillo / Entrada": {
        "min_brightness_pct": 40, "max_brightness_pct": 90,
        "min_color_temp_kelvin": 2400, "max_color_temp_kelvin": 4500,
    },
    "Exterior / Patio": {
        "min_brightness_pct": 20, "max_brightness_pct": 100,
        "min_color_temp_kelvin": 2200, "max_color_temp_kelvin": 4500,
    },
    "Escalera": {
        "min_brightness_pct": 50, "max_brightness_pct": 100,
        "min_color_temp_kelvin": 2700, "max_color_temp_kelvin": 4500,
    },
    # Sin autofill -- deja lo que ya haya en el formulario (o los valores
    # por defecto de schedule.py en una zona nueva) tal cual, para quien
    # prefiera partir de cero y decidir los 4 numeros el mismo.
    "Manual": {},
}


def list_presets() -> list[dict]:
    return [{"name": name, **values} for name, values in ROOM_TYPE_PRESETS.items()]

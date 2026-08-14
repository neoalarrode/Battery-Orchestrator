"""
Deteccion de puerta/ventana abierta SIN sensor dedicado, a partir de la
pendiente de temperatura del sensor EXTERNO de la zona -- inspirado en
versatile_thermostat (jmcollin78), generalizado aqui a calor Y frio: una
ventana abierta en pleno invierno enfria de golpe (pendiente negativa
fuerte mientras se pide calor); en pleno verano con el aire acondicionado
puesto, calienta de golpe (pendiente positiva fuerte mientras se pide
frio) -- en ambos casos, mas alla de lo que la inercia normal de la zona
explicaria.

Nada de machine learning: una pendiente suavizada (dos pesos fijos, 0.2/
0.8, el mismo esquema que versatile_thermostat) mas dos umbrales en texto
plano -- "abre" cuando la pendiente cruza el umbral de alerta EN CONTRA
de lo que se esta pidiendo, "cierra" cuando vuelve a un valor razonable.
Pensado como RESPALDO para ventanas sin sensor fisico, opcional (ver
CONF_AUTO_WINDOW_DETECTION en const.py) -- nunca sustituye a un sensor
real declarado, solo se suma.
"""

from __future__ import annotations

ALERT_SLOPE_DEG_H = 4.0        # °C/h EN CONTRA de lo pedido que dispara la alerta
END_ALERT_SLOPE_DEG_H = 1.0    # por debajo de esto (en valor absoluto) se da por cerrada otra vez
MAX_PLAUSIBLE_JUMP_DEG = 2.0   # salto entre dos lecturas mayor que esto se descarta (glitch del sensor, no ventana)
MIN_SAMPLES = 3                # lecturas minimas antes de fiarse de la pendiente


class WindowSlopeDetector:
    """Un detector por zona. `update()` se llama en cada ciclo de
    decision con la lectura EXTERNA actual; devuelve si la alerta esta
    activa ahora mismo."""

    def __init__(self) -> None:
        self._slope = 0.0
        self._last_temp: float | None = None
        self._last_ts = None
        self._samples = 0
        self._alert = False

    def update(self, temp: float, now, wants_heat: bool, wants_cool: bool) -> bool:
        if self._last_temp is not None and self._last_ts is not None:
            dt_h = (now - self._last_ts).total_seconds() / 3600
            jump = abs(temp - self._last_temp)
            if dt_h > 0 and jump <= MAX_PLAUSIBLE_JUMP_DEG:
                raw_slope = (temp - self._last_temp) / dt_h
                self._slope = 0.2 * self._slope + 0.8 * raw_slope
                self._samples += 1
        self._last_temp, self._last_ts = temp, now

        if self._samples < MIN_SAMPLES:
            return self._alert

        against_heat = wants_heat and self._slope <= -ALERT_SLOPE_DEG_H
        against_cool = wants_cool and self._slope >= ALERT_SLOPE_DEG_H
        if against_heat or against_cool:
            self._alert = True
        elif abs(self._slope) < END_ALERT_SLOPE_DEG_H:
            self._alert = False
        return self._alert

    @property
    def slope_deg_h(self) -> float:
        return round(self._slope, 2)

"""
Media movil exponencial (EMA) simple, para suavizar la lectura del sensor
de temperatura externo de una zona -- un pico de ruido puntual del sensor
no debe hacer que el motor decida algo distinto de golpe.

Nada de machine learning: un unico numero (el valor suavizado) que se
actualiza cada vez que llega una lectura nueva, ponderando mas lo
reciente que lo antiguo segun cuanto tiempo ha pasado (vida media
configurable). Ademas de suavizar, `age_seconds()` deja saber cuanto hace
que no llega una lectura nueva de verdad -- lo usa climate.py para el
margen de gracia si el sensor se queda "congelado" (ver
STALE_SENSOR_*_SECONDS en climate.py).
"""

from __future__ import annotations


class Ema:
    def __init__(self, halflife_seconds: float, max_alpha: float = 0.5, precision: int = 2) -> None:
        self._halflife = max(1.0, halflife_seconds)
        self._max_alpha = max_alpha
        self._precision = precision
        self._value: float | None = None
        self._last_ts = None

    def update(self, value: float, now) -> float:
        if self._value is None or self._last_ts is None:
            self._value = value
        else:
            dt = max(0.0, (now - self._last_ts).total_seconds())
            # alpha basado en vida media: una lectura de hace `halflife`
            # segundos pesa la mitad que una de ahora mismo. Recortado a
            # `max_alpha` para que un hueco muy largo sin lecturas no
            # descarte de golpe todo el historico suavizado -- se
            # actualiza gradualmente, nunca de un salto.
            alpha = min(self._max_alpha, 1 - 0.5 ** (dt / self._halflife)) if dt > 0 else 0.0
            self._value = self._value + alpha * (value - self._value)
        self._last_ts = now
        return self.value

    @property
    def value(self) -> float | None:
        return round(self._value, self._precision) if self._value is not None else None

    def age_seconds(self, now) -> float | None:
        return (now - self._last_ts).total_seconds() if self._last_ts is not None else None

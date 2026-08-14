"""
Puente entre `tuya_lan.py` (100% asyncio, portado tal cual desde Tuya
Orchestrator) y el resto de Home Orchestrator (100% hilos sincronos). Una
sola instancia para TODOS los dispositivos Tuya del plugin -- un unico
event loop de asyncio en su propio hilo, no uno por dispositivo.

Deliberadamente SIN ningun patron reactivo propio: `TuyaLocalDevice` ya
empuja los cambios de DP por si solo (protocolo LAN push, via el callback
`on_update`) -- lo unico que hace falta aqui es guardar ese ultimo valor
de forma segura entre hilos y, opcionalmente, avisar a quien le interese
(`on_any_change`) de que algo cambio. Decidir qué hacer con ese cambio
(replanificar una zona de Climate, publicar por MQTT...) no es trabajo de
este modulo -- lo reactivo de verdad ya vive en quien consume esto
(`ClimatePlugin` ya tiene su propio `ReactiveTrigger`, ver climate_plugin.py).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Callable

from .discovery import DiscoveredDevice, PersistentDiscovery
from .profile import ClimateMapping, DeviceProfile, parse_profile
from .tuya_lan import TuyaLocalDevice

log = logging.getLogger("tuya.device_manager")

DEFAULT_CALL_TIMEOUT_SECONDS = 10
RECONNECT_CHECK_INTERVAL_SECONDS = 30

# Historial LOCAL por (device_id, dp_id) -- para que thermal_model.py
# pueda aprender la inercia termica de un actuador consumido
# INTERNAMENTE (sin pasar por HA, asi que sin historico en su recorder).
# Mismos limites de sensatez que ya usa thermal_model.py para el
# historico de HA (ver MAX_STATES_PER_ENTITY ahi): un limite duro por
# cuenta de puntos Y una edad maxima, para que esto nunca crezca sin fin
# en un proceso que vive dias/semanas seguidas.
HISTORY_MAX_POINTS_PER_DP = 5000
HISTORY_MAX_AGE_DAYS = 14


class TuyaDeviceManager:
    """`on_any_change(device_id)`, si se da, se llama desde el hilo del
    event loop cada vez que llega un DP nuevo de CUALQUIER dispositivo --
    un unico hook simple, no una cola de eventos ni un sistema de
    suscripcion por dispositivo (no hace falta mas que eso hoy)."""

    def __init__(self, on_any_change: Callable[[str], None] | None = None) -> None:
        self._on_any_change = on_any_change
        self._devices: dict[str, TuyaLocalDevice] = {}
        self._profiles: dict[str, DeviceProfile] = {}
        self._state: dict[str, dict[int, Any]] = {}
        # (device_id, dp_id) -> [(epoch_ts, raw_value), ...], en orden
        # cronologico. Alimenta thermal_model.py para actuadores/sensores
        # consumidos internamente (sin historico en el recorder de HA) --
        # ver get_actuator_history()/get_sensor_history() mas abajo.
        self._dp_history: dict[tuple[str, int], list[tuple[float, Any]]] = {}
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_ready = threading.Event()
        self._thread: threading.Thread | None = None
        # Escucha PERSISTENTE de los broadcasts LAN (no una ventana de
        # unos segundos por consulta) -- mismo motivo que el proyecto
        # original: un dispositivo que emite en un intervalo irregular
        # puede no caer dentro de una ventana corta, pero casi nunca se
        # pierde una escucha que esta abierta ~100% del tiempo. Puramente
        # informativo hasta que el usuario decide añadir uno (ver
        # get_discovered_devices()) -- nunca añade nada por su cuenta.
        self._discovery = PersistentDiscovery()

    # ------------------------------------------------------------ arranque

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run_loop, name="tuya-loop", daemon=True)
        self._thread.start()
        self._loop_ready.wait(timeout=5)

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._loop_ready.set()
        loop.create_task(self._reconnect_loop())
        loop.create_task(self._discovery.start())
        loop.run_forever()

    def get_discovered_devices(self) -> list[DiscoveredDevice]:
        """Snapshot de lo visto por broadcast LAN hasta ahora -- solo
        device_id/ip/product_key/version (el broadcast NUNCA lleva el
        local_key; eso solo lo da la nube de Tuya al vincular una cuenta,
        ver tuya_cloud.py). Puramente informativo: el usuario decide, uno
        a uno, si añade alguno (ver tuya_plugin.py)."""
        return list(self._discovery.devices.values())

    async def _reconnect_loop(self) -> None:
        """Mismo criterio que el __init__.py original: cualquier
        dispositivo que se haya quedado desconectado se reintenta solo,
        respetando su propio backoff (seconds_until_retry) -- nunca a
        martillazos."""
        while True:
            await asyncio.sleep(RECONNECT_CHECK_INTERVAL_SECONDS)
            for device_id, device in list(self._devices.items()):
                if device.connected or device.seconds_until_retry() > 0:
                    continue
                try:
                    await device.connect()
                except Exception:
                    log.debug("Tuya %s: reintento de conexion fallido", device_id, exc_info=True)

    def _run_coro(self, coro, timeout: float = DEFAULT_CALL_TIMEOUT_SECONDS):
        if self._loop is None:
            raise RuntimeError("TuyaDeviceManager.start() no se ha llamado todavia")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    # --------------------------------------------------------- dispositivos

    def add_device(
        self, device_id: str, address: str, local_key: str,
        protocol_version: str, profile_yaml: str,
    ) -> None:
        profile = parse_profile(profile_yaml)

        def _on_update(dps: dict[int, Any]) -> None:
            with self._lock:
                self._state.setdefault(device_id, {}).update(dps)
                self._record_dp_history(device_id, dps)
            if self._on_any_change:
                try:
                    self._on_any_change(device_id)
                except Exception:
                    log.exception("Fallo en on_any_change para %s", device_id)

        device = TuyaLocalDevice(
            device_id=device_id, address=address, local_key=local_key,
            protocol_version=protocol_version, on_update=_on_update,
        )
        device.add_dps_to_request(profile.all_dp_ids())

        self._devices[device_id] = device
        self._profiles[device_id] = profile
        self._state.setdefault(device_id, {})

        self._run_coro(self._connect_and_prime(device_id))

    def _record_dp_history(self, device_id: str, dps: dict[int, Any]) -> None:
        """Llamar SIEMPRE con `self._lock` ya tomado -- comparte
        estructura con `self._state`, mismo criterio de bloqueo. Poda por
        cuenta Y por edad en cada escritura -- barato (listas ya cortas
        por el propio tope) y evita depender de un hilo de limpieza
        aparte."""
        now = time.time()
        cutoff = now - HISTORY_MAX_AGE_DAYS * 86400
        for dp_id, value in dps.items():
            key = (device_id, dp_id)
            points = self._dp_history.setdefault(key, [])
            points.append((now, value))
            if len(points) > HISTORY_MAX_POINTS_PER_DP or (points and points[0][0] < cutoff):
                self._dp_history[key] = [p for p in points if p[0] >= cutoff][-HISTORY_MAX_POINTS_PER_DP:]

    async def _connect_and_prime(self, device_id: str) -> None:
        device = self._devices[device_id]
        await device.connect()
        dps = await device.status()
        with self._lock:
            self._state.setdefault(device_id, {}).update(dps)
            self._record_dp_history(device_id, dps)
        if self._on_any_change:
            self._on_any_change(device_id)

    def remove_device(self, device_id: str) -> None:
        device = self._devices.pop(device_id, None)
        self._profiles.pop(device_id, None)
        with self._lock:
            self._state.pop(device_id, None)
        if device is not None:
            try:
                self._run_coro(device.close())
            except Exception:
                log.exception("Fallo cerrando la conexion a %s", device_id)

    def connected(self, device_id: str) -> bool:
        device = self._devices.get(device_id)
        return bool(device and device.connected)

    def profile(self, device_id: str) -> DeviceProfile | None:
        return self._profiles.get(device_id)

    # ------------------------------------------------------------- lectura

    def get_raw(self, device_id: str, dp_id: int) -> Any:
        with self._lock:
            return self._state.get(device_id, {}).get(dp_id)

    def get_decoded(self, device_id: str, dp_id: int) -> Any:
        profile = self._profiles.get(device_id)
        raw = self.get_raw(device_id, dp_id)
        if profile is None:
            return raw
        mapping = next((d for d in profile.dps if d.dp_id == dp_id), None)
        return mapping.decode(raw) if mapping else raw

    def get_actuator_history(self, device_id: str, climate_index: int, days: int) -> list[dict]:
        """Historico on/off de un termostato Tuya consumido internamente,
        en la MISMA forma que climate/thermal_model.py ya espera del
        recorder de HA (`[{"state": "on"|"off", "last_updated": epoch}]`)
        -- usa el `switch_dp` del bloque `climates:` como señal de
        "actuando" (es lo unico que un perfil Tuya declara de forma
        fiable como on/off del propio termostato; no hay un `hvac_action`
        de ciclado interno que traducir, a diferencia de un climate.* de
        HA). Sin `switch_dp` en el perfil, no hay nada que aprender de
        este actuador -- lista vacia, nunca un dato inventado."""
        profile = self._profiles.get(device_id)
        if profile is None or climate_index >= len(profile.climates):
            return []
        switch_dp = profile.climates[climate_index].switch_dp
        if switch_dp is None:
            return []
        cutoff = time.time() - days * 86400
        with self._lock:
            points = list(self._dp_history.get((device_id, switch_dp), []))
        return [{"state": "on" if bool(v) else "off", "last_updated": ts} for ts, v in points if ts >= cutoff]

    # ------------------------------------------------------------ escritura

    def set_dp(self, device_id: str, dp_id: int, raw_value: Any, timeout: float = DEFAULT_CALL_TIMEOUT_SECONDS) -> None:
        device = self._devices.get(device_id)
        if device is None:
            raise KeyError(f"dispositivo Tuya desconocido: {device_id}")
        self._run_coro(device.set_dps({dp_id: raw_value}), timeout=timeout)
        with self._lock:
            self._state.setdefault(device_id, {})[dp_id] = raw_value
            self._record_dp_history(device_id, {dp_id: raw_value})

    # ---------------------------------------------------- fachada climate

    def climate_handle(self, device_id: str, climate_index: int = 0) -> "TuyaClimateHandle | None":
        profile = self._profiles.get(device_id)
        if profile is None or climate_index >= len(profile.climates):
            return None
        return TuyaClimateHandle(self, device_id, profile.climates[climate_index])


class TuyaClimateHandle:
    """Fachada minima para que ZoneRunner controle un `climates:` de un
    perfil Tuya como si fuera un actuador mas -- mismos campos/metodos que
    ya lee/llama de un climate.* de HA, pero resueltos EN EL MISMO
    PROCESO contra `TuyaDeviceManager`, sin pasar por ha_websocket.py."""

    def __init__(self, manager: TuyaDeviceManager, device_id: str, mapping: ClimateMapping) -> None:
        self._manager = manager
        self._device_id = device_id
        self._mapping = mapping

    @property
    def available(self) -> bool:
        return self._manager.connected(self._device_id)

    @property
    def current_temperature(self) -> float | None:
        m = self._mapping
        if m.current_temp_dp is None:
            return None
        raw = self._manager.get_decoded(self._device_id, m.current_temp_dp)
        if raw is None:
            return None
        return raw / m.current_temp_scale if m.current_temp_scale else raw

    @property
    def target_temperature(self) -> float | None:
        m = self._mapping
        if m.target_temp_dp is None:
            return None
        raw = self._manager.get_decoded(self._device_id, m.target_temp_dp)
        if raw is None:
            return None
        return raw / m.target_temp_scale if m.target_temp_scale else raw

    @property
    def hvac_mode(self) -> str:
        m = self._mapping
        if m.switch_dp is not None and not self._manager.get_decoded(self._device_id, m.switch_dp):
            return "off"
        if m.mode_dp is not None and m.mode_map:
            raw = self._manager.get_decoded(self._device_id, m.mode_dp)
            return m.mode_map.get(raw, "heat")
        return "heat"

    def set_temperature(self, value: float) -> None:
        m = self._mapping
        if m.target_temp_dp is None:
            return
        raw = round(value * m.target_temp_scale) if m.target_temp_scale else value
        self._manager.set_dp(self._device_id, m.target_temp_dp, raw)

    def set_hvac_mode(self, hvac_mode: str) -> None:
        m = self._mapping
        if m.switch_dp is not None:
            self._manager.set_dp(self._device_id, m.switch_dp, hvac_mode != "off")
        if hvac_mode == "off":
            return
        if m.mode_dp is not None and m.mode_map:
            reverse = {v: k for k, v in m.mode_map.items()}
            raw = reverse.get(hvac_mode)
            if raw is not None:
                self._manager.set_dp(self._device_id, m.mode_dp, raw)

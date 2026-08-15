"""
Puente entre `python-kasa` (100% asyncio, la MISMA libreria que usa el
`tplink` de Home Assistant -- ver `kasa.Discover`/`kasa.Device`, no una
reimplementacion propia del protocolo como con Tuya) y el resto de Home
Orchestrator (100% hilos sincronos). Una sola instancia para TODOS los
dispositivos TP-Link del plugin -- un unico event loop de asyncio en su
propio hilo, mismo patron que `tuya/device_manager.py`.

DIFERENCIA REAL DE ARQUITECTURA frente a Tuya: el protocolo LAN de Tuya
empuja los cambios de DP solo (push, ver `on_update` de `tuya_lan.py`).
TP-Link/Kasa NO empuja nada -- hay que preguntar (`device.update()`)
para tener un estado fresco, exactamente igual que hace el propio
`TPLinkDataUpdateCoordinator` de Home Assistant (`coordinator.py` del
componente real: `timedelta(seconds=5)`, ver comentario ahi mismo). Este
modulo hace lo mismo: un bucle de sondeo cada `POLL_INTERVAL_SECONDS`
(mismo valor que HA), no un callback reactivo de verdad.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Callable

from kasa import AuthenticationError, Credentials, Device, Discover, KasaException, Module

log = logging.getLogger("tplink.device_manager")

DEFAULT_CALL_TIMEOUT_SECONDS = 10
POLL_INTERVAL_SECONDS = 5  # igual que TPLinkDataUpdateCoordinator de HA


class TplinkDeviceManager:
    """`on_any_change(device_id)`, si se da, se llama desde el hilo del
    event loop tras cada sondeo CON EXITO de cualquier dispositivo -- un
    unico hook simple, igual que TuyaDeviceManager (no hay push real que
    distinguir "cambio de verdad" de "sondeo sin novedad", asi que aqui
    se avisa siempre que el sondeo responde)."""

    def __init__(self, on_any_change: Callable[[str], None] | None = None) -> None:
        self._on_any_change = on_any_change
        self._devices: dict[str, Device] = {}
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_ready = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ arranque

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run_loop, name="tplink-loop", daemon=True)
        self._thread.start()
        self._loop_ready.wait(timeout=5)

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._loop_ready.set()
        loop.create_task(self._poll_loop())
        loop.run_forever()

    def _run_coro(self, coro, timeout: float = DEFAULT_CALL_TIMEOUT_SECONDS):
        if self._loop is None:
            raise RuntimeError("TplinkDeviceManager.start() no se ha llamado todavia")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            for device_id, device in list(self._devices.items()):
                try:
                    await device.update()
                except AuthenticationError:
                    log.warning(
                        "TP-Link %s: credencial rechazada al sondear -- revisa la cuenta "
                        "TP-Link/Tapo declarada en la pagina del plugin", device_id,
                    )
                    continue
                except KasaException:
                    log.debug("TP-Link %s: fallo sondeando (dispositivo apagado/sin red?)", device_id, exc_info=True)
                    continue
                if self._on_any_change:
                    try:
                        self._on_any_change(device_id)
                    except Exception:
                        log.exception("Fallo en on_any_change para %s", device_id)

    # --------------------------------------------------------- descubrimiento

    async def _discover(self, credentials: Credentials | None) -> dict[str, dict]:
        found = await Discover.discover(credentials=credentials, discovery_timeout=8)
        async def _describe(host: str, device) -> tuple[str, dict | None]:
            try:
                # BUG REAL, confirmado en produccion: el objeto que
                # devuelve el broadcast SOLO trae `_discovery_info` (el
                # paquete crudo de anuncio) sin parsear -- `alias`/
                # `model`/`device_type` estan vacios/rompen hasta que se
                # llama a `update()` de verdad (mismo paso que hace
                # `discover_single` por dentro, que por eso SI funcionaba
                # ya para añadir un dispositivo por IP). Sin este
                # `update()`, escanear devolvia SIEMPRE `KeyError` en
                # `device_type` para TODOS los dispositivos, incluido uno
                # que se sabia soportado (verificado contra la bombilla
                # real del usuario).
                await device.update()
                return host, {
                    "alias": device.alias,
                    "model": device.model,
                    "device_type": str(device.device_type),
                    "needs_auth": not device.alias,
                }
            except Exception:
                # Una camara Tapo (SMART.IPCAMERA) responde al broadcast
                # pero python-kasa no la soporta de verdad (esa API es
                # completamente distinta a la de enchufes/bombillas) --
                # se descarta aqui en vez de tirar todo el escaneo abajo
                # por un solo dispositivo que nunca iba a poder añadirse
                # de todos modos.
                log.debug("TP-Link: dispositivo en %s no soportado (¿camara Tapo?), se omite del escaneo", host, exc_info=True)
                return host, None
            finally:
                try:
                    await device.disconnect()
                except Exception:
                    pass

        # BUG REAL, confirmado en produccion: describir cada dispositivo
        # UNO A UNO (update+disconnect secuencial) sobre una red con mas
        # de una decena de respuestas superaba de sobra el timeout total
        # de `_run_coro` (`TimeoutError`, escaneo entero perdido pese a
        # que cada dispositivo individual responde en menos de 1s) -- en
        # paralelo, con `asyncio.gather`, el tiempo total es el del MAS
        # LENTO, no la suma de todos.
        results = await asyncio.gather(*(_describe(host, device) for host, device in found.items()))
        return {host: info for host, info in results if info is not None}

    def discover(self, credentials: Credentials | None) -> dict[str, dict]:
        return self._run_coro(self._discover(credentials), timeout=30)

    # --------------------------------------------------------- dispositivos

    async def _discover_and_connect(self, host: str, credentials: Credentials | None) -> Device:
        # `discover_single` (no `connect()` directo) porque, igual que
        # hace Home Assistant, es lo que resuelve SOLO el protocolo real
        # del dispositivo (Kasa clasico / KLAP / AES-Tapo) sin que quien
        # llama tenga que saber de antemano cual es -- ver
        # `Discover.discover_single` de python-kasa.
        device = await Discover.discover_single(host, credentials=credentials, discovery_timeout=10)
        await device.update()
        return device

    def add_device(self, device_id: str, host: str, credentials: Credentials | None = None) -> None:
        device = self._run_coro(self._discover_and_connect(host, credentials), timeout=15)
        with self._lock:
            self._devices[device_id] = device

    def remove_device(self, device_id: str) -> None:
        with self._lock:
            self._devices.pop(device_id, None)

    def get_device(self, device_id: str) -> Device | None:
        return self._devices.get(device_id)

    def connected(self, device_id: str) -> bool:
        # python-kasa no expone un "connected" persistente como el LAN
        # push de Tuya -- el ultimo sondeo con exito ES la señal de
        # disponibilidad (si el sondeo lleva fallando, `_poll_loop` ya lo
        # habria quitado... salvo que no lo quita, se queda intentando --
        # ver nota mas abajo). De momento: "esta dado de alta" basta.
        return device_id in self._devices

    # ------------------------------------------------------------ escritura

    async def _turn_on(self, device_id: str, brightness_pct: float | None, color_temp_kelvin: float | None, hs: tuple[float, float] | None) -> None:
        device = self._devices.get(device_id)
        if device is None:
            raise KeyError(f"dispositivo TP-Link desconocido: {device_id}")
        light = device.modules.get(Module.Light)
        if light is None:
            await device.turn_on()
            return
        # Mismo orden de prioridad que `light.py` real de HA
        # (`async_turn_on`): color_temp o hsv PRIMERO (ya incluyen el
        # brillo si se declara), si no hay ninguno de los dos se cae a
        # solo ajustar brillo/encender.
        if color_temp_kelvin is not None and light.is_variable_color_temp:
            lo, hi = light.valid_temperature_range
            clamped = max(lo, min(hi, round(color_temp_kelvin)))
            await light.set_color_temp(clamped, brightness=_pct(brightness_pct))
        elif hs is not None and light.is_color:
            hue, sat = round(hs[0]), round(hs[1])
            await light.set_hsv(hue, sat, _pct(brightness_pct))
        elif brightness_pct is not None and light.is_dimmable:
            await light.set_brightness(_pct(brightness_pct))
        else:
            await device.turn_on()

    async def _turn_off(self, device_id: str) -> None:
        device = self._devices.get(device_id)
        if device is None:
            raise KeyError(f"dispositivo TP-Link desconocido: {device_id}")
        await device.turn_off()

    def turn_on(self, device_id: str, brightness_pct: float | None = None,
                color_temp_kelvin: float | None = None, hs: tuple[float, float] | None = None) -> None:
        self._run_coro(self._turn_on(device_id, brightness_pct, color_temp_kelvin, hs))

    def turn_off(self, device_id: str) -> None:
        self._run_coro(self._turn_off(device_id))

    # ------------------------------------------------------- fachada light

    def light_handle(self, device_id: str) -> "TplinkLightHandle | None":
        device = self._devices.get(device_id)
        if device is None or device.modules.get(Module.Light) is None:
            return None
        return TplinkLightHandle(self, device_id)


def _pct(value: float | None) -> int | None:
    return round(value) if value is not None else None


class TplinkLightHandle:
    """Fachada minima para que Lighting controle una bombilla TP-Link EN
    EL MISMO PROCESO -- mismo contrato que `tuya.device_manager.
    TuyaLightHandle` (available/is_on/brightness_pct/color_temp_kelvin/
    turn_on/turn_off), para que `lighting/zone_runner.py` no necesite
    saber de que marca es el bridge al que esta hablando."""

    def __init__(self, manager: TplinkDeviceManager, device_id: str) -> None:
        self._manager = manager
        self._device_id = device_id

    def _device(self) -> Device | None:
        return self._manager.get_device(self._device_id)

    def _light(self):
        device = self._device()
        return device.modules.get(Module.Light) if device else None

    @property
    def available(self) -> bool:
        return self._manager.connected(self._device_id)

    @property
    def is_on(self) -> bool:
        device = self._device()
        return bool(device and device.is_on)

    @property
    def brightness_pct(self) -> float | None:
        light = self._light()
        return float(light.brightness) if light and light.is_dimmable else None

    @property
    def color_temp_kelvin(self) -> int | None:
        light = self._light()
        return int(light.color_temp) if light and light.is_variable_color_temp else None

    def turn_on(self, brightness_pct: float | None = None, color_temp_kelvin: float | None = None) -> None:
        self._manager.turn_on(self._device_id, brightness_pct=brightness_pct, color_temp_kelvin=color_temp_kelvin)

    def turn_off(self) -> None:
        self._manager.turn_off(self._device_id)

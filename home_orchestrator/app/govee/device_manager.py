"""
Puente LAN con bombillas Govee -- protocolo UDP no oficial pero bien
documentado por la comunidad (el mismo que usa la via "LAN" de
govee2mqtt de wez, ver https://github.com/wez/govee2mqtt/blob/main/docs/
LAN.md, y proyectos hermanos como govee-lan-hass) -- SOLO la via local,
a proposito. govee2mqtt en si combina TRES canales (LAN, AWS IoT con
email/contraseña de la cuenta -- protocolo NO documentado --, y la API
REST oficial que exige pedir una API key al fabricante); ninguno de los
dos ultimos encaja con el "sin cajas negras" del resto de Home
Orchestrator -- mismo criterio que ya se aplico a Tuya (LAN unicamente,
nunca la nube del fabricante). Un dispositivo sin la "Govee LAN API"
activada en la app oficial (ajuste por dispositivo) simplemente no
respondera aqui -- no hay forma de rodear eso sin la nube.

Puertos (fijos, del propio protocolo, no configurables):
  - 4001: el dispositivo ESCUCHA aqui el mensaje de "scan" (enviado por
    multicast a 239.255.255.250).
  - 4002: el CLIENTE escucha aqui -- el dispositivo responde tanto al
    scan como a cualquier consulta de estado a este puerto, por unicast.
  - 4003: el dispositivo ESCUCHA aqui los comandos de control (turn/
    brightness/colorwc) y las consultas de estado (devStatus), enviados
    por unicast (el multicast solo hace falta para el descubrimiento).

A diferencia de TP-Link (donde `python-kasa` ya habla el protocolo real)
aqui no hay libreria de terceros en Python -- se reimplementa el JSON
crudo, mismo espiritu que `tuya/tuya_lan.py`. A diferencia de Tuya (LAN
con PUSH real de cambios), Govee tampoco empuja nada por su cuenta salvo
en respuesta a un `devStatus` -- de ahi el sondeo periodico, mismo
patron que `tplink/device_manager.py`.
"""

from __future__ import annotations

import colorsys
import json
import logging
import socket
import threading
import time
from typing import Callable

log = logging.getLogger("govee.device_manager")

MULTICAST_IP = "239.255.255.250"
SCAN_PORT = 4001
LISTEN_PORT = 4002
CONTROL_PORT = 4003

POLL_INTERVAL_SECONDS = 5
STALE_AFTER_SECONDS = 20  # sin devStatus en este margen, se considera "sin conexion"
SCAN_WINDOW_SECONDS = 4

MIN_KELVIN, MAX_KELVIN = 2000, 9000


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _hs_to_rgb(hue: float, sat: float) -> tuple[int, int, int]:
    """HS (grados/porcentaje, escala de HA) -> RGB 0-255 con V=100 fijo --
    el propio brillo de Govee se manda por separado (`brightness`), igual
    que `light.hs_color` de HA nunca mezcla el value de HSV con el
    brillo, son dos campos independientes."""
    r, g, b = colorsys.hsv_to_rgb(hue / 360, sat / 100, 1.0)
    return round(r * 255), round(g * 255), round(b * 255)


class GoveeDeviceManager:
    """`on_any_change(device_id)`, si se da, se llama desde el hilo
    receptor cada vez que llega un `devStatus` de un dispositivo dado de
    alta -- mismo contrato simple que `TplinkDeviceManager`/
    `TuyaDeviceManager` (no hay forma de distinguir "cambio de verdad" de
    "sondeo sin novedad", asi que se avisa siempre que responde)."""

    def __init__(self, on_any_change: Callable[[str], None] | None = None) -> None:
        self._on_any_change = on_any_change
        self._lock = threading.RLock()
        self._devices: dict[str, dict] = {}  # device_id -> {"ip", "status", "last_seen"}
        self._ip_to_id: dict[str, str] = {}
        self._sock: socket.socket | None = None
        self._scan_lock = threading.Lock()
        self._scan_results: dict[str, dict] | None = None  # None = ningun escaneo en curso ahora mismo

    # ------------------------------------------------------------ arranque

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", LISTEN_PORT))
        self._sock = sock
        threading.Thread(target=self._recv_loop, name="govee-recv", daemon=True).start()
        threading.Thread(target=self._poll_loop, name="govee-poll", daemon=True).start()

    def _recv_loop(self) -> None:
        while True:
            try:
                raw, addr = self._sock.recvfrom(4096)
            except OSError:
                log.exception("Govee: fallo leyendo del socket UDP -- se detiene el receptor")
                return
            try:
                msg = json.loads(raw.decode("utf-8")).get("msg") or {}
            except (ValueError, UnicodeDecodeError):
                continue
            cmd = msg.get("cmd")
            data = msg.get("data") or {}
            if cmd == "scan":
                self._on_scan_response(addr[0], data)
            elif cmd == "devStatus":
                self._on_status_response(addr[0], data)

    def _on_scan_response(self, ip: str, data: dict) -> None:
        device = data.get("device")
        if not device:
            return
        with self._scan_lock:
            if self._scan_results is not None:
                self._scan_results[device] = {"ip": ip, "sku": data.get("sku"), "device": device}

    def _on_status_response(self, ip: str, data: dict) -> None:
        with self._lock:
            device_id = self._ip_to_id.get(ip)
            if device_id is None:
                return
            self._devices[device_id]["status"] = data
            self._devices[device_id]["last_seen"] = time.time()
        if self._on_any_change:
            try:
                self._on_any_change(device_id)
            except Exception:
                log.exception("Fallo en on_any_change para %s", device_id)

    def _poll_loop(self) -> None:
        while True:
            time.sleep(POLL_INTERVAL_SECONDS)
            with self._lock:
                ips = [d["ip"] for d in self._devices.values()]
            for ip in ips:
                self._send(ip, "devStatus", {})

    # --------------------------------------------------------- descubrimiento

    def discover(self, timeout: float = SCAN_WINDOW_SECONDS) -> list[dict]:
        with self._scan_lock:
            self._scan_results = {}
        self._send(MULTICAST_IP, "scan", {"account_topic": "reserve"})
        time.sleep(timeout)
        with self._scan_lock:
            results = list(self._scan_results.values())
            self._scan_results = None
        return results

    # --------------------------------------------------------- dispositivos

    def add_device(self, device_id: str, ip: str) -> None:
        with self._lock:
            self._devices[device_id] = {"ip": ip, "status": None, "last_seen": 0.0}
            self._ip_to_id[ip] = device_id
        self._send(ip, "devStatus", {})

    def remove_device(self, device_id: str) -> None:
        with self._lock:
            info = self._devices.pop(device_id, None)
            if info:
                self._ip_to_id.pop(info["ip"], None)

    def connected(self, device_id: str) -> bool:
        with self._lock:
            info = self._devices.get(device_id)
            if not info or info["status"] is None:
                return False
            return (time.time() - info["last_seen"]) < STALE_AFTER_SECONDS

    def get_status(self, device_id: str) -> dict | None:
        with self._lock:
            info = self._devices.get(device_id)
            return dict(info["status"]) if info and info["status"] else None

    def _ip_of(self, device_id: str) -> str | None:
        with self._lock:
            info = self._devices.get(device_id)
            return info["ip"] if info else None

    # ------------------------------------------------------------ escritura

    def _send(self, ip: str, cmd: str, data: dict) -> None:
        if self._sock is None:
            return
        payload = json.dumps({"msg": {"cmd": cmd, "data": data}}).encode("utf-8")
        port = SCAN_PORT if ip == MULTICAST_IP else CONTROL_PORT
        try:
            self._sock.sendto(payload, (ip, port))
        except OSError:
            log.exception("Govee: fallo enviando '%s' a %s", cmd, ip)

    def turn_on(self, device_id: str, brightness_pct: float | None = None,
                color_temp_kelvin: float | None = None, hs: tuple[float, float] | None = None) -> None:
        ip = self._ip_of(device_id)
        if ip is None:
            raise KeyError(f"dispositivo Govee desconocido: {device_id}")
        self._send(ip, "turn", {"value": 1})
        if color_temp_kelvin is not None:
            kelvin = int(_clamp(round(color_temp_kelvin), MIN_KELVIN, MAX_KELVIN))
            self._send(ip, "colorwc", {"color": {"r": 0, "g": 0, "b": 0}, "colorTemInKelvin": kelvin})
        elif hs is not None:
            r, g, b = _hs_to_rgb(hs[0], hs[1])
            self._send(ip, "colorwc", {"color": {"r": r, "g": g, "b": b}, "colorTemInKelvin": 0})
        if brightness_pct is not None:
            self._send(ip, "brightness", {"value": int(_clamp(round(brightness_pct), 1, 100))})
        # Repintado optimista LOCAL (mismo criterio que `manual_command`
        # de Lighting con su luz dummy): el proximo `devStatus` -- pedido
        # aqui mismo debajo, o el del sondeo periodico -- ya confirma el
        # valor real, pero el dashboard no tiene que esperar a eso.
        self._merge_optimistic(device_id, on=True, brightness_pct=brightness_pct, color_temp_kelvin=color_temp_kelvin)
        self._send(ip, "devStatus", {})

    def turn_off(self, device_id: str) -> None:
        ip = self._ip_of(device_id)
        if ip is None:
            raise KeyError(f"dispositivo Govee desconocido: {device_id}")
        self._send(ip, "turn", {"value": 0})
        self._merge_optimistic(device_id, on=False)
        self._send(ip, "devStatus", {})

    def _merge_optimistic(self, device_id: str, on: bool | None = None,
                           brightness_pct: float | None = None, color_temp_kelvin: float | None = None) -> None:
        with self._lock:
            info = self._devices.get(device_id)
            if info is None:
                return
            status = dict(info["status"] or {})
            if on is not None:
                status["onOff"] = 1 if on else 0
            if brightness_pct is not None:
                status["brightness"] = round(brightness_pct)
            if color_temp_kelvin is not None:
                status["colorTemInKelvin"] = round(color_temp_kelvin)
            info["status"] = status

    # ------------------------------------------------------- fachada light

    def light_handle(self, device_id: str) -> "GoveeLightHandle | None":
        with self._lock:
            if device_id not in self._devices:
                return None
        return GoveeLightHandle(self, device_id)


class GoveeLightHandle:
    """Mismo contrato que `TplinkLightHandle`/`TuyaLightHandle`
    (available/is_on/brightness_pct/color_temp_kelvin/turn_on/turn_off)
    -- Lighting no necesita saber que esto es Govee."""

    def __init__(self, manager: GoveeDeviceManager, device_id: str) -> None:
        self._manager = manager
        self._device_id = device_id

    @property
    def available(self) -> bool:
        return self._manager.connected(self._device_id)

    @property
    def is_on(self) -> bool:
        status = self._manager.get_status(self._device_id)
        return bool(status and status.get("onOff") == 1)

    @property
    def brightness_pct(self) -> float | None:
        status = self._manager.get_status(self._device_id)
        if not status or status.get("brightness") is None:
            return None
        return float(status["brightness"])

    @property
    def color_temp_kelvin(self) -> int | None:
        status = self._manager.get_status(self._device_id)
        if not status:
            return None
        kelvin = status.get("colorTemInKelvin")
        # Govee reporta 0 cuando el modo activo es color RGB, no
        # temperatura de color -- 0 no es un Kelvin valido, es "no aplica
        # ahora mismo" (mismo criterio que `_color_temp_active` de TP-Link).
        return int(kelvin) if kelvin else None

    def turn_on(self, brightness_pct: float | None = None, color_temp_kelvin: float | None = None,
                hs: tuple[float, float] | None = None) -> None:
        self._manager.turn_on(self._device_id, brightness_pct=brightness_pct, color_temp_kelvin=color_temp_kelvin, hs=hs)

    def turn_off(self) -> None:
        self._manager.turn_off(self._device_id)

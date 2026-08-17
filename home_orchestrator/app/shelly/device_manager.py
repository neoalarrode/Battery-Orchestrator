"""
Puente HTTP local con dispositivos Shelly -- API oficial y documentada
del fabricante (https://shelly-api-docs.shelly.cloud/), sin cuenta ni
nube: Gen1 (parametros por querystring, "/light/0", "/color/0",
"/relay/0") y Gen2/Gen3 (RPC JSON sobre HTTP, "/rpc/<Metodo>") -- misma
via "local" que usa el propio componente `shelly` de Home Assistant
(alli via CoIoT/WebSocket con `aioshelly`; aqui HTTP puro por
simplicidad, sin sus dependencias asyncio -- "igual que el original" en
el SITIO donde habla (LAN, nunca la nube), no en la libreria por dentro).

Alcance deliberado (documentado, no un descuido): un dispositivo se
clasifica en UNA de tres "capacidades" al añadirlo -- `switch` (rele
simple, on/off), `light` (atenuador blanco) o `rgbw` (color RGB +
intensidad) -- segun lo que reporte `/shelly` + `/status` (Gen1) o
`Shelly.GetStatus` (Gen2/3). No se cubre cada variante de hardware
Shelly que existe (persianas, medidores trifasicos, TRVs, sensores...),
solo el control de iluminacion que necesita Lighting -- mismo criterio
de alcance que el resto de plugins puente de este nucleo.

SIN HARDWARE SHELLY REAL para verificar esta version contra un
dispositivo fisico (a diferencia del resto del repo, que documenta cada
bug real encontrado contra hardware de verdad) -- los payloads de aqui
son los de la documentacion oficial, no verificados en produccion
todavia. Revisar contra un dispositivo real en cuanto haya uno a mano.
"""

from __future__ import annotations

import colorsys
import concurrent.futures
import ipaddress
import logging
import socket as socket_module
import threading
import time
from typing import Callable

import requests

log = logging.getLogger("shelly.device_manager")

REQUEST_TIMEOUT_SECONDS = 4
POLL_INTERVAL_SECONDS = 5
MIN_KELVIN, MAX_KELVIN = 2700, 6500  # rango tipico de los Shelly Duo/Bulb (blancos regulables)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _hs_to_rgb(hue: float, sat: float) -> tuple[int, int, int]:
    """HS (grados/porcentaje, escala de HA) -> RGB 0-255 con V=100 fijo --
    mismo criterio que `govee/device_manager.py:_hs_to_rgb` (el brillo se
    manda por separado, `gain`/`brightness`, nunca mezclado con el value
    de HSV)."""
    r, g, b = colorsys.hsv_to_rgb(hue / 360, sat / 100, 1.0)
    return round(r * 255), round(g * 255), round(b * 255)


class ShellyDeviceManager:
    """`on_any_change(device_id)`, si se da, se llama desde el hilo de
    sondeo tras cada lectura CON EXITO -- mismo contrato simple que
    `TplinkDeviceManager`/`GoveeDeviceManager`."""

    def __init__(self, on_any_change: Callable[[str], None] | None = None) -> None:
        self._on_any_change = on_any_change
        self._lock = threading.RLock()
        self._devices: dict[str, dict] = {}  # device_id -> {"host","gen","capability","model","status","connected"}

    # ------------------------------------------------------------ arranque

    def start(self) -> None:
        threading.Thread(target=self._poll_loop, name="shelly-poll", daemon=True).start()

    def _poll_loop(self) -> None:
        while True:
            time.sleep(POLL_INTERVAL_SECONDS)
            with self._lock:
                ids = list(self._devices.keys())
            for device_id in ids:
                # BUG REAL: `_refresh` solo atrapa `requests.RequestException`,
                # pero por debajo hace `r.json()` e indexa lo que venga
                # (`_read_gen1_state`/`_read_gen2_state`). Un dispositivo que
                # responda con algo que no sea JSON, o con `lights: {}` en vez
                # de una lista, lanza ValueError/TypeError/AttributeError, que
                # escapaban de aqui y MATABAN el hilo de sondeo: a partir de
                # ese momento NINGUN Shelly se volvia a sondear en toda la vida
                # del proceso, en silencio y con `connected()` siguiendo en
                # True. Un fallo leyendo UN dispositivo no puede dejar sin
                # sondeo a los demas.
                try:
                    self._refresh(device_id)
                except Exception:
                    log.exception(
                        "Shelly %s: fallo inesperado al sondear -- se omite este ciclo, "
                        "el resto de dispositivos sigue sondeandose", device_id,
                    )

    # ------------------------------------------------------------ deteccion

    def _probe(self, host: str) -> dict:
        """`/shelly` es el UNICO endpoint identico en Gen1 y Gen2/3 (ver
        "Gen1 Compatibility" en la doc oficial) -- de aqui sale `gen`
        (ausente = Gen1) y `type`/`model`, sin tener que adivinar antes
        de saber con quien se habla."""
        r = requests.get(f"http://{host}/shelly", timeout=REQUEST_TIMEOUT_SECONDS)
        r.raise_for_status()
        return r.json()

    def _detect(self, host: str) -> dict:
        info = self._probe(host)
        gen = int(info.get("gen") or 1)
        if gen >= 2:
            status = self._rpc(host, "Shelly.GetStatus")
            if any(k.startswith("rgbw:") or k.startswith("rgb:") for k in status):
                capability = "rgbw"
            elif any(k.startswith("light:") for k in status):
                capability = "light"
            else:
                capability = "switch"
        else:
            status = self._gen1_status(host)
            if status.get("lights"):
                # Un Gen1 RGBW2 en modo "color" reporta sus canales con
                # `red`/`green`/`blue` dentro de `lights[0]`; en modo
                # "white" (o un Duo/Bulb normal) solo trae `brightness`/
                # `temp`. Se decide UNA vez, al añadir el dispositivo.
                capability = "rgbw" if "red" in status["lights"][0] else "light"
            else:
                capability = "switch"
        return {"gen": gen, "capability": capability, "model": info.get("model") or info.get("type")}

    # ------------------------------------------------------------- Gen1 --

    def _gen1_status(self, host: str) -> dict:
        r = requests.get(f"http://{host}/status", timeout=REQUEST_TIMEOUT_SECONDS)
        r.raise_for_status()
        return r.json()

    def _gen1_get(self, host: str, path: str, params: dict) -> None:
        r = requests.get(f"http://{host}{path}", params=params, timeout=REQUEST_TIMEOUT_SECONDS)
        r.raise_for_status()

    # ------------------------------------------------------------- Gen2+ -

    def _rpc(self, host: str, method: str, params: dict | None = None) -> dict:
        r = requests.get(f"http://{host}/rpc/{method}", params=params or {}, timeout=REQUEST_TIMEOUT_SECONDS)
        r.raise_for_status()
        return r.json()

    # --------------------------------------------------------- descubrimiento

    def discover(self, timeout_seconds: float = 0.8) -> list[dict]:
        """Barrido ACTIVO de la subred /24 propia del contenedor (sondas
        HTTP en paralelo a `/shelly`) -- Shelly no tiene un broadcast LAN
        tan simple como Govee/Tuya (su descubrimiento real es mDNS,
        `aioshelly` usa `zeroconf`), y añadir esa dependencia solo para
        esto no compensaba. Sondear las ~254 IPs de la /24 en paralelo
        con un timeout corto es mas lento que un multicast, pero real --
        ningun resultado inventado, solo lo que responde de verdad.

        BUG REAL, confirmado en produccion (0 dispositivos encontrados
        pese a tener 4 Shelly reales en la LAN): `gethostbyname(gethostname())`
        NO devuelve la IP de la LAN real bajo Supervisor de HA -- devuelve
        la IP del contenedor en la red INTERNA de gestion de Supervisor
        (`172.30.32.x`, para la comunicacion Supervisor<->addon/Ingress),
        que sigue existiendo aunque `host_network: true` este activo para
        el trafico normal. El barrido se hacia contra la subred
        EQUIVOCADA -- nunca podia encontrar nada en la LAN de verdad
        (192.168.x.x). El truco real para sacar la IP de la interfaz de
        SALIDA de verdad: abrir un socket UDP y "conectarlo" a cualquier
        IP externa (con UDP no se manda ningun paquete real, solo hace
        que el kernel elija la interfaz/IP de salida correcta) y leer
        `getsockname()` -- verificado contra el host real: devuelve
        192.168.1.93, la IP de la LAN, no la de gestion de Supervisor."""
        try:
            probe_sock = socket_module.socket(socket_module.AF_INET, socket_module.SOCK_DGRAM)
            try:
                probe_sock.connect(("192.0.2.1", 80))  # 192.0.2.0/24 (TEST-NET-1, RFC 5737) -- nunca se manda nada de verdad
                local_ip = probe_sock.getsockname()[0]
            finally:
                probe_sock.close()
        except OSError:
            log.warning("Shelly: no se pudo determinar la IP de la LAN real -- sin escaneo posible")
            return []
        network = ipaddress.ip_network(f"{local_ip}/24", strict=False)
        hosts = [str(h) for h in network.hosts()]

        def _probe_one(host: str) -> dict | None:
            try:
                r = requests.get(f"http://{host}/shelly", timeout=timeout_seconds)
                if r.ok:
                    info = r.json()
                    return {"host": host, "model": info.get("model") or info.get("type"), "gen": int(info.get("gen") or 1)}
            except requests.RequestException:
                pass
            return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
            results = list(pool.map(_probe_one, hosts))
        return [r for r in results if r is not None]

    # --------------------------------------------------------- dispositivos

    def add_device(self, device_id: str, host: str) -> None:
        meta = self._detect(host)
        with self._lock:
            self._devices[device_id] = {
                "host": host, "gen": meta["gen"], "capability": meta["capability"],
                "model": meta["model"], "status": None, "connected": False,
            }
        self._refresh(device_id)

    def remove_device(self, device_id: str) -> None:
        with self._lock:
            self._devices.pop(device_id, None)

    def get_device(self, device_id: str) -> dict | None:
        with self._lock:
            info = self._devices.get(device_id)
            return dict(info) if info else None

    def connected(self, device_id: str) -> bool:
        with self._lock:
            info = self._devices.get(device_id)
            return bool(info and info["connected"])

    # ------------------------------------------------------------ estado --

    def _refresh(self, device_id: str) -> None:
        with self._lock:
            info = self._devices.get(device_id)
        if info is None:
            return
        try:
            status = self._read_gen2_state(info) if info["gen"] >= 2 else self._read_gen1_state(info)
        except requests.RequestException:
            with self._lock:
                dev = self._devices.get(device_id)
                if dev is not None:
                    dev["connected"] = False
            return
        with self._lock:
            dev = self._devices.get(device_id)
            if dev is not None:
                dev["status"] = status
                dev["connected"] = True
        if self._on_any_change:
            try:
                self._on_any_change(device_id)
            except Exception:
                log.exception("Fallo en on_any_change para %s", device_id)

    def _read_gen1_state(self, info: dict) -> dict:
        raw = self._gen1_status(info["host"])
        if info["capability"] == "switch":
            relay = (raw.get("relays") or [{}])[0]
            return {"on": bool(relay.get("ison"))}
        light = (raw.get("lights") or [{}])[0]
        out = {"on": bool(light.get("ison"))}
        if info["capability"] == "rgbw":
            out["brightness_pct"] = light.get("gain")
        else:
            out["brightness_pct"] = light.get("brightness")
            out["kelvin"] = light.get("temp")
        return out

    def _read_gen2_state(self, info: dict) -> dict:
        raw = self._rpc(info["host"], "Shelly.GetStatus")
        if info["capability"] == "switch":
            sw = raw.get("switch:0") or {}
            return {"on": bool(sw.get("output"))}
        key = "rgbw:0" if "rgbw:0" in raw else ("rgb:0" if "rgb:0" in raw else "light:0")
        comp = raw.get(key) or {}
        return {"on": bool(comp.get("output")), "brightness_pct": comp.get("brightness")}

    # ------------------------------------------------------------ escritura

    def turn_on(self, device_id: str, brightness_pct: float | None = None,
                color_temp_kelvin: float | None = None, hs: tuple[float, float] | None = None) -> None:
        info = self.get_device(device_id)
        if info is None:
            raise KeyError(f"dispositivo Shelly desconocido: {device_id}")
        if info["gen"] >= 2:
            self._turn_on_gen2(info["host"], info["capability"], brightness_pct, hs)
        else:
            self._turn_on_gen1(info["host"], info["capability"], brightness_pct, color_temp_kelvin, hs)
        self._refresh(device_id)

    def turn_off(self, device_id: str) -> None:
        info = self.get_device(device_id)
        if info is None:
            raise KeyError(f"dispositivo Shelly desconocido: {device_id}")
        host, gen, capability = info["host"], info["gen"], info["capability"]
        if gen >= 2:
            method = {"switch": "Switch.Set", "rgbw": "RGBW.Set", "light": "Light.Set"}[capability]
            self._rpc(host, method, {"id": 0, "on": "false"})
        else:
            path = {"switch": "/relay/0", "rgbw": "/color/0", "light": "/light/0"}[capability]
            self._gen1_get(host, path, {"turn": "off"})
        self._refresh(device_id)

    def _turn_on_gen1(self, host: str, capability: str, brightness_pct, color_temp_kelvin, hs) -> None:
        if capability == "switch":
            self._gen1_get(host, "/relay/0", {"turn": "on"})
            return
        if capability == "rgbw":
            params = {"turn": "on"}
            if hs is not None:
                r, g, b = _hs_to_rgb(hs[0], hs[1])
                params.update(red=r, green=g, blue=b)
            if brightness_pct is not None:
                params["gain"] = round(_clamp(brightness_pct, 1, 100))
            self._gen1_get(host, "/color/0", params)
            return
        params = {"turn": "on"}
        if brightness_pct is not None:
            params["brightness"] = round(_clamp(brightness_pct, 1, 100))
        if color_temp_kelvin is not None:
            params["temp"] = round(_clamp(color_temp_kelvin, MIN_KELVIN, MAX_KELVIN))
        self._gen1_get(host, "/light/0", params)

    def _turn_on_gen2(self, host: str, capability: str, brightness_pct, hs) -> None:
        if capability == "switch":
            self._rpc(host, "Switch.Set", {"id": 0, "on": "true"})
            return
        if capability == "rgbw":
            params = {"id": 0, "on": "true"}
            if hs is not None:
                r, g, b = _hs_to_rgb(hs[0], hs[1])
                params["rgb"] = f"[{r},{g},{b}]"
            if brightness_pct is not None:
                params["brightness"] = round(_clamp(brightness_pct, 1, 100))
            self._rpc(host, "RGBW.Set", params)
            return
        params = {"id": 0, "on": "true"}
        if brightness_pct is not None:
            params["brightness"] = round(_clamp(brightness_pct, 1, 100))
        self._rpc(host, "Light.Set", params)

    # ------------------------------------------------------- fachada light

    def light_handle(self, device_id: str) -> "ShellyLightHandle | None":
        with self._lock:
            exists = device_id in self._devices
        # Un rele puro (capability == "switch") TAMBIEN se ofrece como
        # light_handle a proposito -- un Shelly 1 controlando una luz de
        # techo fisica es un uso real y comun; Lighting ya sabe tratar
        # brightness_pct/color_temp_kelvin en None con normalidad (mismo
        # criterio que una bombilla TP-Link no atenuable).
        return ShellyLightHandle(self, device_id) if exists else None


class ShellyLightHandle:
    """Mismo contrato que `TplinkLightHandle`/`GoveeLightHandle`
    (available/is_on/brightness_pct/color_temp_kelvin/turn_on/turn_off) --
    Lighting no necesita saber que esto es Shelly."""

    def __init__(self, manager: ShellyDeviceManager, device_id: str) -> None:
        self._manager = manager
        self._device_id = device_id

    def _status(self) -> dict | None:
        info = self._manager.get_device(self._device_id)
        return info.get("status") if info else None

    @property
    def available(self) -> bool:
        return self._manager.connected(self._device_id)

    @property
    def is_on(self) -> bool:
        status = self._status()
        return bool(status and status.get("on"))

    @property
    def brightness_pct(self) -> float | None:
        status = self._status()
        if not status or status.get("brightness_pct") is None:
            return None
        return float(status["brightness_pct"])

    @property
    def color_temp_kelvin(self) -> int | None:
        status = self._status()
        if not status or not status.get("kelvin"):
            return None
        return int(status["kelvin"])

    def turn_on(self, brightness_pct: float | None = None, color_temp_kelvin: float | None = None,
                hs: tuple[float, float] | None = None) -> None:
        self._manager.turn_on(self._device_id, brightness_pct=brightness_pct, color_temp_kelvin=color_temp_kelvin, hs=hs)

    def turn_off(self) -> None:
        self._manager.turn_off(self._device_id)

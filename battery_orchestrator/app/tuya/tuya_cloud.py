"""
Tuya Cloud (OpenAPI) -- usado SOLO para vincular una cuenta y traer el
local_key + esquema real de cada dispositivo (necesario para el perfil
automatico, ver auto_profile.py). Nada de este modulo se vuelve a llamar
una vez el dispositivo esta dado de alta -- operacion normal 100% LAN (ver
tuya_lan.py). Adaptado del original (aiohttp/asyncio) a `requests` sincrono
-- mismo criterio que el resto de clientes HTTP de Home Orchestrator
(ha_mqtt.py, ecoflow_cloud.py de Energy), y esto no es sensible a
rendimiento (solo se llama al emparejar, no en el bucle de control).

Firma de peticion HMAC-SHA256 documentada en
https://developer.tuya.com/en/docs/iot/api-request -- requiere un proyecto
"Cloud" (gratuito) de Tuya IoT Platform con la cuenta de la app vinculada
(Devices -> Link Tuya App Account), el UID que se pide ahi es el mismo que
pide este modulo.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any

import requests

from .const import TUYA_REGIONS

log = logging.getLogger("tuya.cloud")


class TuyaCloudAuthError(Exception):
    """Credenciales/firma rechazadas."""


class TuyaCloudApiError(Exception):
    """Cualquier otra respuesta no exitosa de la API."""


class TuyaCloudApi:
    def __init__(self, region: str, access_id: str, access_secret: str) -> None:
        self._base = TUYA_REGIONS[region]
        self._access_id = access_id
        self._access_secret = access_secret
        self._token: str | None = None
        self._token_expires_at: float = 0

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - 30:
            return self._token
        result = self._request("GET", "/v1.0/token?grant_type=1", signed_with_token=False)
        self._token = result["access_token"]
        self._token_expires_at = time.time() + result.get("expire_time", 7200)
        return self._token

    def _sign(self, method: str, path: str, body: str, token: str | None, t: str) -> str:
        content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        string_to_sign = "\n".join([method, content_hash, "", path])
        message = self._access_id + (token or "") + t + string_to_sign
        return hmac.new(
            self._access_secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
        ).hexdigest().upper()

    def _request(self, method: str, path: str, body: dict | None = None, signed_with_token: bool = True) -> dict[str, Any]:
        body_str = "" if body is None else json.dumps(body, separators=(",", ":"))
        t = str(int(time.time() * 1000))
        token = self._get_token() if signed_with_token else None
        sign = self._sign(method, path, body_str, token, t)
        headers = {
            "client_id": self._access_id,
            "sign": sign,
            "t": t,
            "sign_method": "HMAC-SHA256",
            "Content-Type": "application/json",
        }
        if token:
            headers["access_token"] = token

        resp = requests.request(method, self._base + path, headers=headers, data=body_str or None, timeout=15)
        data = resp.json()

        if not data.get("success"):
            code = data.get("code")
            msg = data.get("msg")
            if code in (1004, 1013, 1010):
                raise TuyaCloudAuthError(f"{code}: {msg}")
            raise TuyaCloudApiError(f"{code}: {msg}")
        return data.get("result", {})

    def validate(self) -> None:
        """Lanza excepcion si las credenciales son invalidas -- usado por
        la interfaz para fallar rapido al vincular una cuenta."""
        self._get_token()

    def get_user_devices(self, uid: str) -> list[dict[str, Any]]:
        result = self._request("GET", f"/v1.0/users/{uid}/devices")
        devices = result if isinstance(result, list) else result.get("devices", [])
        return [
            {
                "device_id": d["id"],
                "name": d.get("name") or d["id"],
                "product_id": d.get("product_id"),
                "category": d.get("category"),
                "local_key": d.get("local_key"),
                "online": d.get("online", False),
            }
            for d in devices
        ]

    def get_device_schema(self, device_id: str) -> list[dict[str, Any]]:
        """Esquema DP real del dispositivo, normalizado. Consulta v1.1 Y
        v2.0 y las fusiona por dp_id (v1.1 gana en conflicto, v2.0 rellena
        lo que falte) -- v1.1 puede devolver success=true con un esquema
        PARCIAL, confirmado en el proyecto original contra un aire
        acondicionado real (v1.1 solo daba 6 DP, v2.0 daba ~25)."""
        entries_by_dp: dict[int, dict[str, Any]] = {}
        errors: list[Exception] = []

        try:
            result = self._request("GET", f"/v1.1/devices/{device_id}/specifications")
            for e in _normalize_v11_schema(result):
                entries_by_dp[e["dp_id"]] = e
        except (TuyaCloudApiError, TuyaCloudAuthError) as err:
            errors.append(err)

        try:
            result = self._request("GET", f"/v2.0/cloud/thing/{device_id}/model")
            for e in _normalize_v20_schema(result):
                entries_by_dp.setdefault(e["dp_id"], e)
        except (TuyaCloudApiError, TuyaCloudAuthError) as err:
            errors.append(err)

        if not entries_by_dp and errors:
            raise errors[0]
        return list(entries_by_dp.values())


def _normalize_v11_schema(result: dict[str, Any]) -> list[dict[str, Any]]:
    functions = {f["code"]: f for f in result.get("functions", []) if f.get("dp_id") is not None}
    statuses = {s["code"]: s for s in result.get("status", []) if s.get("dp_id") is not None}
    entries: dict[str, dict[str, Any]] = {}
    for code, s in statuses.items():
        access = "rw" if code in functions else "ro"
        entries[code] = _entry(code, s["dp_id"], s["type"], access, s.get("values"))
    for code, f in functions.items():
        if code in entries:
            continue
        entries[code] = _entry(code, f["dp_id"], f["type"], "wr", f.get("values"))
    return list(entries.values())


def _normalize_v20_schema(result: dict[str, Any]) -> list[dict[str, Any]]:
    model = result.get("model")
    model = json.loads(model) if isinstance(model, str) else (model or {})
    entries = []
    for service in model.get("services", []):
        for prop in service.get("properties", []):
            type_spec = prop.get("typeSpec", {})
            entries.append(
                _entry(prop["code"], prop["abilityId"], type_spec.get("type", "raw"), prop.get("accessMode", "ro"), type_spec)
            )
    return entries


_TYPE_NORMALIZE = {
    "boolean": "bool", "bool": "bool", "integer": "value", "value": "value",
    "enum": "enum", "bitmap": "bitmap", "string": "string", "json": "json", "raw": "raw",
}


def _entry(code: str, dp_id: int, raw_type: str, access: str, values: Any) -> dict[str, Any]:
    if isinstance(values, str):
        try:
            values = json.loads(values)
        except (ValueError, TypeError):
            values = {}
    return {
        "code": code,
        "dp_id": int(dp_id),
        "type": _TYPE_NORMALIZE.get(str(raw_type).lower(), "raw"),
        "access": access,
        "values": values or {},
    }

"""
Resuelve el userId numerico de una cuenta EcoFlow a partir de su email y
contraseña — el MISMO flujo que usa la app oficial de EcoFlow (y que ya
usa `rabits/ha-ef-ble`, referencia de este modulo): un login normal
contra el API de cuenta de EcoFlow, no el API de desarrollador.

La contraseña se manda tal cual UNA vez, para esta llamada, y no se
guarda en ningun sitio de la app — ni en config.json ni en ningun log:
lo unico que se persiste despues es el userId ya resuelto (un
identificador de cuenta, no un secreto).
"""

from __future__ import annotations

import base64
import logging

import requests

log = logging.getLogger("ecoflow_login")

LOGIN_URL = "https://api.ecoflow.com/auth/login"
TIMEOUT = 15


class EcoFlowLoginError(Exception):
    pass


def resolve_user_id(email: str, password: str) -> str:
    """
    Devuelve el userId numerico de la cuenta, o lanza EcoFlowLoginError
    con un mensaje claro (credenciales incorrectas, fallo de red...).
    """
    payload = {
        "scene": "IOT_APP",
        "appVersion": "1.0.0",
        "password": base64.b64encode(password.encode()).decode(),
        "email": email.strip(),
        "oauth": {"bundleId": "com.ef.EcoFlow"},
        "userType": "ECOFLOW",
    }
    try:
        r = requests.post(
            LOGIN_URL, json=payload,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        # El mensaje de EcoFlowLoginError llega tal cual a la respuesta HTTP
        # (ver main.py:api_ecoflow_login) -- el texto de una RequestException
        # de verdad puede incluir la URL/host de destino, nunca se reenvia
        # sin mas. El detalle real se registra aqui, no se pierde.
        log.warning("EcoFlow: fallo de red contactando con la API", exc_info=True)
        raise EcoFlowLoginError("No se pudo contactar con EcoFlow") from e

    if not r.ok:
        raise EcoFlowLoginError(f"EcoFlow respondió con un error ({r.status_code})")

    try:
        data = r.json()
    except ValueError as e:
        raise EcoFlowLoginError("Respuesta inesperada de EcoFlow") from e

    if data.get("code") != "0":
        raise EcoFlowLoginError(data.get("message") or "Email o contraseña incorrectos")

    try:
        return str(data["data"]["user"]["userId"])
    except (KeyError, TypeError) as e:
        raise EcoFlowLoginError("Respuesta inesperada de EcoFlow (sin userId)") from e

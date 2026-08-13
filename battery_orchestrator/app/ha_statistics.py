"""
Reconstruye el HISTORICO de los sensores de energia acumulada
(sensor.battery_orchestrator_energy_charged/discharged) en vez de dejar
que su primera publicacion aparezca como un salto de golpe en las
graficas del Panel de Energia de HA.

No hay ningun endpoint REST para esto -- las "long-term statistics" de
HA solo se pueden importar por WEBSOCKET (`recorder/import_statistics`),
asi que este modulo es el unico sitio de toda la app que habla por WS en
vez de REST. Usa el token de Supervisor (`SUPERVISOR_TOKEN`, HA lo
inyecta solo en cualquier add-on) contra `ws://supervisor/core/websocket`
— el mismo host interno que ya usa `ha_client.py` por REST.
"""

from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("ha_statistics")

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN")
WS_URL = "ws://supervisor/core/websocket"
TIMEOUT = 20


def import_statistics(statistic_id: str, unit: str, points: list[dict]) -> bool:
    """
    Importa un historico de "long-term statistics" para un sensor que YA
    existe (`source: "recorder"`, mismo criterio que usan las
    integraciones oficiales para rellenar el pasado de un contador que se
    acaba de dar de alta).

    `points`: [{"start": "2026-08-01T00:00:00+00:00", "sum": 1.234}, ...]
    ordenados por tiempo — "sum" es el ACUMULADO hasta el final de esa
    hora (no la energia de esa hora sola), tal como lo pide HA.

    Devuelve True si HA confirmo el import; False si algo fallo (sin
    token, sin conexion, formato rechazado...) — nunca lanza, quien llama
    decide que hacer con un fallo (reintentar mas tarde, avisar al
    usuario).
    """
    if not SUPERVISOR_TOKEN:
        log.warning("Sin SUPERVISOR_TOKEN -- no se puede hablar con el websocket de HA")
        return False
    if not points:
        return True

    import websocket  # websocket-client -- solo hace falta aqui, import perezoso

    try:
        ws = websocket.create_connection(WS_URL, timeout=TIMEOUT)
    except Exception as e:
        log.warning(f"No se pudo abrir el websocket de HA: {e}")
        return False

    try:
        hello = json.loads(ws.recv())
        if hello.get("type") != "auth_required":
            log.warning(f"Handshake de websocket inesperado: {hello}")
            return False

        ws.send(json.dumps({"type": "auth", "access_token": SUPERVISOR_TOKEN}))
        auth_result = json.loads(ws.recv())
        if auth_result.get("type") != "auth_ok":
            log.warning(f"Autenticacion de websocket fallida: {auth_result}")
            return False

        ws.send(json.dumps({
            "id": 1,
            "type": "recorder/import_statistics",
            "metadata": {
                "has_mean": False,
                "has_sum": True,
                "statistic_id": statistic_id,
                "name": None,
                "source": "recorder",
                "unit_of_measurement": unit,
            },
            "stats": points,
        }))
        result = json.loads(ws.recv())
        if not result.get("success"):
            log.warning(f"HA rechazo el import de estadisticas de {statistic_id}: {result}")
        return bool(result.get("success"))
    except Exception as e:
        log.warning(f"Fallo importando estadisticas de {statistic_id}: {e}")
        return False
    finally:
        try:
            ws.close()
        except Exception:
            pass

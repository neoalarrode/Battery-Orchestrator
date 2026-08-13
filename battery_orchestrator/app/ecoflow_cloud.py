"""
Cliente para el API Cloud (REST + MQTT) de EcoFlow — sin pasar por Home
Assistant para nada, son llamadas directas a la nube de EcoFlow con las
credenciales (Access Key / Secret Key) que el usuario declara en la propia
configuracion de Battery Orchestrator.

Dos protocolos, cada uno con un papel distinto:

  - REST (`https://api-e.ecoflow.com`, firmado con HMAC-SHA256): para
    descubrir dispositivos y como red de seguridad de lectura si el feed
    MQTT no ha mandado nada todavia. El snapshot que da (`quota/all`) es un
    subconjunto MUY reducido (~15 campos) — nunca incluye el control de
    tareas de carga/descarga.

  - MQTT (`mqtt-e.ecoflow.com:8883`, tls): el feed EN VIVO de verdad, con
    actualizaciones incrementales (cada mensaje trae solo lo que ha
    cambiado, no el estado completo) — y el UNICO camino, documentado o
    no, para leer y escribir las "tareas" de carga/descarga
    (`allTimerTask`/`cfgAllTimerTask`) que es como EcoFlow modela "cargar
    ahora a X vatios" / "descargar ahora a X vatios". Verificado a mano
    contra una cuenta real: el comando de escritura no esta documentado
    por EcoFlow en ningun sitio, pero responde con exito y el propio feed
    refleja el cambio.

Conexion MQTT PERSISTENTE (una sola, reutilizada) a proposito: EcoFlow
limita a 10 client_id distintos por cuenta y dia — reconectar con un
client_id nuevo en cada ciclo lo agotaria en minutos. Se guarda un cliente
por cada par (access_key, secret_key) y se reutiliza mientras el proceso
viva.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import logging
import random
import ssl
import threading
import time

import requests

log = logging.getLogger("ecoflow_cloud")

# Endpoint EU explicitamente — la cuenta del usuario es de esta region;
# `api.ecoflow.com` (global) tambien responde pero no es el que hay que usar.
BASE_URL = "https://api-e.ecoflow.com"
TIMEOUT = 15

MQTT_KEEPALIVE = 30
SET_ACK_TIMEOUT_SECONDS = 10
# Cuanto se considera "todavia fresco" un dato de MQTT antes de caer al
# snapshot REST como red de seguridad (ver `get_live_state`).
LIVE_STATE_STALE_SECONDS = 120


class EcoFlowError(Exception):
    pass


# --------------------------------------------------------------------------
# REST firmado (HMAC-SHA256) — ver developer-eu.ecoflow.com para el detalle
# exacto del formato de firma, que NO es "ordenar todo junto": los
# parametros de la peticion (si los hay) van ordenados alfabeticamente
# aparte, y accessKey/nonce/timestamp se anaden sin reordenar al final.
# --------------------------------------------------------------------------

def _flatten(d, prefix="") -> dict:
    """Aplana un dict/list anidado en claves punto/corchete — hace falta
    para firmar peticiones cuyo cuerpo lleva estructuras anidadas (p.ej. el
    propio `cfgAllTimerTask`), no solo pares plano a plano."""
    out = {}
    if isinstance(d, dict):
        for k, v in d.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else k))
    elif isinstance(d, list):
        for i, v in enumerate(d):
            out.update(_flatten(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = d
    return out


def _signed_headers(access_key: str, secret_key: str, body_params: dict | None = None) -> dict:
    nonce = str(random.randint(100000, 999999))
    timestamp = str(int(time.time() * 1000))
    flat = _flatten(body_params or {})
    qs = "&".join(f"{k}={flat[k]}" for k in sorted(flat.keys()))
    sign_str = qs + ("&" if qs else "") + f"accessKey={access_key}&nonce={nonce}&timestamp={timestamp}"
    signature = hmac.new(secret_key.encode(), sign_str.encode(), hashlib.sha256).hexdigest()
    return {"accessKey": access_key, "nonce": nonce, "timestamp": timestamp, "sign": signature}


def _rest_get(access_key: str, secret_key: str, path: str, params: dict | None = None) -> dict:
    headers = _signed_headers(access_key, secret_key, params)
    r = requests.get(f"{BASE_URL}{path}", headers=headers, params=params or {}, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "0":
        raise EcoFlowError(f"{path}: {data.get('message')}")
    return data.get("data", {})


def list_devices(access_key: str, secret_key: str) -> list[dict]:
    """Todos los dispositivos EcoFlow visibles con estas credenciales —
    para el "Buscar baterias EcoFlow" en la configuracion (mismo patron que
    ya usa Climate Orchestrator, ver climate_link.py)."""
    data = _rest_get(access_key, secret_key, "/iot-open/sign/device/list")
    return data if isinstance(data, list) else []


def get_main_sn(access_key: str, secret_key: str, sn: str) -> str | None:
    """
    Los sistemas STREAM con varias unidades enlazadas (BKW) comparten UNA
    sola lista de tareas de carga/descarga, y los comandos de control hay
    que mandarlos al dispositivo "principal" del grupo, no a cualquiera —
    esta llamada lo resuelve a partir de CUALQUIER sn del grupo. Para un
    dispositivo suelto (sin grupo), devuelve su propio sn.
    """
    try:
        data = _rest_get(access_key, secret_key, "/iot-open/sign/device/system/main/sn", {"sn": sn})
        return data.get("sn") or sn
    except (EcoFlowError, requests.RequestException):
        return None


def get_quota_all(access_key: str, secret_key: str, sn: str) -> dict | None:
    """
    Snapshot REST puntual — SOLO como red de seguridad si el feed MQTT
    todavia no ha mandado nada (arranque en frio) o lleva demasiado sin
    actualizar. Nunca incluye tareas de carga/descarga ni limites en
    vatios, solo lo que ya viste en `developer-eu.ecoflow.com`.
    """
    try:
        return _rest_get(access_key, secret_key, "/iot-open/sign/device/quota/all", {"sn": sn})
    except (EcoFlowError, requests.RequestException) as e:
        log.warning(f"No se pudo leer quota/all de {sn}: {e}")
        return None


def _get_certification(access_key: str, secret_key: str) -> dict | None:
    try:
        return _rest_get(access_key, secret_key, "/iot-open/sign/certification")
    except (EcoFlowError, requests.RequestException) as e:
        log.warning(f"No se pudieron obtener credenciales MQTT: {e}")
        return None


# --------------------------------------------------------------------------
# Identificacion de la tarea de carga vs. la de descarga dentro de
# `allTimerTask.timeTask[]` — mismo criterio que ya usa `rabits/ha-ef-ble`
# por BLE: la de carga es la que trae `chgTask` con `devTargetSoc` no
# vacio; la de descarga es la que trae `homeNeedPowerLimited` (sin
# `chgTask`). Puede no haber ninguna de las dos si el sistema nunca se
# configuro por "Modo de funcionamiento personalizado" en la app.
# --------------------------------------------------------------------------

def _find_charge_task(all_timer_task: dict) -> dict | None:
    for task in (all_timer_task or {}).get("timeTask", []):
        chg = task.get("chgTask")
        if chg and chg.get("devTargetSoc"):
            return task
    return None


def _find_discharge_task(all_timer_task: dict) -> dict | None:
    for task in (all_timer_task or {}).get("timeTask", []):
        if "homeNeedPowerLimited" in task and not task.get("chgTask"):
            return task
    return None


# Mismos campos que expone la MQTT en vivo para los puertos MPPT --
# corresponden uno a uno con los `pv_power_N` de eflib (protobuf) por
# BLE: `powGetPv`/`powGetPv2`/`powGetPv3`/`powGetPv4`, verificado contra
# una lectura real de una STREAM Ultra X (traia `powGetPv3` y
# `powGetPvSum`). No todos los modelos reportan todos los canales.
PV_CHANNEL_QUOTA_FIELDS = {"1": "powGetPv", "2": "powGetPv2", "3": "powGetPv3", "4": "powGetPv4"}


def pv_channels_from_state(state: dict) -> dict:
    """
    Puertos MPPT a partir del estado en vivo de MQTT -- a diferencia de
    BLE (que sabe de antemano, por la clase del modelo, si un puerto
    existe o no aunque todavia no haya reportado nada), aqui solo se
    puede saber que un puerto existe cuando YA ha mandado un valor -- no
    hay forma de distinguir "este modelo no tiene este puerto" de
    "todavia no ha llegado su primer dato" desde Cloud. Por eso solo se
    devuelven los canales que SI tienen valor; los demas simplemente no
    aparecen (nunca un "no soportado" que en realidad seria "aun sin
    dato").
    """
    channels = {}
    for ch, field in PV_CHANNEL_QUOTA_FIELDS.items():
        val = state.get(field)
        if val is not None:
            try:
                channels[ch] = float(val)
            except (TypeError, ValueError):
                pass
    return channels


class EcoFlowCloudClient:
    """
    Una conexion MQTT persistente para una cuenta EcoFlow — mantiene en
    memoria el ultimo estado conocido (merge de actualizaciones
    incrementales) y la ultima `allTimerTask` completa de cada grupo, para
    poder modificar una tarea sin tener que adivinar el resto de campos
    que no se estan tocando (justo el riesgo de "romper la programacion
    real" que hay que evitar).
    """

    def __init__(self, access_key: str, secret_key: str):
        self.access_key = access_key
        self.secret_key = secret_key
        self._lock = threading.Lock()
        self._live_state: dict[str, dict] = {}          # sn -> propiedades mergeadas
        self._live_state_ts: dict[str, float] = {}       # sn -> ultimo mensaje recibido
        self._all_timer_task: dict[str, dict] = {}        # main_sn -> ultima allTimerTask completa
        self._all_timer_task_ts: dict[str, float] = {}
        self._pending_acks: dict[int, threading.Event] = {}
        self._pending_results: dict[int, dict] = {}
        self._subscribed_sns: set[str] = set()
        self._client = None
        self._username = None
        self._started = False

    # -- ciclo de vida ----------------------------------------------------

    def start(self) -> bool:
        """Conecta el cliente MQTT una vez. Llamadas posteriores no hacen
        nada si ya esta en marcha (o si sigue intentandolo)."""
        with self._lock:
            if self._started:
                return True
            cert = _get_certification(self.access_key, self.secret_key)
            if not cert:
                return False
            self._username = cert["certificateAccount"]
            password = cert["certificatePassword"]
            url = cert["url"]
            port = int(cert["port"])

            import paho.mqtt.client as mqtt

            # client_id ESTABLE (no aleatorio) — EcoFlow limita a 10
            # client_id distintos por cuenta y dia; un id nuevo en cada
            # reinicio del addon lo agotaria enseguida.
            client_id = f"BatteryOrchestrator-{self._username}"
            client = mqtt.Client(
                client_id=client_id, protocol=mqtt.MQTTv311,
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            )
            client.username_pw_set(self._username, password)
            client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
            client.on_connect = self._on_connect
            client.on_message = self._on_message
            client.on_disconnect = self._on_disconnect
            client.reconnect_delay_set(min_delay=1, max_delay=30)

            try:
                client.connect(url, port, keepalive=MQTT_KEEPALIVE)
            except Exception as e:
                log.warning(f"No se pudo conectar al MQTT de EcoFlow: {e}")
                return False

            client.loop_start()
            self._client = client
            self._started = True
            return True

    def stop(self):
        # OJO: nunca llamar a loop_stop()/disconnect() con self._lock
        # cogido — el hilo de red de paho tambien necesita ese lock dentro
        # de _on_message/_on_connect, y loop_stop() espera a que ese hilo
        # termine su iteracion actual: si esta bloqueado esperando el
        # mismo lock que nosotros tenemos, se produce un interbloqueo y
        # stop() no vuelve nunca.
        client = self._client
        with self._lock:
            self._started = False
        if client is not None:
            client.loop_stop()
            client.disconnect()

    def ensure_subscribed(self, sn: str):
        """Se suscribe a los topics de un dispositivo la primera vez que
        se declara — sin esto no llega ningun dato en vivo de el."""
        if sn in self._subscribed_sns or self._client is None:
            return
        for topic in (f"/open/{self._username}/{sn}/quota", f"/open/{self._username}/{sn}/set_reply"):
            self._client.subscribe(topic, qos=1)
        self._subscribed_sns.add(sn)

    # -- callbacks MQTT -----------------------------------------------------

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            log.info("Conectado al MQTT de EcoFlow")
            for sn in list(self._subscribed_sns):
                self._subscribed_sns.discard(sn)
                self.ensure_subscribed(sn)
        else:
            log.warning(f"Conexion MQTT de EcoFlow rechazada, rc={rc}")

    def _on_disconnect(self, client, userdata, *args):
        log.info("MQTT de EcoFlow desconectado (paho reintentara solo)")

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        parts = msg.topic.split("/")
        if len(parts) < 4:
            return
        sn = parts[3]

        if msg.topic.endswith("/set_reply"):
            req_id = payload.get("id")
            with self._lock:
                event = self._pending_acks.get(req_id)
                if event is not None:
                    self._pending_results[req_id] = payload.get("data", {})
                    event.set()
            return

        # /quota — actualizacion incremental: se MERGEA sobre lo que ya
        # habia, nunca se sustituye entero (cada mensaje solo trae lo que
        # cambio desde el anterior).
        with self._lock:
            state = self._live_state.setdefault(sn, {})
            state.update(payload)
            self._live_state_ts[sn] = time.time()
            if "allTimerTask" in payload:
                self._all_timer_task[sn] = payload["allTimerTask"]
                self._all_timer_task_ts[sn] = time.time()

    # -- lectura ------------------------------------------------------------

    def get_live_state(self, sn: str) -> dict | None:
        """
        Estado mergeado en vivo de este dispositivo, o `None` si todavia
        no ha llegado nada por MQTT (arranque en frio) — quien llama debe
        caer entonces a `get_quota_all` como red de seguridad, igual que
        ya hace el resto de la app con otras fuentes en vivo.
        """
        self.ensure_subscribed(sn)
        with self._lock:
            ts = self._live_state_ts.get(sn)
            if ts is None or (time.time() - ts) > LIVE_STATE_STALE_SECONDS:
                return None
            return dict(self._live_state.get(sn, {}))

    def get_all_timer_task(self, main_sn: str) -> dict | None:
        self.ensure_subscribed(main_sn)
        with self._lock:
            return copy.deepcopy(self._all_timer_task.get(main_sn))

    # -- escritura ------------------------------------------------------------

    def _publish_set(self, main_sn: str, params: dict, timeout: float = SET_ACK_TIMEOUT_SECONDS) -> bool:
        if self._client is None:
            return False
        req_id = random.randint(1, 2_000_000_000)
        event = threading.Event()
        with self._lock:
            self._pending_acks[req_id] = event
        payload = {
            "id": req_id, "version": "1.0", "sn": main_sn,
            "cmdId": 17, "cmdFunc": 254, "dirDest": 1, "dirSrc": 1, "dest": 2,
            "needAck": True, "params": params,
        }
        self._client.publish(f"/open/{self._username}/{main_sn}/set", json.dumps(payload), qos=1)
        ok = event.wait(timeout)
        with self._lock:
            result = self._pending_results.pop(req_id, None)
            self._pending_acks.pop(req_id, None)
        if not ok:
            log.warning(f"Sin respuesta de EcoFlow a un comando para {main_sn} en {timeout}s")
            return False
        return bool(result and result.get("configOk"))

    def set_charging_task(
        self, main_sn: str, battery_sn: str,
        enable: bool | None = None, power_limit_w: float | None = None, target_soc: float | None = None,
    ) -> bool:
        """
        Activa/desactiva la tarea de carga programada y/o ajusta el
        limite de potencia de carga desde red y el SOC objetivo de UNA
        bateria concreta dentro del grupo — se lee la `allTimerTask`
        completa mas reciente y solo se modifica la entrada de
        `battery_sn`, se reenvia entera (igual que hace `ha-ef-ble` por
        BLE): mandar solo el fragmento que cambia perderia el resto de la
        programacion del grupo.

        Devuelve False sin mandar nada si todavia no se conoce la
        `allTimerTask` actual (nunca se escribe a ciegas) o si esta
        bateria no tiene entrada en la tarea de carga.
        """
        current = self.get_all_timer_task(main_sn)
        if current is None:
            log.warning(f"No hay allTimerTask conocida de {main_sn} todavia, no se manda el comando")
            return False
        task = _find_charge_task(current)
        if task is None:
            return False
        dev_entry = next(
            (d for d in task["chgTask"]["devTargetSoc"] if d.get("sn") == battery_sn), None
        )
        if dev_entry is None:
            log.warning(f"{battery_sn} no tiene entrada en la tarea de carga del grupo de {main_sn}")
            return False
        if enable is not None:
            task["isEnable"] = enable
        if power_limit_w is not None:
            dev_entry["chgFromGridPowerLimited"] = int(power_limit_w)
        if target_soc is not None:
            dev_entry["targetSoc"] = int(target_soc)
        return self._publish_set(main_sn, {"cfgAllTimerTask": current})

    def set_discharging_task(
        self, main_sn: str, enable: bool | None = None, power_limit_w: float | None = None,
    ) -> bool:
        """Misma logica que `set_charging_task` pero para la tarea de
        descarga (compartida por todo el grupo, no por bateria)."""
        current = self.get_all_timer_task(main_sn)
        if current is None:
            log.warning(f"No hay allTimerTask conocida de {main_sn} todavia, no se manda el comando")
            return False
        task = _find_discharge_task(current)
        if task is None:
            return False
        if enable is not None:
            task["isEnable"] = enable
        if power_limit_w is not None:
            task["homeNeedPowerLimited"] = int(power_limit_w)
        return self._publish_set(main_sn, {"cfgAllTimerTask": current})

    def set_simple(self, main_sn: str, key: str, value) -> bool:
        """Para los comandos SI documentados y ya de un solo campo
        (cfgRelay2Onoff, cfgFeedGridMode, cfgBackupReverseSoc...) — no
        necesitan leer nada antes, se mandan directos."""
        return self._publish_set(main_sn, {key: value})


# --------------------------------------------------------------------------
# Un cliente MQTT por credenciales, reutilizado mientras el proceso viva —
# ver docstring del modulo (limite de 10 client_id/dia de EcoFlow).
# --------------------------------------------------------------------------

_clients: dict[tuple[str, str], EcoFlowCloudClient] = {}
_clients_lock = threading.Lock()


def get_client(access_key: str, secret_key: str) -> EcoFlowCloudClient | None:
    if not access_key or not secret_key:
        return None
    key = (access_key, secret_key)
    with _clients_lock:
        client = _clients.get(key)
        if client is None:
            client = EcoFlowCloudClient(access_key, secret_key)
            _clients[key] = client
        if not client._started:
            client.start()
        return client

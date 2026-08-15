"""
Plugin de Starlink para el nucleo Home Orchestrator -- a diferencia del
resto de plugins, NO es una reimplementacion propia: integra TAL CUAL (a
peticion expresa del usuario, "no quiero desperdiciar unos graficos muy
bonitos" + "no acepto recortar funciones, implementa la libreria de
verdad") el proyecto completo de Dishylink
(https://github.com/DaveyHert/dishylink, MIT) -- frontend, historian Y
servidor de cuenta, los tres reales, ninguno recortado.

`app/starlink_dist/` es su propio `npm run build --base=./` con UN solo
cambio de codigo fuente antes de compilar (ver `app/starlink_dist/
PATCH.md`): la app da por hecho que se sirve en la RAIZ del dominio (su
dev harness, Electron, la extension) -- aqui cuelga de
"/plugins/starlink/", nunca de "/". El parche llama a `setDishHost()`/
`setApiHost()`/`setCloudHost()` (los propios mecanismos de extension del
proyecto) con rutas RELATIVAS -- cero logica nueva, solo apuntarlas
donde de verdad cuelga esta pagina.

`app/starlink_node/` es su HISTORIAN real (`collector/`, registro de
historico dia/semana/mes -- sin el, esas graficas se quedan siempre
vacias) y su SERVIDOR DE CUENTA real (`cloud/starlinkCloudHandler.ts`,
identidad/plan/direccion de servicio, control de dispositivos del
router via la nube) -- vendorizados TAMBIEN tal cual, mas un fichero
NUEVO de esta integracion (`cloud-server.mts`, sin equivalente en el
proyecto original: une el handler real de cuenta a un `node:http`
normal y corriente en vez de un plugin de Vite, porque aqui no hay
ningun Vite en produccion -- mismo espiritu que el proxy `/dishy` de
aqui abajo). Los dos corren como procesos Node de fondo (ver
`start_background_threads`), y este backend Python los expone por
`/api/*` y `/cloud/*` -- mismos nombres de ruta relativos que ya usa el
frontend sin ningun cambio adicional.

El dish (192.168.100.1:9201) habla grpc-web DIRECTO desde el navegador
en el modo "dev harness" del proyecto original -- pero el dish solo
responde CORS/Referer a su propio origen (ver LOCAL-API.md: "port 9201
only answers CORS preflights for the dish's own origin" + "requests
carrying an unrecognized Referer header get an empty 200 back"), asi
que un origen de terceros no puede llamarlo cross-origin de verdad. Su
propio servidor de desarrollo (Vite) resuelve esto con un proxy
same-origin en "/dishy" que quita Referer/Origin antes de reenviar (ver
su vite.config.ts) -- este plugin REPLICA ese mismo contrato en Python
para el dish y el router (`_dishy_proxy`/`_router_proxy` mas abajo); el
historian y el cloud-server, al ser procesos Node normales, hablan con
el dish/router directamente sin ese problema (no son un navegador).

Router: IP configurable (`starlink_store.py`, `/api/router-config`)
porque, a diferencia del dish, la IP por defecto del router Starlink
(192.168.1.1) puede colisionar con el router propio de esta instalacion
(misma LAN 192.168.1.0/24) -- automatico por defecto, con override
manual, exactamente lo pedido. Cambiar la IP reinicia el historian y el
cloud-server (las variables de entorno solo se leen al arrancar).
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading

import flask
import requests

import starlink_store
from plugin_base import Plugin

log = logging.getLogger("starlink_plugin")

_APP_DIR = os.path.dirname(__file__)
_DIST_DIR = os.path.join(_APP_DIR, "starlink_dist")
_NODE_DIR = os.path.join(_APP_DIR, "starlink_node")
_PROTOSET_PATH = os.path.join(_DIST_DIR, "dish.protoset")
_COOKIE_FILE = "/data/starlink/.starlink-cookie"

# IP fija del dish -- universal, la misma para cualquier instalacion de
# Starlink (no es una IP de ESTA red, es la que el propio dish se asigna
# a si mismo en su microred interna con el router). Puerto 9201 =
# grpc-web (HTTP/1.1), el que usa la app desde el navegador -- el 9200
# (grpc nativo HTTP/2) es el que usarian herramientas como `grpcurl`,
# nunca un fetch de navegador.
DISH_BASE_URL = "http://192.168.100.1:9201"
DISH_HANDLE_URL = f"{DISH_BASE_URL}/SpaceX.API.Device.Device/Handle"
ROUTER_PORT = 9001
HISTORIAN_PORT = 8088
CLOUD_PORT = 8089
_PROXY_TIMEOUT_SECONDS = 10
_NODE_START_TIMEOUT_SECONDS = 20

# Mismas cabeceras que quita el proxy de desarrollo REAL de Dishylink
# (ver su vite.config.ts, evento "proxyReq") -- el dish/router devuelve
# un 200 vacio (nunca un error claro) si reconoce un Referer/Origin de
# navegador que no es el suyo propio.
_STRIP_REQUEST_HEADERS = {"referer", "origin", "host", "content-length", "connection"}
_STRIP_RESPONSE_HEADERS = {"content-length", "content-encoding", "transfer-encoding", "connection"}


def _router_ip() -> str:
    return (starlink_store.load_config().get("router_ip") or "").strip() or "192.168.1.1"


def _router_base_url() -> str:
    return f"http://{_router_ip()}:{ROUTER_PORT}"


def _router_handle_url() -> str:
    return f"{_router_base_url()}/SpaceX.API.Device.Device/Handle"


class StarlinkPlugin(Plugin):
    slug = "starlink"
    name = "Starlink Orchestrator"
    version = "0.2.0"

    def __init__(self) -> None:
        self._app = flask.Flask("starlink_plugin", static_folder=_DIST_DIR, static_url_path="")
        self._node_lock = threading.Lock()
        self._historian_proc: subprocess.Popen | None = None
        self._cloud_proc: subprocess.Popen | None = None
        self._register_routes()

    def flask_app(self):
        return self._app

    # ------------------------------------------------------------- arranque

    def start_background_threads(self) -> None:
        self._ensure_node_deps()
        self._start_node_services()

    def _ensure_node_deps(self) -> None:
        """`npm install` UNA vez (las dependencias no viajan en el
        tarball descargado, ver `.gitignore`/`.dockerignore` -- ni el
        proyecto original ni este las commitea). Bloqueante a proposito:
        el historian/cloud-server no tienen nada que hacer sin esto, y
        es una sola vez por instalacion/actualizacion del plugin, no en
        cada arranque (se salta si `node_modules` ya existe)."""
        node_modules = os.path.join(_NODE_DIR, "node_modules")
        if os.path.isdir(node_modules):
            return
        log.info("Starlink: instalando dependencias Node (primera vez, tsx + @bufbuild/protobuf)...")
        try:
            subprocess.run(
                ["npm", "install", "--no-audit", "--no-fund", "--omit=dev"],
                cwd=_NODE_DIR, check=True, capture_output=True, text=True, timeout=180,
            )
            log.info("Starlink: dependencias Node instaladas")
        except Exception:
            log.exception("Starlink: fallo instalando dependencias Node -- historian/cuenta no arrancaran")

    def _node_env(self) -> dict:
        env = dict(os.environ)
        env["DISH_URL"] = DISH_HANDLE_URL
        env["ROUTER_URL"] = _router_handle_url()
        env["HISTORIAN_PROTOSET"] = _PROTOSET_PATH
        return env

    def _start_node_services(self) -> None:
        if not os.path.isdir(os.path.join(_NODE_DIR, "node_modules")):
            log.warning("Starlink: node_modules ausente, no se arrancan historian/cuenta")
            return
        env = self._node_env()
        os.makedirs(os.path.dirname(_COOKIE_FILE), exist_ok=True)

        with self._node_lock:
            historian_env = dict(env, HISTORIAN_DATA_DIR="/data/starlink/historian", HISTORIAN_PORT=str(HISTORIAN_PORT))
            self._historian_proc = subprocess.Popen(
                ["npx", "tsx", "collector/historian.mts"],
                cwd=_NODE_DIR, env=historian_env,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            )
            cloud_env = dict(env, CLOUD_PORT=str(CLOUD_PORT), CLOUD_COOKIE_FILE=_COOKIE_FILE)
            self._cloud_proc = subprocess.Popen(
                ["npx", "tsx", "cloud-server.mts"],
                cwd=_NODE_DIR, env=cloud_env,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            )
        threading.Thread(target=self._log_stderr, args=(self._historian_proc, "historian"), daemon=True).start()
        threading.Thread(target=self._log_stderr, args=(self._cloud_proc, "cloud-server"), daemon=True).start()
        log.info("Starlink: historian (puerto %d) y servidor de cuenta (puerto %d) arrancados", HISTORIAN_PORT, CLOUD_PORT)

    def _log_stderr(self, proc: subprocess.Popen, label: str) -> None:
        if proc.stderr is None:
            return
        for line in proc.stderr:
            log.info("[starlink:%s] %s", label, line.rstrip())

    def _restart_node_services(self) -> None:
        """Llamado tras guardar una IP de router nueva -- las variables
        de entorno (`ROUTER_URL`) solo se leen al arrancar cada proceso
        Node, asi que un cambio en caliente necesita relanzarlos."""
        with self._node_lock:
            for proc in (self._historian_proc, self._cloud_proc):
                if proc is not None and proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
        self._start_node_services()

    # --------------------------------------------------------------- rutas

    def _register_routes(self) -> None:
        app = self._app

        @app.get("/")
        def _index():
            return flask.send_from_directory(_DIST_DIR, "index.html")

        @app.post("/dishy/<path:sub>")
        def _dishy_proxy(sub):
            """Espejo exacto del proxy de desarrollo real del proyecto
            (`/dishy` -> `192.168.100.1:9201`, cabeceras Referer/Origin
            fuera) -- ver docstring del modulo. `sub` incluye ya el
            metodo RPC completo (p.ej. `SpaceX.API.Device.Device/Handle`),
            el cliente lo construye igual que en su propio codigo."""
            return self._proxy_response(DISH_BASE_URL, sub, "dish")

        @app.post("/router/<path:sub>")
        def _router_proxy(sub):
            """Mismo mecanismo que `_dishy_proxy`, contra el ROUTER en
            vez del dish -- IP configurable, ver `/api/router-config`."""
            return self._proxy_response(_router_base_url(), sub, "router")

        @app.get("/api/router-config")
        def _get_router_config():
            return flask.jsonify(starlink_store.load_config())

        @app.post("/api/router-config")
        def _set_router_config():
            payload = flask.request.get_json(force=True) or {}
            result = starlink_store.save_router_ip(payload.get("router_ip", ""))
            self._restart_node_services()
            return flask.jsonify(result)

        @app.route("/api/<path:sub>", methods=["GET", "POST"])
        def _historian_proxy(sub):
            """El historian REAL (ver docstring del modulo) -- registro
            de historico dia/semana/mes, alertas, eventos, mapa de
            obstruccion. `sub` incluye ya la ruta completa de su propia
            API (`samples`, `energy`, `outages`, `alerts`...)."""
            return self._local_proxy(HISTORIAN_PORT, f"api/{sub}", "historian")

        @app.route("/cloud/<path:sub>", methods=["GET", "POST", "DELETE"])
        def _cloud_proxy(sub):
            """El servidor de cuenta REAL (ver docstring del modulo)."""
            return self._local_proxy(CLOUD_PORT, f"cloud/{sub}", "cloud-server")

    @staticmethod
    def _proxy_response(base_url: str, sub: str, target_label: str):
        """Comun a `_dishy_proxy`/`_router_proxy`. `target_label` solo
        para el mensaje de log/error (nunca se filtra el detalle real de
        la excepcion al cliente -- alerta real de CodeQL, py/stack-
        trace-exposure, corregida: antes se devolvia `str(exc)` tal
        cual, exponiendo detalles internos de red a quien sea que pueda
        alcanzar esta pagina)."""
        headers = {
            k: v for k, v in flask.request.headers.items()
            if k.lower() not in _STRIP_REQUEST_HEADERS
        }
        try:
            upstream = requests.post(
                f"{base_url}/{sub}",
                headers=headers,
                data=flask.request.get_data(),
                timeout=_PROXY_TIMEOUT_SECONDS,
            )
        except requests.RequestException:
            log.warning("Fallo hablando con el %s de Starlink (%s)", target_label, sub, exc_info=True)
            return flask.Response(f"{target_label} unreachable", status=502)
        resp_headers = [
            (k, v) for k, v in upstream.headers.items()
            if k.lower() not in _STRIP_RESPONSE_HEADERS
        ]
        return flask.Response(upstream.content, status=upstream.status_code, headers=resp_headers)

    @staticmethod
    def _local_proxy(port: int, sub: str, target_label: str):
        """Proxy simple hacia un servicio Node LOCAL (historian/cuenta,
        ver `start_background_threads`) -- sin las cabeceras especiales
        del dish/router (no hace falta, no es el mismo servicio), pero
        mismo cuidado de nunca filtrar el detalle de la excepcion al
        cliente."""
        try:
            upstream = requests.request(
                flask.request.method,
                f"http://127.0.0.1:{port}/{sub}",
                params=flask.request.args,
                data=flask.request.get_data(),
                headers={"Content-Type": flask.request.content_type} if flask.request.content_type else {},
                timeout=_PROXY_TIMEOUT_SECONDS,
            )
        except requests.RequestException:
            log.warning("Fallo hablando con el servicio local '%s' (%s)", target_label, sub, exc_info=True)
            return flask.Response(f"{target_label} not running", status=503)
        return flask.Response(upstream.content, status=upstream.status_code, content_type=upstream.headers.get("content-type"))

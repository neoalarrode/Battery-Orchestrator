"""
Plugin de Starlink para el nucleo Home Orchestrator -- a diferencia del
resto de plugins, NO es una reimplementacion propia: sirve TAL CUAL (a
peticion expresa del usuario, "adaptalo minimamente, no quiero
desperdiciar unos graficos muy bonitos") el build web oficial de
Dishylink (https://github.com/DaveyHert/dishylink, MIT), una app de
monitorizacion de Starlink ya hecha y muy cuidada -- el objetivo aqui no
es rehacer sus graficos, es integrarla.

`app/starlink_dist/` es su propio `npm run build --base=./` con UN solo
cambio de codigo fuente antes de compilar (ver `app/starlink_dist/
PATCH.md` para el detalle exacto y como reconstruirlo tras una
actualizacion del proyecto original): la app da por hecho que se sirve
en la RAIZ del dominio (su dev harness, Electron, la extension) -- aqui
cuelga de "/plugins/starlink/", nunca de "/". Sin el parche, tanto el
proxy `/dishy` como el fichero `dish.protoset` se pedian con rutas
absolutas de raiz de dominio ("/dishy/...", "/dish.protoset"), que bajo
Ingress (o incluso por IP directa) se resuelven contra el sitio
equivocado -- BUG REAL, confirmado en produccion: la interfaz mostraba
"dish unreachable" pero CERO peticiones llegaban al proxy de aqui abajo,
porque el fetch del protoset fallaba antes de intentar hablar con el
dish siquiera. El parche llama a `setDishHost()` (el propio mecanismo de
extension del proyecto, ya usado por sus builds de Electron/extension)
con las mismas rutas mas como RELATIVAS -- cero logica nueva, solo
apuntarlas donde de verdad cuelga esta pagina.

Lo UNICO que hace falta en el backend (aparte de ese parche): la app
habla con el dish (192.168.100.1:9201) por grpc-web DIRECTO desde el
navegador en su modo "dev harness" -- pero el dish solo responde
CORS/Referer a su propio origen (ver LOCAL-API.md del proyecto: "port
9201 only answers CORS preflights for the dish's own origin" + "requests
carrying an unrecognized Referer header get an empty 200 back"), asi que
un origen de terceros no puede llamarlo cross-origin de verdad. Su
propio servidor de desarrollo (Vite) ya resuelve esto con un proxy
same-origin en "/dishy" que reescribe la URL y quita las cabeceras
Referer/Origin antes de reenviar (ver su vite.config.ts) -- este plugin
REPLICA EXACTAMENTE ese mismo contrato en el backend, ya que aqui no hay
ningun Vite corriendo en produccion.

Deliberadamente SIN proxy de router (`/router/...`, lista de
dispositivos/uso por wifi de la app original): la direccion por defecto
del router Starlink (192.168.1.1, ver su core/dishClient.ts) coincide
muy probablemente con la del propio router de esta instalacion (LAN
192.168.1.0/24, mismo rango que el host de HAOS, 192.168.1.93) --
reenviar ahi seria hablar con el router equivocado del usuario, no con
el de Starlink. El dashboard del DISH (rendimiento, latencia,
obstruccion, alineacion, consumo) funciona igual sin esto -- si en algun
momento se confirma que el router Starlink SI tiene una IP propia
distinguible en esta instalacion, añadir ese proxy es trivial (mismo
patron que `_dishy_proxy` de aqui abajo).
"""

from __future__ import annotations

import logging
import os

import flask
import requests

from plugin_base import Plugin

log = logging.getLogger("starlink_plugin")

_DIST_DIR = os.path.join(os.path.dirname(__file__), "starlink_dist")

# IP fija del dish -- universal, la misma para cualquier instalacion de
# Starlink (no es una IP de ESTA red, es la que el propio dish se asigna
# a si mismo en su microred interna con el router). Puerto 9201 =
# grpc-web (HTTP/1.1), el que usa la app desde el navegador -- el 9200
# (grpc nativo HTTP/2) es el que usarian herramientas como `grpcurl`,
# nunca un fetch de navegador.
DISH_BASE_URL = "http://192.168.100.1:9201"
_PROXY_TIMEOUT_SECONDS = 10

# Mismas cabeceras que quita el proxy de desarrollo REAL de Dishylink
# (ver su vite.config.ts, evento "proxyReq") -- el dish devuelve un 200
# vacio (nunca un error claro) si reconoce un Referer/Origin de
# navegador que no es el suyo propio.
_STRIP_REQUEST_HEADERS = {"referer", "origin", "host", "content-length", "connection"}
_STRIP_RESPONSE_HEADERS = {"content-length", "content-encoding", "transfer-encoding", "connection"}


class StarlinkPlugin(Plugin):
    slug = "starlink"
    name = "Starlink Orchestrator"
    version = "0.1.1"

    def __init__(self) -> None:
        self._app = flask.Flask("starlink_plugin", static_folder=_DIST_DIR, static_url_path="")
        self._register_routes()

    def flask_app(self):
        return self._app

    def start_background_threads(self) -> None:
        # Nada que arrancar -- la app habla con el dish EN VIVO desde el
        # propio navegador cada vez que la pagina esta abierta (poll a 1Hz
        # desde el cliente, ver su core/dishClient.ts), no hay ningun
        # ciclo de fondo propio de este plugin. El "historian" (registro
        # de historico de dias/semanas) es un proceso Node aparte del
        # proyecto original (`collector/`) que deliberadamente NO se ha
        # portado en esta primera integracion -- fuera del alcance de
        # "adaptacion minima".
        pass

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
            headers = {
                k: v for k, v in flask.request.headers.items()
                if k.lower() not in _STRIP_REQUEST_HEADERS
            }
            try:
                upstream = requests.post(
                    f"{DISH_BASE_URL}/{sub}",
                    headers=headers,
                    data=flask.request.get_data(),
                    timeout=_PROXY_TIMEOUT_SECONDS,
                )
            except requests.RequestException as exc:
                log.warning("Fallo hablando con el dish Starlink (%s): %s", sub, exc)
                return flask.Response(f"dish unreachable: {exc}", status=502)
            resp_headers = [
                (k, v) for k, v in upstream.headers.items()
                if k.lower() not in _STRIP_RESPONSE_HEADERS
            ]
            return flask.Response(upstream.content, status=upstream.status_code, headers=resp_headers)

        @app.get("/api/status")
        def _status():
            return flask.jsonify({"version": self.version})

"""
Contrato que cualquier plugin del nucleo Home Orchestrator tiene que
cumplir. Deliberadamente minimo por ahora (dos metodos): el objetivo de
esta primera fase es demostrar el mecanismo de carga con Battery como
primer plugin, no diseñar de golpe todo lo que un plugin pueda necesitar a
futuro (eso se amplia cuando de verdad haga falta, con Climate como
segundo plugin real probandolo).
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class Plugin(ABC):
    slug: str
    name: str
    version: str

    # Si True, este plugin puede servir la raiz "/" del nucleo (ver
    # core_app.py) -- Energy es hoy el unico que lo declara (tiene un
    # dashboard pensado para ser la app principal). Sin NINGUN plugin
    # instalado que lo declare, el nucleo sirve su propio catalogo/tienda
    # en la raiz en vez de nada (ver core_shell.py) -- asi una instalacion
    # fresca, de verdad vacia, siempre tiene algo que mostrar.
    serves_root: bool = False

    @abstractmethod
    def flask_app(self):
        """
        Devuelve la app Flask de este plugin. Con un solo plugin instalado
        (el caso de hoy), el nucleo sirve esta app directamente. El dia que
        haya mas de uno a la vez, esto pasa a fusionarse como blueprints
        bajo una app compartida del nucleo -- no hace falta resolverlo
        todavia con un solo plugin real.
        """

    @abstractmethod
    def start_background_threads(self) -> None:
        """Arranca los hilos de fondo propios del plugin (ciclo de
        planificacion, publicacion de sensores en vivo, WebSocket
        reactivo...). Se llama una vez, al arrancar el nucleo."""

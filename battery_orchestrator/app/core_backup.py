"""
Copia de seguridad COMPLETA del directorio persistente (`/data`), a nivel
de nucleo -- no solo la configuracion de un plugin (eso ya existia, ver
`/api/config/export` de Battery). Pensado para poder respaldar la
instalacion entera antes de un cambio de riesgo (p.ej. sacar un plugin
del Dockerfile) y restaurarla exactamente si algo sale mal.

Deliberadamente generico: recoge TODOS los `*.json` directamente bajo
`/data` (config de cada plugin, historicos, estado de sesion...) sin
tener que conocer de antemano la lista exacta de ficheros de cada
plugin -- un plugin nuevo que añada su propio fichero de estado entra
solo en el backup sin tocar este modulo. `options.json` se excluye a
proposito: es de Supervisor (opciones del addon), no datos de la app, y
restaurarlo a ciegas podria pisar algo que Supervisor gestiona por su
cuenta.
"""

from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("core_backup")

DATA_DIR = os.path.dirname(__import__("config_store").CONFIG_PATH)
EXCLUDED_FILES = {"options.json"}
BACKUP_FORMAT_VERSION = 1


class BackupError(Exception):
    pass


def create_backup() -> dict:
    """Devuelve un dict serializable en JSON con TODOS los ficheros
    `*.json` de `/data` (menos los excluidos), listo para descargar tal
    cual desde la interfaz."""
    files = {}
    if os.path.isdir(DATA_DIR):
        for name in sorted(os.listdir(DATA_DIR)):
            if not name.endswith(".json") or name in EXCLUDED_FILES:
                continue
            path = os.path.join(DATA_DIR, name)
            if not os.path.isfile(path):
                continue
            try:
                with open(path) as f:
                    files[name] = json.load(f)
            except (OSError, json.JSONDecodeError):
                log.exception("No se pudo leer '%s' para el backup -- se omite", name)

    return {
        "backup_format_version": BACKUP_FORMAT_VERSION,
        "files": files,
    }


def restore_backup(bundle: dict) -> list[str]:
    """Escribe de vuelta cada fichero del bundle en `/data`, atomico por
    fichero (escribe a `.tmp` y renombra) para no dejar nada a medias si
    falla a mitad. Devuelve la lista de ficheros restaurados. No borra
    ficheros que existan en disco pero no esten en el bundle -- restaurar
    nunca deja la instalacion con MENOS datos de los que tenia, solo
    sobrescribe lo que trae el backup."""
    if not isinstance(bundle, dict) or not isinstance(bundle.get("files"), dict):
        raise BackupError("el archivo no tiene el formato esperado de copia de seguridad")

    files = bundle["files"]
    restored = []
    real_data_dir = os.path.realpath(DATA_DIR)
    os.makedirs(DATA_DIR, exist_ok=True)
    for name, content in files.items():
        if not isinstance(name, str) or not name.endswith(".json") or name in EXCLUDED_FILES:
            log.warning("Backup: fichero '%r' ignorado (fuera del formato esperado)", name)
            continue
        path = os.path.join(DATA_DIR, name)
        # BUG REAL, marcado por CodeQL (py/path-injection): comprobar
        # solo que `name` no contenga "/"/"\\" no es una prueba real de
        # que la ruta final se queda dentro de DATA_DIR -- mas robusto
        # (y lo que CodeQL de verdad reconoce como neutralizado):
        # resolver la ruta final y comprobar que sigue siendo hija real
        # de DATA_DIR antes de escribir nada. `name` viene de un backup
        # subido por el propio usuario -- nunca de una fuente ajena,
        # pero la comprobacion cuesta lo mismo y cierra la clase entera
        # de bug, no solo el patron concreto que se nos ocurriera a
        # mano (symlinks, codificaciones raras...).
        if os.path.realpath(path) != os.path.join(real_data_dir, name):
            log.warning("Backup: fichero '%s' ignorado (ruta fuera de %s)", name, DATA_DIR)
            continue
        tmp_path = path + ".restoring.tmp"
        with open(tmp_path, "w") as f:
            json.dump(content, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
        restored.append(name)

    log.info("Backup restaurado: %d ficheros (%s)", len(restored), ", ".join(restored))
    return restored

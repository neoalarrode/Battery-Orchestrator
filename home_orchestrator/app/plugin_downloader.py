"""
Descarga e instalacion VERIFICADA de plugins de primera parte.

Diseño (ver conversacion): solo del propio repo (`neoalarrode/Home-
Orchestrator`), nunca una URL arbitraria -- "descargar" es siempre
`git archive` de un TAG concreto (el mismo mecanismo que ya usa Supervisor
para clonar el addon), verificado por sha256 ANTES de tocar disco. El tag
y el sha256 estan pineados en `plugin_loader.PLUGIN_CATALOG` (a mano, en
el momento de publicar cada version -- mismo criterio que el resto de
numeros de version duplicados en este repo). Si el hash no coincide, se
descarta entero y no se instala nada -- falla cerrado.

Cada plugin persiste bajo `/data/plugins/<slug>/<tag>/`, con un symlink
`current` a la version activa -- `/data/` es el volumen persistente del
addon, asi que sobrevive a actualizaciones/reinicios Y a restaurar una
copia de seguridad de Supervisor (que ya incluye todo `/data` sin logica
especial por mi parte, ver conversacion). `plugin_loader.py` antepone esa
ruta a `sys.path` antes de importar, asi que una version descargada
siempre gana sobre la que venga precargada en la imagen (si la hay).
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import shutil
import tarfile
import urllib.request

log = logging.getLogger("plugin_downloader")

REPO = "neoalarrode/Home-Orchestrator"
DATA_DIR = os.environ.get("PLUGIN_DATA_DIR", "/data/plugins")
SUBPATH = "home_orchestrator/app"  # donde vive el codigo de los plugins dentro del repo
DOWNLOAD_TIMEOUT_SECONDS = 30


class PluginDownloadError(Exception):
    pass


def _tarball_url(tag: str) -> str:
    return f"https://github.com/{REPO}/archive/refs/tags/{tag}.tar.gz"


def _fetch_tarball(tag: str) -> bytes:
    url = _tarball_url(tag)
    try:
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as r:
            return r.read()
    except Exception as exc:
        raise PluginDownloadError(f"no se pudo descargar {url}: {exc}") from exc


def _safe_relpath(name: str, prefix: str) -> str:
    rel = os.path.relpath(name, prefix)
    if rel.startswith("..") or os.path.isabs(rel):
        raise PluginDownloadError(f"ruta fuera de sitio en el tarball: {name!r}")
    return rel


def is_installed(slug: str, tag: str) -> bool:
    return os.path.isdir(os.path.join(DATA_DIR, slug, tag))


def current_path(slug: str) -> str | None:
    link = os.path.join(DATA_DIR, slug, "current")
    return link if os.path.exists(link) else None


def download_plugin(slug: str, tag: str, sha256_hex: str, files: list[str]) -> str:
    """Descarga el tarball del tag, verifica su sha256, extrae SOLO los
    ficheros/paquetes listados en `files` (rutas relativas a
    `SUBPATH/` dentro del tarball) a `DATA_DIR/<slug>/<tag>/`, y mueve el
    symlink `current` a esa version. Devuelve la ruta final."""
    log.info("Descargando plugin '%s' @ %s", slug, tag)
    data = _fetch_tarball(tag)

    digest = hashlib.sha256(data).hexdigest()
    if digest.lower() != sha256_hex.lower():
        raise PluginDownloadError(
            f"sha256 no coincide para {slug}@{tag}: esperado {sha256_hex}, obtenido {digest} -- descartado, no se instala nada"
        )

    dest_dir = os.path.join(DATA_DIR, slug, tag)
    tmp_dir = dest_dir + ".tmp"
    if os.path.exists(tmp_dir):
        shutil.rmtree(tmp_dir)
    os.makedirs(tmp_dir, exist_ok=True)

    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            names = tar.getnames()
            if not names:
                raise PluginDownloadError(f"tarball vacio para {slug}@{tag}")
            root_prefix = names[0].split("/")[0]
            base = f"{root_prefix}/{SUBPATH}"

            for rel in files:
                member_prefix = f"{base}/{rel}"
                matches = [
                    m for m in tar.getmembers()
                    if m.name == member_prefix or m.name.startswith(member_prefix + "/")
                ]
                if not matches:
                    raise PluginDownloadError(f"'{rel}' no encontrado en el tarball de {slug}@{tag}")
                for m in matches:
                    relative = _safe_relpath(m.name, base)
                    target = os.path.join(tmp_dir, relative)
                    if m.isdir():
                        os.makedirs(target, exist_ok=True)
                    elif m.isfile():
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        with open(target, "wb") as f:
                            f.write(tar.extractfile(m).read())
                    # symlinks/otros tipos dentro del repo: ignorados a proposito
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    if os.path.exists(dest_dir):
        shutil.rmtree(dest_dir)
    os.rename(tmp_dir, dest_dir)

    current_link = os.path.join(DATA_DIR, slug, "current")
    if os.path.islink(current_link) or os.path.exists(current_link):
        os.remove(current_link)
    os.symlink(dest_dir, current_link)

    log.info("Plugin '%s' @ %s instalado y verificado en %s", slug, tag, dest_dir)
    return dest_dir


def remove_plugin_files(slug: str) -> None:
    d = os.path.join(DATA_DIR, slug)
    if os.path.isdir(d) or os.path.islink(d):
        shutil.rmtree(d, ignore_errors=True)
        log.info("Ficheros descargados de '%s' eliminados", slug)

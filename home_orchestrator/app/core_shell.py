"""
Nucleo DE VERDAD de Home Orchestrator -- lo unico que la imagen trae
siempre, sea cual sea el plugin (o ninguno) que este instalado.

Expone dos cosas:
  - `core_api_bp`: blueprint con la tienda de plugins (`/api/core/plugins*`)
    y la copia de seguridad completa (`/api/core/backup*`). Deliberadamente
    un Blueprint, no rutas sueltas de una app concreta: si HAY un plugin
    que sirve la raiz (ver `Plugin.serves_root`, hoy solo Energy), este
    blueprint se registra DIRECTAMENTE sobre su app (`app.register_blueprint`)
    para que seguir sirviendo en el mismo origen de siempre; si NO hay
    ninguno (instalacion recien nacida, o Energy desinstalado), se registra
    sobre `build_shell_app()` en su lugar. Mismas rutas, misma API, sirvan
    lo que sirvan por debajo.
  - `build_shell_app()`: la app que se sirve en la raiz cuando NINGUN
    plugin instalado declara `serves_root` -- un catalogo minimo con
    boton de instalar por plugin y, ademas, restaurar una copia de
    seguridad completa (que de paso INSTALA los plugins que esa copia
    tenia instalados, no solo restaura sus ficheros -- ver
    `_ensure_plugins_from_config`).
"""

from __future__ import annotations

import logging
import os

import flask
from flask import Blueprint, jsonify, request

log = logging.getLogger("core_shell")

core_api_bp = Blueprint("core_api", __name__)

# Sistema de diseño compartido (CSS/JS reusado por TODAS las paginas de
# plugin, ver core_static/design-system.css) -- fichero del NUCLEO, nunca
# descargable por plugin, servido en una ruta estable ("/shared/...")
# para que valga igual este quien este instalado en la raiz. Un
# Blueprint con `static_folder` es la forma normal de Flask de servir un
# directorio de ficheros estaticos sin tener que escribir una vista por
# fichero.
core_static_bp = Blueprint(
    "core_static", __name__,
    static_folder=os.path.join(os.path.dirname(__file__), "core_static"),
    static_url_path="/shared",
)


@core_api_bp.get("/api/core/plugins")
def api_list_plugins():
    import plugin_loader
    return jsonify(plugin_loader.list_catalog())


@core_api_bp.post("/api/core/plugins/<slug>/install")
def api_install_plugin(slug):
    import plugin_loader
    if slug not in plugin_loader.PLUGIN_REGISTRY:
        return jsonify({"error": "plugin desconocido"}), 404
    try:
        plugin_loader.install_plugin(slug)
    except Exception:
        log.exception("Fallo instalando el plugin '%s'", slug)
        return jsonify({"error": "fallo instalando el plugin"}), 502
    return jsonify({"installed": True, "restart_required": True})


@core_api_bp.post("/api/core/plugins/<slug>/uninstall")
def api_uninstall_plugin(slug):
    import plugin_loader
    try:
        plugin_loader.uninstall_plugin(slug)
    except ValueError:
        log.warning("Fallo desinstalando el plugin '%s'", slug, exc_info=True)
        return jsonify({"error": "plugin desconocido"}), 400
    return jsonify({"installed": False, "restart_required": True})


@core_api_bp.get("/api/core/backup")
def api_core_backup():
    import core_backup
    bundle = core_backup.create_backup()
    resp = jsonify(bundle)
    resp.headers["Content-Disposition"] = "attachment; filename=home_orchestrator_backup.json"
    return resp


def _ensure_plugins_from_config() -> list[str]:
    """Tras restaurar los ficheros de una copia de seguridad, `config.json`
    ya trae `core.installed_plugins` de la instalacion original -- pero
    eso es solo DATOS; el CODIGO de un plugin descargable puede no estar
    presente todavia en esta maquina (p.ej. una instalacion fresca que
    restaura sobre si misma). Se descarga/verifica cada uno que falte,
    igual que si el usuario hubiera pulsado "Instalar" a mano uno a uno."""
    import config_store
    import plugin_loader

    ensured = []
    for slug in config_store.get_installed_plugins():
        if slug not in plugin_loader.PLUGIN_REGISTRY:
            log.warning("Backup: plugin '%s' referenciado en la copia pero no existe en este catalogo -- se omite", slug)
            continue
        try:
            plugin_loader.install_plugin(slug)
            ensured.append(slug)
        except Exception:
            log.exception("Backup: fallo asegurando el codigo del plugin '%s'", slug)
    return ensured


@core_api_bp.post("/api/core/backup/restore")
def api_core_backup_restore():
    import core_backup

    bundle = request.get_json(force=True, silent=True)
    if bundle is None:
        return jsonify({"error": "JSON invalido"}), 400
    try:
        restored = core_backup.restore_backup(bundle)
    except core_backup.BackupError:
        log.warning("Fallo restaurando la copia de seguridad", exc_info=True)
        return jsonify({"error": "el fichero de copia de seguridad no es valido"}), 400

    ensured_plugins = _ensure_plugins_from_config() if "config.json" in restored else []
    return jsonify({
        "restored_files": restored,
        "ensured_plugins": ensured_plugins,
        "restart_required": True,
    })


_CATALOG_PAGE = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Home Orchestrator</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20viewBox%3D%270%200%2024%2024%27%3E%3Cdefs%3E%3ClinearGradient%20id%3D%27g%27%20x1%3D%270%27%20y1%3D%270%27%20x2%3D%2724%27%20y2%3D%2724%27%20gradientUnits%3D%27userSpaceOnUse%27%3E%3Cstop%20offset%3D%270%27%20stop-color%3D%27%238b5cf6%27%2F%3E%3Cstop%20offset%3D%271%27%20stop-color%3D%27%2322d3ee%27%2F%3E%3C%2FlinearGradient%3E%3C%2Fdefs%3E%3Crect%20width%3D%2724%27%20height%3D%2724%27%20rx%3D%276%27%20fill%3D%27url%28%23g%29%27%2F%3E%3C%2Fsvg%3E">
<style>
  :root {
    color-scheme: dark light;
    --bg:#0b0a16; --card:#14132a; --text:#eae8f7; --muted:#8b87ab; --border:#2a2850;
    --accent:#8b5cf6; --accent-2:#22d3ee; --accent-soft:#8b5cf626; --accent-ink:#0b0a16;
    --glow: 0 0 0 1px #8b5cf64d, 0 0 24px -6px #8b5cf673;
    --green:#34d399; --green-soft:#34d39920; --red:#fb7185; --red-soft:#fb718520;
    --radius:12px; --font: -apple-system,"Segoe UI",Arial,sans-serif;
    --font-mono: ui-monospace,"SF Mono",Menlo,Consolas,monospace;
  }
  @media (prefers-color-scheme: light) {
    :root { --bg:#f2f1fa; --card:#fff; --text:#1c1a2e; --muted:#68648c; --border:#dcd9f2;
      --accent:#7c3aed; --accent-2:#0891b2; --accent-soft:#7c3aed15; --accent-ink:#fff;
      --green:#159a67; --green-soft:#159a6715; --red:#e0475f; --red-soft:#e0475f15; }
  }
  * { box-sizing: border-box; }
  body { font-family: var(--font); background: var(--bg); color: var(--text); max-width: 720px; margin: 0 auto; padding: 48px 20px 90px; line-height: 1.5; }
  h1 { font-size: 1.5rem; margin: 0 0 6px; display: flex; align-items: center; gap: 10px; }
  .brand-mark { width: 32px; height: 32px; border-radius: 9px; background: linear-gradient(135deg, var(--accent), var(--accent-2)); display: inline-flex; align-items: center; justify-content: center; color: var(--accent-ink); box-shadow: var(--glow); }
  .subtitle { color: var(--muted); font-size: .92rem; margin-bottom: 32px; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: var(--radius); padding: 20px 22px; margin-bottom: 16px; }
  .card h2 { font-size: .92rem; margin: 0 0 4px; }
  .help { font-size: .8rem; color: var(--muted); margin: 0 0 14px; }
  .plugin-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px,1fr)); gap: 12px; margin-top: 6px; }
  .plugin-tile { border: 1px solid var(--border); border-radius: var(--radius); padding: 18px; text-align: center; display: flex; flex-direction: column; align-items: center; gap: 6px; }
  .plugin-tile-icon { width: 40px; height: 40px; border-radius: 11px; background: linear-gradient(135deg, var(--accent), var(--accent-2)); display: flex; align-items: center; justify-content: center; color: var(--accent-ink); }
  .plugin-tile-name { font-weight: 700; }
  .plugin-tile-desc { font-size: .76rem; color: var(--muted); }
  button { padding: 9px 16px; border-radius: 8px; border: none; cursor: pointer; background: var(--accent); color: var(--accent-ink); font-weight: 600; font-size: .85rem; margin-top: 8px; }
  button.secondary { background: var(--accent-soft); color: var(--accent); }
  .pill { display: inline-flex; align-items: center; gap: 6px; padding: 5px 12px; border-radius: 20px; font-size: .72rem; font-weight: 700; font-family: var(--font-mono); }
  .pill-ok { background: var(--green-soft); color: var(--green); }
  .toast { position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%); background: var(--text); color: var(--bg); padding: 10px 18px; border-radius: 9px; font-size: .85rem; opacity: 0; transition: opacity .25s; }
  .toast.show { opacity: .95; }
</style>
</head>
<body>
<h1><span class="brand-mark"><svg width="16" height="16" viewBox="0 0 24 24"><path d="M13 2 4 14h6l-1 8 9-12h-6l1-8Z" fill="currentColor"/></svg></span>Home Orchestrator</h1>
<div class="subtitle">Instalación nueva — elige qué instalar, o restaura una copia de seguridad para traer tu configuración de vuelta.</div>

<div class="card">
  <h2>Restaurar copia de seguridad</h2>
  <p class="help">Sube un archivo de copia de seguridad — se restaura toda tu configuración y se instalan (descargados y verificados) los mismos plugins que tenías.</p>
  <input type="file" id="restore-file" accept="application/json" style="display:none" onchange="restoreBackup(this)">
  <button onclick="document.getElementById('restore-file').click()">Restaurar desde archivo</button>
</div>

<div class="card">
  <h2>Catálogo de plugins</h2>
  <p class="help">Solo del catálogo oficial — nunca fuentes externas.</p>
  <div id="plugin-grid" class="plugin-grid"></div>
</div>

<div class="toast" id="toast"></div>

<script>
function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2600);
}
function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

// Mismos iconos SVG por plugin que el resto de paginas (ver
// core_static/plugin-switch.js) -- antes esta pagina usaba un emoji
// generico (⚡ solo para battery, ◐ para TODO lo demas) sin distinguir
// ningun plugin de otro.
const CATALOG_ICONS = {
  battery: '<path d="M13 2 4 14h6l-1 8 9-12h-6l1-8Z" fill="currentColor"/>',
  climate: '<path d="M12 3a3 3 0 0 0-3 3v7.1a4 4 0 1 0 6 0V6a3 3 0 0 0-3-3Zm0 2a1 1 0 0 1 1 1v7.6l.6.5a2 2 0 1 1-3.2 0l.6-.5V6a1 1 0 0 1 1-1Z" fill="currentColor"/>',
  tuya: '<path d="M6 4h9a2 2 0 0 1 2 2v9l-7 7-9-9V6a2 2 0 0 1 2-2Zm2.5 2.5a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3Z" fill="currentColor"/>',
  lighting: '<path d="M12 2a7 7 0 0 0-4 12.74V17a2 2 0 0 0 2 2h4a2 2 0 0 0 2-2v-2.26A7 7 0 0 0 12 2Zm-2 17h4v1a1 1 0 0 1-1 1h-2a1 1 0 0 1-1-1v-1Z" fill="currentColor"/>',
  tplink: '<path d="M12 5a7 7 0 0 0-4.95 11.95l1.41-1.41a5 5 0 1 1 7.08 0l1.41 1.41A7 7 0 0 0 12 5Zm0 4a3 3 0 0 0-2.12 5.12l1.41-1.41a1 1 0 1 1 1.42 0l1.41 1.41A3 3 0 0 0 12 9Z" fill="currentColor"/>',
  starlink: '<path d="M12 3a9 9 0 0 1 9 9h-2a7 7 0 0 0-7-7V3Z" fill="currentColor"/><circle cx="12" cy="12" r="2" fill="currentColor"/><path d="M12 14v7M9 21h6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" fill="none"/>',
};

async function loadCatalog() {
  const r = await fetch('api/core/plugins');
  const plugins = await r.json();
  document.getElementById('plugin-grid').innerHTML = plugins.map(p => `
    <div class="plugin-tile">
      <span class="plugin-tile-icon"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">${CATALOG_ICONS[p.slug] || ''}</svg></span>
      <span class="plugin-tile-name">${esc(p.name)}</span>
      <span class="plugin-tile-desc">${esc(p.description)}</span>
      ${p.installed
        ? '<span class="pill pill-ok">● Instalado</span>'
        : `<button onclick="install('${p.slug}')">Instalar</button>`}
    </div>
  `).join('');
}

async function install(slug) {
  const r = await fetch(`api/core/plugins/${slug}/install`, {method: 'POST'});
  if (r.ok) { toast('Instalado — reinicia el add-on para aplicarlo'); loadCatalog(); }
  else { const b = await r.json().catch(() => ({})); toast(b.error || 'Fallo al instalar'); }
}

async function restoreBackup(input) {
  const file = input.files[0];
  if (!file) return;
  try {
    const text = await file.text();
    const bundle = JSON.parse(text);
    const r = await fetch('api/core/backup/restore', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(bundle)});
    const body = await r.json();
    if (r.ok) {
      toast(`Restaurado (${body.restored_files.length} ficheros, ${body.ensured_plugins.length} plugin(s)) — reinicia el add-on`);
      loadCatalog();
    } else {
      toast(body.error || 'Fallo al restaurar');
    }
  } catch (e) {
    toast('Archivo inválido');
  }
  input.value = '';
}

loadCatalog();
</script>
</body>
</html>"""


def build_shell_app():
    """App minima servida en la raiz cuando NINGUN plugin instalado
    declara `serves_root` -- catalogo + restaurar copia de seguridad."""
    app = flask.Flask("core_shell")
    app.register_blueprint(core_api_bp)
    app.register_blueprint(core_static_bp)

    @app.get("/")
    def _catalog_page():
        return _CATALOG_PAGE

    return app

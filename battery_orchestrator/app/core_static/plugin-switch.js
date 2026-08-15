/*
Selector de plugins COMPARTIDO -- fichero del nucleo, servido en
/shared/plugin-switch.js (ver core_app.py) para que las 5 paginas dejen
de llevar cada una su propia copia pegada de iconos/etiquetas/logica de
rutas (que se iba desincronizando: un plugin nuevo habia que acordarse de
añadirlo a mano en CADA pagina).

Uso, en el <script> de cada pagina:

    renderPluginSwitch("climate");

Rutas siempre ABSOLUTAS ("/", "/plugins/<slug>/", "/api/core/plugins") --
`DispatcherMiddleware` (ver core_app.py) enruta cualquier peticion que no
empiece por "/plugins/<otro-slug>" al app raiz, asi que "/api/core/..."
y "/shared/..." resuelven igual da igual desde que pagina se pidan, sin
tener que calcular "cuantos niveles subir" en cada plantilla.
*/

const PLUGIN_ICONS = {
  battery: '<path d="M13 2 4 14h6l-1 8 9-12h-6l1-8Z" fill="currentColor"/>',
  climate: '<path d="M12 3a3 3 0 0 0-3 3v7.1a4 4 0 1 0 6 0V6a3 3 0 0 0-3-3Zm0 2a1 1 0 0 1 1 1v7.6l.6.5a2 2 0 1 1-3.2 0l.6-.5V6a1 1 0 0 1 1-1Z" fill="currentColor"/>',
  tuya: '<path d="M6 4h9a2 2 0 0 1 2 2v9l-7 7-9-9V6a2 2 0 0 1 2-2Zm2.5 2.5a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3Z" fill="currentColor"/>',
  lighting: '<path d="M12 2a7 7 0 0 0-4 12.74V17a2 2 0 0 0 2 2h4a2 2 0 0 0 2-2v-2.26A7 7 0 0 0 12 2Zm-2 17h4v1a1 1 0 0 1-1 1h-2a1 1 0 0 1-1-1v-1Z" fill="currentColor"/>',
  tplink: '<path d="M12 5a7 7 0 0 0-4.95 11.95l1.41-1.41a5 5 0 1 1 7.08 0l1.41 1.41A7 7 0 0 0 12 5Zm0 4a3 3 0 0 0-2.12 5.12l1.41-1.41a1 1 0 1 1 1.42 0l1.41 1.41A3 3 0 0 0 12 9Z" fill="currentColor"/>',
};
const PLUGIN_LABELS = { battery: "Energy", climate: "Climate", tuya: "Tuya", lighting: "Lighting", tplink: "TP-Link" };

// Solo los plugins con un dashboard de verdad aparecen en el selector de
// nivel superior (ver tarea de arquitectura de paginas) -- Tuya/TP-Link
// son pura configuracion, se acceden desde dentro de Climate/Lighting,
// no como una "app" mas con la que nadie interactua por si sola.
const PLUGIN_SWITCH_VISIBLE = new Set(["battery", "climate", "lighting"]);

function _pluginHref(slug) {
  return slug === "battery" ? "/" : `/plugins/${slug}/`;
}

async function renderPluginSwitch(slug, containerId = "plugin-switch-nav") {
  const nav = document.getElementById(containerId);
  if (!nav) return;
  try {
    const plugins = await (await fetch("/api/core/plugins")).json();
    nav.innerHTML = plugins
      .filter((p) => p.installed && PLUGIN_SWITCH_VISIBLE.has(p.slug))
      .map((p) => {
        const current = p.slug === slug ? ' class="current"' : "";
        const icon = PLUGIN_ICONS[p.slug] || "";
        const label = PLUGIN_LABELS[p.slug] || p.name;
        return `<a href="${_pluginHref(p.slug)}"${current}><svg width="13" height="13" viewBox="0 0 24 24" fill="none" aria-hidden="true">${icon}</svg>${label}</a>`;
      })
      .join("");
  } catch (e) {
    /* no bloquea el resto de la pagina */
  }
}

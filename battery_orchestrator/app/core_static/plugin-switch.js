/*
Selector de plugins COMPARTIDO -- fichero del nucleo, servido en
/shared/plugin-switch.js (ver core_app.py) para que las 5 paginas dejen
de llevar cada una su propia copia pegada de iconos/etiquetas/logica de
rutas (que se iba desincronizando: un plugin nuevo habia que acordarse de
añadirlo a mano en CADA pagina).

Uso, en el <script> de cada pagina:

    renderPluginSwitch("climate");

BUG REAL, confirmado por el usuario en produccion (las paginas se veian
sin ningun estilo -- serif, sin colores -- bajo Home Assistant): las
rutas de aqui (y las de `/shared/design-system.css`/`/shared/plugin-
switch.js` en cada plantilla) eran ABSOLUTAS ("/api/core/plugins",
"/plugins/<slug>/"). Eso solo es correcto accediendo al add-on
DIRECTAMENTE por su IP:puerto -- bajo el Ingress real de HA (`ingress:
true` en config.yaml, la via normal por la que el usuario entra desde la
barra lateral), el navegador esta en realidad en un prefijo dinamico
tipo "/api/hassio_ingress/<token>/...", y una ruta absoluta empezando
por "/" se va al DOMINIO RAIZ de HA, no al add-on -- 404 en todo lo que
empieza por "/", CSS y JS compartidos incluidos, y la navegacion entre
plugins tambien rota.

`ingressRoot()` calcula el prefijo real de la peticion en tiempo de
ejecucion (nunca hardcodeado) mirando la URL actual del navegador --
funciona igual de bien accediendo directo por IP:puerto (donde el
prefijo es simplemente "/") que por Ingress (cualquier prefijo, cualquier
profundidad). Los ficheros ESTATICOS (`design-system.css`, este mismo
script) no pueden usar esto -- se resuelven ANTES de que corra ningun
JS -- asi que cada plantilla los enlaza con una ruta RELATIVA fija segun
su propia profundidad de montaje (ver comentario en cada plantilla).
*/

function ingressRoot() {
  const path = location.pathname;
  const idx = path.indexOf("/plugins/");
  return idx === -1 ? path.replace(/[^/]*$/, "") : path.slice(0, idx + 1);
}

const PLUGIN_ICONS = {
  battery: '<path d="M13 2 4 14h6l-1 8 9-12h-6l1-8Z" fill="currentColor"/>',
  climate: '<path d="M12 3a3 3 0 0 0-3 3v7.1a4 4 0 1 0 6 0V6a3 3 0 0 0-3-3Zm0 2a1 1 0 0 1 1 1v7.6l.6.5a2 2 0 1 1-3.2 0l.6-.5V6a1 1 0 0 1 1-1Z" fill="currentColor"/>',
  tuya: '<path d="M6 4h9a2 2 0 0 1 2 2v9l-7 7-9-9V6a2 2 0 0 1 2-2Zm2.5 2.5a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3Z" fill="currentColor"/>',
  lighting: '<path d="M12 2a7 7 0 0 0-4 12.74V17a2 2 0 0 0 2 2h4a2 2 0 0 0 2-2v-2.26A7 7 0 0 0 12 2Zm-2 17h4v1a1 1 0 0 1-1 1h-2a1 1 0 0 1-1-1v-1Z" fill="currentColor"/>',
  tplink: '<path d="M12 5a7 7 0 0 0-4.95 11.95l1.41-1.41a5 5 0 1 1 7.08 0l1.41 1.41A7 7 0 0 0 12 5Zm0 4a3 3 0 0 0-2.12 5.12l1.41-1.41a1 1 0 1 1 1.42 0l1.41 1.41A3 3 0 0 0 12 9Z" fill="currentColor"/>',
  starlink: '<path d="M12 3a9 9 0 0 1 9 9h-2a7 7 0 0 0-7-7V3Z" fill="currentColor"/><circle cx="12" cy="12" r="2" fill="currentColor"/><path d="M12 14v7M9 21h6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" fill="none"/>',
};
const PLUGIN_LABELS = { battery: "Energy", climate: "Climate", tuya: "Tuya", lighting: "Lighting", tplink: "TP-Link", starlink: "Starlink" };

// Solo los plugins con un dashboard de verdad aparecen en el selector de
// nivel superior (ver tarea de arquitectura de paginas) -- Tuya/TP-Link
// son pura configuracion, se acceden desde dentro de Climate/Lighting,
// no como una "app" mas con la que nadie interactua por si sola. Starlink
// SI es un dashboard real (build oficial de Dishylink, ver starlink_
// plugin.py) -- va visible igual que Climate/Lighting.
const PLUGIN_SWITCH_VISIBLE = new Set(["battery", "climate", "lighting", "starlink"]);

function _pluginHref(slug) {
  const root = ingressRoot();
  return slug === "battery" ? root : `${root}plugins/${slug}/`;
}

async function renderPluginSwitch(slug, containerId = "plugin-switch-nav") {
  const nav = document.getElementById(containerId);
  if (!nav) return;
  try {
    const plugins = await (await fetch(`${ingressRoot()}api/core/plugins`)).json();
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

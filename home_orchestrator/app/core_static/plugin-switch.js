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

// "Configuración" no es un plugin -- vive en la pagina de Energy (rejilla
// con la config de cada plugin instalado), pero es alcanzable desde el
// selector de nivel superior de CUALQUIER pagina, a peticion expresa del
// usuario ("configuracion aplica a todos"). Antes solo vivia dentro del
// propio submenu de Energy; ver templates/index.html para la mitad que
// la recibe (`?tab=config`, manejado en su arranque).
const CONFIG_ICON =
  '<path d="M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Zm8.4 5.4-1.8.4a6.6 6.6 0 0 1-.8 1.9l1 1.6-1.7 1.7-1.6-1a6.6 6.6 0 0 1-1.9.8l-.4 1.8h-2.4l-.4-1.8a6.6 6.6 0 0 1-1.9-.8l-1.6 1-1.7-1.7 1-1.6a6.6 6.6 0 0 1-.8-1.9l-1.8-.4v-2.4l1.8-.4a6.6 6.6 0 0 1 .8-1.9l-1-1.6 1.7-1.7 1.6 1a6.6 6.6 0 0 1 1.9-.8l.4-1.8h2.4l.4 1.8a6.6 6.6 0 0 1 1.9.8l1.6-1 1.7 1.7-1 1.6a6.6 6.6 0 0 1 .8 1.9l1.8.4v2.4Z" fill="currentColor"/>';

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
    const pluginLinks = plugins
      .filter((p) => p.installed && PLUGIN_SWITCH_VISIBLE.has(p.slug))
      .map((p) => {
        const current = p.slug === slug ? ' class="current"' : "";
        const icon = PLUGIN_ICONS[p.slug] || "";
        const label = PLUGIN_LABELS[p.slug] || p.name;
        return `<a href="${_pluginHref(p.slug)}"${current}><svg width="13" height="13" viewBox="0 0 24 24" fill="none" aria-hidden="true">${icon}</svg><span>${label}</span></a>`;
      });
    const configHref = `${ingressRoot()}?tab=config`;
    const configLink = `<a href="${configHref}"${slug === "config" ? ' class="current"' : ""}><svg width="13" height="13" viewBox="0 0 24 24" fill="none" aria-hidden="true">${CONFIG_ICON}</svg><span>Configuración</span></a>`;
    nav.innerHTML = [...pluginLinks, configLink].join("");
  } catch (e) {
    /* no bloquea el resto de la pagina */
  }
}

/*
Mini-grafica de tendencia COMPARTIDA (sparkline), mismo patron visual que
las tarjetas de metrica del Dishylink real (Download/Upload/Latencia...):
una linea fina sin ejes ni rejilla, con un punto en el valor mas reciente.
Antes de esta version cada plugin que queria una de estas se la escribia
a mano (Energy ya llevaba la suya, para el SOC) -- esta es la version
generica para el resto (Climate, Lighting...), sin depender de que cada
pagina reimplemente el mismo SVG.

`values`: array de numeros, en orden cronologico (el ultimo es "ahora").
`opts.colorVar`: variable CSS a usar de color de linea/punto (por defecto
--accent). Devuelve "" si no hay al menos 2 puntos (nada que dibujar).
*/
function renderSparkline(values, opts = {}) {
  const pts = (values || []).filter(v => v !== null && v !== undefined && !Number.isNaN(v));
  if (pts.length < 2) return "";
  const colorVar = opts.colorVar || "--accent";
  const min = Math.min(...pts), max = Math.max(...pts);
  const range = Math.max(1e-6, max - min);
  const w = 100, h = 22, pad = 3;
  const coords = pts.map((v, i) => {
    const x = pts.length === 1 ? 0 : (i / (pts.length - 1)) * w;
    const y = pad + (1 - (v - min) / range) * (h - pad * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  const [lx, ly] = coords[coords.length - 1].split(",");
  return `<svg viewBox="0 0 ${w} ${h}" width="100%" height="${h}" class="sparkline" preserveAspectRatio="none" aria-hidden="true">
    <polyline points="${coords.join(" ")}" fill="none" stroke="var(${colorVar})" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"></polyline>
    <circle cx="${lx}" cy="${ly}" r="2.4" fill="var(${colorVar})"></circle>
  </svg>`;
}

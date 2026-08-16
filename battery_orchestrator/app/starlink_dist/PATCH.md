# Parches aplicados a Dishylink antes de compilar

`app/starlink_dist/` es el build web oficial de
[Dishylink](https://github.com/DaveyHert/dishylink) (MIT), con varios
cambios de código fuente antes de `npm run build` -- necesarios porque
esta app está pensada para correr en la raíz del dominio (dev harness,
Electron, extensión), y aquí se sirve bajo `/plugins/starlink/`, con
servicios de fondo reales detrás (ver `app/starlink_node/`, no forma
parte de este `dist/` sino que corre como procesos Node aparte).

## 1. `src/main.tsx` -- rutas relativas para dish/router/historian/cuenta

Añadidas llamadas a `setDishHost()`/`setApiHost()`/`setCloudHost()` (los
propios mecanismos de extensión del proyecto) antes de que la app
arranque:

```ts
import { setApiHost, setRecorderInProcess } from "./lib/apiHost.ts";
import { setDishHost } from "@core/dishClient.ts";
import { setSatelliteHost } from "./lib/satellites.ts";

setDishHost({
  dishHandleUrl: "dishy/SpaceX.API.Device.Device/Handle",
  protosetUrl: "dish.protoset",
  routerHandleUrl: "router/SpaceX.API.Device.Device/Handle",
});

const relativeFetch = (path: string, init?: RequestInit) => fetch(path.replace(/^\//, ""), init);
setApiHost({ transport: relativeFetch });
setCloudHost({
  transport: async ({ path, method = "GET", body, signal }) => {
    const response = await relativeFetch(path, {
      method, signal,
      ...(body === undefined ? {} : { headers: { "content-type": "application/json" }, body: JSON.stringify(body) }),
    });
    return { status: response.status, body: await response.json().catch(() => ({})) };
  },
});
setSatelliteHost("celestrak");
```

**Por qué**: `DISH_HANDLE_URL`, el `protosetUrl`/`routerHandleUrl` por
defecto, y las rutas hardcodeadas en `apiHost.ts`/`cloudHost.ts`
(`/api/...`, `/cloud/...`) son todas ABSOLUTAS de raíz de dominio. Bajo
Home Assistant Ingress (o incluso por IP directa, ya que esta app cuelga
de `/plugins/starlink/`, nunca de `/`), esas rutas se resuelven contra
el dominio raíz, no contra este add-on. Síntoma real visto en
producción: la interfaz mostraba "dish unreachable" con CERO peticiones
llegando al backend -- el `fetch` del protoset fallaba antes de intentar
hablar con el dish siquiera. La última línea (`setSatelliteHost`) es el
mismo problema para el mapa de satélites: `satellites.ts` pedía
`/celestrak/...` (también absoluto de raíz) contra un proxy que en dev
monta Vite (`vite.config.ts`) y que aquí no existía -- 404 silencioso,
síntoma real: "The satellite data source isn't responding right now"
perpetuo. `starlink_plugin.py` añade el proxy real (`/celestrak/<path>`
-> `celestrak.org`) y esta línea lo apunta ahí, igual que el resto.

## 2. `src/components/dashboard/TopBar.tsx` -- botón de vuelta

Un enlace (`../../`, misma convención relativa que el resto de Home
Orchestrator) al principio de la cabecera para volver a Home
Orchestrator -- esta app no lleva nuestro topbar/selector de plugins (a
propósito, mantiene su propio diseño intacto), así que necesitaba su
propio camino de vuelta.

## 3. `src/components/settings/RouterSettingsTab.tsx` -- IP de router manual

Nuevo componente `RouterAddressOverride` (mismo lenguaje visual que el
resto de la pestaña, usando `SettingRow`/`Input`/`actionButton` ya
existentes) que permite fijar a mano la IP del router cuando la
automática (192.168.1.1) no se alcanza -- muy probable en instalaciones
donde esa IP coincide con la del propio router de la vivienda. Guarda
via `/api/router-config` (nuevo, ver `starlink_plugin.py`).

## 4. `src/lib/routerAddressOverride.ts` -- NUEVO fichero

Cliente HTTP mínimo para el punto 3 (`GET`/`POST /api/router-config`).

## 5. `app/starlink_node/cloud-server.mts` -- NUEVO fichero (fuera de este `dist/`)

No forma parte del build web -- vive en `app/starlink_node/`, corre como
proceso Node de fondo. Une el handler de cuenta REAL del proyecto
(`cloud/starlinkCloudHandler.ts`, sin tocar) a un servidor `node:http`
normal en vez de un plugin de Vite (aquí no hay Vite en producción).
Documentado en `app/starlink_node/PATCH.md`.

## 6. `src/components/settings/RouterSettingsTab.tsx` -- lectura/escritura avanzada del router

Sustituido el aviso estático ("Custom DNS, bypass mode... not exposed
here") por controles reales de confirmación armada (mismo lenguaje
visual que `DangerAction`): nombre de red (SSID) por banda combinada o
solo 5 GHz, contraseña WPA2, bypass mode, DNS personalizado + lista de
servidores, DNS seguro, rango DHCP, país regulatorio, apagado
individual de banda 2.4/5 GHz, band steering y modo exterior. Content
filtering se documenta como NO disponible -- no existe ningún campo así
en el esquema real del dish (`dish.protoset`), a diferencia del resto,
que sí son campos reales confirmados contra el dispositivo.

Cada escritura pasa por `src/lib/wifiConfigUpdate.ts` (NUEVO,
`POST /cloud/wifi-config`, mismo patrón que `dishConfigUpdate.ts`) ->
`cloud/starlinkCloudHandler.ts` (`updateWifiConfig`, NUEVO en este
handler pero código real del mismo módulo, sin librería añadida) ->
`core/wifiConfigUpdate.ts` (NUEVO, construye el `wifiSetConfig` con los
flags `apply_*` reales del esquema) -> la cuenta cloud del usuario,
firmada con su cookie de sesión -- el firmware actual rechaza esta RPC
por LAN (grpc-status 7, ver `LOCAL-API.md` del proyecto original), así
que cada escritura requiere cuenta conectada, exactamente igual que un
cambio de configuración del dish.

## Para reconstruir tras una actualización de Dishylink

```bash
git clone --depth 1 https://github.com/DaveyHert/dishylink.git
cd dishylink
npm install
# aplicar los cambios 1-4 de arriba en src/
npx tsc -b
npx vite build --base=./
# copiar dist/* a app/starlink_dist/ de este repo (+ LICENSE como
# DISHYLINK_LICENSE.txt). Ver tambien app/starlink_node/PATCH.md para
# el historian/servidor de cuenta.
```

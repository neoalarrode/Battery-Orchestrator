# Parche aplicado a Dishylink antes de compilar

`app/starlink_dist/` es el build oficial de
[Dishylink](https://github.com/DaveyHert/dishylink) (MIT), con UN solo
cambio de código fuente antes de `npm run build` -- necesario porque esta
app está pensada para correr en la raíz del dominio (dev harness,
Electron, extensión), y aquí se sirve bajo `/plugins/starlink/`.

## El cambio

En `src/main.tsx`, añadida una llamada a `setDishHost()` (el propio
mecanismo de extensión del proyecto, ya usado por las builds de
Electron/extensión) antes de que la app arranque:

```ts
import { setDishHost } from "@core/dishClient.ts";

setDishHost({
  dishHandleUrl: "dishy/SpaceX.API.Device.Device/Handle",
  protosetUrl: "dish.protoset",
});
```

## Por qué hace falta

`DISH_HANDLE_URL` y el `protosetUrl` por defecto (`core/dishClient.ts`)
son rutas ABSOLUTAS de raíz de dominio (`/dishy/...`, `/dish.protoset`).
Bajo Home Assistant Ingress (o incluso accediendo directo por IP, ya que
esta app cuelga de `/plugins/starlink/`, nunca de `/`), esas rutas se
resuelven contra el dominio raíz, no contra este add-on -- el `fetch`
del protoset falla antes de intentar siquiera hablar con el dish.
Síntoma real visto en producción: la interfaz mostraba "dish
unreachable" pero CERO peticiones llegaban al proxy `/dishy` del
backend (`starlink_plugin.py:_dishy_proxy`) -- confirmado con los logs
del propio add-on.

Con rutas RELATIVAS (sin `/` inicial), se resuelven contra el directorio
de la página actual, que es exactamente donde vive este plugin -- igual
de correcto sirviendo bajo Ingress que por IP directa.

## Para reconstruir tras una actualización de Dishylink

```bash
git clone --depth 1 https://github.com/DaveyHert/dishylink.git
cd dishylink
npm install
# aplicar el cambio de arriba en src/main.tsx
npx tsc -b
npx vite build --base=./
# copiar dist/* a app/starlink_dist/ de este repo (+ LICENSE como
# DISHYLINK_LICENSE.txt)
```

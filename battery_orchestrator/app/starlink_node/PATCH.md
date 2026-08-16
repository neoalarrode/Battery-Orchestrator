# `app/starlink_node/` -- historian y servidor de cuenta reales

Este directorio vendoriza, de
[Dishylink](https://github.com/DaveyHert/dishylink) (MIT), las partes
del proyecto que corren como PROCESO DE FONDO en vez de en el
navegador:

- `collector/` -- el historian real (registro de historico dia/semana/
  mes: energía, alertas, eventos, mapa de obstrucción, uso por
  dispositivo). Sin tocar.
- `core/` -- modulos compartidos entre el frontend y el historian
  (cliente grpc-web, decodificacion de telemetria, actualizaciones de
  configuracion...). Sin tocar.
- `cloud/` -- el handler REAL de la cuenta Starlink (identidad, plan,
  direccion de servicio, control de dispositivos via la nube). Sin
  tocar.
- `public/dish.protoset` -- mismo fichero vendorizado que ya usa el
  frontend (`app/starlink_dist/dish.protoset`), copia identica.

## `cloud-server.mts` -- ÚNICO fichero NUEVO de esta integración

No existe en el proyecto original. Necesario porque el handler de
cuenta real (`cloud/starlinkCloudHandler.ts`) esta pensado para
conectarse a UN TRANSPORTE (su propio dev server via un plugin de Vite,
el proceso principal de Electron, o el service worker de la extension)
-- aqui no hay ninguno de los tres en produccion, asi que este fichero
une ese mismo handler a un servidor `node:http` normal y corriente,
replicando EXACTAMENTE la misma logica de rutas que ya tiene
`dev/starlinkCloudProxy.ts` (el plugin de Vite real del proyecto) pero
sin ninguna dependencia de Vite.

Incluye la ruta `/cloud/wifi-config` (POST), que llama a
`handler.updateWifiConfig` -- ver `cloud/starlinkCloudHandler.ts`
mas abajo, unico modulo REAL del proyecto que este parche toca (el
resto de `cloud/`/`core/` sigue sin tocar).

## `cloud/starlinkCloudHandler.ts` -- ÚNICO fichero de Dishylink modificado en este directorio

Añadidos `prepareWifiConfigUpdate` (opción inyectable, mismo patrón que
`prepareDishConfigUpdate`/`prepareDeviceUpdate` ya existentes) y
`updateWifiConfig`/`applyWifiConfigUpdate`/`validWifiConfig` -- misma
disciplina de validación que `updateDishConfig`/`validDishConfig`
(nunca se acepta protobuf del renderer, solo campos con nombre y su
valor). Usa `core/wifiConfigUpdate.ts` (NUEVO, ver mas abajo) para
construir el `wifiSetConfig` -- el resto del fichero, sin tocar.

## `core/wifiConfigUpdate.ts` -- NUEVO fichero

Construye la petición `wifiSetConfig` (nombre/contraseña de red, bypass
mode, DNS personalizado/seguro, rango DHCP, país, apagado de banda,
band steering, modo exterior) con los flags `apply_*` reales -- mismo
patrón que `core/dishConfigUpdate.ts` (`CONFIG_APPLY_FLAG`), pero para
el `targetId` del router ("Router-...") en vez del dish ("ut...").

**Corrección real (v0.3.1)**: la primera versión de este fichero ponía
`networkName`/`networkName5ghz`/`networkPassword` directamente en
`wifiConfig`, deducido de `strings dish.protoset | grep -B/-A` --
método que resultó NO fiable (`strings` no preserva la estructura de
mensajes anidados, solo el orden de bytes). Verificado en producción: el
propio decodificador real (`fromJson` contra el `Request` real) lo
rechazó con `"key \"networkName\" is unknown"` -- esos tres campos
existen de verdad en el protoset, pero en `WifiSetupRequest` (el asistente
de primer arranque), no en `WifiConfig`. Corregido introspeccionando el
registro real (`registry.getMessage(...).fields`, no `strings`): el
SSID/contraseña editables de verdad viven dentro de
`networks[0].basicServiceSets[].ssid` / `.authWpa2.password` -- el mismo
sitio de donde ya los LEE `RouterSettingsTab.tsx`. Cada escritura ahora
lee primero la red actual (`getWifiConfig()`) y solo sustituye SSID/
contraseña dentro de esa estructura, preservando bssid/banda/interfaz de
cada entrada -- verificado localmente decodificando la petición
construida contra el esquema real antes de desplegar (sin tocar el
dispositivo).

## Como se ejecutan

`starlink_plugin.py` (`start_background_threads`) lanza dos procesos:

```
npx tsx collector/historian.mts   # puerto 8088 (HISTORIAN_PORT)
npx tsx cloud-server.mts          # puerto 8089 (CLOUD_PORT)
```

Configurados por variables de entorno (ningun cambio de codigo fuente
para esto -- `collector/historian.mts` YA es configurable así, y
`cloud-server.mts` sigue el mismo criterio a proposito):

| Variable | Para que |
|---|---|
| `DISH_URL` | endpoint grpc-web del dish (fijo, `192.168.100.1:9201`) |
| `ROUTER_URL` | endpoint grpc-web del router (configurable, ver `starlink_store.py`/`/api/router-config`) |
| `HISTORIAN_PROTOSET` | ruta al `dish.protoset` vendorizado |
| `HISTORIAN_DATA_DIR` | `/data/starlink/historian` -- persistente entre reinicios del add-on |
| `HISTORIAN_PORT` | 8088 |
| `CLOUD_PORT` | 8089 |
| `CLOUD_COOKIE_FILE` | `/data/starlink/.starlink-cookie` -- persistente |

`starlink_plugin.py` los expone al frontend por `/api/*` (historian) y
`/cloud/*` (cuenta), mismos nombres relativos que el frontend ya usa por
defecto (ver `app/starlink_dist/PATCH.md`, puntos 1-4) -- ningun cambio
adicional hace falta en el lado del navegador para que esto funcione.

## Dependencias

Solo dos, las mismas que usa el codigo original en tiempo de ejecucion
(nada de React/Vite/Electron, eso es build-time del frontend, no del
historian): `@bufbuild/protobuf` (decodificar `dish.protoset`) y `tsx`
(ejecutar `.mts` directamente, sin paso de compilacion aparte). Se
instalan la PRIMERA vez que se activa el plugin (`npm install` dentro de
este directorio, ver `starlink_plugin.py:_ensure_node_deps`) -- no
viajan en el tarball descargado ni en la imagen base del addon (ver
`.dockerignore`/`.gitignore`).

## Para reconstruir tras una actualización de Dishylink

```bash
git clone --depth 1 https://github.com/DaveyHert/dishylink.git
cd dishylink
# copiar collector/, core/, cloud/ (sin los *.test.*) y public/dish.protoset
# a app/starlink_node/ de este repo, reemplazando lo que haya
# cloud-server.mts es NUESTRO, no se toca al actualizar Dishylink salvo
# que cambie la firma de createCloudHandler/DishClient.load en core/cloud
```

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

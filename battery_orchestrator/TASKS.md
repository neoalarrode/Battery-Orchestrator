# Tareas pendientes -- Home Orchestrator

Última actualización: sesión del 2026-08-15. Este fichero es para retomar
el trabajo si se pierde el contexto de la conversación -- no forma parte
del producto, no hace falta desplegarlo ni versionarlo con cuidado.

## Contexto general

Repo: `neoalarrode/Home-Orchestrator` (antes `Battery-Orchestrator`).
Deploy real en `root@192.168.1.93` (SSH, ver memoria `ha_host_ssh_root_password`),
addon `bfadc9b2_battery_orchestrator`. Verificación contra HA real en
`https://haos.ericlarrode.com` (token en memoria `ha_long_lived_token`).

Patrón de despliegue (repetido en todo el proyecto, ver CHANGELOG.md para
el detalle exacto de cada versión):
1. Bump versión del/de los plugin(s) tocados (atributo `version` en su
   `*_plugin.py`) + entrada en CHANGELOG.md.
2. `python3 -m py_compile` de todo lo tocado.
3. `git add -A && git commit && git tag vX.Y.Z && git push origin HEAD && git push origin vX.Y.Z`.
4. Descargar el tarball real del tag, `shasum -a 256`, verificar contra
   lo esperado.
5. Actualizar el pin (`tag`+`sha256`+`version`) de cada plugin tocado en
   `app/plugin_loader.py` -> commit/tag SEPARADO (aunque sea solo eso).
6. Si el commit anterior toca un fichero núcleo (`core_app.py`,
   `core_shell.py`, `plugin_loader.py`, `ha_websocket.py`,
   `config_store.py`...) también hay que bumpear `version` en
   `config.yaml` (versión del addon) en ESE MISMO commit/tag, y ese tag
   SÍ lleva GitHub Release (`gh release create vX.Y.Z --title ... --notes ...`).
   Un cambio que SOLO toca contenido de un plugin (sin tocar plugin_loader.py
   en el mismo commit) NO lleva Release.
7. SSH: `ha store reload && ha addons update bfadc9b2_battery_orchestrator`.
   `ha addons update` a veces NO reconstruye la imagen de verdad -- por
   seguridad, forzar siempre `ha addons rebuild bfadc9b2_battery_orchestrator`
   después.
8. Instalar el contenido nuevo de cada plugin tocado:
   `docker exec app_bfadc9b2_battery_orchestrator curl -s -X POST
   http://127.0.0.1:8099/api/core/plugins/<slug>/install`.
9. **Verificar en disco, nunca fiarse solo de la API**: `docker exec ...
   grep <algo del cambio> /data/plugins/<slug>/current/<fichero>.py` (o
   `/app/<fichero>.py` si es núcleo).
10. `ha addons restart bfadc9b2_battery_orchestrator`, esperar ~10s,
    comprobar `docker ps` (Up Ns, sin reinicios) y
    `docker logs ... | grep -i "Plugin cargado\|AssertionError"` -- las
    5 líneas "Plugin cargado" con la versión nueva, CERO AssertionError.
    Esperar otros 15-20s más y volver a comprobar `docker ps` antes de
    dar el despliegue por bueno (el crash-loop de la v0.19.2 tardaba unos
    segundos en manifestarse).

## Hecho en esta sesión (todo desplegado y verificado en producción)

- **Sistema de diseño compartido**: `app/core_static/design-system.css` +
  `plugin-switch.js`, servidos en `/shared/*` (`core_shell.core_static_bp`).
  Climate/Tuya/Lighting/TP-Link migradas al 100%. Battery (`app/templates/index.html`,
  el más grande y antiguo) solo enlazado de forma ADITIVA -- su `<style>`
  local completo sigue ahí, dedup pendiente (ver más abajo).
- **Lighting: latencia de encendido al detectar presencia** (bug real
  reportado por el usuario, "debería ser inmediato como con Node-RED").
  Cinco causas reales encontradas y arregladas, en este orden (cada una
  se creía la última hasta que el usuario probó en real y seguía lento):
  1. `ReactiveTrigger` con margen fijo de 5s heredado de Battery -> 0.2s
     para Lighting (`min_interval_seconds` configurable por instancia).
  2. 7 lecturas completas de HA por WebSocket por evento (una por zona,
     `ZoneRunner.decide_and_act` pedía su propio `get_states()`) -> UNA
     lectura compartida por ciclo (`LightingPlugin._run_reactive_cycle`).
  3. Luces de una misma zona encendiéndose en SERIE (llamadas bloqueantes
     a bridges TP-Link/Tuya, una tras otra) -> en PARALELO
     (`concurrent.futures.ThreadPoolExecutor`, tiempo total = el de la
     luz más lenta, no la suma).
  4. **La causa de fondo, la que explicaba que TODAS las zonas fueran
     lentas por igual (incluso luces nativas de HA sin TP-Link/Tuya de
     por medio)**: `HAWebSocketClient.get_states()` pedía el volcado
     COMPLETO de HA (1770 entidades, ~870KB) por WebSocket en cada
     llamada. Ahora hay una copia local (`_states_cache`) sembrada una
     vez al conectar y mantenida en vivo con cada `state_changed` (ya
     nos llegan todos, se filtraban en memoria) -- beneficia también a
     Climate (mismo cliente compartido). `get_states()`/`get_state()`
     pasan a ser lecturas locales instantáneas.
  5. `zone_store.update_zone_state` releía/reescribía el fichero de
     config COMPLETO del addon (compartido con Battery/Climate/Tuya/TP-Link)
     una vez POR ZONA en cada ciclo (7 lecturas + 7 escrituras de disco
     por evento) -> `update_zone_states` (plural), una sola vez para las
     7 al final del ciclo.
  - Además: reintento de TP-Link (colisión de sesión KLAP con la
    integración nativa de HA) bajado de 1.0s a 0.15s por intento.
  - Resultado medido tras el fix 5 (log `Ciclo reactivo de Lighting:
    X.XXXs total`): bajó de 5-10s a ~1.2-3s. El usuario pidió <1s pero
    dijo **"Déjalo de momento así y continúa con el resto"** -- posible
    margen de mejora todavía ahí si se retoma (ver pendientes).
- **Lighting: nuevo sufijo de luz `:solo_encendido`** (a petición expresa
  del usuario, para las lámparas del Salón `light.salon_delante`/
  `light.salon_derecha`) -- excluye la luz TANTO de brillo como de color
  de la curva solar (a diferencia de `:solo_brillo`, que solo excluye
  color); la zona solo la enciende/apaga. Ya aplicado a la zona real del
  Salón (`rules_text` actualizado vía API).
- Versión actual del addon en producción: **0.21.9**. Última tag de
  contenido: **v0.21.8**. Plugins: battery v0.11.78, climate v0.3.4,
  tuya v0.4.2, lighting v0.5.5, tplink v0.1.7.

## Pendiente -- revisión de arquitectura de páginas (orden acordado: "base
compartida primero", ya hecha; ahora dashboards reales)

Decisión del usuario (verbatim, resumida): Climate/Lighting/Tuya/Tapo
"tienen hecho realmente son configuraciones no son Dashboard útil". Pidió:
- Climate: página con gráfico por zona de lo que pretende hacer + una
  tarjeta de termostato interactiva (como el thermostat card real).
- Lighting: listado de zonas interactivo -- no solo presencia
  detectada/no detectada, sino encender/apagar/modificar colores.
- Tuya/TP-Link: van a la pestaña de configuración (ya excluidas del
  `PLUGIN_SWITCH_VISIBLE` del nav superior, pero el enlace cruzado desde
  DENTRO de Climate/Lighting real todavía NO está construido).
- Energy no debería forzar el `main` del proyecto/obligar a instalarlo.

### 1. Dashboard de Climate -- PRIMER PASO HECHO Y DESPLEGADO (v0.22.1)

Tarjeta de termostato interactiva añadida a cada zona de
`climate_templates/index.html` (stepper de temperatura, selector de
modo/preset), sobre el gráfico de previsión de 24h que YA EXISTÍA (no
hacía falta construirlo). Backend: `POST /api/zones/<id>/set_temperature`
`/set_hvac_mode` `/set_preset_mode` en `climate_plugin.py`, llaman directo
a `ZoneRunner.set_temperature`/`set_hvac_mode`/`set_preset_mode` (métodos
que YA EXISTÍAN, mismo mecanismo que la orden MQTT real). Verificado
funcionalmente en producción contra la zona Dormitorio (cambio de target
low/high, cambio de preset, vuelta al valor original). Desplegado como
v0.22.1, `docker ps` estable, sin AssertionError.

**Pendiente si se quiere pulir más**: la página sigue teniendo el mismo
formulario de configuración larga debajo (sin pestañas Dashboard/Config
separadas) -- decidir si merece la pena separar visualmente ahora o
dejarlo así (la tarjeta interactiva ya está arriba del todo, visible sin
scroll para pocas zonas).

### 1bis. Dashboard de Climate -- notas técnicas ya no necesarias (referencia)

Investigado (backend YA EXISTE, no hace falta tocar Python, solo
construir la vista):
- `GET /api/zones` (climate_plugin.py:140) ya devuelve por zona:
  `config` + `live: {available, hvac_mode, hvac_action, current_temperature,
  target_temperature, target_temperature_low, target_temperature_high, reason}`.
- `GET /api/zones/<id>/forecast` (climate_plugin.py:184,
  `climate/zone_forecast.py:build_forecast`) ya devuelve 48 puntos
  (24h pasado real + 24h futuro proyectado EN VIVO con el mismo
  `scheduler.decide_action` que decide de verdad) por zona:
  `{dt, historical, indoor_temp, outdoor_temp, occupied, occupied_pct,
  action (heat/cool/idle), target_temp, reason}`. Es EXACTAMENTE el
  gráfico "qué pretende hacer la zona" que pidió el usuario -- ya
  calculado, solo falta pintarlo (usar la skill `dataviz` para el
  gráfico: line chart con `indoor_temp` real vs `target_temp`, sombra de
  `occupied_pct`, color de fondo o marcador por `action`).
- Para la tarjeta de termostato interactiva: NO hay todavía un endpoint
  para fijar target manualmente sin pasar por MQTT/HA -- las órdenes
  reales pasan por `ZoneRunner._call_climate_service` (climate/zone_runner.py:542),
  que ya resuelve bridge refs (Tuya) O `ws.call_service("climate", ...)`
  para lo nativo de HA. Camino más simple: exponer un endpoint nuevo
  `POST /api/zones/<id>/set_temperature` / `/set_hvac_mode` en
  `climate_plugin.py` que llame directo a `runner._call_climate_service`
  (mismo patrón que Lighting's `manual_command`, ver `lighting_plugin.py`
  y `lighting/zone_runner.py:manual_command` como referencia de diseño
  ya usada y probada en este proyecto) -- así la tarjeta del dashboard no
  depende de que HA esté exponiendo el `climate.*` bien, ni de ir a
  buscar el entity_id correcto desde el frontend.
- Nota: hay una línea muerta en `climate/zone_runner.py:1282`
  (`self.decide_and_act()` después de un `return`, dentro de
  `build_forecast_chart`) -- inofensiva pero se puede limpiar de paso.

Pasos que faltan:
1. (Opcional pero recomendado) Backend: `POST /api/zones/<id>/set_temperature`
   y `/set_hvac_mode` en climate_plugin.py, llamando a
   `runner._call_climate_service` (revisar si hace falta exponerlo
   público o añadir un método wrapper en ZoneRunner, ahora mismo es
   "privado" con `_`).
2. Frontend: nueva pestaña "Dashboard" en `climate_templates/index.html`
   (usar `.page-tabs` ya preparado en `core_static/design-system.css`,
   ver comentario en ese fichero) o una plantilla nueva
   `climate_templates/dashboard.html` sencilla enlazada desde ahí --
   decidir cuál según cuánto se quiera tocar la plantilla existente.
   Por cada zona: tarjeta con temp actual/target grande + selector
   hvac_mode + stepper de target, y el gráfico de 48h (cargar la skill
   `dataviz` antes de construirlo, seguir su procedimiento de 7 pasos).
3. Compilar, desplegar (bump climate_plugin.py, CHANGELOG, tag, pin,
   config.yaml si toca núcleo, SSH, verificar).
4. Enlace cruzado a Tuya desde esta página (para las zonas con
   actuadores Tuya) -- pendiente también del punto 3 de más abajo.

### 2. Dashboard de Lighting -- HECHO Y DESPLEGADO (v0.22.5)

Tarjeta interactiva por zona en `lighting_templates/index.html`: botón
encender/apagar, color nativo (`<input type=color>` -> HS en el
navegador), slider de brillo, slider de temperatura de color de blancos.
Backend: `POST /api/zones/<id>/manual_command` en `lighting_plugin.py`,
llama directo a `ZoneRunner.manual_command` (mismo mecanismo que la luz
dummy MQTT). `GET /api/zones` expone `group` (estado agregado) por zona.

**Bug real encontrado y arreglado durante la propia verificación**:
`group_state()` no reflejaba el brillo manual recien mandado (solo el de
la curva automática) hasta el siguiente reajuste periódico -- nuevo
`_manual_brightness_pct`, mismo patrón que el `_manual_hs` ya existente.
Verificado en producción contra la zona Cocina (encender a 30% de
brillo, ver el valor reflejado correctamente, apagar, confirmar que
vuelve a "sin dato" -- con el retraso esperado de ~5s del sondeo TP-Link).

### 3. Tuya/TP-Link a solo-configuración -- A MEDIAS

`PLUGIN_SWITCH_VISIBLE` en `core_static/plugin-switch.js` ya las excluye
del nav superior (`Set(["battery","climate","lighting"])`). Falta:
- Enlace real DESDE DENTRO de Climate/Lighting hacia Tuya/TP-Link (p.ej.
  un botón/link "Gestionar dispositivos Tuya" en la sección de
  actuadores de una zona), para que sigan siendo alcanzables sin volver
  a meterlas en el nav principal.
- Revisar si merece la pena quitarles cualquier resto de "Dashboard" que
  puedan tener y dejarlas puramente como formulario de configuración.

### 4. Root neutral, Energy no obligatorio -- NO EMPEZADO, EL MÁS DELICADO

Toca `core_app.py`/`core_shell.py` -- LOS MISMOS ficheros del crash-loop
grave de esta sesión (bug: `start_background_threads()` antes de
`register_blueprint()`, ver CHANGELOG 0.19.2). Cualquier cambio aquí
necesita: compilar, probar mentalmente el orden de arranque con cuidado,
y tras desplegar, verificar estabilidad sostenida (`docker ps` con
"Up Ns" creciente, cero AssertionError) durante al menos 20-30s antes de
dar por bueno. No apresurar este punto.

### 5. Rules editor visual (Lighting) -- NO EMPEZADO

Sustituir el textarea de `rules_text` (formato `Nombre; si
entidad=valor; luces=light.a,light.b:solo_brillo`) por un formulario
visual. Mencionado en sesiones anteriores, nunca empezado.

### 6. Dropdown de room-presets en la UI de Lighting -- BACKEND HECHO, UI NO

`GET /api/room-presets` (`lighting/presets.py:list_presets`) ya existe y
funciona. Falta usarlo en el formulario de edición de zona de
`lighting_templates/index.html` (un desplegable que rellene brillo/color
min/max al elegir un tipo de estancia).

### 7. Logos para los complementos -- NO EMPEZADO

Añadir un logo/icono propio a cada plugin (hoy solo tienen el favicon SVG
inline y el icono del `plugin-switch`). Revisar si HA espera un formato
concreto (`icon.png`/`logo.png` en la raíz del addon, ver documentación
de Supervisor de add-ons) para que aparezca en el listado de HA, aparte
del uso interno en nuestras propias páginas.

### 8. Plugin de Starlink -- NO EMPEZADO

Referencia dada por el usuario: https://github.com/DaveyHert/dishylink
(hay que integrar esa app/lógica en nuestro sistema, no enlazarla suelta
-- mismo patrón "plugin propio" que Tuya/TP-Link, no una redirección).
Falta: leer el repo de referencia para entender su protocolo real hacia
la antena Starlink (gRPC local del dish, normalmente `192.168.100.1:9200`)
antes de diseñar nada.

### 9. Actualizar todo el repositorio -- NO EMPEZADO

Identidad (README, nombre mostrado, badges), y documentación de cada
plugin (uno por carpeta, o una sección por plugin en el README
principal) -- revisar qué existe ya en el repo antes de escribir nada
nuevo.

## Otras notas sueltas

- `app/templates/index.html` (Battery/Energy): dedup completo del CSS
  compartido pendiente, fuera del alcance de la fase 1 por tamaño/riesgo.
  Sigue teniendo su propio `<style>` completo ademas del link a
  `/shared/design-system.css`.
- Credenciales/tokens usados esta sesión están en memoria persistente
  (`ha_host_ssh_root_password`, `ha_long_lived_token`) -- no hace falta
  volver a pedirlos.

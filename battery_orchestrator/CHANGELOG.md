# Changelog

## 0.11.98
**Quinto bug real de la misma cadena, confirmado en producción: el fix anterior (republicar discovery al reconciliar) nunca llegaba a dispararse porque "Forzar decisión" (y cualquier llamada directa a `decide_and_act`) no reintentaba resolver la capacidad pendiente** — solo `handle_reactive_event`/`refresh_forecast` lo hacían, y si ninguno de los dos se disparaba a tiempo (zona con pocos eventos reactivos, o el usuario probando con el botón manual), la zona se quedaba pillada en "no disponible"/solo "apagado" indefinidamente. Además, Climate arranca SIEMPRE antes que Tuya (orden fijo de plugins), así que una zona con un actuador de otro plugin se construye casi con toda seguridad ANTES de que ese dispositivo termine de conectar por LAN — la capacidad pendiente es el caso NORMAL al arrancar, no una rareza. Fix: `decide_and_act()` ahora también reintenta resolver la capacidad pendiente al principio, siempre — cualquier camino (reactivo, periódico, o forzado a mano) la desatasca.

## 0.11.97
Sha256 de Climate re-pineado al tag `v0.11.96` (fix carrera de discovery MQTT) — verificado con una descarga real antes de fijarlo.

## 0.11.96
**Cuarto bug real, confirmado en producción tras desplegar el fix anterior: la zona seguía mostrando solo "apagado"/"auto" pese a que la capacidad ya se calculaba bien.** Causa: `publish_discovery()` se llamaba UNA sola vez, en el instante exacto de construir la zona (`ClimatePlugin._start_zone`) — si en ese momento un actuador de otro plugin (Tuya) todavía no había terminado de conectar por la LAN (conexión en su propio hilo, con su propio tiempo de negociación), la capacidad se calculaba vacía y se publicaba vacía a HA (discovery RETENIDO en MQTT). El runner se autocorregía por dentro poco después (`_capability_pending` ya existía para esto), pero ese discovery nunca se volvía a publicar — la entidad de HA se quedaba pegada hasta el siguiente reinicio del addon, que podía volver a tener la misma carrera. Fix: `_reconcile_hvac_mode` ahora republica el discovery en el momento exacto en que la capacidad real se conoce por primera vez. Verificado con un test sintético: construcción con el actuador aún desconectado → discovery NO se publica todavía → reconexión simulada → discovery se republica UNA vez con los modos/ventilador reales → un segundo evento no vuelve a republicar.

## 0.11.95
Sha256 de Climate y Tuya re-pineados al tag `v0.11.94` (fix capability/fan_modes de actuadores de otro plugin) — verificado con una descarga real antes de fijarlo.

## 0.11.94
**Tres bugs reales, confirmados en producción contra el AC real del usuario (Salón, AirClima 12000 vía Tuya): la zona nunca ofrecía los modos de ventilador reales ni pasaba a "ventilador" en vez de apagar del todo.**

1. `TuyaClimateHandle` (device_manager.py) nunca exponía `hvac_modes`/`fan_mode`/`fan_modes` reales del perfil, aunque el perfil generado desde la nube SÍ los trae (`mode_map`: cold→cool, hot→heat, wet→dry, wind→fan_only, auto→heat_cool; `fan_map`: strong/high/mid_high/mid/mid_low/low/mute/auto en el caso real probado) — siempre devolvía `["off","heat","cool"]` y `fan_modes: []` a fuego. Ahora los deriva del `mode_map`/`fan_map` de verdad, y se añade `set_fan_mode()` (antes no existía ningún método para cambiar la velocidad).
2. `ZoneRunner._compute_capability()`/`_available_fan_modes()` preguntaban por el camino equivocado (`self.ws.get_state()`, que solo conoce entidades reales de HA) en vez de `self._get_state()` (que sí resuelve un actuador de otro plugin como Tuya) — para CUALQUIER zona con un actuador de otro plugin, la capacidad real nunca se detectaba, bloqueando en silencio el fallback "ventilar en vez de apagar del todo" (`_smart_idle_action`).
3. `mqtt_climate.py:publish_discovery()` anunciaba a Home Assistant una lista de modos/ventilador FIJA a fuego en el código (`["off","heat_cool","heat","cool","dry","fan_only"]` / `["auto"]`), ignorando por completo la capacidad real calculada por el runner — ahora publica `runner.hvac_modes`/`runner.fan_modes` de verdad.

También se enruta `set_fan_mode` para actuadores de otro plugin en `_call_climate_service` (antes se ignoraba en silencio, comentario ya desfasado). Verificado con un test sintético usando el perfil YAML real del AirClima del usuario: `hvac_modes`/`fan_modes`/`fan_mode` decodifican correctamente, `set_fan_mode` escribe el DP correcto, y un calentador simple sin `mode_dp` sigue devolviendo `["off","heat"]` como antes.

## 0.11.93
Sha256 de Tuya re-pineado al tag `v0.11.92` (fix cuenta borrada al añadir dispositivo) — verificado con una descarga real antes de fijarlo.

## 0.11.92
**Bug real, confirmado en producción: la cuenta Tuya vinculada desaparecía sola al añadir el primer dispositivo.** `tuya_store.save_devices()` escribía `{"devices": devices}` como sección COMPLETA del plugin en el config compartido, borrando la clave `"account"` guardada en esa misma sección — cualquier alta/edición/borrado de dispositivo (todos pasan por `save_devices`) volatilizaba la cuenta sin ningún aviso. Reproducido exacto en los logs: vincular cuenta → resolver el primer dispositivo (200, la cuenta seguía ahí) → añadirlo (`POST /api/devices`, 201) → a partir de ahí todo `/resolve` posterior devolvía 400 "vincula primero una cuenta Tuya", aunque la interfaz siguiera mostrando la cuenta como vinculada hasta el siguiente refresco. Fix: `save_devices` ahora lee la sección actual primero y solo reemplaza `"devices"`, igual que ya hacía `save_account` con `"account"`. Verificado con un test sintético: la cuenta sobrevive a añadir/editar/borrar dispositivos.

## 0.11.91
**Cambio de red — el descubrimiento de Tuya-por-LAN no podía funcionar todavía.** `tuya/discovery.py` escucha paquetes de BROADCAST UDP en los puertos 6666/6667/7000 (así se anuncian los dispositivos Tuya en la red local), pero `config.yaml` no declaraba `host_network` ni ningún puerto UDP — el addon corría en la red bridge aislada de Docker por defecto, y un broadcast del LAN nunca llega ahí (publicar los puertos individualmente tampoco basta: un broadcast no es una conexión dirigida a un puerto concreto). Se añade `host_network: true` — mismo patrón que usan otros addons de descubrimiento en LAN (ESPHome y similares). Efecto secundario esperado: el addon pasa a compartir la pila de red del host directamente (sin el aislamiento de la NAT/bridge de Docker) — los puertos 8098 (wallpanel)/8099 (ingress) siguen siendo los mismos de siempre.

## 0.11.90
Sha256 de Tuya re-pineado al tag `v0.11.89` (ahora descargable de verdad) — verificado con una descarga real antes de fijarlo.

## 0.11.89
**Bug real, confirmado en producción: instalar Tuya Orchestrator desde la tienda daba 404 al intentar configurarlo.** Causa: `.dockerignore` seguía excluyendo `tuya_plugin.py`/`tuya/`/`tuya_templates/` con un comentario ya desfasado ("todavía no existen") de cuando de verdad no existían, y el catálogo (`plugin_loader.py`) tenía a Tuya marcado `downloadable: False` — la combinación significa que el plugin NUNCA estaba disponible en ningún sitio (ni horneado en la imagen, ni descargable), así que marcarlo "instalado" solo hacía que el núcleo intentase `import tuya_plugin` y fallase en silencio (`ModuleNotFoundError`, capturado y logueado por `load_all_plugins()` para no tumbar el resto del addon) — sin ese módulo cargado, no hay ruta `/plugins/tuya/` que montar, de ahí el 404. Fix: Tuya pasa a ser descargable de verdad, igual que Energy y Climate (tag+sha256 pineados en el catálogo). Sigue sin verificar contra un dispositivo Tuya físico real — eso queda pendiente del usuario, que es quien tiene el hardware.

## 0.11.88
Sha256 de Climate re-pineado al tag `v0.11.87` (modulación de consigna + anticipar ocupación) — verificado con una descarga real antes de fijarlo.

## 0.11.87
**Dos mejoras reales al motor de decisión en vivo de Climate** (a petición explícita, tras el gráfico de previsión de la versión anterior). Ambas simétricas para frío y calor, ambas con fallback exacto al comportamiento de antes cuando no hay datos, ambas nunca cruzan los límites de seguridad de la zona (`min_temp`/`max_temp` siguen mandando por encima de todo):

1. **Modulación de consigna por inercia + previsión exterior** (`scheduler._modulate_target`, nuevo): si la previsión exterior va a acercar la zona a la consigna por sí sola en las próximas 3h (calentando en invierno, enfriando en verano), el motor pide algo menos de golpe activo — hasta 3°C menos — dejando que la inercia real de la zona y el exterior hagan parte del trabajo, con más antelación. Ejemplo real: consigna 24°C, pero la previsión exterior sube fuerte y la zona retiene bien el calor — en vez de forzar el equipo a 24°C ya, se pide ~22°C con más antelación, confiando en que el exterior complete el resto. El motivo en texto plano siempre explica el porqué y el número exacto.

2. **Anticipar la llegada según el patrón histórico de ocupación** (`scheduler._occupancy_anticipate`, nuevo, usa `climate/occupancy.py` ya construido para el gráfico): si la zona no está ocupada ahora pero el patrón histórico dice que suele ocuparse dentro de poco, empieza a acercarse a la consigna de confort con antelación — para que ya esté lista cuando de verdad llegue alguien, en vez de reaccionar solo cuando el sensor de presencia se activa. Nunca sustituye una anulación manual, nunca inventa un patrón sin muestras suficientes.

Ninguna de las dos toca `min_temp`/`max_temp`, y ambas se desactivan solas (comportamiento idéntico al de antes) cuando falta previsión exterior, modelo térmico aprendido, o patrón de ocupación con muestras suficientes — nunca inventan un dato que no está.

## 0.11.86
Sha256 de Climate re-pineado al tag `v0.11.85` (gráfico de previsión 24h) — verificado con una descarga real antes de fijarlo. Aprendida la lección de v0.11.83/84: el re-pin va en el MISMO commit/tag que se despliega, nunca en uno posterior.

## 0.11.85
**Gráfico de previsión de 24h por zona en Climate** (pedido explícitamente): cada tarjeta de zona tiene ahora un botón "Previsión 24h" que despliega un gráfico como el de SOC de Energy — mitad histórico real (temperatura interior/exterior, ocupación real, qué estaban haciendo los actuadores según su propio historial) y mitad proyección EN VIVO, hora a hora, llamando literalmente a `scheduler.decide_action` (la misma función que decide de verdad, nunca una lógica paralela) con el mismo modelo de Newton simple que ya usa `_anticipate` para avanzar la temperatura simulada. Las horas se sombrean en gris según lo probable que sea que la zona esté ocupada a esa hora (histórico real para el pasado, patrón por hora del día para el futuro) — puramente informativo, nunca alimenta la decisión real. Al pasar el ratón por cualquier hora se ve el desglose completo: temperatura interior/exterior, ocupación, qué quiere hacer el sistema y por qué, tanto para horas pasadas como futuras.

A petición explícita del usuario, la mitad futura del gráfico SÍ elige qué preset proyectar en cada hora según el patrón histórico de ocupación de esa hora del día (`climate/occupancy.py`, nuevo — % de días de los últimos 14 en que la zona estuvo ocupada a esa hora en punto, estadística simple y verificable a mano, nunca aprendizaje automático) — el modo "manual" nunca se sustituye, y sin muestras suficientes se cae al preset activo real de ahora mismo. Importante: esto es SOLO para la proyección del gráfico — el motor de decisión EN VIVO (`decide_and_act`) sigue exactamente igual que antes, sin usar patrones de ocupación para decidir de verdad. Eso queda como cambio aparte, pendiente de diseño explícito (ver conversación).

Nuevos endpoints/módulos: `GET /api/zones/<id>/forecast` en `climate_plugin.py`, `climate/zone_forecast.py` (construcción de los puntos), `climate/occupancy.py` (patrón de ocupación compartido), y varios métodos públicos nuevos en `ZoneRunner` (`current_targets`, `preset_targets_for_occupancy`, `thermal_model_snapshot`, `zone_estimated_power_w`) para que `zone_forecast.py` pueda leer su estado sin tocar atributos privados.

## 0.11.84
Sha256 de Energy re-pineado al tag `v0.11.83` (fix del `PLUGIN_SWITCH_ICONS` referenciado antes de declararse) — verificado con una descarga real antes de fijarlo. El re-pin se me quedó sin commitear al publicar v0.11.83, así que esa imagen se reconstruyó todavía con el pin viejo (`v0.11.75`); esta versión lo corrige de verdad.

## 0.11.83
**Bug real, confirmado en producción: el panel de Energy se quedaba sin ningún dato en vivo** (selector de plugins vacío, todas las tarjetas mostrando el placeholder "todavía no hay datos" en lugar del ciclo real) reportado por captura desde el móvil. Causa: `templates/index.html` usaba la constante `PLUGIN_SWITCH_ICONS` (declarada con `const`, más abajo en el mismo fichero, dentro de la rejilla de Configuración) antes de que se declarase — al ser `const` de nivel superior eso lanza `ReferenceError: Cannot access 'PLUGIN_SWITCH_ICONS' before initialization` nada más ejecutarse el script, y al no estar capturado en ningún `try/catch` aborta TODO lo que viene después en el mismo bloque `<script>`, incluida la IIFE de arranque (`loadConfig()`, `refreshStatus()`, `refreshLive()`, `renderPluginSwitch()`...). El HTML/CSS se veía bien porque no depende de JS, pero ni un solo dato dinámico llegaba a cargar. Fix: `PLUGIN_SWITCH_ICONS`/`PLUGIN_SWITCH_LABEL` ahora se declaran al principio del script, antes de cualquier uso. Comprobado NO desde `docker exec curl` (eso solo prueba el backend, que ya estaba sano) sino leyendo el propio HTML servido — el fallo era puramente de orden de ejecución del JS del cliente, invisible desde el servidor.

## 0.11.82
Sha256 de Climate re-pineado al tag `v0.11.81` (histórico local de Tuya para el modelo térmico) — verificado con una descarga real antes de fijarlo.

## 0.11.81
**El modelo térmico de Climate ya aprende de dispositivos Tuya consumidos internamente**: `device_manager.py` guarda su propio historial local por datapoint (capado por cuenta y por 14 días), y `thermal_model.py` lo consulta igual que ya consulta el recorder de HA cuando el actuador es una referencia de otro plugin — sin esto, un termostato Tuya usado vía `climate_entities` no generaba ningún histórico del que aprender su inercia térmica real. Verificado con un histórico simulado (3 tramos de calentamiento reales): el modelo aprende ~1.0°C/h de verdad, sin ninguna llamada a HA.

## 0.11.80
Sha256 de Climate re-pineado al tag `v0.11.79` (ya trae el registro genérico de proveedores) — verificado con una descarga real antes de fijarlo.

## 0.11.79
**Registro genérico de proveedores de actuadores climate.*** — hasta ahora Climate conocía a Tuya por su nombre a mano. Ahora cualquier plugin que exponga `climate_handle()`/`list_climate_actuators()` se registra solo (`core_app.py` los conecta tras cargar los plugins, sin lista hardcodeada); `zone_runner.py` deja de mencionar "Tuya" en ningún sitio — solo sabe preguntarle al registro. Preparado para que una marca futura se sume sin tocar Climate ni el núcleo.

**Selector de actuadores en el formulario de zona**: `GET /api/actuators` agrega lo que ofrece cada proveedor registrado, marcando `already_used` contra todas las zonas existentes — un dispositivo ya asignado no vuelve a aparecer como opción. El campo de texto libre para `climate.*` de HA se mantiene tal cual.

Verificado de punta a punta con un proveedor de prueba: registro, resolución, filtrado de "ya en uso", y cero regresión sin ningún proveedor instalado.

## 0.11.78
Sha256 de Climate re-pineado al tag `v0.11.77` (ya trae el enganche de Tuya en `zone_runner.py`) — verificado con una descarga real antes de fijarlo.

## 0.11.77
**Descubrimiento de dispositivos Tuya, con el usuario decidiendo siempre si añadir o no** — portados `discovery.py` (escucha persistente de broadcasts LAN, cero dependencia de HA), `tuya_cloud.py` (adaptado de aiohttp a `requests` síncrono — solo se usa para vincular una cuenta y traer `local_key`+esquema real, nunca en operación normal) y `auto_profile.py` (genera un perfil YAML de partida a partir del esquema real del dispositivo). Flujo: "Detectados" enseña lo visto en la LAN (puramente informativo) → el usuario pulsa "Añadir" → se resuelve contra la cuenta vinculada y se PRECARGA el formulario de siempre con el perfil generado → el usuario lo revisa/edita → guarda. Nada se conecta ni se persiste hasta ese último paso — igual que el `config_flow` del proyecto original.

**Climate ya puede controlar un termostato Tuya de verdad, sin pasar por Home Assistant**: `climate_entities` de una zona acepta `tuya:<device_id>` además de un `climate.*` de HA — `ZoneRunner` lo resuelve contra `TuyaClimateHandle` en el mismo proceso. `core_app.py` conecta ambos plugins tras cargarlos (si Tuya no está instalado, las zonas que lo referencien simplemente no lo controlan, no revienta nada). Verificado de punta a punta con el método real de decisión (`_drive_climate_actuator`) y un bloqueo explícito que confirma que nunca se llama a `ws.call_service`/`ws.get_state` para un actuador Tuya.

Tuya se queda todavía fuera de la tienda (`downloadable: false`) — sigue pendiente de verificar contra un dispositivo físico real.

## 0.11.76
Sha256 de Energy y Climate re-pineados al tag `v0.11.75` (ya incluye el selector de plugins dinámico de 0.11.74) — verificado con una descarga real de ambos antes de fijarlo. Corrige que el selector dinámico llevaba dos versiones desplegado sin efecto real: el código descargado en producción seguía siendo el de antes del cambio, porque nada disparó una re-descarga tras el commit anterior.

## 0.11.75
Sin cambios de codigo -- version puente para poder pinear el sha256 real de Energy/Climate contra un tarball que YA incluye el selector de plugins dinamico de 0.11.74 (ver 0.11.76).

## 0.11.74
**Plugin de Tuya completo (todavía no instalable desde la tienda)**: `device_manager.py` (puente sincrono/asincrono — un solo event loop de asyncio en su propio hilo para todos los dispositivos, `tuya_lan.py` empuja los cambios solo, sin patrón reactivo propio duplicado), `mqtt_tuya.py` (Discovery genérico por dominio — switch/sensor/number/binary_sensor/select/climate, no solo termostatos), `tuya_plugin.py` + interfaz de alta de dispositivos (perfil YAML declarativo, igual que Tuya Orchestrator).

Verificado con pruebas reales de lógica (sin dispositivo físico a mano): perfil real → fachada `TuyaClimateHandle` computando modo/temperaturas correctamente; publicación MQTT Discovery + estado + enrutado de comandos con un broker simulado. Los tres plugins (Energy/Climate/Tuya) montados juntos arrancan limpios.

**Selector de plugins de la cabecera y el panel de "Configuración" pasan a ser dinámicos** (antes: HTML fijo con Battery/Climate a mano) — se generan desde `/api/core/plugins`, mostrando solo lo que está instalado de verdad. Corrige un fallo latente: un enlace fijo a un plugin desinstalado habría quedado muerto.

Tuya se queda fuera de la tienda (`downloadable: false`) hasta poder verificarlo contra un dispositivo real — mismo criterio de no ofrecer instalar algo que no se ha probado en producción todavía.

## 0.11.73
**Arranque del plugin de Tuya** (tercer plugin, en construcción — todavía no se carga ni aparece en la tienda). Diseño: dispositivos Tuya consumidos de dos formas — internamente por Climate (nuevo tipo de actuador resuelto en el mismo proceso, sin pasar por HA) y, opcionalmente, expuestos a HA por MQTT Discovery para cualquier dominio (no solo climates: switch, sensor, number, binary_sensor, select).

Portados `tuya/tuya_lan.py` (protocolo LAN cifrado de Tuya, handshake de sesión 3.4 incluido) y `tuya/profile.py` (perfiles YAML declarativos de datapoints) desde `neoalarrode/Tuya-Orchestrator` — ninguno de los dos toca nada de Home Assistant, así que se reutiliza el protocolo ya probado en producción tal cual, sin reescribirlo. `pycryptodome`/`pyyaml` añadidas al Dockerfile para esto. Pendiente: `tuya/device_manager.py` (sustituto del coordinator de HA, mismo patrón reactivo que ya usa Climate), `mqtt_tuya.py`, el enganche en `ZoneRunner` y la página de alta de cuenta/dispositivos.

## 0.11.72
Sha256 de Energy y Climate re-pineados en el catálogo, ambos al tag `v0.11.71` (el que trae el fix de rutas relativas) — verificado con una descarga real de los dos antes de fijarlo. Sin esto, instalar cualquiera de los dos desde cero seguiría trayendo la versión con el 404.

## 0.11.71
**Bug real: 404 al entrar en Climate desde el panel** — el selector de plugins y las llamadas a la API usaban rutas ABSOLUTAS (`/plugins/climate/`, `/api/...`). Bajo el proxy de ingress de HA (que antepone un token a toda la URL) una ruta absoluta se resuelve contra la raíz del dominio, no contra el prefijo de ingress — el enlace/petición se sale del túnel y HA responde 404. Corregido a rutas relativas en todos los sitios nuevos de esta fase (selector de plugins de Energy y Climate, formulario de zonas de Climate, catálogo del núcleo) — mismo criterio que ya seguía el resto de la app desde siempre (`fetch('api/status')`, nunca `fetch('/api/status')`).

**Jerarquía de marca corregida**: la cabecera de Energy decía "Energy Orchestrator" como si fuera el nombre del sistema entero — ahora dice "Home Orchestrator" (eyebrow) + "Energy" (plugin), igual que ya hacían las páginas de Climate y del catálogo del núcleo.

## 0.11.70
**Energy deja de venir precargado en la imagen** — la imagen ya solo trae el núcleo (`core_*.py`, `plugin_*.py`, `config_store.py`, `ha_websocket.py`, `ha_mqtt.py`). Verificado ANTES de desplegar con la prueba más exigente posible: un directorio con únicamente los ficheros del núcleo (sin Energy ni Climate) descargó los dos plugins de verdad desde GitHub, los verificó por sha256 y arrancó igual que producción — dashboard, tienda y todo.

Con esto una instalación fresca de Home Orchestrator viene de verdad vacía: solo el catálogo de la tienda en la raíz hasta que se instale algo (o se restaure una copia de seguridad, que instala automáticamente lo que corresponda). Este addon en concreto siguió el mismo camino cuidadoso que con Climate: backup completo (0.11.65) → aislamiento de fallos entre plugins (0.11.66) → descarga forzada de Energy antes de tocar la imagen → este cambio.

## 0.11.69
Sha256 real de Energy pineado en el catálogo (calculado y verificado contra una descarga real del tag `v0.11.68` antes de fijarlo) — mismo procedimiento de dos pasos que ya se siguió con Climate. Con esto la tienda ya puede descargar/verificar Energy de verdad, no solo Climate.

## 0.11.68
**Núcleo de verdad vacío**: Energy deja de ser obligatorio. Nuevo `core_shell.py` — la tienda de plugins y la copia de seguridad (`/api/core/*`) ya no viven dentro de Energy, viven en el núcleo mismo, como un Blueprint que se registra sobre quien sirva la raíz (`Plugin.serves_root`, hoy solo Energy) — o, si NINGÚN plugin instalado la sirve, el propio núcleo sirve una página de catálogo + restaurar copia de seguridad en su lugar. Con esto una instalación con cero plugins instalados ya no es un caso raro que había que evitar: es el estado inicial normal.

**Restaurar copia de seguridad ya instala los plugins que le correspondan**: al restaurar, además de traer de vuelta toda la configuración, se descargan (verificados) los plugins que esa copia tenía instalados — no solo los datos, también el código.

Energy pasa a ser descargable como Climate (`plugin_loader.PLUGIN_CATALOG`), aunque de momento sigue viniendo en la imagen mientras se termina de verificar esta pieza — el siguiente paso es sacarlo también del Dockerfile.

## 0.11.67
**Climate deja de venir precargado en la imagen** (`.dockerignore` nuevo, excluye `climate_plugin.py`/`climate/`/`climate_templates/` del build) — a partir de ahora se instala de verdad desde la tienda, descargado y verificado por sha256, no incluido de fábrica. Desplegado con red de seguridad completa: copia de seguridad de todo `/data` tomada antes del cambio (0.11.65), aislamiento de fallos entre plugins (0.11.66) y, en esta instalación en concreto, Climate ya descargado y verificado a `/data/plugins/climate/` ANTES de quitarlo de la imagen, para que el arranque nunca se quede sin su código.

Energy (el núcleo) sigue viniendo siempre en la imagen — no tiene sentido descargarlo aparte de lo que lo carga.

## 0.11.66
**Aislamiento de fallos entre plugins**: si un plugin OPCIONAL (Climate, o cualquier otro futuro) falla al cargar — código no encontrado, error al importar — el núcleo ya no se cae entero; se registra el error y se sigue arrancando sin él. Solo un fallo del núcleo (Energy) revienta el arranque, porque sin eso no hay nada que servir en la raíz. Paso previo, deliberado, antes de sacar Climate del Dockerfile (siguiente versión): así un problema con su descarga nunca deja la instalación entera sin responder.

## 0.11.65
**Copia de seguridad completa del núcleo** (`core_backup.py`, nuevo): a diferencia de la copia de seguridad que ya existía (solo la configuración de Battery/Energy), esta recoge TODOS los ficheros de estado bajo `/data` — configuración de todos los plugins, históricos, capacidad, savings... — sin necesitar conocer de antemano la lista exacta de cada plugin (recoge cualquier `*.json` de `/data`, excepto `options.json`, que es de Supervisor). `GET /api/core/backup` la descarga, `POST /api/core/backup/restore` la restaura fichero a fichero de forma atómica, sin borrar nada que no venga en el backup. Construida como red de seguridad antes de sacar Climate del Dockerfile (siguiente paso).

## 0.11.64
**Descarga real de plugins**, tal y como se planteó: `plugin_downloader.py` descarga el tarball de un tag concreto del propio repo (`https://github.com/neoalarrode/Home-Orchestrator/archive/refs/tags/<tag>.tar.gz`), calcula su sha256 y lo compara contra el valor pineado en `plugin_loader.PLUGIN_CATALOG` **antes** de extraer nada — si no coincide, se descarta entero y no se instala nada (falla cerrado). Solo entonces extrae los ficheros de ESE plugin (nunca el repo entero) a `/data/plugins/<slug>/<tag>/`, con un symlink `current` a la versión activa.

Verificado de verdad contra el repo público (no un mock): descarga real del tag `v0.11.63`, sha256 correcto → instala y arranca `ClimatePlugin` desde la copia descargada (con prioridad sobre la que trae la imagen); sha256 manipulado → rechazado, no toca disco.

Energy (antes Battery) se queda fuera de este mecanismo a propósito — es el núcleo, siempre viene con el addon, no tiene sentido descargarlo aparte. Climate ya es descargable de verdad desde la tienda: instalar cuando no viene precargado ahora dispara una descarga real, no solo activa un flag.

Pendiente antes de poder decir que una instalación fresca viene "vacía de verdad": sacar Climate del Dockerfile (que hoy lo sigue precargando como red de seguridad) y montar la pantalla de catálogo cuando no hay ningún plugin cargado en `/` — deliberadamente no se toca todavía para no arriesgar tu instalación real mientras se prueba el mecanismo de descarga.

## 0.11.63
**Renombrado a "Energy"**: el plugin ya no se llama "Battery" de cara al usuario (título, cabecera, selector de plugins, tienda) — pasa a "Energy Orchestrator", porque ya no solo gestiona baterías: también solar y cargas diferibles. Cambio solo de nombre visible; el slug interno (`battery`), el namespace de configuración (`plugins.battery`), el slug del add-on (`battery_orchestrator`) y todos los entity_id existentes (`sensor.battery_orchestrator_*`) se quedan exactamente igual — cero migración, cero riesgo para automatizaciones o integraciones ya montadas sobre esos nombres.

**Tienda de plugins real** (pestaña "Tienda", nueva): antes solo existía un selector para configurar plugins YA instalados; ahora hay una sección aparte, con la misma estética, que lista el catálogo completo (instalados y no) con botón Instalar/Quitar. Instalar/quitar escribe en `core.installed_plugins` (nuevo campo, con migración automática — su ausencia se interpreta como "todo lo que ya traía el addon", cero cambio para instalaciones existentes) y `plugin_loader.py` ya respeta esa lista al arrancar. Energy no se puede quitar (es el núcleo, sirve la raíz). Todavía no descarga nada de red — activa/desactiva plugins que ya vienen en la imagen; la descarga real es el siguiente paso.

## 0.11.62
**La pestaña "Configuración" pasa a ser un selector de plugins**, no el formulario en bruto directamente: al entrar aparecen los plugins instalados como tarjetas (icono + nombre + qué configura cada uno) — Battery se queda en la misma página (su formulario de siempre, ahora detrás de un clic, con un "◂ Plugins" para volver) y Climate lleva a su propia página. Prepara el terreno para que futuros plugins encajen en el mismo sitio sin mezclar su configuración con la de los demás.

Los iconos del selector de plugins de la cabecera (introducido en 0.11.61) pasan de emoji a SVG en línea con el resto del sistema (mismo rayo y termómetro que ya se usaban como favicon/marca de cada página) — los emoji rompían con la estética del panel.

## 0.11.61
**Interfaz adaptada a la vía de plugins**: nuevo selector "Battery ⇄ Climate" en la cabecera de ambas páginas (mismo componente visual en las dos, mismos tokens de color) — cambiar de plugin ya se siente como un único sistema, no dos apps sueltas.

**Primera interfaz real para el plugin de Climate** (`climate_plugin v0.2.0`, servida en `/plugins/climate/`): tarjetas de zona con temperatura actual/objetivo, modo, acción (calentando/enfriando/inactivo...) y el motivo textual de la última decisión; botones por zona para forzar una decisión ahora, editar o eliminar; formulario de alta/edición con sensores, actuadores, presets, límites y modo simulación (con aviso explícito si se desactiva, porque en ese momento empieza a accionar dispositivos reales). Sin frameworks — mismo estilo autocontenido en un único HTML que ya usa Battery.

## 0.11.60
`POST /api/zones/<id>/refresh` en el plugin de Climate — fuerza una decisión ahora mismo, sin esperar al próximo evento reactivo o al refresco periódico. Surgido al verificar en producción una zona de prueba recién creada (útil también como diagnóstico manual en el futuro, no solo para pruebas).

## 0.11.59
**Segundo plugin real: Climate**, montado en `/plugins/climate` junto a Battery (que sigue en la raíz, sin cambios de comportamiento). Puerto de todo Climate Orchestrator (el custom_component HACS separado) a este plugin, con dos cambios de fondo respecto al original:

- **Todo por WebSocket, nunca REST** — `ha_websocket.py` se amplía con una capa de petición/respuesta (`call_service`, `get_states`, `get_history` con formato comprimido y relleno de atributos diff-codificados) para cubrir lo que antes hacía `hass.services.async_call`/`hass.states.get`/`history.get_significant_states` dentro de HA Core.
- **Termostatos nativos vía MQTT Discovery** (no REST, no un sensor secundario) — cada zona se publica como una entidad `climate.*` real, con HomeKit/Matter incluido, usando el aprovisionamiento automático de credenciales del broker local añadido en 0.11.57 (`services: mqtt:want`).

Piezas nuevas: `climate/zone_store.py` (config+estado de cada zona, namespaced bajo `plugins.climate` en el mismo config.json compartido — mismo criterio de migración automática que Battery), `climate/zone_runner.py` (la lógica de decisión completa, 1:1 con el custom_component salvo el puerto async→sync), `climate/mqtt_climate.py` (discovery + publicación de estado + comandos), `climate_plugin.py` (arranque, WebSocket/MQTT compartidos entre zonas, ciclo reactivo + refresco periódico por zona con jitter). API nueva: `GET/POST /api/zones`, `PUT/DELETE /api/zones/<id>`, `GET /api/status` — todo bajo `/plugins/climate`.

`core_app.py` ahora fusiona las apps Flask de los plugins con `DispatcherMiddleware` en vez de servir solo la primera.

Sin zonas configuradas todavía (el registro empieza vacío) — el plugin arranca y se conecta, pero no hace nada hasta que se den de alta zonas. Las 2 zonas reales de producción (`climate.salon_salon`, `climate.dormitorio_4`) siguen en el custom_component de Climate Orchestrator de siempre; no se tocan hasta verificar este plugin a fondo con una zona de pruebas primero.

## 0.11.58
**Bug real, confirmado**: `sensor.battery_orchestrator_energy_charged`/`_discharged` no correspondían con `sensor.battery_orchestrator_power` porque no medían lo mismo. La acumulación usaba la potencia PLANIFICADA (`distribution["per_battery"]`, lo que el ciclo decidió mandar) multiplicada por el `cycle_seconds` NOMINAL — no la potencia real medida, y en descarga ni siquiera se repartía de verdad entre baterías, solo se estimaba proporcional a la potencia máxima declarada de cada una (el propio comentario del código ya lo admitía).

Además, el ciclo reactivo (v0.11.55) empeoró esto: al poder ejecutarse `run_cycle` mucho más a menudo que `cycle_seconds`, cada ejecución reactiva seguía multiplicando por el `cycle_seconds` NOMINAL completo, contando de más cada vez que disparaba antes de tiempo.

Corregido: ahora se integra la potencia REAL medida (misma fuente que `sensor.battery_orchestrator_power`, `_live_battery_totals`) sobre el tiempo REAL transcurrido desde la última vuelta — mismo criterio que ya usa `solar_energy_store.py` para la energía solar. Si no hay dato en vivo de una batería en ese instante, no se acumula nada para ella ese tick (mejor perder un incremento pequeño, que se recupera solo, que acumular un número inventado).

## 0.11.57
Declara `services: [mqtt:want]` en `config.yaml` — Supervisor aprovisiona automáticamente credenciales del broker MQTT local (Mosquitto) al propio addon, vía `http://supervisor/services/mqtt`, sin ninguna acción manual del usuario. Preparación para el plugin de Climate (fase 2, MQTT Discovery) — todavía no se usa MQTT hacia el broker local en esta versión, solo se solicita el acceso.

## 0.11.56
**Primer paso hacia Home Orchestrator**: el proyecto se reorganiza como núcleo de plugins — Battery pasa a ser el primer plugin, cargado por un cargador propio (`plugin_loader.py`) a través de un contrato mínimo (`plugin_base.py`). Nuevo punto de entrada `core_app.py` (antes `main.py` directo); `main.py` sigue intacto por dentro, sin mover ninguno de sus ~20 módulos (`ha_client.py`, `scheduler.py`, `battery_exec.py`...) — cero cambio de comportamiento, es una fachada sobre el mismo código de siempre.

**Migración automática de configuración**: `config.json` pasa de formato plano (baterías/tarifa/... en la raíz) a formato con namespace por plugin (`plugins.battery`), para que futuros plugins tengan su propia sección sin pisarse. La migración es automática y transparente al arrancar — verificada exhaustivamente contra la configuración real de producción (4 baterías, credenciales EcoFlow, tarifa, arrays solares) antes de publicar esta versión: ningún valor se pierde ni se altera, y `load_config()`/`save_config()` siguen devolviendo/aceptando el mismo dict plano de siempre, así que ningún otro módulo necesita cambiar una línea.

De momento el cargador de plugins SOLO carga plugins de primera parte incluidos en este mismo repo (ver `plugins.json` en la raíz, el registro oficial) — la descarga dinámica de plugins queda para una fase posterior, pendiente de decidir cómo se verifican/firman antes de ejecutar código dentro de un proceso con credenciales reales.

El add-on sigue siendo el mismo (`slug: battery_orchestrator` sin cambios) — Supervisor no lo trata como una instalación nueva, no hace falta reconfigurar nada.

## 0.11.55
**Nuevo: ciclo de planificación reactivo, vía WebSocket** (`ha_websocket.py`, nuevo módulo). Hasta ahora todo el add-on funcionaba por sondeo: `run_cycle` solo se relanzaba cada `cycle_seconds` (30-60s típico), aunque el consumo o el solar cambiaran mucho antes. Ahora el add-on abre una conexión WebSocket persistente a HA (`/api/websocket`), se suscribe a `state_changed` de los sensores que declares (consumo, solar, SOC/potencia de baterías por HA, PVPC si aplica), y en cuanto cambian de verdad dispara una reevaluación del ciclo en segundos, no en minutos.

- Reconexión automática con backoff si se cae el WebSocket (WiFi, reinicio de HA Core...) — nunca deja al add-on sin datos por un fallo puntual.
- El ciclo PERIÓDICO de siempre se mantiene intacto como respaldo — si el WebSocket falla, todo sigue funcionando exactamente igual que antes de esta versión.
- Debounce/coalesce (`ReactiveTrigger`): varios sensores cambiando casi a la vez no lanzan el ciclo completo más de una vez cada 5s — reacciona casi al instante al primer cambio, y si llega más durante esa ventana, recoge todo en una sola vuelta más justo después, nunca se pierde un cambio real.
- Nuevo `_run_cycle_lock`: el disparo periódico y el reactivo nunca se ejecutan a la vez — el que llega segundo simplemente espera a que termine el primero.
- Las baterías EcoFlow (BLE/Cloud) no son entidades de HA, así que no participan de este mecanismo — su frescura la sigue cubriendo `_live_sensor_loop` como hasta ahora.

## 0.11.54
Fix: "Flujo de energía ahora mismo" (`/api/live`) mostraba un consumo total absurdamente bajo mientras la batería descargaba cientos de W. Causa: en modo "separate", `load_now_w` se leía directo de `load_sensor` (p.ej. "consumo_instantaneo") sin reconstruirlo — ese sensor es solo el lado de red, YA SIN la carga de baterías, no el consumo total de la vivienda (igual que ya se documentaba en `true_load_forecast`). Faltaba sumarle de vuelta el solar y la descarga de baterías, tal y como el modo "combined" ya hacía bien un poco más arriba en el mismo endpoint.

## 0.11.53
Simplificación: los sensores agregados EcoFlow-específicos (`sensor.battery_orchestrator_ecoflow_discharge_power`/`_charge_power`, añadidos en 0.11.52) se eliminan — eran redundantes con `sensor.battery_orchestrator_power` (ya existente, con signo, agnóstico de fabricante: suma TODAS las baterías del sistema, no solo EcoFlow). `true_load_forecast`/`true_load_forecast_from_grid` ahora usan ese único sensor con `sign_filter` (mismo mecanismo que ya existía para baterías en modo "combined") en vez de un sensor nuevo. También se corrige la detección de anomalías en vivo, que tenía el mismo fallo (solo sumaba descarga de baterías HA, ignoraba EcoFlow) — ahora reusa `live_discharge_w`, ya calculado para todas las baterías.

Nuevo: colchón de seguridad configurable sobre la reserva del planificador (Configuración → Prioridad → "Colchón de seguridad sobre la reserva"), 0-100%, por defecto 15%. Antes, `_reserve_target()` apuntaba exactamente a lo que la previsión decía que hacía falta, sin margen — en bloques largos de valle sin ningún tramo caro visible dentro del horizonte (p.ej. un fin de semana entero, con `weekend_is_valle` activado), la reserva calculada podía ser prácticamente nula y la batería se quedaba al SOC mínimo configurado varias horas seguidas, apostando el 100% a que la previsión de sol del día siguiente se cumpliera al dedillo. Con margen > 0, la batería para de descargar (y empieza a cargar en valle) antes de tocar ese suelo, dejando colchón real para cuando la previsión falle. 0% reproduce el comportamiento de siempre.

## 0.11.52
Causa real de que el planificador subestimara el consumo: la reconstrucción del histórico (`true_load_forecast`) suma de vuelta la descarga de cada batería a partir de su sensor de HA — pero las baterías EcoFlow no tienen ningún sensor de HA propio (se leen por BLE/Cloud), así que desde que se migraron las baterías antiguas a EcoFlow, esa reconstrucción las trataba como si no existieran: solo veía lo que se importaba de red en esas horas, nunca lo que la batería cubría por su cuenta.

Nuevos sensores agregados `sensor.battery_orchestrator_ecoflow_discharge_power`/`_charge_power` (solo la parte EcoFlow, para no duplicar lo que ya cubren los sensores de HA de baterías no-EcoFlow) que se suman de vuelta en la reconstrucción del consumo, en los dos modos ("consumo_instantaneo" y "consumo de la casa combinado").

**Importante**: estos sensores son nuevos, así que no hay pasado que reconstruir con ellos (HA no permite importar histórico de estado, a diferencia de las estadísticas de energía) — el consumo previsto seguirá siendo bajo hasta que pasen unos días y HA acumule historial real de estos sensores nuevos.

## 0.11.51
Nuevo botón "Reconstruir historial de energía" (Configuración → Historial del Panel de Energía): reparte lo ya acumulado en `sensor.battery_orchestrator_energy_charged/discharged` sobre las horas reales en que se movió esa energía (hasta 8 días de detalle horario, vía `history_store`), en vez de que aparezca de golpe como un único escalón feo en la gráfica del Panel de Energía de HA. Lo de antes de esos 8 días, sin detalle horario disponible, se pone como un único escalón justo antes de empezar el detalle real — no se inventa un reparto que no se puede verificar. Acción manual, pensada para una sola vez.

Nota técnica: usa por primera vez el WebSocket de HA (`recorder/import_statistics`, sin equivalente REST) en vez de la API REST habitual — nuevo módulo `ha_statistics.py` y dependencia `websocket-client`. No se ha podido probar en real desde el entorno de desarrollo (necesita el `SUPERVISOR_TOKEN` de dentro del add-on) — pruébalo tú y revisa la gráfica de energía después.

## 0.11.50
SOC por Cloud corregido: `cmsBattSoc` (primer campo mirado hasta ahora) es el SOC AGREGADO de todo el grupo BKW, no el de la unidad individual — mismo fallo que ya se corrigió en BLE en la v0.11.37 (`battery_level` vs `battery_level_main`), aquí pasó desapercibido porque no se había visto un caso donde diera un número claramente erróneo (0%) hasta ahora. Orden nuevo: `bmsBattSoc` (SOC real de esta unidad) primero, `cmsBattSoc` como último recurso.

## 0.11.49
Icono junto al nombre de cada batería EcoFlow en la tarjeta "Baterías" de Estado actual — un globo si el dato de este ciclo vino de Cloud (API), el símbolo de Bluetooth si vino de BLE. No aparece en baterías por Home Assistant (no aplica) ni si todavía no hay ninguna lectura EcoFlow.

## 0.11.48
Causa real de que una batería EcoFlow en Híbrido se quedara sin datos con Bluetooth caído, aun teniendo el SN de Cloud bien vinculado: MQTT solo reenvía por incrementos los campos que CAMBIAN — si el SOC de una unidad lleva tiempo sin variar, puede que ese campo en concreto nunca se haya visto desde que la sesión se suscribió, aunque el resto del estado de esa batería llegue "fresco" por otros campos. `get_live_state` ya no se conforma con "ha llegado algo reciente": ahora acepta qué campos hacen falta de verdad (SOC, potencia agregada, puertos MPPT) y cae al REST si NINGUNO de ellos está presente, aunque el resto esté fresco.

De paso, nueva reconciliación automática en sentido inverso a la ya existente: una batería Híbrida dada de alta solo por Bluetooth (sin SN de Cloud vinculado) ahora se completa sola en cuanto haya una lectura BLE conocida, sin tener que volver a pasar por el descubrimiento a mano.

## 0.11.47
`get_live_state` (Cloud) ya no se queda solo a la escucha del MQTT en frío: si no hay ningún dato fresco todavía (arranque del add-on, o un corte largo de MQTT — hasta ahora se devolvía `None` y a esperar), pregunta activamente al snapshot REST (`quota/all`) en vez de quedarse sin nada mientras llega el próximo mensaje, que podía tardar minutos. Limitado a como mucho una consulta cada 20s por batería para no agotar la cuota de la API. Al vivir dentro de `get_live_state` (la única fuente de estado Cloud de toda la app), beneficia por igual al planificador, al dashboard en vivo y a todo lo demás sin tocar nada más.

## 0.11.46
Tapado el agujero real de la caché BLE: con `fresh=False` (el camino de lectura normal — planificación, `/api/live`, previsión solar) todavía se podía colar a esperar una conexión BLE de verdad si la caché estaba vacía (justo tras un arranque, o tras el enfriamiento de la 0.11.45). Ahora `fresh=False` **nunca** conecta ni espera — lee solo la caché, `None` al instante si no hay nada. Bluetooth y Cloud quedan así completamente desacoplados: Cloud (MQTT) ya estaba siempre conectado de fondo con lectura instantánea; Bluetooth ahora también — solo `_live_sensor_loop` (cada ~10s, en su propio hilo) abre conexión BLE de verdad, y en cuanto detecta que vuelve a responder el resto de la app empieza a usarla sola, sin ningún cambio manual. Los botones de acción directa del usuario ("Buscar puertos MPPT", "Autorrellenar desde la batería") sí siguen esperando a una conexión real cuando hace falta, porque ahí el usuario ha pedido esa espera a propósito.

## 0.11.45
Causa real de los `500 Server Error` del puente BLE (revisado el log de HA Core, no solo el del add-on): `HomeAssistantError: No se pudo conectar con <dirección> en 25s` — un timeout de conexión BLE genuino, no un bug de Python. El problema es que `_live_sensor_loop` (v0.11.42+) reintentaba conectar cada ~10s sin ningún respiro, así que un fallo puntual se convertía en un martilleo constante que probablemente empeoraba la inestabilidad en vez de arreglarla. Ahora hay un enfriamiento de 60s tras un fallo: durante ese tiempo se devuelve lo último en caché (o `None`) sin reintentar, dejando paso limpio al fallback a Cloud en modo Híbrido en vez de bloquear repetidamente en el intento de BLE.

## 0.11.44
Causa probable de que el ciclo de planificación se quedara sin ejecutarse (y sin ningún error) tras la 0.11.42: `/api/live` (sondeado cada 5s por el dashboard) y `_live_sensor_loop` (cada ~10s) forzaban las dos lecturas BLE frescas en paralelo, desde hilos distintos, para las mismas baterías — dos conexiones a la vez al mismo dispositivo BLE pueden colisionar en el puente y dejar todo esperando indefinidamente. La caché de estado BLE se ha movido a `ecoflow_ble.py` (antes solo vivía en `main.py`, así que `battery_exec.py` —el que lee el SOC real cada ciclo— no se beneficiaba de ella) y ahora lleva también un bloqueo por dirección: nunca dos conexiones reales a la misma batería a la vez, venga de donde venga la petición. Solo `_live_sensor_loop` refresca la caché de verdad; el resto (dashboard, ciclo de planificación, menús de EcoFlow) siempre lee de ahí.

## 0.11.43
**Arreglo real** del `TypeError: _live_solar_now_w() missing 1 required positional argument: 'cfg'` en `/api/live` — el decorador `@app.get("/api/live")` había quedado pegado a `_live_solar_now_w` en vez de a `api_live` tras una refactorización de la v0.11.40 (Flask registraba la función equivocada como manejador de la ruta). No era ningún problema de caché de Home Assistant ni del add-on — era un bug real en el código, mis disculpas por la vuelta perdida insistiendo en lo contrario. Revisado el resto de rutas una por una: no hay ningún otro decorador descolocado.

## 0.11.42
- **Nuevo `sensor.battery_orchestrator_solar_energy`** (kWh, `state_class: total_increasing`) — energía solar acumulada de por vida, aparte de `sensor.battery_orchestrator_solar_power` (W, instantáneo). El Panel de Energía de HA pide un sensor acumulado para "Producción de energía solar", no sirve el de potencia. Se integra en el bucle rápido (~10s) multiplicando la potencia en vivo por el tiempo real transcurrido, sin asumir un intervalo fijo.
- **Descubrimiento de puertos MPPT y autorrellenar mucho más rápido**: el estado BLE de una batería EcoFlow ahora se cachea — `_live_sensor_loop` ya mantiene la conexión BLE viva y actualizada cada ~10s de fondo, así que "Buscar puertos MPPT" y "Autorrellenar desde la batería" sirven ese último dato conocido al instante en vez de abrir una conexión nueva cada vez (que podía tardar hasta 30s). Solo se paga esa espera la primera vez, antes de que el ciclo de fondo haya visto la batería.

## 0.11.41
Dos mejoras sobre los paneles vinculados a puertos MPPT de EcoFlow (Configuración → Solar):

- **Selección de varios puertos para la misma zona**: si una zona tiene paneles repartidos en varios puertos MPPT de la misma batería (p. ej. dos entradas del mismo tejado), ahora se pueden marcar todos con casillas y añadirlos juntos como un único panel (se suman) — antes solo dejaba vincular uno por panel.
- **Previsión de Forecast.Solar opcional para el array de EcoFlow**: nueva casilla "Añadir también una previsión para las horas futuras" — el dato de la hora actual siempre viene de la batería, pero ahora se puede además rellenar API key/lat/lon/kWp para que las horas futuras usen una previsión real en vez de quedarse en 0.

Cambio interno: los arrays vinculados a EcoFlow ahora guardan `ecoflow_pv_channels` (lista) en vez de `ecoflow_pv_channel` (uno solo) — si ya tenías paneles EcoFlow dados de alta con la v0.11.39/0.11.40, tendrás que volver a vincularlos (son pocos días de uso, no debería afectar a nadie más).

## 0.11.40
Ronda de correcciones y mejoras sobre los sensores de HA y las baterías EcoFlow:

- **Reinicios de energía cargada/descargada (y de "salud"/ciclos equivalentes)**: causa encontrada — se indexaban por el id de configuración de la batería, que cambia cada vez que se borra y se vuelve a dar de alta la misma batería física. Ahora se indexan por una identidad estable (SN/dirección BLE en EcoFlow, sensor de SOC en Home Assistant), con migración automática del histórico ya guardado bajo el id antiguo.
- **`sensor.battery_orchestrator_power`**: signo corregido — descargando = positivo, cargando = negativo (al revés que antes).
- **SOC y potencia ya se publican en vivo** (cada ~10s, ciclo independiente del de planificación) en vez de esperar al ciclo completo (podía tardar varios minutos). `energy_charged`/`energy_discharged` siguen en el ciclo normal, solo cambian cuando se manda una orden de verdad.
- **Nuevo `sensor.battery_orchestrator_solar_power`**: potencia solar total en vivo, ahora que también se puede ingerir desde puertos MPPT de baterías EcoFlow.
- **Autorrellenar capacidad y límites de potencia** al dar de alta una batería EcoFlow por Bluetooth/Híbrido — botón "Autorrellenar desde la batería" que trae la capacidad real (Wh) y los límites de carga/descarga (W) directos de la propia batería. Requiere el Puente BLE v0.2.3+. La API Cloud no tiene un campo de capacidad fiable, así que en Cloud-only sigue siendo manual.
- **Puertos MPPT también desde Cloud**: el descubrimiento de puertos MPPT (Configuración → Solar) ahora también consulta Cloud (MQTT) cuando BLE no tiene el dato todavía (p. ej. en Híbrido con la batería aún sin verse por Bluetooth) — antes solo miraba BLE.

## 0.11.39
Los puertos MPPT de una batería EcoFlow (paneles conectados directo, sin pasar por AC) ya se pueden **dar de alta en Configuración → Solar**, no solo usarse en vivo por detrás: nueva opción de Origen "Puerto MPPT de una batería EcoFlow" con un menú que pregunta al puente qué puertos tiene ese modelo concreto (1 a 4 según el modelo) y con qué potencia está cada uno ahora mismo — se añaden como cualquier otro panel/array, con su nombre, y quedan marcados automáticamente como "conectado directo a batería". Como cada puerto se da de alta por separado, una misma batería con paneles de zonas u orientaciones distintas puede tener varios paneles declarados, cada uno con su propio dato en vivo. Requiere v0.2.2+ del [Puente BLE](https://github.com/neoalarrode/Battery-Orchestrator-BLE-Bridge).

## 0.11.38
Descubrimiento de baterías EcoFlow **unificado**: en vez de dos listas sueltas (Cloud y Bluetooth) que había que enlazar a mano una con otra, ahora es una sola búsqueda y una sola lista — el backend empareja automáticamente por número de serie (lo devuelven las dos fuentes) y cada fila muestra de un vistazo lo que se ha encontrado de cada lado.

En modo **Híbrido**, si una batería solo aparece por Cloud (el dispositivo no se estaba anunciando por Bluetooth en ese momento), ya no hace falta esperar a que aparezca para darla de alta: se añade igual, marcada como "Bluetooth (buscando…)", y el ciclo de fondo la sigue buscando cada par de minutos — en cuanto se anuncie por Bluetooth, se vincula sola sin que haga falta volver a pasar por el formulario.

## 0.11.37
El SOC de una batería EcoFlow por Bluetooth usaba `battery_level`, que en un sistema con varias unidades EcoFlow enlazadas (BKW) es el **SOC agregado de todo el grupo**, no el de esa unidad — daba un valor que no coincidía con el de la app oficial. Ahora usa `battery_level_main`, el SOC real de la unidad, verificado contra una lectura real (81% agregado vs 82% real de esa unidad).

## 0.11.36
Arreglado el 404 real de "Obtener userId automáticamente" (v0.11.34): la llamada usaba una ruta absoluta (`/api/ecoflow/resolve_user_id`) en vez de relativa como el resto de la app (`api/...`) — bajo el Ingress de Home Assistant la página vive en `.../api/hassio_ingress/<token>/`, así que una ruta con barra inicial se salta ese prefijo y apunta a la raíz del dominio, donde no existe nada. No era la caché (aunque ese arreglo de la v0.11.35 también hacía falta): la petición sí llegaba a salir del navegador, solo que a la URL equivocada.

## 0.11.35
La página principal (`index.html`, todo el frontend en un único archivo) se estaba pudiendo quedar cacheada en el navegador o en el webview de la app móvil de Home Assistant tras actualizar el add-on, así que una actualización de la interfaz podía pasar desapercibida aunque el backend ya estuviera al día — es lo que impidió ver el botón nuevo de "Obtener userId automáticamente" de la v0.11.34 sin refrescar a mano. Ahora se sirve siempre con `Cache-Control: no-store`, para que el navegador la pida fresca en cada visita.

## 0.11.34
El userId de EcoFlow para el modo Bluetooth/Híbrido ya no hace falta copiarlo a mano desde otra integración: en "+ Añadir batería" → EcoFlow → Bluetooth/Híbrido hay ahora un botón "Obtener userId automáticamente" que pide tu email y contraseña de EcoFlow y los enfrenta contra el API de cuenta de EcoFlow (el mismo login que usa la app oficial) para resolver el userId. La contraseña viaja una sola vez a tu propia instancia de Battery Orchestrator para esa consulta y no se guarda en ningún sitio — ni en `config.json` ni en ningún log; lo único que se persiste es el userId ya resuelto, exactamente igual que si lo hubieras pegado tú a mano.

## 0.11.33
El puente BLE se ha rehecho para ser **genérico de verdad** (dominio `battery_orchestrator_ble_bridge`, servicios con campo `brand` en vez de fijos a EcoFlow, repositorio renombrado a [Battery-Orchestrator-BLE-Bridge](https://github.com/neoalarrode/Battery-Orchestrator-BLE-Bridge)) — este parche pone `ecoflow_ble.py` al día con esa nueva forma. Sin cambios de comportamiento para el usuario, solo para que Bluetooth/Híbrido sigan funcionando tras la reestructuración del puente.

## 0.11.32
Reestructuración pedida sobre cómo se añaden baterías EcoFlow — ya no hay una tarjeta aparte de "Baterías EcoFlow": todo vive dentro de "+ Añadir batería", con **Origen** ("Configuración manual" / "EcoFlow") y, al elegir EcoFlow, un segundo desplegable de **Modo de conexión** (Bluetooth / Cloud / Híbrido). El diseño es genérico a propósito para que una marca futura que no sea EcoFlow pueda sumarse sin rediseñar el formulario.
- **Bluetooth** (nuevo, apoyado en el puente [Battery-Orchestrator-EcoFlow-BLE](https://github.com/neoalarrode/Battery-Orchestrator-EcoFlow-BLE) — ver ese repositorio, todavía sin verificar contra hardware real): control directo por Bluetooth, incluido a través de un ESPHome BT Proxy, sin pasar por la nube de EcoFlow para nada.
- **Cloud**: el que ya había desde la v0.11.29/30, sin cambios de comportamiento.
- **Híbrido**: intenta Bluetooth primero (más preciso) y cae a Cloud automáticamente si no responde — verificado en local: con el puente BLE sin instalar, la lectura cae a Cloud sin ningún error ni dato inventado.
- Nuevo campo de cuenta EcoFlow: `userId` (identificador numérico, no la contraseña) para el modo Bluetooth/Híbrido — se obtiene una vez desde `ha-ef-ble` o similar y se guarda en la app, nunca se le pide la contraseña de la cuenta al usuario desde aquí.

## 0.11.31
Las baterías EcoFlow ya alimentan también `/api/live` (antes solo funcionaban en el ciclo de planificación): SOC agregado y potencia en vivo, junto con el resto de baterías, en el widget de "Baterías" de "Estado actual" y en el "Flujo de energía ahora mismo". En sistemas EcoFlow con varias unidades enlazadas, la potencia (que EcoFlow reporta agregada para todo el grupo, no por unidad) solo se cuenta una vez — nunca se duplica por tener varias baterías del mismo grupo declaradas. Verificado contra una instalación real: el SOC tarda algo más en llegar la primera vez (EcoFlow lo manda en su reporte periódico completo, más lento que la potencia), pero se rellena solo en cuanto llega, sin inventar ningún dato mientras tanto.

## 0.11.30
**Baterías EcoFlow gestionadas directamente desde Battery Orchestrator**, sin declarar ningún sensor ni switch de Home Assistant — cablea por completo el módulo `ecoflow_cloud.py` de la v0.11.29:
- Nueva tarjeta "Baterías EcoFlow" en la configuración: Access Key/Secret Key de tu cuenta de desarrollador de EcoFlow, y un botón "Buscar baterías EcoFlow" que descubre automáticamente todos tus dispositivos.
- "+ Añadir batería" tiene ahora un desplegable de **Origen** (Home Assistant / EcoFlow). Al añadir una batería EcoFlow desde el descubrimiento, se abre ya vinculada al dispositivo elegido — solo hace falta rellenar la capacidad real y, si quieres, los límites.
- El planificador trata una batería EcoFlow exactamente igual que cualquier otra: mismo reparto de carga por capacidad, mismo modo simulación, misma estimación de salud. Por debajo, en vez de encender/apagar un switch, activa o desactiva la tarea de carga/descarga programada de EcoFlow y ajusta su límite de potencia y SOC objetivo — verificado contra una instalación real antes de publicarse, incluido un ciclo completo en modo simulación de principio a fin.
- Documentado en DOCS.md/DOCS.en.md ("Baterías EcoFlow" / "EcoFlow batteries").

## 0.11.29
Primera pieza del soporte para baterías EcoFlow (STREAM): nuevo módulo `ecoflow_cloud.py`, cliente directo contra el API Cloud de EcoFlow (REST + MQTT, sin pasar por Home Assistant). **Todavía no está conectado a la interfaz** — es la base ya verificada contra una instalación real, el cableado a la configuración y al planificador llega en una próxima versión. Incluye:
- Descubrimiento de dispositivos y resolución del dispositivo "principal" de un grupo (necesario para mandar comandos en sistemas con varias unidades enlazadas).
- Lectura en vivo por MQTT (mucho más completa que el snapshot REST — incluye vatios y la programación de carga/descarga, cosas que el REST no expone) con caída a REST como red de seguridad si MQTT no ha dicho nada todavía.
- **Control real de las tareas de carga/descarga programadas** (activar/desactivar, límite de potencia por batería, SOC objetivo) vía el comando `cfgAllTimerTask` — no documentado por EcoFlow en ningún sitio, verificado a mano contra una cuenta real antes de darlo por bueno. Nunca escribe a ciegas: si todavía no se conoce la programación actual del grupo, no manda ningún comando.
- Conexión MQTT persistente y reutilizada (EcoFlow limita a 10 identificadores de cliente por cuenta y día).

## 0.11.28
Nuevo: 4 sensores **agregados** (todas las baterías juntas, no uno por batería) pensados específicamente para poder darlos de alta en el **Panel de Energía oficial de Home Assistant** (Ajustes → Paneles → Energía → Baterías):
- `sensor.battery_orchestrator_energy_charged` / `..._energy_discharged`: energía acumulada en kWh, con `device_class: energy` y `state_class: total_increasing` — justo lo que pide ese panel para "energía que entra"/"energía que sale" de la batería. Reutilizan el mismo contador de por vida que ya alimentaba "ciclos equivalentes" (`lifetime_store`), solo sumado entre baterías — ningún dato nuevo, ninguna cuenta duplicada.
- `sensor.battery_orchestrator_soc`: SOC agregado (%), y `sensor.battery_orchestrator_power`: potencia neta en vivo (W, positivo cargando/negativo descargando) — para poder ponerlos en una tarjeta normal del dashboard sin tener que sacarlos de los atributos de `sensor.battery_orchestrator_status`.

## 0.11.27
Nuevo: **Liquid Glass** en todo el panel. Misma paleta violeta/cian, misma cuadrícula HUD de fondo y los mismos componentes de siempre — pero las tarjetas, chips, inputs y demás superficies ahora usan desenfoque real (`backdrop-filter`) con un realce especular en el borde, sobre unas manchas de color ambiente discretas de fondo (sin las cuales el desenfoque no se notaría en nada). Probado antes en una demo aparte y aprobado antes de aplicarlo aquí. Funciona igual en modo claro y oscuro.

## 0.11.26
Reestructuración de la tarjeta "Consumo de la casa" (v0.11.25 lo dejaba mal organizado): ahora el **selector va primero** y decide qué campos rellenar, con "dos sensores" (consumo + vertido opcional) como opción por defecto — antes el sensor de consumo aparecía siempre fijo arriba y el desplegable de vertido quedaba suelto debajo, dando la sensación de ser dos cosas independientes cuando en realidad es una sola elección.
- **Ampliación importante del modo "unificado"**: el sensor único de red con signo ahora alimenta también la **previsión histórica del planificador**, no solo el flujo en vivo — con este sensor basta, no hace falta declarar ningún otro de consumo. Se reconstruye con el balance físico del panel (consumo = sol + red neta + descarga − carga de baterías), igual en vivo que en el histórico.
- Arreglo: la detección de consumo anómalo sumaba sol y descarga por segunda vez sobre un consumo que en modo unificado ya venía completo — corregido antes de publicarse, no llegó a afectar a ninguna instalación.
- Instalaciones que ya tenían guardado el modo de vertido de la v0.11.25 migran solas a la nueva casilla única, sin tener que volver a configurar nada.

## 0.11.25
Nuevo: **vertido a red en vivo**, en la tarjeta "Consumo de la casa" de la configuración. Sigue el mismo patrón "separado vs unificado" que ya usan las baterías para su sensor de potencia:
- Modo **separado**: un sensor dedicado de vertido (opcional — si no lo tienes o no lo quieres, simplemente no se muestra).
- Modo **unificado**: un único sensor con signo del punto de conexión a red (positivo importando, negativo vertiendo), del que se deriva el vertido sin necesitar un segundo sensor.
- El vertido se muestra como una caja aparte en el flujo de energía "ahora mismo" (con el mismo estado "apagado" si es 0W) — **nunca cuenta dentro del "consumo total"** ni afecta al margen de potencia contratada, porque el excedente vertido no pasa por la línea contratada.

## 0.11.24
Tercer paso sobre el flujo de energía: los datos ya eran en vivo (v0.11.22/23), pero "CONSUMO TOTAL AHORA MISMO" excluía a propósito la carga de baterías (no se contaba como "consumo"), mientras que el medidor de margen de potencia contratada SÍ la incluye — dos widgets en la misma pantalla con dos totales distintos que nunca cuadraban entre sí, aunque cada uno fuera correcto por su propia definición.
- **Arreglo**: `renderEnergyFlow()` (la barra de "ahora mismo") ya no recalcula sus propios números a partir de sensores sueltos en el navegador — lee directamente el mismo `energy_flow` que ya usa el medidor de potencia contratada, la misma fuente única de verdad. El total ahora SÍ incluye la carga de baterías, pero SOLO la parte que sale de la red facturable: la carga con excedente solar no pasa por el punto de conexión contratado (es autoconsumo puro), así que no cuenta como "consumo" ni infla el margen de potencia contratada — igual que ya hacía `flow.grid_w` en el backend, ahora el widget cuadra con él en vez de sumar de más.
- Nuevo: el recuadro de "Batería" dentro del flujo siempre muestra la potencia de carga completa (venga de sol, de red o de ambas) con el punto parpadeando mientras carga, y punto fijo mientras descarga — esto es independiente de que solo la parte de red sume al total de arriba.
- Nuevo: si la carga de la batería viene repartida entre excedente solar y red a la vez, el aviso ahora lo desglosa (antes solo decía "de red" o "de sol", sin más detalle si era una mezcla).
- Nuevo: cualquiera de las tres cajas del flujo (Solar / Batería / Red) que esté a 0 W ahora mismo se muestra atenuada ("apagada"), para distinguir de un vistazo qué fuente está realmente aportando algo.

## 0.11.23
Continuación directa de la v0.11.22: aquel arreglo hizo que `energy_flow` usara datos en vivo, pero seguía viviendo dentro de `/api/status` — que solo se actualiza una vez por `run_cycle()` (hasta `cycle_seconds`, 60s típico), no cada vez que el dashboard lo pide. El medidor de margen de potencia contratada (`renderPowerMeter`) solo se refrescaba con ese ritmo, en vez de al segundo.
- **Arreglo**: `energy_flow` (red, solar, entrada/salida de baterías) ahora también se calcula DENTRO de `/api/live` — el endpoint que el dashboard sondea cada 5s de verdad, sin esperar a ningún ciclo. La atribución solar/red de la carga de baterías se calcula igualmente en vivo (si el excedente solar ahora mismo cubre lo que se está cargando, se atribuye a solar; el resto a red), sin depender de la decisión del último ciclo del planificador.
- El medidor de margen y la barra de flujo ya usan preferentemente `/api/live`; `/api/status` se queda como aproximación de partida solo hasta que llega el primer dato en vivo (p.ej. justo al cargar la página).

## 0.11.22
**Arreglo crítico**: el diagrama de "flujo de energía ahora mismo" (y, con él, el medidor de margen de potencia contratada) se construía con los números del PLANIFICADOR (la media histórica prevista para esta hora), no con datos en vivo — a pesar de llamarse "ahora mismo". Si el consumo real se desviaba de la previsión (p.ej. un electrodoméstico encendido a mano), el flujo mostrado y, más grave, el margen de potencia contratada quedaban desfasados de la realidad — pudiendo hacer pensar que sobraba margen cuando no era así, justo el caso que ese medidor existe para evitar.
- Ahora `solar_w`, `load_w`, `battery_net_w` y el resto del flujo se calculan con lectura EN VIVO de los sensores (mismos datos que ya usa `/api/live`) — la carga/descarga total de baterías también se suma en vivo (nuevo `_live_battery_charge_discharge_w`, misma lógica de `power_sensor_mode` que ya usaba `/api/live` por batería). La previsión del planificador solo se usa como red de seguridad si un sensor concreto no responde en ese instante, nunca por defecto.
- La FUENTE de la carga (solar vs red) sigue viniendo de la decisión real que ya tomó el planificador este ciclo (`charge_source`) — eso no se puede medir con un sensor genérico —, pero el vatiaje que se le atribuye ya es el real, no el previsto.

## 0.11.21
Cuarto y último paso sobre el descubrimiento de zonas de Climate Orchestrator: se elimina el sondeo automático por completo (aunque fuera cacheado y ya muy barato, ver v0.11.20) y se sustituye por un botón manual.
- Cambio: `climate_link.py` ya no descubre zonas por su cuenta en ningún momento — ni con temporizador ni cacheado. La lista de `entity_id` se guarda en `config.json` (`climate_orchestrator_zones`) y solo se actualiza cuando el usuario pulsa el nuevo botón **"Buscar zonas de Climate Orchestrator"** en la Configuración (nuevo endpoint `POST /api/climate/discover`). Cada ciclo (`run_cycle`) lee esa lista ya guardada y solo pide, entidad a entidad, su potencia AHORA MISMO — nunca vuelve a preguntar "qué zonas hay" por sí solo.
- Nuevo: la tarjeta de configuración muestra la lista de zonas actualmente monitorizadas y la fecha de la última búsqueda — para poder comprobar de un vistazo qué sensores está teniendo en cuenta la app, sin tener que ir al dashboard.
- Sin Climate Orchestrator instalado, o sin haber pulsado nunca el botón, el comportamiento es exactamente el de siempre (0W, sin zonas) — nada de esto es obligatorio.

## 0.11.20
Tercera pasada de optimización: seguían reportándose cuelgues intermitentes tras la v0.11.19, así que se pasó a pedir EXPRESAMENTE solo las entidades de Climate Orchestrator en vez de filtrar sobre un volcado más genérico.
- Mejora: el descubrimiento de zonas de Climate Orchestrator (`climate_link._discover_zone_ids`, cada 5 min) ya no pide `/api/states` entero ni siquiera en ese ciclo de 5 min — ahora usa la API de plantillas de HA (`POST /api/template`, nuevo `ha_client.render_template`) con `integration_entities('climate_orchestrator')`, una función nativa de HA que consulta directamente el registro de entidades y devuelve SOLO lo que pertenece a esa integración — es HA Core quien resuelve la pertenencia, nunca se serializan ni transmiten las demás entidades de la instalación para descartarlas aquí. Más preciso además que el filtro anterior por atributo (`states.climate` + `climate_orchestrator_zone`): eso habría incluido cualquier otro termostato instalado si compartiera por casualidad el dominio "climate", esto va derecho al registro de entidades de la integración correcta. Si la plantilla fallase por lo que sea (HA muy antiguo, error puntual), sigue existiendo la red de seguridad del volcado completo + atributo, igual que antes — nunca deja de funcionar el descubrimiento, solo cambia el coste del camino normal.

## 0.11.19
Segunda pasada de optimización tras seguir reportándose cuelgues intermitentes de HA Core en una Raspberry Pi 5 (v0.11.18 no bastó por sí sola):
- Arreglo: `has_recent_history()` (usada por la corrección de previsión solar, ver v0.11.0) pedía el histórico completo del sensor solar — potencialmente decenas de miles de puntos en sensores que reportan muy a menudo — en CADA ciclo, solo para una comprobación booleana ("¿tiene ya histórico?") que en la práctica casi nunca cambia. Ahora se cachea 30 min.
- Mejora: `sensor.battery_orchestrator_status` y `sensor.battery_orchestrator_grid_signal` se publicaban en cada ciclo (cada 30-60s típico) — cada publicación escribe una fila nueva en el recorder de HA, y `grid_signal` además dispara una reevaluación reactiva en cada zona de Climate Orchestrator que lo escuche. Ninguno de los dos necesita esa frecuencia (ni el precio/tramo ni el estado cambian tan rápido). Ahora se publican como mucho cada 2 minutos.

## 0.11.18
- Arreglo importante de rendimiento: `climate_link.read_live_power_w()` (la lectura de consumo de Climate Orchestrator, ver v0.11.13) pedía `/api/states` — el volcado COMPLETO de todas las entidades de la instalación — en CADA ciclo (cada `cycle_seconds`, 30s en instalaciones típicas), no solo cuando tocaba redescubrir zonas. En una instalación con miles de entidades, eso es carga real e innecesaria sobre HA Core cada 30 segundos sin parar. Confirmado en producción: HA Core quedándose colgado/sin red intermitentemente desde que se implantó esta integración, sin reinicios visibles — encaja exactamente con este patrón. Ahora el volcado completo solo se pide cuando la caché de descubrimiento caduca (cada 5 min); la lectura fresca de cada zona en cada ciclo se hace con `/api/states/<entity_id>` (una sola entidad, barata), nunca repitiendo el volcado entero.

## 0.11.17
- Corrección sobre la v0.11.16 (nunca llegó a instalarse): la semántica correcta es que el switch de descarga debe quedar ACTIVO tanto en "bloqueada" como en "sin acción" — es el límite de potencia a 0 el que corta la salida de verdad, no el switch (en estos modelos, p.ej. EcoFlow, ese switch es una "tarea", no el interruptor físico; con el switch apagado el equipo puede seguir descargando igual, como un SAI, para sostener la carga conectada). "Cargar" queda como estaba siempre (switch de descarga a secas en OFF, sin tocar el límite) — el cambio de la 0.11.16 ahí estaba equivocado. Confirmado en producción: batería en "sin acción" seguía descargando de verdad con el switch simplemente apagado.

## 0.11.16 (sin publicar de verdad — sustituida por la 0.11.17 antes de instalarse)
- Arreglo: cuando el plan decidía "sin acción" (o al empezar a cargar), la app apagaba directamente el switch de descarga de la batería, sin mirar si había un `discharge_power_limit_entity` declarado — a diferencia del caso "descarga bloqueada", que sí lo prioriza.

## 0.11.15
- Arreglo: `sensor.battery_orchestrator_grid_signal` (la señal para Climate Orchestrator) se calculaba DESPUÉS de la comprobación de disponibilidad de las baterías — si todas tenían el sensor de SOC caído en ese ciclo (p.ej. justo tras reiniciar Home Assistant, mientras integraciones en la nube como EcoFlow todavía reconectan), el ciclo cortaba antes de llegar a publicarla, dejando a Climate Orchestrator sin dato hasta que las baterías volvieran. Confirmado en producción: tras un reinicio de HA, el sensor desapareció (los estados publicados a mano no sobreviven un reinicio de HA Core) y no volvía porque las baterías tardaron varios minutos en reconectar. Precio/tramo/sol no dependen de que las baterías respondan — ahora se calcula y publica ANTES de esa comprobación, con los mismos datos (`prices_tiers`/`pv_forecast`/`load_forecast`) ya disponibles en ese punto.

## 0.11.14
- Arreglo: la integración con Climate Orchestrator (v0.11.13) trataba una zona activa (calentando/enfriando de verdad) pero sin sensor/potencia declarada en Climate Orchestrator igual que una zona inactiva — 0W en los dos casos, escondiendo justo el caso que más importa. Ahora se distinguen con `hvac_action` (atributo estándar de cualquier `climate.*`): inactiva sigue siendo 0W real; activa-sin-dato se marca "desconocida", nunca se suma como si fuera cero, y se avisa en la tarjeta del dashboard con el nombre de la zona.
- Nuevo: Battery Orchestrator publica también su `load_sensor` (el sensor general de consumo de la casa, ya declarado en "Consumo de la casa") en `sensor.battery_orchestrator_grid_signal`, para que Climate Orchestrator pueda **aprender solo** el consumo de sus actuadores (su `power_model.py` ya sabía hacerlo, correlacionando transiciones on/off contra un sensor general) sin que el usuario tenga que declarar el mismo sensor dos veces en dos integraciones distintas.

## 0.11.13
- Nuevo: integración automática con **Climate Orchestrator** (si está instalado), sin ninguna configuración manual en ningún lado:
  - Publica `sensor.battery_orchestrator_grid_signal` (entity_id fijo) con el precio/tramo actual, el excedente solar ahora mismo, el margen de potencia contratada y la previsión hora a hora — para que Climate Orchestrator pueda ajustar su prioridad "ahorro" con datos económicos reales, no solo meteorología.
  - Descubre solo las zonas de Climate Orchestrator (por un atributo marcador en sus propias entidades `climate.*`, sin declarar ningún `entity_id` a mano) y suma su consumo en vivo a lo "esperado" del detector de anomalías — así una calefacción trabajando de verdad un día de frío no se confunde con un consumo fuera de lo normal.
  - Nueva tarjeta "Climate Orchestrator" en el dashboard (oculta si no se detecta ninguna zona) mostrando las zonas detectadas y su consumo en vivo.

## 0.11.12
- Seguridad: `/api/status` (accesible sin autenticación desde el puerto de solo lectura del wallpanel) filtraba el `entity_id` del switch de cada carga diferible, contradiciendo el propio diseño del wallpanel ("ni expone la configuración: nombres de entidades..."). El frontend no usa ese dato desde ahí (la ficha de configuración, que sí lo necesita, lee de `/api/config`, bloqueado en el wallpanel) — se ha quitado de la respuesta. Encontrado en una prueba de intrusión dirigida contra el wallpanel (antes de este arreglo se probaron sistemáticamente spoofing de cabeceras Host/X-Forwarded-*, bypass de método HTTP, normalización de rutas, traversal en la ruta estática y peticiones HTTP en bruto — ninguno de esos vectores logró saltarse la restricción del puerto, que depende del socket real de conexión y no de nada que mande el cliente).

## 0.11.11
Revisión completa del frontend (`index.html`) en busca de bugs. Dos encontrados:
- Arreglo: el aviso nuevo de v0.11.8 ("encendida a mano fuera de ventana — no se toca") no tenía traducción al inglés — con la interfaz en inglés se veía en español sin traducir en el panel de estado. Añadida su traducción.
- Arreglo (robustez): el sondeo periódico de `/api/status` cada 15s no atrapaba fallos de red puntuales — a diferencia de `/api/live`, que sí lo hacía a propósito. Un fallo de red quedaba como una promesa rechazada sin capturar en la consola en vez de reintentarse en silencio en el siguiente sondeo. La carga inicial de la página sigue mostrando la tarjeta de error de conexión igual que antes si falla nada más entrar.

## 0.11.10
- Mejora (rendimiento/estabilidad): la media histórica por hora del día (`hourly_average_forecast_with_reliability`, usada por la previsión de consumo, la de solar y la corrección estadística) volvía a pedir el histórico completo a Home Assistant en CADA ciclo — con varias baterías + solar + consumo eso son varias peticiones de hasta 21 días de historico, algunas con decenas de miles de puntos, repetidas cada `cycle_seconds` (30-60s típico). Esto pudo contribuir a episodios de inestabilidad del propio Home Assistant. Ahora la parte cara (pedir y recorrer el histórico) se cachea 15 minutos; la alineación al horizonte desde la hora actual se sigue recalculando siempre al vuelo, así que no cambia ningún resultado, solo cuántas veces se pide.

## 0.11.9
Revisión completa del proyecto en busca de bugs. Cuatro encontrados y corregidos:
- Arreglo: una carga diferible de frecuencia "once" podía re-programarse (y volver a ejecutarse) justo al terminar su ventana, en vez de marcarse como hecha — el planificador la recalculaba (por el orden de ejecución dentro del ciclo: planificar pasa antes que marcar "done") antes de que el resto del ciclo llegara a marcarla, perdiendo la evidencia de que ya había terminado. Ahora, una vez decidida una ocurrencia "once", se reutiliza siempre tal cual hasta que se marque "done" — nunca se recalcula sola.
- Arreglo (robustez): el modo de tarifa dinámica PVPC y el endpoint `/api/live` (el que refresca el dashboard cada pocos segundos) no atrapaban fallos de red/HA pasajeros (502/503, timeout) — solo el 404. Mismo tipo de fallo ya corregido en v0.11.7 para el resto de lecturas, pero estos dos puntos se habían quedado fuera.
- Arreglo: `/api/battery_health` cruzaba los datos de capacidad real y ciclos de vida de cada batería por NOMBRE en vez de por ID — si renombrabas una batería, o dos compartían nombre, los datos se podían atribuir a la batería equivocada. Ahora se cruzan por ID.

## 0.11.8
- Arreglo: si encendías a mano una carga diferible (p.ej. el lavavajillas) fuera de su ventana programada, el siguiente ciclo la volvía a apagar — el código apagaba el switch sin más cada vez que "ahora" no caía en ninguna ventana, sin distinguir entre "esta carga la había encendido la propia app y le tocaba apagarla" y "esto lo ha encendido el usuario a mano y no le corresponde a la app tocarlo". Ahora se usa el registro interno de sesión (que ya existía para medir energía) para saber si fue la app quien la encendió: solo apaga lo que ella misma prendió; un encendido manual fuera de ventana se respeta y se deja tal cual.

## 0.11.7
- Arreglo (robustez): un fallo de red pasajero contra Home Assistant (502/503 del Supervisor, típico mientras HA Core arranca o se reinicia; timeout) tumbaba el ciclo de planificación ENTERO — incluida la decisión de carga/descarga de baterías — porque `get_numeric_state`, `pv_forecast_from_entity` y el histórico usado por la previsión (`hourly_average_forecast_with_reliability`) no atrapaban `requests.RequestException`, solo el 404 (`HAError`). Ahora esos fallos pasajeros caen al valor por defecto de cada función (o a lista vacía en el histórico, con su misma lógica de reintento/relleno ya existente) en vez de propagar la excepción.
- Arreglo: una carga diferible con una ocurrencia "empezada" cuya hora de inicio ya había quedado fuera de la ventana de planificación (p.ej. tras un reinicio del addon a media mañana con una ocurrencia de medianoche todavía sin limpiar) hacía `ValueError` en `deferrable_scheduler.plan_for_load` y tumbaba también el resto del ciclo, incluida la decisión de baterías. Ahora esa ocurrencia se ignora para el cálculo de horas bloqueadas en vez de crashear — ya pasó, no hay nada que bloquear para las horas que quedan hoy. Además, un fallo al planificar una carga diferible concreta ya no bloquea a las demás cargas ni a la decisión de baterías: se registra en el log y se continúa.

## 0.11.6
- Arreglo: horizonte de planificación por defecto demasiado corto (24-30h) — según la hora del día, el plan podía no llegar a ver la punta del día siguiente y decidir "sin acción (no compensa)" en la madrugada que le tocaba cargar, aunque la batería estuviera en el mínimo. Con la reconstrucción de consumo ya corregida (v0.11.3-0.11.5) la batería se agota antes en el día, lo que hacía mucho más visible este límite preexistente. Nuevo valor por defecto para instalaciones nuevas: 48h (cubre el día siguiente completo sea cual sea la hora actual). Las instalaciones existentes mantienen su valor guardado — se recomienda subirlo a 48h o más desde Configuración → General; añadida nota explicativa en ese campo.

## 0.11.5
- Arreglo definitivo: la causa raíz real de la energía necesaria prevista demasiado baja no eran los fixes de signo de v0.11.3/v0.11.4 (necesarios pero insuficientes) — era que la reconstrucción de consumo (`true_load_forecast`) construía la lista de sensores de descarga de batería leyendo siempre el campo `power_sensor`, que solo se usa en modo "separado". En modo "Combinado" (un único sensor con signo, seleccionable desde la ficha de cada batería) el dato vive en `net_power_sensor`, y `power_sensor` queda vacío — así que para cualquier batería en modo combinado el término de descarga histórica no se sumaba NUNCA, ni con signo bueno ni malo, sencillamente estaba ausente. Confirmado reproduciendo exactamente los valores reportados (492W, 219W, 50W...) al calcular sin ningún término de batería. Ahora se elige el sensor correcto según el modo de cada batería, igual que ya hacía el cálculo en vivo (`net_power_w`); también corregida la misma lectura en la detección de anomalías de consumo.

## 0.11.4
- Arreglo: el `abs()` de v0.11.3 se aplicaba sobre la MEDIA ya calculada del sensor de descarga, no sobre cada muestra individual. En sensores bidireccionales (carga positiva/descarga negativa) esto no bastaba: si una franja horaria mezclaba muestras de carga y descarga de distintos días (p.ej. unos días todavía cargando a esa hora, otros ya descargando), esas muestras se cancelaban entre sí ANTES de aplicar el valor absoluto, y el resultado seguía hundiéndose cerca de cero pese al fix anterior. Confirmado con datos reales: la hora 08:00 daba una media de -8.9W (cancelación) cuando el sensor de descarga dedicado de la misma batería mostraba 165.4W reales en esa franja. Ahora el valor absoluto se aplica a cada muestra antes de promediar, no después.

## 0.11.3
- Arreglo: la reconstrucción histórica de consumo (`true_load_forecast`) sumaba la media histórica del sensor de descarga de cada batería tal cual, sin `abs()` — si ese sensor reporta la descarga en negativo (el mismo caso de signo invertido ya detectado y corregido en el cálculo en vivo, ver `net_power_w`), una media histórica negativa RESTABA del consumo reconstruido en vez de sumar, hundiendo artificialmente la energía necesaria prevista justo en las horas donde históricamente hubo descarga (típicamente horas de sol insuficiente). Ahora se toma en valor absoluto, igual que ya se hacía en el cálculo en vivo.

## 0.11.2
- Arreglo: la previsión de consumo (`hourly_average_forecast`, usada tanto para reconstruir el consumo real como para la corrección de previsión solar de v0.11.0) no exigía un mínimo de muestras reales por franja horaria — una sola lectura suelta en una hora concreta (p.ej. una nube pasajera, o un sensor recién dado de alta que apenas ha visto esa franja una vez) bastaba para fijar la "media" de toda esa hora, arrastrando ruido a la previsión. Esto podía hacer que la energía necesaria prevista se hundiese de forma poco realista en horas de sol, porque el consumo de red ya está cerca de cero cuando el sol cubre la casa y una media solar mal calculada no lo compensaba. Ahora una franja horaria necesita al menos 3 muestras reales para considerarse fiable; si no las tiene, se rellena con la media de las franjas que sí las tienen (igual que ya se hacía para horas sin ningún dato). La corrección de previsión solar de v0.11.0 también respeta ahora esta fiabilidad hora a hora, en vez de un chequeo global de "hay algún dato en las últimas 24h".

## 0.11.1
- Seguridad: `/api/run_now` devolvía el mensaje de la excepción real al cliente si el ciclo forzado fallaba (alerta CodeQL "Information exposure through an exception") — podía filtrar rutas de fichero o nombres internos. Ahora el detalle completo solo va al log del addon; el cliente recibe un mensaje genérico.

## 0.11.0
- Arreglo: la carga en hora valle calculaba el objetivo de reserva contra todo el horizonte de previsión en vez de pararse en el siguiente tramo valle, y no tenía en cuenta las horas llano (solo punta) al decidir cuánto cargar — ahora cubre correctamente llano + punta hasta el próximo valle, priorizando siempre cubrir antes las horas punta.
- Nuevo: corrección estadística de la previsión solar. Si un array de paneles declara su sensor de generación real ("current_sensor"), la previsión hora a hora se corrige con la media real de esa misma hora del día en los últimos días (igual que ya se hacía con el consumo): se usa el mínimo entre esa media real y la previsión oficial (API de Forecast.Solar o sensor de HA), así se prioriza lo que la ubicación real ha demostrado generar (sombras, obstáculos...) salvo que la previsión oficial sea aún más baja para esa hora (señal de peor tiempo de lo habitual). Sin histórico todavía (sensor recién declarado), se usa la previsión oficial sin corregir.

## 0.10.3
- Nuevo: favicon en la pestaña del navegador (el mismo cuadrado degradado violeta→cian con el rayo de la cabecera) — antes no se veía ningún icono propio.

## 0.10.2
- Arreglo: "Fiabilidad de la previsión" (antes "Precisión última hora") restaba directamente los puntos de desviación de SOC contra 100 — una escala sin relación real (una desviación de 3 puntos es gravísima si solo se preveía mover 2, e insignificante si se preveían mover 25; restar sin más trataba los dos casos igual). Ahora se calcula en proporción a cuánto preveía moverse la batería esa hora, con un mínimo de 10 puntos de referencia para no disparar porcentajes absurdos cuando apenas se preveía movimiento.

## 0.10.1
- Arreglo: al exponer el puerto de solo lectura (wallpanel) fuera de la LAN a través de un proxy o reenvío de puertos, la interfaz podía romperse al arrancar ("No se pudo cargar la configuración") — detectaba el modo wallpanel mirando si el navegador veía literalmente el puerto 8098, y un proxy puede evitar que lo vea aunque el servidor siga bloqueando esa ruta igualmente. Ahora se decide por la respuesta real del servidor (si `/api/config` falla, se cae a modo de solo lectura), no por adivinar el puerto.

## 0.10.0
- Nuevo: sensor de potencia de batería con carga y descarga — en "Configuración → Baterías" ahora se puede elegir entre ningún sensor, dos sensores por separado (descarga, como antes, y opcionalmente uno de carga) o un único sensor combinado con signo (positivo cargando, negativo descargando). Con lectura de carga disponible, "Cargando/Descargando" y "Flujo de energía ahora mismo" pasan a mostrar la carga en vivo (antes solo se veía la última orden mandada), y el widget indica si esa carga viene de excedente solar o de red, comparando en vivo si hay importación de red a la vez. Las instalaciones que ya tenían un sensor de descarga declarado siguen funcionando igual, sin tener que tocar nada.
- Arreglo: el consumo total en vivo (cajita "Consumo" y "Flujo de energía ahora mismo") podía inflarse cuando había excedente solar exportándose sin usar — se estaba contando toda la producción solar como si se hubiera consumido entera, en vez de solo la parte que de verdad ha ido a la casa o a cargar la batería.

## 0.9.2
- Arreglo: "Consumo" y "Flujo de energía ahora mismo" en vivo usaban directamente el sensor de consumo declarado como si fuera el consumo total — pero ese sensor es la base YA SIN la carga de baterías (así lo pide la propia tarjeta de "Consumo de la casa"), así que en cuanto el sol o las baterías cubrían casi todo el consumo, esos widgets se quedaban mostrando casi 0W aunque hubiera cientos de W circulando de verdad. Ahora se reconstruye igual que en el resto de la app: base + solar + descarga de baterías.
- Arreglo: "Objetivo de reserva" (en "Próxima punta") contaba la punta de TODO el horizonte de previsión configurado (podía incluir la de mañana), no solo la que queda antes del próximo valle — inflaba muchísimo el número en instalaciones con horizonte largo. Ahora usa el mismo criterio de corte en el próximo valle que ya usa el planificador de verdad para decidir cuánto cargar.

## 0.9.1
- Arreglo: la potencia de descarga en vivo de cada batería (cajita "Descargando" y la barra de "Flujo de energía ahora mismo") asumía que el sensor de descarga siempre da un valor positivo. Algunas integraciones de batería/inversor exponen en cambio una "potencia de batería" con signo, negativa al descargar — con esas, la aportación de la batería se recortaba a 0 y ese consumo se le atribuía por error a la red. Ahora se usa el valor absoluto de la lectura, así que da igual el convenio de signo del sensor concreto.

## 0.9.0
- Arreglo: el gráfico "Flujo de energía ahora mismo" se quedaba parado y con números irreales — se basaba en la previsión media histórica de esa hora (recalculada solo una vez por ciclo completo, cada `cycle_seconds`), no en lo que estaba pasando de verdad. Ahora se lee directo de Home Assistant y se refresca cada 5 segundos, igual que el resto del panel "en vivo". La carga de batería (que no tiene sensor de potencia en vivo, solo el de descarga) se muestra aparte como la última orden mandada, para no mezclar dato medido con dato ordenado en el mismo número.
- Arreglo: en el puerto de solo lectura (wallpanel), "Margen de potencia contratada" aparecía siempre como no configurado, aunque sí lo estuviera — dependía de la configuración completa, que ese puerto no tiene acceso. Ahora viaja en el propio estado en vivo.
- Nuevo: brillo animado en las barras indicadoras de "Estado actual" (flujo de energía, medidor de potencia, baterías, próxima punta), para que no se vean estáticas — respeta la preferencia de menos movimiento del sistema.
- Nuevo: las barras de baterías individuales ahora se colorean según cuánto se queda cada una por debajo de lo esperado (verde cerca, naranja algo por debajo, rojo muy por debajo) — comparando contra la media ponderada por la capacidad real declarada de cada una, no una media simple, para que una batería más grande o más pequeña no parezca desviada solo por su tamaño.
- Nuevo: "Precisión última hora" sustituye a "Reserva actual" en la tarjeta de "Próxima punta" — ya no mide cuánta reserva hay acumulada (eso lo siguen mostrando "SOC ahora" y "Objetivo de reserva"), mide si lo que ha pasado de verdad en la última hora se parece a lo que el plan predijo para el final de esa hora, con el detalle siempre visible (esperado vs. real). Útil para detectar consumos inesperados (p.ej. un aparato encendido a tope) sin confundirlo con un problema de la batería.

## 0.8.0
- Nuevo: panel de solo lectura (wallpanel) — además de Ingress, el add-on expone su propio puerto (8098 por defecto, configurable/desactivable desde la pestaña de red del add-on) para acceder al panel directamente por IP, sin pasar por el login de Home Assistant. Pensado para dejarlo fijo en una tablet de pared con WallPanel/Fully Kiosk. Por ese puerto no aparece la pestaña "Configuración" ni el botón "Ejecutar ciclo ahora", y el propio servidor rechaza (403) cualquier lectura o escritura de la configuración aunque se llame a la API directamente saltándose la interfaz — a diferencia de Ingress, ese puerto no lleva delante el login de Home Assistant.

## 0.7.2
- Arreglo: la barra de "Flujo de energía ahora mismo" solo representaba el reparto de la producción SOLAR (a casa / a batería), así que en cuanto había importación de red esta no aparecía en la barra en absoluto — solo como número suelto debajo. Ahora la barra representa el CONSUMO TOTAL activo ahora mismo (casa + lo que se esté cargando en la batería, si procede) y se rellena en proporción a de dónde sale esa energía: solar, batería descargando o red — los tres siempre suman exactamente el total.

## 0.7.1
- Cambio: el refresco en vivo cada 5 segundos (SOC, solar, consumo) ya no se muestra en una línea de texto aparte — ahora actualiza directamente el número dentro de las propias cajitas de "Estado actual" (SOC agregado, Solar, Consumo), sin esperar al ciclo completo de optimización. La cajita "Cargando/Descargando" también se refresca en vivo, pero solo mientras se está descargando — no hay forma fiable de leer la potencia de carga real en vivo (el sensor de batería declarado es de descarga, no de carga), así que ese número se deja tal cual hasta el próximo ciclo en vez de inventarlo.

## 0.7.0
- Nuevo: cargas diferibles — declara electrodomésticos con un enchufe/switch controlable (lavadora, lavavajillas, termo eléctrico...) en "Configuración → Cargas diferibles". Para cada uno eliges la frecuencia (puntual, diaria o varias veces al día, con días de la semana concretos si quieres) y si se puede interrumpir a medias o no. La app decide sola la hora que más conviene: primero busca excedente solar suficiente, y si no lo hay, la hora más barata disponible. Con un sensor de consumo (opcional), la app aprende sola cuánta energía gasta cada activación y cuánto tarda de verdad su ciclo, para que una carga no interrumpible (lavadora, lavavajillas) nunca se corte a medio programa.
- Nuevo: el consumo esperado de las cargas diferibles activas se suma a la previsión que usa el detector de anomalías, para que la app no confunda una carga que ella misma acaba de encender con un consumo fuera de lo normal.
- Nuevo: widget "Cargas diferibles" en Estado actual, con el estado en vivo (encendida/apagada, potencia real) y la ventana programada de cada carga.
- Nuevo: la línea "En vivo ahora" (SOC, solar, consumo) en Estado actual se refresca cada 5 segundos leyendo directo de Home Assistant, sin esperar al próximo ciclo completo de optimización.

## 0.6.0
- Nuevo: interfaz bilingüe español/inglés — se autodetecta el idioma del navegador, y hay un desplegable con banderita en la esquina superior derecha para elegirlo a mano (Auto/Español/English). El idioma elegido se guarda como el de esta instalación (junto al resto de la configuración), así que no hace falta volver a seleccionarlo al entrar desde otro dispositivo o navegador.
- Nuevo: README y DOCS traducidos al inglés (`README.en.md`, `DOCS.en.md`), con enlaces cruzados entre ambos idiomas en la cabecera de cada documento.
- Nuevo: los paneles/arrays solares ahora se pueden editar, no solo añadir/eliminar (igual que las baterías).
- Arreglo: la tarjeta "Seguridad y límites" no tenía botón de guardado — los cambios de potencia contratada o días de histórico no se guardaban hasta pulsar el de otra tarjeta.

## 0.5.6
- Arreglo: si Home Assistant tardaba en responder (timeout puntual del Supervisor) al mandar la orden a UNA batería, el ciclo entero se abortaba con una excepción sin haber llegado a avisar al resto de baterías esa pasada. Ahora cada batería se manda por separado: un fallo puntual en una queda registrado como aviso en su propia línea del log, y no impide que se les mande la orden a las demás ni que el ciclo termine con normalidad (histórico, ahorro, estado, etc.).

## 0.5.5
- Arreglo: con un horizonte de previsión que llegaba a la punta del día siguiente, el motor sumaba la punta de HOY y la de MAÑANA como si fuera una sola reserva a cubrir ya mismo — sin contar con que el valle de esta noche vuelve a recargar la batería antes de que llegue la punta de mañana. Esto forzaba cargas de emergencia en llano (más caras que valle) y bloqueaba descargas en llano que en realidad no hacían falta, aunque sobrara batería al final del día. Ahora la cuenta de "punta que queda por cubrir" se corta en la próxima hora valle, ya que esa hora es en sí misma una nueva oportunidad de recarga barata.

## 0.5.4
- Nuevo: "Estado actual" con seis mejoras — diagrama del flujo de energía ahora mismo (de dónde sale la potencia solar y a dónde va), desglose individual por batería sin ir a Configuración, medidor de cuánto se está usando de la potencia contratada, cuenta atrás al próximo cambio de tramo tarifario (no solo a la próxima punta), tendencia del SOC de las últimas horas en la propia tarjeta, y comparativa del consumo de hoy frente a la media de los últimos 7 días.
- Cambio: el histórico ahora conserva 8 días (antes 3) para poder calcular la comparativa de consumo semanal.

## 0.5.3
- Nuevo: la batería ahora también descarga en horas valle, pero solo con el excedente de SOC por encima de la reserva necesaria para punta/llano futuros — típico tras un día de mucho sol con buena previsión para el siguiente. Antes se quedaba parada toda la noche comprando de red aunque estuviera llena. Nunca toca la reserva, y de paso libera hueco para no desperdiciar el sol del día siguiente. Aplica en los tres modos de prioridad.
- Nuevo: tipo de instalación por panel/string solar — "autoconsumo (AC)" (comportamiento de siempre) o "conectado directo a batería (inversor integrado)". Va en cada panel, no en la batería, porque una misma instalación puede tener paneles de los dos tipos a la vez. Con "conectado directo", la app descuenta esa potencia de lo que pide por AC al resto de baterías en vez de mandar una orden de carga innecesaria; sí sigue mandando orden para cargar desde red o para descargar.
- Cambio: se fusionan los dos apartados de solar en uno — cada panel/array declarado en "Previsión solar" lleva ahora su propio sensor de generación instantánea (antes había un único sensor agregado aparte). Así puedes declarar varios strings/tejados sin crear un sensor agregado en Home Assistant. Si veníais de una versión anterior con un solo panel declarado, el sensor antiguo se traslada solo la primera vez que arranca; con varios paneles hay que reasignarlo a mano una vez.

## 0.5.2
- Nuevo: interruptor "Carga sostenida" en Configuración → Prioridad, disponible con "Ahorro" o "Longevidad" (no aplica con "Autoconsumo solar"). Con él activo, la carga deliberada desde red (valle y emergencia en llano) ya no va siempre a máxima potencia — se reparte hasta la próxima vez que la batería vaya a hacer falta de verdad (llano o punta, lo primero que llegue), con margen de seguridad del 20%. Menos calor/estrés en la batería. Si el tiempo se agota, la potencia sube sola hasta el máximo sin necesitar una rama de emergencia aparte.

## 0.5.1
- Arreglo: el "SOC agregado" de "Estado actual" mostraba la PROYECCIÓN de cómo quedaría el SOC al final de la hora actual (el plan trabaja en pasos de una hora), no el SOC real medido ahora mismo — con mucho excedente solar cargando, se disparaba muy por encima de las lecturas reales de cada batería (p. ej. 97.6% con baterías al 46-64%). Ahora usa el SOC real ponderado, el mismo que ya se publicaba en el sensor de Home Assistant.

## 0.5.0
- Nuevo: ahorro acumulado — compara el coste real pagado con el que se habría pagado sin batería, hora a hora, y lo acumula por día y en total. Se ve en "Estado actual".
- Nuevo: cuenta atrás a la próxima hora punta en "Estado actual", con la reserva de energía actual frente al objetivo que está usando el planificador.
- Nuevo: detección de anomalías de consumo — compara el consumo real medido ahora contra la previsión histórica de esa hora; si se dispara y se sostiene varios ciclos, se marca "Anómalo" (antes "Saludable") y aparece una notificación en Home Assistant más un cuadro con el detalle (desde cuándo, consumo real vs. esperado, diferencia).
- Nuevo: exportar/importar configuración completa desde "Configuración → Copia de seguridad", para no perderla si hay que reinstalar el add-on.
- Nuevo: modo de prioridad configurable — "Ahorro" (el comportamiento de siempre), "Autoconsumo solar" (nunca carga desde red, solo con excedente) o "Longevidad de batería" (como ahorro, pero sin superar el 90% de SOC).

## 0.4.0
- Nuevo: interfaz reorganizada en pestañas — Estado actual, Previsión, Salud de batería y Configuración.
- Nuevo: gráfica del SOC agregado a lo largo del día en "Previsión", con las franjas de tarifa de fondo y tooltip por hora.
- Nuevo: pestaña "Salud de batería" — estima la capacidad real de cada batería (comparada con la declarada) observando cuánta energía hace falta para mover su SOC un tramo grande, además de los ciclos equivalentes de por vida.
- Nuevo: README y DOCS reescritos para reflejar el estado actual de la app (pestañas, salud de batería, carga de emergencia en llano, fórmula de consumo real vigente).

## 0.3.0
- Nuevo: tabla "Plan del día" de 00:00 a 00:00 — combina lo ya ocurrido hoy (histórico real, guardado por hora) con lo previsto desde ahora en adelante, diferenciado visualmente.
- Nuevo: icono y logo propios del add-on.

## 0.2.5
- Arreglo: la tabla de plan mostraba como máximo 24 filas aunque el horizonte configurado fuera mayor (p.ej. 48h).

## 0.2.4
- Arreglo: el SOC se quedaba siempre tope en 97% aunque se configurase el máximo al 100% (error al comparar un rango de energía contra un nivel absoluto de batería).
- Nuevo: carga de emergencia en llano cuando no va a llegar a cubrir toda la punta siguiente solo con lo cargado en valle.

## 0.2.3
- Arreglo: prioridad de descarga — antes se gastaba batería en horas llano aunque hubiera punta sin cubrir más tarde ese mismo día. Ahora reserva primero lo necesario para toda la punta futura.
- Arreglo: el objetivo de SOC no respetaba el `max_soc_pct` real de las baterías al calcular la reserva.

## 0.2.2
- Arreglo importante: la previsión de consumo salía plana (mismo valor en todas las horas) porque la petición de histórico a Home Assistant pedía más días de los que el `recorder` conserva por defecto (10 días). Ahora reintenta automáticamente con ventanas más cortas.

## 0.2.1
- Simplificado el cálculo de consumo real: `consumo_instantaneo (o similar) + solar + descarga de baterías` — ya no hace falta un sensor de carga con signo, solo el de descarga/salida que la mayoría de baterías ya exponen.
- Nuevo: botón "Editar" en cada batería (antes solo se podía añadir/eliminar).

## 0.2.0
- Nuevo: cálculo de consumo real de la casa combinando red + solar + baterías, para no depender de un único sensor que se queda a 0 cuando la batería cubre el consumo.

## 0.1.2
- Nuevo: botón de guardado propio en las tarjetas de Previsión solar y Consumo (antes solo se guardaban con el botón general, poco visible).

## 0.1.1
- Arreglo crítico: interbloqueo (deadlock) en el primer arranque que impedía guardar cualquier configuración — `load_config()` llamaba a `save_config()` con un lock no reentrante.

## 0.1.0
- Primera versión: planificador de carga/descarga adaptativo (precio + sol + consumo, sin programación lineal ni parámetros ocultos), interfaz web de configuración, reparto de carga proporcional a la capacidad real de cada batería.

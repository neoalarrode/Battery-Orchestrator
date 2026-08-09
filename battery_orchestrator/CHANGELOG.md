# Changelog

## 0.11.20
Tercera pasada de optimización: el usuario seguía reportando cuelgues intermitentes tras la v0.11.19 y preguntó si se podía pedir EXPRESAMENTE solo las entidades de Climate Orchestrator en vez de filtrar sobre un volcado más genérico.
- Mejora: el descubrimiento de zonas de Climate Orchestrator (`climate_link._discover_zone_ids`, cada 5 min) ya no pide `/api/states` entero ni siquiera en ese ciclo de 5 min — ahora usa la API de plantillas de HA (`POST /api/template`, nuevo `ha_client.render_template`) con `integration_entities('climate_orchestrator')`, una función nativa de HA que consulta directamente el registro de entidades y devuelve SOLO lo que pertenece a esa integración — es HA Core quien resuelve la pertenencia, nunca se serializan ni transmiten las demás entidades de la instalación para descartarlas aquí. Más preciso además que el filtro anterior por atributo (`states.climate` + `climate_orchestrator_zone`): eso habría incluido cualquier otro termostato instalado si compartiera por casualidad el dominio "climate", esto va derecho al registro de entidades de la integración correcta. Si la plantilla fallase por lo que sea (HA muy antiguo, error puntual), sigue existiendo la red de seguridad del volcado completo + atributo, igual que antes — nunca deja de funcionar el descubrimiento, solo cambia el coste del camino normal.

## 0.11.19
Segunda pasada de optimización tras seguir reportándose cuelgues intermitentes de HA Core en una Raspberry Pi 5 (v0.11.18 no bastó por sí sola):
- Arreglo: `has_recent_history()` (usada por la corrección de previsión solar, ver v0.11.0) pedía el histórico completo del sensor solar — potencialmente decenas de miles de puntos en sensores que reportan muy a menudo — en CADA ciclo, solo para una comprobación booleana ("¿tiene ya histórico?") que en la práctica casi nunca cambia. Ahora se cachea 30 min.
- Mejora: `sensor.battery_orchestrator_status` y `sensor.battery_orchestrator_grid_signal` se publicaban en cada ciclo (cada 30-60s típico) — cada publicación escribe una fila nueva en el recorder de HA, y `grid_signal` además dispara una reevaluación reactiva en cada zona de Climate Orchestrator que lo escuche. Ninguno de los dos necesita esa frecuencia (ni el precio/tramo ni el estado cambian tan rápido). Ahora se publican como mucho cada 2 minutos.

## 0.11.18
- Arreglo importante de rendimiento: `climate_link.read_live_power_w()` (la lectura de consumo de Climate Orchestrator, ver v0.11.13) pedía `/api/states` — el volcado COMPLETO de todas las entidades de la instalación — en CADA ciclo (cada `cycle_seconds`, 30s en instalaciones típicas), no solo cuando tocaba redescubrir zonas. En una instalación con miles de entidades, eso es carga real e innecesaria sobre HA Core cada 30 segundos sin parar. Reportado por el usuario: HA Core quedándose colgado/sin red intermitentemente desde que se implantó esta integración, sin reinicios visibles — encaja exactamente con este patrón. Ahora el volcado completo solo se pide cuando la caché de descubrimiento caduca (cada 5 min); la lectura fresca de cada zona en cada ciclo se hace con `/api/states/<entity_id>` (una sola entidad, barata), nunca repitiendo el volcado entero.

## 0.11.17
- Corrección sobre la v0.11.16 (nunca llegó a instalarse): la semántica correcta, confirmada por el usuario, es que el switch de descarga debe quedar ACTIVO tanto en "bloqueada" como en "sin acción" — es el límite de potencia a 0 el que corta la salida de verdad, no el switch (en estos modelos, p.ej. EcoFlow, ese switch es una "tarea", no el interruptor físico; con el switch apagado el equipo puede seguir descargando igual, como un SAI, para sostener la carga conectada). "Cargar" queda como estaba siempre (switch de descarga a secas en OFF, sin tocar el límite) — el cambio de la 0.11.16 ahí estaba equivocado. Reportado por el usuario: batería en "sin acción" seguía descargando de verdad con el switch simplemente apagado.

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
- Arreglo: el `abs()` de v0.11.3 se aplicaba sobre la MEDIA ya calculada del sensor de descarga, no sobre cada muestra individual. En sensores bidireccionales (carga positiva/descarga negativa) esto no bastaba: si una franja horaria mezclaba muestras de carga y descarga de distintos días (p.ej. unos días todavía cargando a esa hora, otros ya descargando), esas muestras se cancelaban entre sí ANTES de aplicar el valor absoluto, y el resultado seguía hundiéndose cerca de cero pese al fix anterior. Confirmado con datos reales del usuario: la hora 08:00 daba una media de -8.9W (cancelación) cuando el sensor de descarga dedicado de la misma batería mostraba 165.4W reales en esa franja. Ahora el valor absoluto se aplica a cada muestra antes de promediar, no después.

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

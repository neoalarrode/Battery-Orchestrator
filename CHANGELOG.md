# Changelog

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

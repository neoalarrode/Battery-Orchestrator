# Changelog

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

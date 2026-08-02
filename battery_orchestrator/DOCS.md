# Battery Orchestrator

## Qué hace

Cada ciclo (configurable, por defecto cada 60s):

1. Calcula el precio de la luz de las próximas horas (tarifa fija punta/llano/valle, o PVPC dinámico vía sensor de HA).
2. Suma la previsión solar de todos los paneles/arrays que declares, corrigiendo la hora actual con la generación real medida si tienes un sensor configurado.
3. Calcula el consumo previsto de la casa a partir del histórico real (media por hora del día de los últimos N días).
4. Decide si conviene cargar (excedente solar siempre; o red en horas valle, solo lo justo para cubrir la próxima punta) o descargar (en punta/llano, cada batería se autogestiona).
5. Reparte la carga entre tus baterías proporcional a su capacidad real declarada.
6. Aplica la decisión a Home Assistant (o solo la registra, en modo simulación).

## Primeros pasos

1. Instala el add-on y ábrelo (aparece en el panel lateral gracias a Ingress).
2. **Empieza en modo simulación** (activado por defecto en "General"): verás en "Estado actual" exactamente lo que HARÍA, sin tocar nada real.
3. Da de alta cada batería: nombre, capacidad real en Wh, el sensor de su SOC (%), el switch de carga y el de descarga. Si tu batería expone entidades `number` para limitar la potencia de carga/descarga, decláralas también (opcional pero recomendado).
4. Configura la tarifa: fija (introduce tus precios punta/llano/valle y horarios) o PVPC (indica tu sensor de HA).
5. Añade tus paneles solares: por sensor de HA que ya publique previsión, o directamente por la API de Forecast.Solar (necesitas lat/lon/inclinación/azimut/kWp de tu instalación).
6. Consumo real de la casa — dos formas:
   - **Recomendada:** indica el sensor de potencia de red (neto, +importa/−exporta) en "Consumo de la casa", y opcionalmente un sensor de potencia con signo por batería (+descarga/−carga) al dar de alta cada una. La app calcula el consumo real sumando red + sol + baterías hora a hora, así que funciona bien aunque las baterías cubran buena parte del consumo (no depende de un único sensor que se quede a 0 cuando la red no interviene).
   - **Alternativa simple:** si ya tienes un sensor que sea directamente el consumo real de la casa (sin incluir la carga de las baterías), indícalo en "sensor de consumo directo". Solo se usa si no has puesto el sensor de potencia de red.
7. Si tienes potencia contratada, indícala en "Seguridad y límites" para que nunca la supere al cargar.
8. Pulsa "Ejecutar ciclo ahora" y revisa el plan de las próximas horas.
9. Cuando confíes en las decisiones, desactiva el modo simulación.

## Notas de seguridad

- Una batería con el sensor de SOC caído se omite ese ciclo entero (no se inventa un valor).
- Si una batería llega al 100% y sigue habiendo excedente solar, su límite de descarga se pone a 0W para que no se autodescargue sin necesidad.
- La potencia contratada solo limita la carga desde red (la carga con excedente solar no cuenta, no tira de la red).

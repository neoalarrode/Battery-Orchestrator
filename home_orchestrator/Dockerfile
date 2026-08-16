ARG BUILD_FROM
FROM $BUILD_FROM

# nodejs/npm: SOLO los necesita el plugin Starlink, para correr TAL CUAL
# (nunca reimplementado) el historian y el servidor de cuenta reales de
# Dishylink (https://github.com/DaveyHert/dishylink, MIT) -- ver
# app/starlink_node/PATCH.md. Mismo compromiso documentado abajo para las
# dependencias Python por plugin: se instala aqui, en la imagen base,
# aunque Starlink no este instalado -- no hay (todavia) un mecanismo de
# dependencias DE SISTEMA por plugin, solo el paquete Python por
# `pip3 install` de la linea de mas abajo.
RUN apk add --no-cache python3 py3-pip nodejs npm

WORKDIR /app
COPY app/ /app/
# flask/requests/paho-mqtt/websocket-client: dependencias del NUCLEO (core_*.py,
# ha_mqtt.py, ha_websocket.py) -- siempre hacen falta, sea cual sea el plugin.
# pycryptodome/pyyaml: solo las necesita el plugin de Tuya (protocolo LAN
# cifrado + perfiles YAML). python-kasa: solo el plugin de TP-Link -- la
# MISMA libreria que usa el componente `tplink` real de Home Assistant (no
# una reimplementacion propia del protocolo, a diferencia de Tuya). Se
# instalan todas aqui igualmente, a falta de un mecanismo de dependencias
# POR plugin (instalarlas junto con la descarga verificada del propio
# plugin, ver plugin_downloader.py) que todavia no existe. Compromiso
# deliberado, documentado: mientras no haya ese mecanismo, la imagen crece
# un poco por cada plugin nuevo con dependencias propias.
RUN pip3 install --no-cache-dir --break-system-packages flask requests paho-mqtt websocket-client pycryptodome pyyaml python-kasa

COPY run.sh /
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]

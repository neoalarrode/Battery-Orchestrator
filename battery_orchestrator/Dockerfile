ARG BUILD_FROM
FROM $BUILD_FROM

RUN apk add --no-cache python3 py3-pip

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

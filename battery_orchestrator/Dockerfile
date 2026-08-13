ARG BUILD_FROM
FROM $BUILD_FROM

RUN apk add --no-cache python3 py3-pip

WORKDIR /app
COPY app/ /app/
RUN pip3 install --no-cache-dir --break-system-packages flask requests paho-mqtt websocket-client

COPY run.sh /
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]

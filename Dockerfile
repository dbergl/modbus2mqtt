FROM python:alpine

RUN mkdir -p /app/conf/
WORKDIR /app

# upgrade pip to avoid warnings during the docker build
RUN pip install --root-user-action=ignore --upgrade pip

RUN pip install --root-user-action=ignore --no-cache-dir pyserial pymodbus==3.7.4 paho-mqtt>=2.1.0

COPY modbus2mqtt.py ./
COPY modbus2mqtt modbus2mqtt/

ENTRYPOINT [ "python", "-u", "./modbus2mqtt.py", "--config", "/app/conf/modbus2mqtt.csv" ]

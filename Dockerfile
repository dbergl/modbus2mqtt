FROM python:3.14-slim AS builder

# No compiler toolchain here on purpose: every runtime dependency (paho-mqtt,
# pymodbus, pyserial) ships a pure-Python py3-none-any wheel, so nothing is
# built from source.

COPY requirements.txt .

RUN pip install --user --no-cache-dir -r requirements.txt

FROM python:3.14-slim

RUN adduser worker && usermod -a -G dialout worker && install -o worker -g worker -d /config /logs

COPY --chown=worker:worker --from=builder /root/.local /home/worker/.local
COPY --chown=worker:worker modbus2mqtt/ /app/

VOLUME ["/config","/logs"]
ENV PATH="/home/worker/.local/bin:${PATH}"

USER worker
WORKDIR /app

CMD [ "python3", "-u", "modbus2mqtt.py" ]

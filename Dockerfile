FROM python:3.13-slim-bookworm AS builder
RUN apt-get update && apt-get install build-essential -y
RUN pip install --upgrade pip

COPY requirements.txt .

RUN pip install --user --no-cache-dir -r requirements.txt

FROM python:3.13-slim-bookworm

RUN adduser worker && usermod -a -G dialout worker && install -o worker -g worker -d /config /logs

COPY --chown=worker:worker --from=builder /root/.local /home/worker/.local
COPY --chown=worker:worker modbus2mqtt/ /app/

VOLUME ["/config","/logs"]
ENV PATH="/home/worker/.local/bin:${PATH}"

USER worker
WORKDIR /app

CMD [ "python3", "-u", "modbus2mqtt.py" ]

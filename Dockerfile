FROM python:3.14-slim AS builder

# No compiler toolchain here on purpose: every runtime dependency (paho-mqtt,
# tmodbus, serialx) ships a prebuilt wheel, so nothing is built from source.

COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /bin/uv

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
COPY modbus2mqtt/ modbus2mqtt/

RUN uv sync --locked --no-dev --no-editable

FROM python:3.14-slim

RUN adduser worker && usermod -a -G dialout worker && install -o worker -g worker -d /config /logs

COPY --from=builder /opt/venv /opt/venv
COPY --chown=worker:worker docker/modbus2mqtt.py /app/modbus2mqtt.py

VOLUME ["/config","/logs"]
ENV PATH="/opt/venv/bin:${PATH}"

USER worker
WORKDIR /app

CMD [ "python3", "-u", "modbus2mqtt.py" ]

import logging
import subprocess
import sys
from pathlib import Path

from modbus2mqtt.cli import build_parser, configure_logging


def test_parser_env_defaults(monkeypatch):
    for k, v in {"MQTT_HOST": "broker", "MQTT_PORT": "1999", "MQTT_TOPIC": "x/",
                 "MQTT_USERNAME": "u", "MQTT_PASSWORD": "p", "RTU": "/dev/ttyUSB0",
                 "RTU_BAUD": "19200", "CONFIG_FILE": "/c.csv", "LOG_LEVEL": "1"}.items():
        monkeypatch.setenv(k, v)
    a = build_parser().parse_args([])
    assert (a.mqtt_host, a.mqtt_port, a.mqtt_topic, a.mqtt_user, a.mqtt_pass) == ("broker", 1999, "x/", "u", "p")
    assert (a.rtu, a.rtu_baud, a.config, a.verbosity) == ("/dev/ttyUSB0", 19200, "/c.csv", 1)


def test_parser_plain_defaults(monkeypatch):
    for k in ("MQTT_HOST", "MQTT_PORT", "MQTT_TOPIC", "MQTT_USERNAME", "MQTT_PASSWORD", "RTU", "RTU_BAUD", "CONFIG_FILE", "LOG_LEVEL"):
        monkeypatch.delenv(k, raising=False)
    a = build_parser().parse_args([])
    assert (a.mqtt_host, a.mqtt_port, a.mqtt_topic, a.rtu_baud, a.rtu_parity) == ("localhost", None, "modbus/", 9600, "none")
    assert (a.tcp_port, a.set_modbus_timeout, a.verbosity, a.diagnostics_rate, a.set_loop_break) == (502, 1.0, 3, 0, None)


def test_module_help_runs():
    r = subprocess.run([sys.executable, "-m", "modbus2mqtt", "--help"], capture_output=True, text=True)
    assert r.returncode == 0 and "--rtu" in r.stdout and "--avoid-fc6" in r.stdout


def test_docker_shim_help_runs(tmp_path):
    shim = tmp_path / "modbus2mqtt.py"
    shim.write_text(Path("docker/modbus2mqtt.py").read_text())
    r = subprocess.run([sys.executable, "-u", "modbus2mqtt.py", "--help"], cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 0 and "--rtu" in r.stdout


def test_verbosity_mapping():
    root, tmodbus_log = logging.getLogger(), logging.getLogger("tmodbus")
    saved = root.level, tmodbus_log.level
    expected = {-1: logging.CRITICAL, 0: logging.CRITICAL, 1: logging.WARNING, 2: logging.INFO,
                3: logging.INFO, 4: logging.DEBUG, 5: logging.DEBUG}
    try:
        assert {n: configure_logging(n) for n in expected} == expected
        configure_logging(1)
        assert tmodbus_log.getEffectiveLevel() == logging.WARNING
        configure_logging(4)
        assert tmodbus_log.getEffectiveLevel() == logging.DEBUG
    finally:
        root.setLevel(saved[0])
        tmodbus_log.setLevel(saved[1])

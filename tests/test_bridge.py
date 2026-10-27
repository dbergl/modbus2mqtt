"""Bridge paths the e2e tests cannot reach: tmodbus's default reconnect blocks for up to 60 s before LinkDown."""

import asyncio
import queue
from types import SimpleNamespace

from paho.mqtt.client import MQTTMessage

from modbus2mqtt.bridge import Bridge
from modbus2mqtt.cli import build_parser
from modbus2mqtt.config import load_config
from modbus2mqtt.modbus import LinkDown
from tests.harness import BASIC_CSV, wait_for


class StubLink:
    connected = True

    def __init__(self) -> None:
        self.writes: list[tuple] = []
        self.link_down_writes = 1

    async def connect(self) -> None:
        pass

    async def read(self, unit_id, fc, address, count):
        return [0] * count

    async def write(self, unit_id, fc, address, value, *, force_multiple=False):
        self.writes.append((unit_id, fc, address, value))
        if self.link_down_writes:
            self.link_down_writes -= 1
            raise LinkDown("port gone")
        return fc


async def test_write_hitting_link_down_is_retried_not_counted(tmp_path):
    (tmp_path / "c.csv").write_text(BASIC_CSV)
    config = load_config(tmp_path / "c.csv")
    published = []
    mqtt = SimpleNamespace(prefix="modbus/", inbox=queue.SimpleQueue(), initial_connection_made=True,
                           publish=lambda topic, payload, qos=0, retain=False: published.append((topic, payload)),
                           publish_device_state=lambda device, online: published.append(("state", online)))
    msg = MQTTMessage(topic=b"modbus/dev/set/i16")
    msg.payload = b"-5"
    mqtt.inbox.put(msg)
    link = StubLink()
    bridge = Bridge(build_parser().parse_args(["--tcp", "x"]), config, link, mqtt)
    stop = asyncio.Event()
    task = asyncio.create_task(bridge.run(stop))
    try:
        await wait_for(lambda: ("modbus/dev/state/i16", -5) in published)
    finally:
        stop.set()
        await task
    assert link.writes == [(1, 6, 1, 0xFFFB)] * 2
    assert ("state", False) not in published

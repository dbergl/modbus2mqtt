"""End-to-end characterization of the bridge: real CLI, fake MQTT broker, real Modbus TCP server.

These tests pin down the bridge's external behaviour (captured from the pymodbus version) so the tmodbus rewrite
can be checked against it. They drive the bridge only through cli.run, the paho Client
class, the Modbus TCP socket and SIGTERM.
"""

import asyncio
import json
import os
import signal
from pathlib import Path

import pytest
from tmodbus.exceptions import IllegalDataAddressError

from tests.fakes.modbus_device import FakeModbusDevice
from tests.harness import BASIC_CSV, running_bridge, wait_for

GOLDEN = Path(__file__).parent / "golden" / "ha_discovery.json"
DISCOVERY_TOPIC = "homeassistant/device/modbus_dev_1/config"
TZ_CSV = BASIC_CSV.replace(
    "reference,clock,9,r,packedtime,,\n",
    'reference,clock,9,r,packedtime,,"{""timezone"":""America/Los_Angeles""}"\n',
)


@pytest.fixture
async def device():
    dev = FakeModbusDevice()
    await dev.start()
    yield dev
    await dev.stop()


def state(ref: str) -> str:
    return f"modbus/dev/state/{ref}"


def published_after(broker, mark: int, topic: str) -> list[bytes]:
    return [p.payload for p in broker.published[mark:] if p.topic == topic]


async def wait_online(broker) -> None:
    await wait_for(lambda: getattr(broker.last("modbus/dev/connected"), "payload", None) == b"online")


async def test_harness_smoke(tmp_path, monkeypatch, device):
    async with running_bridge(tmp_path, monkeypatch, BASIC_CSV, device) as broker:
        await wait_for(lambda: broker.last(state("u16")) is not None)
        assert broker.last("modbus/connected").payload == b"online"


async def test_poll_publishes_retained_state(tmp_path, monkeypatch, device):
    h = device.holding[1]
    h[0], h[1] = 7, 0xFFFF
    h[2], h[3] = 0x0000, 0x3FC0          # 1.5 as float32BE (low word first)
    h[4], h[5] = 0xFFFE, 0xFFFF          # -2 as int32BE (low word first)
    h[6], h[7] = 0x4142, 0x4344          # "ABCD"
    h[8] = 0x1234
    device.coils[1][0] = True
    device.inputs[1][0] = 42
    expected = {"u16": b"7", "i16": b"-1", "f32": b"1.5", "s32": b"-2",
                "name": b"ABCD", "hi": b"18", "relay": b"True", "inp": b"42"}
    async with running_bridge(tmp_path, monkeypatch, BASIC_CSV, device) as broker:
        await wait_for(lambda: all(broker.last(state(r)) is not None for r in expected))
        for ref, payload in expected.items():
            msg = broker.last(state(ref))
            assert (msg.payload, msg.retain, msg.qos) == (payload, True, 0), ref


async def test_publish_only_on_change(tmp_path, monkeypatch, device):
    async with running_bridge(tmp_path, monkeypatch, BASIC_CSV, device) as broker:
        await wait_for(lambda: broker.payloads(state("u16")))
        await asyncio.sleep(0.3)
        assert broker.payloads(state("u16")) == [b"0"]
        device.holding[1][0] = 5
        await wait_for(lambda: len(broker.payloads(state("u16"))) == 2)
        assert broker.payloads(state("u16")) == [b"0", b"5"]


async def test_always_publish(tmp_path, monkeypatch, device):
    async with running_bridge(tmp_path, monkeypatch, BASIC_CSV, device, "--always-publish") as broker:
        await wait_for(lambda: broker.payloads(state("u16")))
        mark = len(broker.payloads(state("u16")))
        await asyncio.sleep(0.3)
        assert len(broker.payloads(state("u16"))) - mark >= 3


async def test_write_holding_fc6(tmp_path, monkeypatch, device):
    async with running_bridge(tmp_path, monkeypatch, BASIC_CSV, device) as broker:
        await wait_for(lambda: broker.payloads(state("i16")))
        broker.inject("modbus/dev/set/i16", "-5")
        await wait_for(lambda: device.writes)
        assert device.writes[-1] == (1, 6, 1, 0xFFFB)
        await wait_for(lambda: broker.last(state("i16")).payload == b"-5")


async def test_write_avoid_fc6_uses_fc16(tmp_path, monkeypatch, device):
    async with running_bridge(tmp_path, monkeypatch, BASIC_CSV, device, "--avoid-fc6") as broker:
        await wait_for(lambda: broker.payloads(state("i16")))
        broker.inject("modbus/dev/set/i16", "-5")
        await wait_for(lambda: device.writes)
        assert device.writes[-1] == (1, 16, 1, [0xFFFB])


async def test_write_int32_uses_fc16(tmp_path, monkeypatch, device):
    async with running_bridge(tmp_path, monkeypatch, BASIC_CSV, device) as broker:
        await wait_for(lambda: broker.payloads(state("s32")))
        broker.inject("modbus/dev/set/s32", "-2")
        await wait_for(lambda: device.writes)
        assert device.writes[-1] == (1, 16, 4, [0xFFFE, 0xFFFF])


async def test_write_coil_fc5(tmp_path, monkeypatch, device):
    async with running_bridge(tmp_path, monkeypatch, BASIC_CSV, device) as broker:
        await wait_for(lambda: broker.payloads(state("relay")))
        broker.inject("modbus/dev/set/relay", "1")
        await wait_for(lambda: device.writes)
        assert device.writes[-1] == (1, 5, 0, True)


async def test_read_only_ref_not_writable(tmp_path, monkeypatch, device):
    async with running_bridge(tmp_path, monkeypatch, BASIC_CSV, device) as broker:
        await wait_for(lambda: broker.payloads(state("u16")))
        broker.inject("modbus/dev/set/u16", "3")
        await asyncio.sleep(0.3)
        assert device.writes == []


async def test_garbage_set_payloads_ignored(tmp_path, monkeypatch, device):
    async with running_bridge(tmp_path, monkeypatch, BASIC_CSV, device) as broker:
        await wait_for(lambda: broker.payloads(state("u16")))
        for payload in ("abc", "99999", "", b"\xff\xfe"):
            broker.inject("modbus/dev/set/i16", payload)
        await asyncio.sleep(0.3)
        device.holding[1][0] = 11
        await wait_for(lambda: broker.last(state("u16")).payload == b"11")
        assert device.writes == []


async def test_device_exception_goes_offline_and_recovers(tmp_path, monkeypatch, device):
    async with running_bridge(tmp_path, monkeypatch, BASIC_CSV, device) as broker:
        await wait_online(broker)
        mark = len(device.reads)
        failed_reads_when_offline = []

        def record(msg):
            if (msg.topic, msg.payload) == ("modbus/dev/connected", b"offline"):
                failed_reads_when_offline.append(len(device.reads) - mark)

        broker.on_publish.append(record)
        device.fail_with = IllegalDataAddressError
        await wait_for(lambda: broker.last("modbus/dev/connected").payload == b"offline")
        msg = broker.last("modbus/dev/connected")
        assert (msg.retain, msg.qos) == (True, 1)
        assert failed_reads_when_offline[0] >= 2
        device.fail_with = None
        await wait_online(broker)
        msg = broker.last("modbus/dev/connected")
        assert (msg.retain, msg.qos) == (True, 1)


async def test_autoremove_and_reset(tmp_path, monkeypatch):
    # Two units: if every poller were disabled the bridge would re-enable them all at once,
    # so a healthy unit 2 is needed to observe unit 1 staying removed.
    csv = BASIC_CSV + "poller,dev2,2,0,1,holding_register,0.05\nreference,v,0,r,uint16,,\n"
    device = FakeModbusDevice(unit_ids=(1, 2))
    await device.start()

    def reads(unit):
        return sum(1 for r in device.reads if r[0] == unit)

    try:
        async with running_bridge(tmp_path, monkeypatch, csv, device, "--autoremove") as broker:
            await wait_online(broker)
            device.fail_units = {1}
            device.fail_with = IllegalDataAddressError

            async def unit1_reads_stopped():
                while True:
                    before = reads(1)
                    await asyncio.sleep(0.3)
                    if reads(1) == before:
                        return

            unit2_before = reads(2)
            await asyncio.wait_for(unit1_reads_stopped(), 5)
            assert reads(2) > unit2_before
            device.fail_with = None
            mark = reads(1)
            await asyncio.sleep(0.3)
            assert reads(1) == mark, "removed pollers must stay removed until reset-autoremove"
            broker.inject("modbus/reset-autoremove", "1")
            await wait_for(lambda: reads(1) > mark + 3)
    finally:
        await device.stop()


async def test_link_loss_pauses_and_queues_writes(tmp_path, monkeypatch, device):
    async with running_bridge(tmp_path, monkeypatch, BASIC_CSV, device) as broker:
        await wait_online(broker)
        await device.stop()
        mark = len(broker.published)
        # Give the bridge time to notice the loss; a write arriving in the same instant is Task 7's test.
        await asyncio.sleep(0.3)
        broker.inject("modbus/dev/set/i16", "9")
        await asyncio.sleep(0.5)
        assert b"offline" not in published_after(broker, mark, "modbus/dev/connected")
        assert device.writes == []
        reads = len(device.reads)
        await device.start()
        await wait_for(lambda: device.writes)
        assert device.writes[-1] == (1, 6, 1, 9)
        await wait_for(lambda: len(device.reads) > reads + 3)
        assert b"offline" not in published_after(broker, mark, "modbus/dev/connected")


async def test_per_device_lwt_and_global_lwt(tmp_path, monkeypatch, device):
    async with running_bridge(tmp_path, monkeypatch, BASIC_CSV, device) as broker:
        await wait_online(broker)
        wills = [c.will for c in broker.clients]
        assert ("modbus/connected", b"offline", 2, True) in wills
        assert ("modbus/dev/connected", b"offline", 1, True) in wills
        mark = len(broker.published)
    assert published_after(broker, mark, "modbus/connected") == [b"offline"]
    assert published_after(broker, mark, "modbus/dev/connected") == [b"offline"]


async def test_diagnostics(tmp_path, monkeypatch, device):
    async with running_bridge(tmp_path, monkeypatch, BASIC_CSV, device, "--diagnostics-rate", "1") as broker:
        topics = [state("diagnostics_errors_percent"), state("diagnostics_errors_total")]
        await wait_for(lambda: all(broker.last(t) for t in topics))
        for t in topics:
            msg = broker.last(t)
            assert (msg.retain, msg.qos) == (True, 1), t
        assert broker.last(topics[0]).payload == b"0.0"
        assert broker.last(topics[1]).payload == b"0"


async def test_ha_discovery_matches_golden(tmp_path, monkeypatch, device):
    async with running_bridge(tmp_path, monkeypatch, TZ_CSV, device, "--add-to-homeassistant") as broker:
        await wait_for(lambda: broker.payloads(DISCOVERY_TOPIC))
        msg = broker.last(DISCOVERY_TOPIC)
        discovery = json.loads(msg.payload)
        del discovery["o"]["sw"]
        if os.environ.get("UPDATE_GOLDEN") == "1":
            GOLDEN.write_text(json.dumps(discovery, indent=2, sort_keys=True) + "\n")
        assert discovery == json.loads(GOLDEN.read_text())
        assert (msg.qos, msg.retain) == (1, False)
        count = len(broker.payloads(DISCOVERY_TOPIC))
        broker.inject("homeassistant/status", "online")
        await wait_for(lambda: len(broker.payloads(DISCOVERY_TOPIC)) > count)


async def test_timezone_set_via_mqtt(tmp_path, monkeypatch, device):
    h = device.holding[1]
    h[9], h[10], h[11] = (26 << 8) | 10, (27 << 8) | 12, (34 << 8) | 56   # 2026-10-27 12:34:56
    tz_topic = state("clock/timezone")
    async with running_bridge(tmp_path, monkeypatch, BASIC_CSV, device) as broker:
        await wait_for(lambda: broker.payloads(state("clock")))
        broker.inject("modbus/dev/set/clock/timezone", "Europe/Berlin")
        await wait_for(lambda: broker.payloads(tz_topic))
        msg = broker.last(tz_topic)
        assert (msg.payload, msg.qos, msg.retain) == (b"Europe/Berlin", 1, True)
        await wait_for(lambda: broker.last(state("clock")).payload == b"2026-10-27T12:34:56")
        broker.inject("modbus/dev/set/clock/timezone", "Not/AZone")
        await wait_for(lambda: len(broker.payloads(tz_topic)) == 2)
        assert broker.last(tz_topic).payload == b"Europe/Berlin"


async def test_scaled_float_publishes(tmp_path, monkeypatch, device):
    csv = BASIC_CSV.replace("reference,f32,2,r,float32BE,,", "reference,f32,2,r,float32BE,2,")
    device.holding[1][2], device.holding[1][3] = 0x0000, 0x3FC0          # 1.5 as float32BE
    async with running_bridge(tmp_path, monkeypatch, csv, device) as broker:
        await wait_for(lambda: broker.last(state("f32")) is not None)
        assert broker.last(state("f32")).payload == b"3.0"


async def test_write_publishes_state_immediately(tmp_path, monkeypatch, device):
    # Slow poll, so only the post-write publish (not the next poll) can show the new value in time.
    csv = BASIC_CSV.replace("poller,dev,1,0,12,holding_register,0.05", "poller,dev,1,0,12,holding_register,5")
    async with running_bridge(tmp_path, monkeypatch, csv, device) as broker:
        await wait_for(lambda: broker.payloads(state("i16")), timeout=10)   # first poll lands within rate*U(0,1)
        broker.inject("modbus/dev/set/i16", "-5")
        await wait_for(lambda: broker.last(state("i16")).payload == b"-5", timeout=1.0)


async def test_sigterm_during_reconnect_exits_promptly(tmp_path, monkeypatch, device):
    loop = asyncio.get_running_loop()
    async with running_bridge(tmp_path, monkeypatch, BASIC_CSV, device) as broker:
        await wait_online(broker)
        await device.stop()
        await asyncio.sleep(0.2)   # a poll is now blocked in tmodbus's reconnect backoff
        started = loop.time()
        signal.raise_signal(signal.SIGTERM)
        await wait_for(lambda: broker.last("modbus/connected").payload == b"offline", timeout=3)
    assert loop.time() - started < 3
    assert broker.last("modbus/connected").payload == b"offline"


async def test_write_racing_link_loss_not_lost(tmp_path, monkeypatch, device):
    async with running_bridge(tmp_path, monkeypatch, BASIC_CSV, device) as broker:
        await wait_online(broker)
        mark = len(broker.published)
        await device.stop()
        broker.inject("modbus/dev/set/i16", "7")
        await asyncio.sleep(0.5)
        await device.start()
        await wait_for(lambda: (1, 6, 1, 7) in device.writes)
        assert b"offline" not in published_after(broker, mark, "modbus/dev/connected")

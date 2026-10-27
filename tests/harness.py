"""Run the real bridge against a fake Modbus device and a fake MQTT broker."""

import asyncio
import queue
import signal
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import paho.mqtt.client

from modbus2mqtt import cli
from tests.fakes.modbus_device import FakeModbusDevice
from tests.fakes.mqtt import FakeBroker

BASIC_CSV = """\
type,topic,col2,col3,col4,col5,col6
poller,dev,1,0,12,holding_register,0.05
reference,u16,0,r,uint16,,
reference,i16,1,rw,int16,,
reference,f32,2,r,float32BE,,
reference,s32,4,rw,int32BE,,
reference,name,6,r,string4,,
reference,hi,8,r,hibyte,,
reference,clock,9,r,packedtime,,
poller,dev,1,0,4,coil,0.05
reference,relay,0,rw,,,
poller,dev,1,0,2,input_register,0.05
reference,inp,0,r,,,
"""


def _reset_legacy_globals() -> None:
    """The legacy bridge keeps its state in module globals; start each run clean."""
    from modbus2mqtt import modbus2mqtt as legacy

    legacy.pollers.clear()
    legacy.deviceList.clear()
    legacy.referenceList.clear()
    legacy.writeQueue = queue.SimpleQueue()
    legacy.adder = None


async def wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError(f"condition not met within {timeout} s")
        await asyncio.sleep(0.01)


async def _run_bridge(argv: list[str]) -> None:
    try:
        await cli.run(argv)
    except SystemExit as exc:
        if exc.code not in (0, None):
            raise AssertionError(f"bridge exited with {exc.code!r}") from exc


@asynccontextmanager
async def running_bridge(tmp_path, monkeypatch, csv_text: str, device: FakeModbusDevice,
                         *extra_args: str) -> AsyncIterator[FakeBroker]:
    config = tmp_path / "config.csv"
    config.write_text(csv_text)
    broker = FakeBroker()
    monkeypatch.setattr(paho.mqtt.client, "Client", broker.client_factory)
    saved_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    _reset_legacy_globals()
    argv = ["--tcp", "127.0.0.1", "--tcp-port", str(device.port), "--config", str(config),
            "--mqtt-host", "fake", "--verbosity", "4", *extra_args]
    task = asyncio.create_task(_run_bridge(argv))
    try:
        # Raising SIGTERM before the bridge has installed its handler would kill pytest.
        await wait_for(lambda: task.done() or signal.getsignal(signal.SIGTERM) is not saved_handlers[signal.SIGTERM])
        yield broker
    finally:
        try:
            if not task.done():
                signal.raise_signal(signal.SIGTERM)
            await asyncio.wait_for(task, 5)
        finally:
            for sig, handler in saved_handlers.items():
                signal.signal(sig, handler)

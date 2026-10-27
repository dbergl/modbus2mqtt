"""ModbusLink over a real tmodbus TCP client talking to FakeModbusDevice."""

import argparse
import asyncio

import pytest
from tenacity import AsyncRetrying, RetryError, stop_after_attempt, wait_fixed
from tenacity import Future as TenacityFuture
from tmodbus.exceptions import (
    CRCError,
    IllegalDataAddressError,
    ModbusConnectionError,
    RequestRetryFailedError,
    ServerDeviceBusyError,
    ServerDeviceFailureError,
)

from modbus2mqtt.modbus import DeviceError, LinkDown, classify, create_link
from tests.fakes.modbus_device import FakeModbusDevice


def tcp_args(port: int, timeout: float = 1.0) -> argparse.Namespace:
    return argparse.Namespace(rtu=None, tcp="127.0.0.1", tcp_port=port, set_modbus_timeout=timeout)


def short_reconnect() -> AsyncRetrying:
    return AsyncRetrying(stop=stop_after_attempt(2), wait=wait_fixed(0.05))


@pytest.fixture
async def device():
    dev = FakeModbusDevice(unit_ids=(1, 2))
    await dev.start()
    yield dev
    await dev.stop()


@pytest.fixture
async def link(device):
    lnk = create_link(tcp_args(device.port), reconnect=short_reconnect())
    await lnk.connect()
    yield lnk
    await lnk.close()


async def test_read_each_fc(device, link):
    device.coils[1][0], device.coils[1][2] = True, True
    device.discrete[1][1] = True
    device.holding[1][5], device.holding[1][6] = 1234, 65535
    device.inputs[1][7] = 42

    assert await link.read(1, 1, 0, 3) == [True, False, True]
    assert await link.read(1, 2, 0, 2) == [False, True]
    assert await link.read(1, 3, 5, 2) == [1234, 65535]
    assert await link.read(1, 4, 7, 1) == [42]
    assert link.connected is True


async def test_two_units_share_link(device, link):
    device.holding[1][0] = 11
    device.holding[2][0] = 22
    assert await link.read(1, 3, 0, 1) == [11]
    assert await link.read(2, 3, 0, 1) == [22]
    assert await link.read(1, 3, 0, 1) == [11]


async def test_device_exception_raises_device_error(device, link):
    device.fail_with = IllegalDataAddressError
    with pytest.raises(DeviceError):
        await link.read(1, 3, 0, 1)
    with pytest.raises(DeviceError):
        await link.write(1, 6, 0, 1)


async def test_write_paths(device, link):
    assert await link.write(1, 5, 3, True) == 5
    assert await link.write(1, 6, 4, 777) == 6
    assert await link.write(2, 6, 10, [1, 2, 3]) == 16
    assert await link.write(1, 6, 8, 9, force_multiple=True) == 16
    assert device.writes == [
        (1, 5, 3, True),
        (1, 6, 4, 777),
        (2, 16, 10, [1, 2, 3]),
        (1, 16, 8, [9]),
    ]
    assert device.coils[1][3] is True
    assert device.holding[2][12] == 3


async def test_write_retries_device_error(device, link):
    device.fail_next = 2
    assert await link.write(1, 6, 0, 5) == 6
    assert device.writes == [(1, 6, 0, 5)]

    device.fail_next = 3
    with pytest.raises(DeviceError):
        await link.write(1, 6, 1, 6)
    assert device.fail_next == 0          # all three attempts reached the device
    assert device.writes == [(1, 6, 0, 5)]


async def test_reads_not_retried(device, link):
    device.fail_next = 1
    with pytest.raises(DeviceError):
        await link.read(1, 3, 0, 1)
    assert device.reads == [(1, 3, 0, 1)]


@pytest.fixture
async def fast_link(device):
    lnk = create_link(tcp_args(device.port, timeout=0.15), reconnect=short_reconnect())
    await lnk.connect()
    yield lnk
    await lnk.close()


async def test_read_retries_timeouts(device, fast_link):
    device.holding[1][0] = 42
    device.drop_next = 3
    assert await fast_link.read(1, 3, 0, 1) == [42]
    assert len(device.reads) == 4


async def test_read_gives_up_after_four_timeouts(device, fast_link):
    device.drop_next = 4
    with pytest.raises(DeviceError) as exc_info:
        await fast_link.read(1, 3, 0, 1)
    assert isinstance(exc_info.value.__cause__, TimeoutError)
    assert len(device.reads) == 4


async def test_read_device_exception_not_retried(device, fast_link):
    device.fail_with = ServerDeviceFailureError
    with pytest.raises(DeviceError):
        await fast_link.read(1, 3, 0, 1)
    assert len(device.reads) == 1


async def test_write_timeouts_keep_write_retry_policy(device, fast_link):
    device.drop_next = 2
    assert await fast_link.write(1, 6, 0, 5) == 6   # tenacity write retry, not the read re-send
    assert len(device.writes) == 3   # two unanswered attempts, then the answered one


async def test_device_busy_not_retried_on_reads(device, link):
    device.fail_with = ServerDeviceBusyError
    with pytest.raises(DeviceError):
        await link.read(1, 4, 0, 1)
    assert len(device.reads) == 1


async def test_link_down_is_not_device_error(device, link):
    device.holding[1][0] = 3
    assert await link.read(1, 3, 0, 1) == [3]

    await device.stop()
    with pytest.raises(LinkDown) as exc_info:
        await link.read(1, 3, 0, 1)
    assert isinstance(exc_info.value.__cause__, RequestRetryFailedError)   # reconnect budget exhausted
    assert link.connected is False
    with pytest.raises(LinkDown):
        await link.write(1, 6, 0, 1)
    assert device.writes == []

    await device.start()
    assert await link.read(1, 3, 0, 1) == [3]
    assert link.connected is True


async def test_reconnect_timeout_is_link_down(device):
    """A connect timeout during tmodbus's auto-reconnect inside a request is the link, not the device."""
    lnk = create_link(tcp_args(device.port))          # tmodbus default reconnect strategy
    await lnk.connect()
    await device.stop()
    base = lnk._client.transport.base_transport

    async def open_times_out():
        raise TimeoutError("connect timed out")

    base.open = open_times_out
    try:
        with pytest.raises(LinkDown):
            await lnk.read(1, 3, 0, 1)
        assert lnk.connected is False
    finally:
        await lnk.close()


async def test_connect_failure_raises_link_down(device):
    await device.stop()
    lnk = create_link(tcp_args(device.port), reconnect=short_reconnect())
    with pytest.raises(LinkDown):
        await lnk.connect()
    assert lnk.connected is False


async def test_request_timeout_is_device_error():
    """A device that accepts the connection but never answers times out -> DeviceError."""
    async def silent(reader, writer):
        await reader.read(-1)
        writer.close()

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    lnk = create_link(tcp_args(port, timeout=0.2), reconnect=short_reconnect())
    try:
        await lnk.connect()
        with pytest.raises(DeviceError) as exc_info:
            await lnk.read(1, 3, 0, 1)
        assert isinstance(exc_info.value.__cause__, TimeoutError)
    finally:
        await lnk.close()
        server.close()
        await server.wait_closed()


def retry_failed(cause: Exception) -> RequestRetryFailedError:
    future = TenacityFuture(1)
    future.set_exception(cause)
    err = RequestRetryFailedError("gave up")
    err.__cause__ = RetryError(future)
    return err


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (IllegalDataAddressError(3), DeviceError),
        (CRCError("bad crc", response_bytes=b""), DeviceError),
        (TimeoutError(), DeviceError),
        (ModbusConnectionError("gone"), LinkDown),
        (ConnectionResetError(), LinkDown),
        (FileNotFoundError(), LinkDown),
        (retry_failed(ModbusConnectionError("gone")), LinkDown),
        (retry_failed(ConnectionRefusedError()), LinkDown),
        (retry_failed(ServerDeviceBusyError(3)), DeviceError),
        (retry_failed(TimeoutError()), DeviceError),
    ],
)
def test_classify(raised, expected):
    assert isinstance(classify(raised), expected)


def test_create_link_without_transport_exits():
    with pytest.raises(SystemExit) as exc_info:
        create_link(argparse.Namespace(rtu=None, tcp=None, set_modbus_timeout=1.0))
    assert exc_info.value.code == 1

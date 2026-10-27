"""Modbus access over tmodbus: one shared link, per-unit clients, device-error vs link-down classification."""
from __future__ import annotations

import argparse
import logging
import sys

from tenacity import AsyncRetrying, RetryError, retry_if_exception_type, retry_never, stop_after_attempt, wait_exponential
from tmodbus import AsyncModbusClient, create_async_rtu_client, create_async_tcp_client
from tmodbus.exceptions import InvalidResponseError, ModbusConnectionError, ModbusResponseError, RequestRetryFailedError

log = logging.getLogger(__name__)


class DeviceError(Exception):
    """The device answered with a Modbus exception, timed out, or sent an invalid response (a poll failure)."""


class LinkDown(Exception):
    """The transport is unavailable (connect/reconnect failed, port gone): pause, never count as a failure."""


_LINK_FAILURES = (ModbusConnectionError, OSError)
_DEVICE_FAILURES = (ModbusResponseError, InvalidResponseError, TimeoutError)
_READS = {1: "read_coils", 2: "read_discrete_inputs", 3: "read_holding_registers", 4: "read_input_registers"}


def classify(exc: Exception) -> DeviceError | LinkDown | None:
    """Map a tmodbus/transport exception to DeviceError or LinkDown; None if it is neither."""
    cause = exc
    if isinstance(exc, RequestRetryFailedError):
        retry = exc.__cause__
        last = retry.last_attempt if isinstance(retry, RetryError) else None
        cause = (last.exception() if last is not None else None) or retry
        if not isinstance(cause, _LINK_FAILURES):
            return DeviceError(str(exc))
    # TimeoutError is an OSError subclass, so test device failures first
    if isinstance(cause, _DEVICE_FAILURES):
        return DeviceError(str(cause) or type(cause).__name__)
    if isinstance(cause, _LINK_FAILURES):
        return LinkDown(str(cause) or type(cause).__name__)
    return None


class ModbusLink:
    def __init__(self, transport_client: AsyncModbusClient) -> None:
        self._client = transport_client
        self._units: dict[int, AsyncModbusClient] = {}
        self.connected = False

    async def connect(self) -> None:
        try:
            await self._client.connect()
        except (ModbusConnectionError, OSError) as e:  # includes a connect TimeoutError
            self.connected = False
            raise LinkDown(str(e) or type(e).__name__) from e
        self.connected = True

    async def close(self) -> None:
        await self._client.disconnect()

    def _transport_open(self) -> bool:
        transport = self._client.transport
        return getattr(transport, "base_transport", transport).is_open()

    def _unit(self, unit_id: int) -> AsyncModbusClient:
        if unit_id not in self._units:
            self._units[unit_id] = self._client.for_unit_id(unit_id)
        return self._units[unit_id]

    async def _call(self, unit_id: int, method: str, *args):
        try:
            result = await getattr(self._unit(unit_id), method)(*args)
        except Exception as e:
            mapped = classify(e)
            if mapped is None:
                raise
            if isinstance(e, TimeoutError) and not self._transport_open():
                # tmodbus re-raises a connect timeout from auto-reconnect as a bare TimeoutError. A response
                # timeout leaves the socket/port open (disconnect_on_timeout is off); a failed connect does not.
                mapped = LinkDown(str(e) or type(e).__name__)
            if isinstance(mapped, LinkDown):
                self.connected = False
            raise mapped from e
        self.connected = True
        return result

    async def read(self, unit_id: int, fc: int, address: int, count: int) -> list[int] | list[bool]:
        return await self._call(unit_id, _READS[fc], address, count)

    async def write(self, unit_id: int, fc: int, address: int, value: int | bool | list[int], *,
                    force_multiple: bool = False) -> int:
        if fc == 5:
            used, method, arg = 5, "write_single_coil", bool(value)
        elif fc == 6 and (force_multiple or isinstance(value, list)):
            used, method, arg = 16, "write_multiple_registers", value if isinstance(value, list) else [value]
        elif fc == 6:
            used, method, arg = 6, "write_single_register", value
        else:
            raise ValueError(f"unsupported write function code {fc}")
        retrying = AsyncRetrying(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.25, exp_base=1.5),
                                 retry=retry_if_exception_type(DeviceError), reraise=True)
        async for attempt in retrying:
            with attempt:
                await self._call(unit_id, method, address, arg)
        return used


def create_link(args: argparse.Namespace, *, reconnect: AsyncRetrying | None = None) -> ModbusLink:
    """Build the link from CLI args. `reconnect` replaces tmodbus's default reconnect strategy and also
    bounds how often a request is re-sent across reconnects (device errors are never re-sent)."""
    common = dict(unit_id=1, timeout=args.set_modbus_timeout, wait_between_requests=0.002,
                  retry_on_device_busy=False, auto_reconnect=True if reconnect is None else reconnect,
                  response_retry_strategy=None if reconnect is None else reconnect.copy(retry=retry_never))
    if args.rtu:
        from serialx import Parity, StopBits  # optional dependency, only needed for RTU

        parity = {"none": Parity.NONE, "odd": Parity.ODD, "even": Parity.EVEN}[args.rtu_parity]
        client = create_async_rtu_client(args.rtu, baudrate=int(args.rtu_baud), parity=parity,
                                         stopbits=StopBits.ONE, bytesize=8, **common)
    elif args.tcp:
        client = create_async_tcp_client(args.tcp, args.tcp_port, **common)
    else:
        log.error("You must specify a modbus access method, either --rtu or --tcp")
        sys.exit(1)
    return ModbusLink(client)

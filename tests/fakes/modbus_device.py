"""In-process Modbus TCP device built on the tmodbus server."""

import asyncio
import socket
from collections import defaultdict
from collections.abc import Iterable

from tmodbus.pdu import (
    ReadCoilsPDU,
    ReadDiscreteInputsPDU,
    ReadHoldingRegistersPDU,
    ReadInputRegistersPDU,
    WriteMultipleCoilsPDU,
    WriteMultipleRegistersPDU,
    WriteSingleCoilPDU,
    WriteSingleRegisterPDU,
)
from tmodbus.server import AsyncTcpServer, ModbusRequestRouter


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _TcpServer(AsyncTcpServer):
    """AsyncTcpServer that can drop its open client connections on stop."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._writers: set[asyncio.StreamWriter] = set()

    async def handle_client(self, reader, writer) -> None:
        self._writers.add(writer)
        try:
            await super().handle_client(reader, writer)
        finally:
            self._writers.discard(writer)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
        for writer in list(self._writers):
            writer.close()
        await super().stop()


class FakeModbusDevice:
    def __init__(self, unit_ids: Iterable[int] = (1,)) -> None:
        self.unit_ids = list(unit_ids)
        self.coils: dict[int, dict[int, bool]] = {u: defaultdict(bool) for u in self.unit_ids}
        self.discrete: dict[int, dict[int, bool]] = {u: defaultdict(bool) for u in self.unit_ids}
        self.holding: dict[int, dict[int, int]] = {u: defaultdict(int) for u in self.unit_ids}
        self.inputs: dict[int, dict[int, int]] = {u: defaultdict(int) for u in self.unit_ids}
        self.writes: list[tuple[int, int, int, object]] = []
        # (unit, function_code, address, quantity) of every read request, answered or failed
        self.reads: list[tuple[int, int, int, int]] = []
        self.fail_with: type[Exception] | None = None
        self.fail_units: set[int] | None = None   # units fail_with applies to; None means all
        self.port = _free_port()
        self._server: _TcpServer | None = None
        self._router = self._build_router()

    def _check(self, unit: int, request) -> None:
        if self.fail_with is not None and (self.fail_units is None or unit in self.fail_units):
            raise self.fail_with(request.function_code)

    def _read(self, table: dict, unit: int, request):
        self.reads.append((unit, int(request.function_code), request.start_address, request.quantity))
        self._check(unit, request)
        values = table[unit]
        return [values[a] for a in range(request.start_address, request.start_address + request.quantity)]

    def _build_router(self) -> ModbusRequestRouter:
        router = ModbusRequestRouter()
        units = self.unit_ids

        @router.register(ReadCoilsPDU, unit_id=units)
        async def read_coils(unit, request):
            return self._read(self.coils, unit, request)

        @router.register(ReadDiscreteInputsPDU, unit_id=units)
        async def read_discrete(unit, request):
            return self._read(self.discrete, unit, request)

        @router.register(ReadHoldingRegistersPDU, unit_id=units)
        async def read_holding(unit, request):
            return self._read(self.holding, unit, request)

        @router.register(ReadInputRegistersPDU, unit_id=units)
        async def read_inputs(unit, request):
            return self._read(self.inputs, unit, request)

        @router.register(WriteSingleCoilPDU, unit_id=units)
        async def write_coil(unit, request):
            self._check(unit, request)
            self.writes.append((unit, int(request.function_code), request.address, request.value))
            self.coils[unit][request.address] = request.value
            return request.value

        @router.register(WriteSingleRegisterPDU, unit_id=units)
        async def write_register(unit, request):
            self._check(unit, request)
            self.writes.append((unit, int(request.function_code), request.address, request.value))
            self.holding[unit][request.address] = request.value
            return request.value

        @router.register(WriteMultipleCoilsPDU, unit_id=units)
        async def write_coils(unit, request):
            self._check(unit, request)
            self.writes.append((unit, int(request.function_code), request.start_address, list(request.values)))
            for i, v in enumerate(request.values):
                self.coils[unit][request.start_address + i] = v
            return len(request.values)

        @router.register(WriteMultipleRegistersPDU, unit_id=units)
        async def write_registers(unit, request):
            self._check(unit, request)
            self.writes.append((unit, int(request.function_code), request.start_address, list(request.values)))
            for i, v in enumerate(request.values):
                self.holding[unit][request.start_address + i] = v
            return len(request.values)

        return router

    async def start(self) -> None:
        if self._server is None:
            self._server = _TcpServer("127.0.0.1", self._router, port=self.port)
            await self._server.start()

    async def stop(self) -> None:
        if self._server is not None:
            server, self._server = self._server, None
            await server.stop()

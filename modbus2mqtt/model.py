"""Plain state for configured devices, pollers and references (no MQTT or Modbus calls)."""
from __future__ import annotations

from dataclasses import dataclass, field

from .datatypes import DataType


@dataclass(eq=False)
class Device:
    name: str
    unit_id: int
    writable: dict[str, Reference] = field(default_factory=dict)
    occupied_topics: set[str] = field(default_factory=set)
    error_count: int = 0
    poll_count: int = 0
    connected: bool | None = None
    next_due: float = 0.0  # next diagnostics publication (monotonic seconds)
    mqc: object = None  # per-device MQTT client (legacy bridge; moves out in Task 7)
    mqtt_started: bool = False


@dataclass(eq=False)
class Reference:
    topic: str
    address: int
    dtype: DataType
    rw: str
    scale: float | None = None
    json: dict = field(default_factory=dict)
    publish: bool = True
    device: Device | None = None
    relative: int | None = None  # offset into the poller's block, set when readable
    write_fc: int | None = None
    lastval: object = None


@dataclass(eq=False)
class Poller:
    topic: str
    rate: float
    unit_id: int
    fc: int
    address: int
    size: int
    device: Device
    references: list[Reference] = field(default_factory=list)
    disabled: bool = False
    failcounter: int = 0
    connected: bool = False
    next_due: float = 0.0

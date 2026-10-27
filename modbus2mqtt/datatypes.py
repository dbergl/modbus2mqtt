"""Table-driven conversion between MQTT payloads and Modbus register values.

Naming quirk kept from the original code: for 32-bit types "LE" means the HIGH word comes first,
"BE" swaps the words (low word first).
"""
import logging
import struct
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DataType:
    name: str
    reg_amount: int
    _parse: Callable
    _combine: Callable

    def parse(self, payload, ref):
        """MQTT payload -> register value(s); None means invalid."""
        return self._parse(payload, ref)

    def combine(self, regs, ref):
        """Register list -> published value."""
        return self._combine(list(regs), ref)


def _parse_bool(payload, ref):
    if payload in ("True", "true", "1", "TRUE"):
        return True
    if payload in ("False", "false", "0", "FALSE"):
        return False
    return None


def _int_parser(lo, hi, convert=lambda v: v):
    def parse(payload, ref):
        try:
            value = int(payload)
        except ValueError:
            return None
        return convert(value) if lo <= value <= hi else None
    return parse


def _words(packed, high_first):
    hi, lo = packed >> 16, packed & 0xFFFF
    return [hi, lo] if high_first else [lo, hi]


def _packed(words, high_first):
    hi, lo = (words[0], words[1]) if high_first else (words[1], words[0])
    return (hi << 16) | lo


def _dword_type(name, fmt, high_first):
    """32-bit type packed/unpacked with struct format `fmt` (">I", ">i" or ">f")."""
    def parse(payload, ref):
        try:
            value = float(payload) if fmt == ">f" else int(payload)
            return _words(struct.unpack(">I", struct.pack(fmt, value))[0], high_first)
        except (ValueError, OverflowError, struct.error):
            return None

    def combine(regs, ref):
        return struct.unpack(fmt, struct.pack(">I", _packed(regs, high_first)))[0]

    return DataType(name, 2, parse, combine)


def _ref_target_zone(ref):
    """Optional IANA zone from the reference's HA JSON; unknown names count as not configured."""
    name = (getattr(ref, "json", None) or {}).get("timezone")
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return None


def _parse_packed_time(payload, ref):
    # Accepts ISO 8601 "YYYY-MM-DDTHH:MM:SS[.fff][+-HH:MM|Z]" and the legacy "YYYY-MM-DD HH:MM:SS".
    # The device clock is wall-clock local; an aware input is converted to the configured zone
    # (or the host's local zone) before stripping tzinfo.
    try:
        dt = datetime.fromisoformat(payload.strip().replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        target = _ref_target_zone(ref)
        dt = (dt.astimezone(target) if target else dt.astimezone()).replace(tzinfo=None)
    return [((dt.year % 100) << 8) | dt.month, (dt.day << 8) | dt.hour, (dt.minute << 8) | dt.second]


def _combine_packed_time(regs, ref):
    try:
        dt = datetime(2000 + ((regs[0] >> 8) & 0xFF), regs[0] & 0xFF, (regs[1] >> 8) & 0xFF,
                      regs[1] & 0xFF, (regs[2] >> 8) & 0xFF, regs[2] & 0xFF)
    except ValueError:
        return None
    if _ref_target_zone(ref) is not None:
        return dt.isoformat(timespec="seconds")  # naive; HA's discovery `timezone` field interprets it
    return dt.astimezone().isoformat(timespec="seconds")  # attach the host's local offset


def _byte_type(name, shift):
    def parse(payload, ref):
        try:
            value = int(payload)
        except ValueError:
            return None
        return value << shift if 0 <= value <= 255 else None

    return DataType(name, 1, parse, lambda regs, ref: (regs[0] >> shift) & 0xFF)


def _sized_type(conf, prefix, field, default, cap, builder):
    try:
        length = int(conf[len(prefix):len(prefix) + field])
    except ValueError:
        length = default
    if length > cap:
        log.warning("Data type %s: length too long", prefix.rstrip("-"))
        length = cap
    return builder(length)


def _string_type(length):
    if length % 2:
        length -= 1
        log.warning("Data type string: length must be divisible by 2")

    def parse(payload, ref):
        if len(payload) > length:
            return None
        regs = []
        for i, ch in enumerate(payload):
            if i % 2 == 0:
                regs.append(ord(ch) << 8)
            else:
                regs[-1] += ord(ch)
        return regs

    def combine(regs, ref):
        return "".join(chr(r >> 8) + chr(r & 0xFF) for r in regs)

    return DataType("string", length // 2, parse, combine)


def _list_type(length):
    def parse(payload, ref):
        try:
            parts = payload.strip().split(" ")
            return [int(p) for p in parts] if len(parts) == length else None
        except ValueError:
            return None

    return DataType("list-uint16", length, parse, lambda regs, ref: "".join(f"{r} " for r in regs))


def _signed16(regs, ref):
    return regs[0] - 0x10000 if regs[0] & 0x8000 else regs[0]


_TYPES = {t.name: t for t in (
    DataType("uint16", 1, _int_parser(0, 65535), lambda regs, ref: regs[0]),
    DataType("int16", 1, _int_parser(-32768, 32767, lambda v: v & 0xFFFF), _signed16),
    DataType("bool", 1, _parse_bool, lambda regs, ref: bool(regs[0])),
    _byte_type("hibyte", 8),
    _byte_type("lobyte", 0),
    DataType("packedtime", 3, _parse_packed_time, _combine_packed_time),
    _dword_type("uint32LE", ">I", True), _dword_type("uint32BE", ">I", False),
    _dword_type("int32LE", ">i", True), _dword_type("int32BE", ">i", False),
    _dword_type("float32LE", ">f", True), _dword_type("float32BE", ">f", False),
)}


def parse_data_type(conf):
    if not conf:
        return _TYPES["uint16"]
    if conf.startswith("list-uint16-"):
        return _sized_type(conf, "list-uint16-", 3, 1, 50, _list_type)
    if conf.startswith("string"):
        return _sized_type(conf, "string", 3, 2, 100, _string_type)
    return _TYPES[conf]

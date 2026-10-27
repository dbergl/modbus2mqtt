from types import SimpleNamespace

import pytest

from modbus2mqtt.datatypes import parse_data_type

Ref = SimpleNamespace(json={})
LA = SimpleNamespace(json={"timezone": "America/Los_Angeles"})


@pytest.mark.parametrize("conf,amount", [
    (None, 1), ("", 1), ("uint16", 1), ("int16", 1), ("bool", 1),
    ("hibyte", 1), ("lobyte", 1),
    ("string10", 5), ("string9", 4), ("string200", 50),
    ("list-uint16-3", 3), ("list-uint16-99", 50),
    ("packedtime", 3),
    ("uint32LE", 2), ("uint32BE", 2), ("int32LE", 2), ("int32BE", 2),
    ("float32LE", 2), ("float32BE", 2),
])
def test_reg_amount(conf, amount):
    assert parse_data_type(conf).reg_amount == amount


def test_names():
    assert parse_data_type("string10").name == "string"
    assert parse_data_type("list-uint16-3").name == "list-uint16"
    assert parse_data_type(None).name == "uint16"
    assert parse_data_type("float32BE").name == "float32BE"


@pytest.mark.parametrize("conf,payload,expected", [
    ("uint16", "65535", 65535),
    ("uint16", "65536", None),
    ("uint16", "x", None),
    ("int16", "-1", 0xFFFF),
    ("int16", "32768", None),
    ("uint32LE", "65537", [1, 1]),
    ("uint32BE", "65538", [2, 1]),
    ("int32LE", "-2", [0xFFFF, 0xFFFE]),
    ("int32BE", "-2", [0xFFFE, 0xFFFF]),
    ("float32LE", "1.5", [0x3FC0, 0]),
    ("float32BE", "1.5", [0, 0x3FC0]),
    ("bool", "true", True),
    ("bool", "0", False),
    ("bool", "yes", None),
    ("string10", "AB", [0x4142]),
    ("string4", "ABCDE", None),
    ("list-uint16-3", "1 2 3", [1, 2, 3]),
    ("list-uint16-3", "1 2", None),
    ("hibyte", "2", 0x0200),
    ("lobyte", "256", None),
    ("packedtime", "2026-10-27 13:45:09", [(26 << 8) | 10, (27 << 8) | 13, (45 << 8) | 9]),
])
def test_parse(conf, payload, expected):
    result = parse_data_type(conf).parse(payload, Ref)
    assert result == expected
    if isinstance(expected, bool):
        assert result is expected


PT_REGS = [(26 << 8) | 10, (27 << 8) | 13, (45 << 8) | 9]


@pytest.mark.parametrize("conf,regs,expected", [
    ("int16", [0xFFFF], -1),
    ("uint16", [7], 7),
    ("uint32LE", [1, 1], 65537),
    ("uint32BE", [2, 1], 65538),
    ("int32LE", [0xFFFF, 0xFFFE], -2),
    ("int32BE", [0xFFFE, 0xFFFF], -2),
    ("bool", [1], True),
    ("string10", [0x4142, 0x4344], "ABCD"),
    ("list-uint16-2", [1, 2], "1 2 "),
    ("hibyte", [0x1234], 0x12),
    ("lobyte", [0x1234], 0x34),
])
def test_combine(conf, regs, expected):
    result = parse_data_type(conf).combine(regs, Ref)
    assert result == expected
    if isinstance(expected, bool):
        assert result is expected


def test_combine_float32():
    assert str(parse_data_type("float32BE").combine([0x6666, 0x3F8C], Ref)) == "1.0968749523162842"
    assert str(parse_data_type("float32LE").combine([0x3F8C, 0x6666], Ref)) == "1.0968749523162842"


def test_float_combine_returns_float():
    assert isinstance(parse_data_type("float32BE").combine([0, 0x3FC0], Ref), float)


def test_packedtime_combine_with_timezone():
    assert parse_data_type("packedtime").combine(PT_REGS, LA) == "2026-10-27T13:45:09"


def test_packedtime_combine_without_timezone_has_offset():
    out = parse_data_type("packedtime").combine(PT_REGS, Ref)
    assert out.startswith("2026-10-27T13:45:09")
    assert len(out) > 19


def test_packedtime_invalid_is_none():
    assert parse_data_type("packedtime").combine([0, 0, 0], Ref) is None


def test_packedtime_parse_aware_input_converted_to_zone():
    regs = parse_data_type("packedtime").parse("2026-10-27T20:45:09Z", LA)
    assert regs[1] & 0xFF == 13

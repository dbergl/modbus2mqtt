"""Stable data-type facade over the register conversion code."""
from dataclasses import dataclass
from types import SimpleNamespace

from .dataTypes import DataTypes


@dataclass(frozen=True)
class DataType:
    name: str
    reg_amount: int
    _parse: object = None
    _combine: object = None
    _string_length: int = 0

    def _stub(self, ref):
        return SimpleNamespace(
            stringLength=self._string_length,
            regAmount=self.reg_amount,
            json=getattr(ref, "json", None) or {},
        )

    def parse(self, payload, ref):
        """MQTT payload -> register value(s); None means invalid."""
        return self._parse(self._stub(ref), payload)

    def combine(self, regs, ref):
        """Register list -> published value."""
        return self._combine(self._stub(ref), list(regs))


def parse_data_type(conf):
    stub = SimpleNamespace(stringLength=0, regAmount=1, json={})
    DataTypes.parseDataType(stub, conf)
    if not conf:
        name = "uint16"
    elif conf.startswith("list-uint16-"):
        name = "list-uint16"
    elif conf.startswith("string"):
        name = "string"
    else:
        name = conf
    return DataType(
        name=name,
        reg_amount=stub.regAmount,
        _parse=stub.parse,
        _combine=stub.combine,
        _string_length=stub.stringLength,
    )

"""Load the CSV register definition file into model objects."""
from __future__ import annotations

import csv
import json
import logging
import os
import random
import time
from dataclasses import dataclass, field

from .datatypes import parse_data_type
from .model import Device, Poller, Reference

log = logging.getLogger(__name__)

# poller type -> (function code, max size, unit noun)
_POLLER_TYPES = {
    "holding_register": (3, 123, "registers"),  # RTU supports 125, but be safe for TCP
    "coil": (1, 2000, "coils"),  # some implementations don't support 2008 coils/inputs
    "input_register": (4, 123, "registers"),
    "input_status": (2, 2000, "inputs"),
}
_WRITE_FC = {3: 6, 1: 5}  # holding -> preset single register, coil -> force single coil


@dataclass
class Config:
    pollers: list[Poller] = field(default_factory=list)
    devices: list[Device] = field(default_factory=list)
    references: list[Reference] = field(default_factory=list)  # the HA/publishable list


def _make_reference(row: dict, poller: Poller) -> Reference | None:
    topic = row["topic"]
    scale = None
    if row["col5"]:
        try:
            scale = float(row["col5"])
        except ValueError as exc:
            log.warning("Scaling error for reference %s: %s", topic, exc)
    ha_json: dict = {}
    if row["col6"]:
        try:
            ha_json = json.loads(row["col6"])
        except ValueError as exc:
            log.warning("JSON error for reference %s: %s", topic, exc)
    publish = ha_json.pop("publish", True)
    dtype_name = "bool" if poller.fc in (1, 2) else row["col4"]
    try:
        dtype = parse_data_type(dtype_name)
    except (KeyError, ValueError):
        log.error("Unknown data type %r for reference %s in poller %s, ignoring it.",
                  dtype_name, topic, poller.topic)
        return None
    return Reference(topic, int(row["col2"]), dtype, row["col3"], scale, ha_json, publish)


def _add_reference(config: Config, poller: Poller, ref: Reference) -> None:
    """Check the reference against its poller; add it to the poller and/or the writable set."""
    device = poller.device
    if ref.topic in device.occupied_topics:
        log.warning('Reference topic (%s) is already occupied for poller "%s", ignoring it.',
                    ref.topic, poller.topic)
        return
    device.occupied_topics.add(ref.topic)
    if "r" not in ref.rw and "w" not in ref.rw:
        log.warning('Reference "%s" with topic %s in poller "%s" is neither read nor writable, ignoring it.',
                    ref.address, ref.topic, poller.topic)
        return
    ref.device = device
    if "r" in ref.rw:
        last = ref.address + ref.dtype.reg_amount - 1
        if poller.address <= ref.address and last < poller.address + poller.size:
            ref.relative = ref.address - poller.address
            poller.references.append(ref)
            if "w" not in ref.rw:
                config.references.append(ref)
        else:
            log.warning('Reference "%s" with topic %s is not in range (%s to %s) of poller "%s", '
                        "ignoring it for polling.", ref.address, ref.topic, poller.address,
                        poller.address + poller.size - 1, poller.topic)
    if "w" in ref.rw:
        ref.write_fc = _WRITE_FC.get(poller.fc)
        if ref.write_fc is None:
            log.warning('Reference "%s" with topic %s in poller "%s" is not writable (read-only function code %s)',
                        ref.address, ref.topic, poller.topic, poller.fc)
        else:
            device.writable[ref.topic] = ref
            config.references.append(ref)


def load_config(path: str | os.PathLike) -> Config:
    # CSV columns: type, topic, col2..col6
    #   poller:    type, topic, unit_id, address, size, function, rate
    #   reference: type, topic, address, rw, datatype, scaling, ha json
    config = Config()
    devices: dict[str, Device] = {}
    poller: Poller | None = None
    with open(path, encoding="utf-8-sig", newline="") as csvfile:
        for row in csv.DictReader(csvfile):
            if row["type"] in ("poller", "poll"):
                topic = row["topic"]
                rate = float(row["col6"])
                unit_id = int(row["col2"])
                address = int(row["col3"])
                size = int(row["col4"])
                spec = _POLLER_TYPES.get(row["col5"])
                if spec is None:
                    log.error("Unknown function code (%s), ignoring poller %s.", row["col5"], topic)
                    poller = None
                    continue
                fc, max_size, noun = spec
                if size > max_size:
                    log.warning("Too many %s (max. %s). Ignoring poller %s.", noun, max_size, topic)
                    poller = None
                    continue
                device = devices.get(topic)
                if device is None:
                    device = devices[topic] = Device(topic, unit_id)
                    config.devices.append(device)
                poller = Poller(topic, rate, unit_id, fc, address, size, device,
                                next_due=time.monotonic() + rate * random.uniform(0, 1))
                config.pollers.append(poller)
            elif row["type"] in ("reference", "ref"):
                if poller is None:
                    log.warning("No poller for reference %s.", row["topic"])
                    continue
                ref = _make_reference(row, poller)
                if ref is not None:
                    _add_reference(config, poller, ref)
    return config

import logging

import pytest

from modbus2mqtt.config import load_config
from tests.harness import BASIC_CSV

HEADER = "type,topic,col2,col3,col4,col5,col6\n"


def load(tmp_path, body, header=HEADER, **write_kwargs):
    path = tmp_path / "config.csv"
    path.write_text(header + body, **write_kwargs)
    return load_config(path)


def test_load_basic(tmp_path):
    config = load(tmp_path, BASIC_CSV.removeprefix(HEADER))
    assert len(config.pollers) == 3
    assert len(config.devices) == 1
    device = config.devices[0]
    assert device.name == "dev" and device.unit_id == 1
    assert device.writable.keys() == {"i16", "s32", "relay"}
    refs = {r.topic: r for p in config.pollers for r in p.references}
    assert refs["i16"].write_fc == 6
    assert refs["relay"].write_fc == 5
    assert refs["u16"].write_fc is None
    assert refs["f32"].dtype.name == "float32BE"
    assert refs["relay"].dtype.name == "bool"
    assert refs["inp"].dtype.name == "uint16"
    assert refs["f32"].relative == 2 and refs["f32"].device is device
    assert {r.topic for r in config.references} == set(refs)


def test_oversized_poller_skipped(tmp_path):
    config = load(tmp_path, "poller,dev,1,0,124,holding_register,1\nreference,a,0,r,uint16,,\n")
    assert config.pollers == [] and config.devices == [] and config.references == []


def test_duplicate_topic_ignored(tmp_path):
    config = load(tmp_path, "poller,dev,1,0,4,holding_register,1\n"
                            "reference,a,0,r,uint16,,\nreference,a,1,r,uint16,,\n")
    refs = config.pollers[0].references
    assert [(r.topic, r.address) for r in refs] == [("a", 0)]


def test_out_of_range_ref_not_polled(tmp_path):
    config = load(tmp_path, "poller,dev,1,0,2,holding_register,1\n"
                            "reference,a,5,r,uint16,,\nreference,b,1,r,int32BE,,\n")
    assert config.pollers[0].references == []


def test_input_register_ref_not_writable(tmp_path):
    config = load(tmp_path, "poller,dev,1,0,2,input_register,1\nreference,a,0,rw,uint16,,\n")
    ref = config.pollers[0].references[0]
    assert ref.write_fc is None
    assert config.devices[0].writable == {}


def test_publish_false_in_json(tmp_path):
    config = load(tmp_path, 'poller,dev,1,0,2,holding_register,1\n'
                            'reference,a,0,r,uint16,,"{""publish"": false, ""unit"": ""V""}"\n')
    ref = config.pollers[0].references[0]
    assert ref.publish is False
    assert "publish" not in ref.json and ref.json == {"unit": "V"}


def test_bad_scaling_ignored(tmp_path):
    config = load(tmp_path, "poller,dev,1,0,2,holding_register,1\nreference,a,0,r,uint16,abc,\n"
                            "reference,b,1,r,uint16,0.1,\n")
    a, b = config.pollers[0].references
    assert a.scale is None and b.scale == 0.1


def test_reference_without_poller_ignored(tmp_path):
    config = load(tmp_path, "reference,a,0,r,uint16,,\n"
                            "poller,dev,1,0,2,holding_register,1\nreference,b,0,r,uint16,,\n")
    assert [r.topic for r in config.references] == ["b"]


def test_reference_after_skipped_poller_ignored(tmp_path):
    config = load(tmp_path, "poller,dev,1,0,2,holding_register,1\nreference,a,0,r,uint16,,\n"
                            "poller,big,1,0,2001,coil,1\nreference,b,0,r,,,\n")
    assert [r.topic for r in config.references] == ["a"]


def test_unknown_data_type_skips_reference_and_names_it(tmp_path, caplog):
    with caplog.at_level(logging.ERROR):
        config = load(tmp_path, "poller,dev,1,0,2,holding_register,1\n"
                                "reference,bad,0,r,nonsense,,\nreference,ok,1,r,uint16,,\n")
    assert [r.topic for r in config.pollers[0].references] == ["ok"]
    assert "bad" in caplog.text and "nonsense" in caplog.text
    assert "bad" not in config.devices[0].occupied_topics


def test_load_config_bom_and_crlf(tmp_path):
    plain = load(tmp_path, BASIC_CSV.removeprefix(HEADER))
    path = tmp_path / "bom.csv"
    path.write_bytes(b"\xef\xbb\xbf" + BASIC_CSV.replace("\n", "\r\n").encode())
    bom = load_config(path)
    summary = lambda c: [(p.topic, p.fc, p.address, p.size, [(r.topic, r.write_fc) for r in p.references])
                         for p in c.pollers]
    assert summary(bom) == summary(plain)
    assert [r.topic for r in bom.references] == [r.topic for r in plain.references]


def test_example_csv_loads():
    assert len(load_config("example.csv").pollers) >= 1

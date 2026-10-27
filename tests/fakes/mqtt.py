"""In-memory stand-in for paho.mqtt.client.Client and a broker that records traffic."""

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace

from paho.mqtt.client import MQTTMessage, topic_matches_sub
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode


def _run_on_network_thread(fn, *args) -> None:
    """Run a paho callback on its own thread, as paho's network loop would, and wait for it."""
    errors: list[BaseException] = []

    def target():
        try:
            fn(*args)
        except BaseException as exc:  # re-raised on the caller's thread
            errors.append(exc)

    thread = threading.Thread(target=target, name="fake-paho-network")
    thread.start()
    thread.join()
    if errors:
        raise errors[0]


def _encode_payload(payload) -> bytes:
    """Same conversion paho 2.1 applies in publish() and will_set()."""
    if isinstance(payload, str):
        return payload.encode("utf-8")
    if isinstance(payload, (int, float)):
        return str(payload).encode("ascii")
    if payload is None:
        return b""
    if not isinstance(payload, (bytes, bytearray)):
        raise TypeError("payload must be a string, bytearray, int, float or None.")
    return bytes(payload)


@dataclass
class Published:
    client_id: str
    topic: str
    payload: bytes
    qos: int
    retain: bool


class FakeMqttClient:
    def __init__(self, broker: "FakeBroker", callback_api_version=None, client_id: str = "", *args, **kwargs) -> None:
        self.broker = broker
        self.client_id = client_id
        self.will: tuple[str, bytes, int, bool] | None = None
        self.subscriptions: list[str] = []
        self.connected = False
        self.on_connect = None
        self.on_message = None
        self.on_disconnect = None
        self.on_log = None

    def will_set(self, topic, payload=None, qos=0, retain=False, properties=None) -> None:
        self.will = (topic, _encode_payload(payload), qos, retain)

    def username_pw_set(self, username, password=None) -> None:
        pass

    def tls_set(self, *args, **kwargs) -> None:
        pass

    def tls_insecure_set(self, value) -> None:
        pass

    def connect(self, host, port=1883, keepalive=60, *args, **kwargs) -> int:
        self.connected = True
        return 0

    def loop_start(self) -> int:
        if self.on_connect is not None:
            flags = SimpleNamespace(session_present=False)
            _run_on_network_thread(self.on_connect, self, None, flags, ReasonCode(PacketTypes.CONNACK, "Success"), None)
        return 0

    def loop_stop(self) -> int:
        return 0

    def disconnect(self, *args, **kwargs) -> int:
        self.connected = False
        return 0

    def is_connected(self) -> bool:
        return self.connected

    def subscribe(self, topic, qos=0, *args, **kwargs):
        self.subscriptions.append(topic)
        return (0, 1)

    def publish(self, topic, payload=None, qos=0, retain=False, properties=None):
        msg = Published(self.client_id, topic, _encode_payload(payload), qos, retain)
        self.broker.published.append(msg)
        for observer in self.broker.on_publish:
            observer(msg)
        return SimpleNamespace(rc=0, mid=len(self.broker.published))


@dataclass
class FakeBroker:
    clients: list[FakeMqttClient] = field(default_factory=list)
    published: list[Published] = field(default_factory=list)
    on_publish: list[Callable[[Published], None]] = field(default_factory=list)   # called for every publish

    def client_factory(self, *args, **kwargs) -> FakeMqttClient:
        client = FakeMqttClient(self, *args, **kwargs)
        self.clients.append(client)
        return client

    def inject(self, topic: str, payload: str | bytes) -> None:
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        for client in self.clients:
            if client.on_message and any(topic_matches_sub(sub, topic) for sub in client.subscriptions):
                msg = MQTTMessage(topic=topic.encode("utf-8"))
                msg.payload = payload
                _run_on_network_thread(client.on_message, client, None, msg)

    def payloads(self, topic: str) -> list[bytes]:
        return [p.payload for p in self.published if p.topic == topic]

    def last(self, topic: str) -> Published | None:
        return next((p for p in reversed(self.published) if p.topic == topic), None)

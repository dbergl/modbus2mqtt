"""MQTT side of the bridge: a global client plus one client per device, each with its own LWT."""
from __future__ import annotations

import logging
import queue
import ssl
import time
from collections.abc import Callable

import paho.mqtt.client as mqtt

from .model import Device

log = logging.getLogger(__name__)

_TLS_VERSIONS = {"tlsv1.2": "PROTOCOL_TLSv1_2", "tlsv1.1": "PROTOCOL_TLSv1_1", "tlsv1": "PROTOCOL_TLSv1"}


class MqttSide:
    """Paho callbacks run on paho's network threads; incoming messages reach the bridge only via `inbox`."""

    def __init__(self, args, prefix: str, devices: list[Device]) -> None:
        self.args = args
        self.prefix = prefix
        self.port = args.mqtt_port if args.mqtt_port is not None else (8883 if args.mqtt_use_tls else 1883)
        self.inbox: queue.SimpleQueue[mqtt.MQTTMessage] = queue.SimpleQueue()
        self.initial_connection_made = False
        self.on_connected: Callable[[], None] | None = None
        base = prefix.rstrip("/").replace("/", "_")
        stamp = int(time.time())
        self.client = self._new_client(f"{base}_{stamp}", prefix + "connected", qos=2)
        self.client.on_connect = self._on_connect
        self.client.on_message = lambda client, userdata, msg: self.inbox.put(msg)
        self.client.on_disconnect = lambda client, userdata, flags, rc, props=None: log.info("MQTT disconnected: %s", rc)
        self.client.on_log = lambda client, userdata, level, buf: log.debug("MQTT: %s", buf)
        self._device_clients = {}
        for device in devices:
            client = self._new_client(f"{base}_{device.name}_{stamp}", self._state_topic(device), qos=1)
            client.on_connect = self._device_on_connect(device)
            self._device_clients[device] = client
        self._started: set[Device] = set()

    def _state_topic(self, device: Device) -> str:
        return f"{self.prefix}{device.name}/connected"

    def _new_client(self, client_id: str, will_topic: str, qos: int):
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        client.will_set(will_topic, "offline", qos=qos, retain=True)
        a = self.args
        if a.mqtt_user or a.mqtt_pass:
            client.username_pw_set(a.mqtt_user, a.mqtt_pass)
        if a.mqtt_use_tls:
            version = _TLS_VERSIONS.get(a.mqtt_tls_version)
            if a.mqtt_tls_version is not None and version is None:
                log.warning("Unknown TLS version %r - ignoring", a.mqtt_tls_version)
            client.tls_set(ca_certs=a.mqtt_cacerts, cert_reqs=ssl.CERT_NONE if a.mqtt_insecure else ssl.CERT_REQUIRED,
                           tls_version=getattr(ssl, version) if version else None)
            if a.mqtt_insecure:
                client.tls_insecure_set(True)
        return client

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if not (reason_code == "Success" and client.is_connected()):
            log.error("MQTT failed to connect: %s", reason_code)
            return
        log.info("MQTT broker connected: %s:%s", self.args.mqtt_host, self.port)
        client.subscribe(self.prefix + "+/set/#")
        client.subscribe(self.prefix + "reset-autoremove")
        client.publish(self.prefix + "connected", "online", qos=1, retain=True)
        # Device clients start on the first successful connect only; paho reconnects them afterwards.
        for device, device_client in self._device_clients.items():
            if device not in self._started:
                try:
                    device_client.connect(self.args.mqtt_host, self.port, 60)
                    device_client.loop_start()
                    self._started.add(device)
                except Exception as exc:
                    log.error("Failed to start MQTT client for device %s: %s", device.name, exc)
        self.initial_connection_made = True
        if self.on_connected:
            try:
                self.on_connected()
            except Exception:
                # Never let the hook (e.g. HA discovery) block polling or kill paho's network thread.
                log.exception("on_connected hook failed")

    def _device_on_connect(self, device: Device):
        def on_connect(client, userdata, flags, reason_code, properties) -> None:
            if reason_code == "Success":
                # Republish the current Modbus state; before the first poll device.connected is None -> offline.
                self.publish_device_state(device, bool(device.connected))
        return on_connect

    def start(self) -> None:
        log.info("Connecting to MQTT broker %s:%s", self.args.mqtt_host, self.port)
        self.client.connect(self.args.mqtt_host, self.port, 60)
        self.client.loop_start()

    def publish(self, topic: str, payload, qos: int = 0, retain: bool = False):
        return self.client.publish(topic, payload, qos=qos, retain=retain)

    def publish_device_state(self, device: Device, online: bool) -> None:
        self._device_clients[device].publish(self._state_topic(device), "online" if online else "offline",
                                             qos=1, retain=True)

    def stop(self) -> None:
        for device, client in self._device_clients.items():
            self.publish_device_state(device, False)
            client.loop_stop()
            client.disconnect()
        self.publish(self.prefix + "connected", "offline", qos=1, retain=True)
        self.client.loop_stop()
        self.client.disconnect()

"""The bridge: polls Modbus, publishes changes to MQTT, and turns MQTT `set` messages into Modbus writes."""
from __future__ import annotations

import asyncio
import logging
import queue
import time
from collections import deque
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import Config
from .homeassistant import HassConnector
from .modbus import DeviceError, LinkDown, ModbusLink
from .model import Device, Poller, Reference
from .mqtt import MqttSide

log = logging.getLogger(__name__)


class Bridge:
    def __init__(self, args, config: Config, link: ModbusLink, mqtt: MqttSide) -> None:
        self.args, self.config, self.link, self.mqtt = args, config, link, mqtt
        self.prefix = mqtt.prefix
        self.ha = HassConnector(mqtt, self.prefix) if args.add_to_homeassistant else None
        self.pending: deque = deque()  # messages put back after a link loss; served before the inbox
        self.link_lost = False
        mqtt.on_connected = self._on_mqtt_connected
        for device in config.devices:
            device.next_due = time.monotonic() + args.diagnostics_rate

    def _on_mqtt_connected(self) -> None:  # runs on a paho thread; publishing is thread-safe
        if self.ha:
            self.mqtt.client.subscribe("homeassistant/status")
            self.ha.add_all(self.config.references)

    async def run(self, stop: asyncio.Event) -> None:
        loop_break = 0.01 if self.args.set_loop_break is None else self.args.set_loop_break
        try:
            await self.link.connect()
            log.info("MODBUS connected")
        except LinkDown as exc:
            log.warning("MODBUS connect failed (%s); retrying on the next request", exc)
        try:
            # Polling waits for the first MQTT connection: paho buffers offline only after that.
            while not self.mqtt.initial_connection_made:
                try:
                    await asyncio.to_thread(self.mqtt.start)
                except Exception as exc:
                    log.warning("Error connecting to MQTT broker %s:%s (%s), trying again...",
                                self.args.mqtt_host, self.mqtt.port, exc)
                    await asyncio.sleep(1)
                    continue
                while not self.mqtt.initial_connection_made:
                    await asyncio.sleep(loop_break)
            current = 0
            while not stop.is_set():
                await asyncio.sleep(loop_break)
                poller = self.config.pollers[current]
                current = (current + 1) % len(self.config.pollers)
                try:
                    await self._step(poller)
                except LinkDown as exc:
                    if not self.link_lost:
                        log.warning("MODBUS connection lost (%s); pausing polling and writes", exc)
                        self.link_lost = True
                    await asyncio.sleep(1)
                except Exception:
                    log.exception("Error when polling or publishing, trying again...")
        finally:
            if self.ha:
                self.ha.remove_all()

    async def _step(self, poller: Poller) -> None:
        if not poller.disabled and time.monotonic() >= poller.next_due:
            await self.poll(poller)
            poller.next_due = time.monotonic() + poller.rate
        msg = self.pending.popleft() if self.pending else self._next_inbox()
        if msg is not None:
            try:
                await self.handle_message(msg)
            except LinkDown:
                self.pending.appendleft(msg)
                raise
        if self.link_lost and self.link.connected:
            log.warning("MODBUS connection restored")
            self.link_lost = False
        if self.args.diagnostics_rate > 0 and not self.link_lost:
            for device in self.config.devices:
                self._publish_diagnostics(device)
        if all(p.disabled for p in self.config.pollers):
            await asyncio.sleep(0.01)
            self._reactivate(self.config.pollers)

    def _next_inbox(self):
        try:
            return self.mqtt.inbox.get_nowait()
        except queue.Empty:
            return None

    def _reactivate(self, pollers: list[Poller]) -> None:
        for p in pollers:
            p.disabled = False
            p.failcounter = 0
            log.warning("Reactivated poller %s with ID %s and function code %s.", p.topic, p.unit_id, p.fc)

    async def poll(self, poller: Poller) -> None:
        """Read the poller's block and publish its references. LinkDown propagates and is not counted."""
        try:
            data = await self.link.read(poller.unit_id, poller.fc, poller.address, poller.size)
        except DeviceError as exc:
            log.warning("Device %s (ID %s) failed to answer FC %s at %s: %s",
                        poller.topic, poller.unit_id, poller.fc, poller.address, exc)
            self.record_result(poller, False)
            return
        log.debug("Read MODBUS FC %s ref %s qty %s ID %s: %s", poller.fc, poller.address, poller.size,
                  poller.unit_id, data)
        try:
            for ref in poller.references:
                self.check_publish(ref, data[ref.relative:ref.relative + ref.dtype.reg_amount])
        finally:
            self.record_result(poller, True)

    def record_result(self, poller: Poller, ok: bool) -> None:
        """Count a poll: online on the first success, offline on the second consecutive failure."""
        device = poller.device
        device.poll_count += 1
        if ok:
            poller.failcounter = 0
            if not poller.connected:
                poller.connected = True
                self._set_device_state(device, True)
            return
        device.error_count += 1
        if poller.failcounter == 1:
            if self.args.autoremove:  # also fail all pollers with the same unit id
                for p in self.config.pollers:
                    if p.unit_id == poller.unit_id:
                        p.failcounter = 1
                        p.disabled = True
                        log.warning("Poller %s with ID %s disabled (function code %s, start %s, size %s).",
                                    p.topic, p.unit_id, p.fc, p.address, p.size)
            poller.failcounter = 2
            poller.connected = False
            self._set_device_state(device, False)
        elif poller.failcounter < 2:
            poller.failcounter += 1

    def _set_device_state(self, device: Device, online: bool) -> None:
        if device.connected != online:
            device.connected = online
            log.warning("Device %s is %s", device.name, "online" if online else "offline")
            self.mqtt.publish_device_state(device, online)

    def _publish_diagnostics(self, device: Device) -> None:
        now = time.monotonic()
        if now <= device.next_due:
            return
        device.next_due = now + self.args.diagnostics_rate
        error = device.error_count / device.poll_count * 100 if device.poll_count else 100
        base = f"{self.prefix}{device.name}/state/diagnostics_errors_"
        self.mqtt.publish(base + "percent", str(error), qos=1, retain=True)
        self.mqtt.publish(base + "total", str(device.error_count), qos=1, retain=True)
        device.poll_count = device.error_count = 0

    def check_publish(self, ref: Reference, regs: list) -> None:
        if not ref.publish:
            return
        val = ref.dtype.combine(regs, ref)
        if ref.lastval != val or self.args.always_publish:
            ref.lastval = val
            if ref.scale:
                val = val * ref.scale
            topic = f"{self.prefix}{ref.device.name}/state/{ref.topic}"
            self.mqtt.publish(topic, val, retain=True)
            log.debug("Published %s = %s", topic, val)

    async def handle_message(self, msg) -> None:
        try:
            payload = msg.payload.decode("utf-8")
        except UnicodeDecodeError:
            log.warning("Ignoring non-UTF-8 payload on %s", msg.topic)
            return
        if msg.topic == "homeassistant/status":
            if payload == "online" and self.ha:
                log.info("Home Assistant online, republishing discovery configs")
                self.ha.add_all(self.config.references)
            return
        if msg.topic == self.prefix + "reset-autoremove":
            if not self.args.autoremove:
                log.error("Received reset-autoremove but --autoremove is not enabled. Check flags.")
            elif payload in ("True", "1"):
                log.warning("Reactivating previously disabled pollers (command from MQTT)")
                self._reactivate([p for p in self.config.pollers if p.disabled])
            return
        parts = msg.topic.split("/")
        if len(parts) < 4 or parts[2] != "set":
            return
        device = next((d for d in self.config.devices if d.name == parts[1]), None)
        if device is None:
            return
        reference = "/".join(parts[3:])
        if reference.endswith("/timezone"):
            # Companion text entity: updates the packedtime ref's in-memory zone instead of writing Modbus.
            parent = next((r for r in self.config.references
                           if r.device is device and r.topic == reference[:-len("/timezone")]), None)
            if parent is not None and parent.dtype.name == "packedtime":
                self._set_timezone(parent, payload)
            return
        ref = device.writable.get(reference)
        if ref is None:
            return
        value = ref.dtype.parse(payload, ref)
        if value is None:
            log.warning("Not writing %r to %s/%s: invalid value", payload, device.name, ref.topic)
            return
        try:
            fc = await self.link.write(device.unit_id, ref.write_fc, ref.address, value,
                                       force_multiple=self.args.avoid_fc6)
        except DeviceError as exc:
            log.error("Writing %r to %s (ID %s) at %s (%s) failed: %s",
                      value, device.name, device.unit_id, ref.address, ref.topic, exc)
            return
        log.info("Wrote %r to %s (ID %s) at %s using FC %s", value, device.name, device.unit_id, ref.address, fc)
        # The write succeeded, so the new state can be published without waiting for the next poll.
        self.check_publish(ref, value if isinstance(value, list) else [value])

    def _set_timezone(self, ref: Reference, name: str) -> None:
        """In-memory IANA zone override for a packedtime ref (lost on restart). Republishes discovery so HA
        picks up the `timezone` field, and forces the next poll to publish in the new zone."""
        name = name.strip()
        tz_topic = f"{self.prefix}{ref.device.name}/state/{ref.topic}/timezone"
        if name:
            try:
                ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError):
                log.warning("Unknown timezone %r for %s; ignoring", name, ref.topic)
                self.mqtt.publish(tz_topic, ref.json.get("timezone", ""), qos=1, retain=True)  # snap HA back
                return
            ref.json["timezone"] = name
        else:
            ref.json.pop("timezone", None)
        log.info("Updated timezone for %s/%s -> %s", ref.device.name, ref.topic, ref.json.get("timezone", "(none)"))
        ref.lastval = None
        if self.ha:
            self.ha.add_all(self.config.references)  # also republishes the companion state
        else:
            self.mqtt.publish(tz_topic, ref.json.get("timezone", ""), qos=1, retain=True)

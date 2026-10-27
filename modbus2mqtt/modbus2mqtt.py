# spicierModbus2mqtt - Modbus TCP/RTU to MQTT bridge (and vice versa)
# https://github.com/mbs38/spicierModbus2mqtt
#
# Written in 2018 by Max Brueggemann <mail@maxbrueggemann.de>
#
#
# Provided under the terms of the MIT license.

# Contains a bunch of code taken from:
# modbus2mqtt - Modbus client with MQTT publishing
# Written and (C) 2015 by Oliver Wagner <owagner@tellerulam.com>
# Provided under the terms of the MIT license.

# Main improvements over modbus2mqtt:
# - more abstraction when writing to coils/registers using mqtt. Writing is now
#   possible without having to know device id, reference, function code etc.
# - specific coils/registers can be made read only
# - multiple devices on one bus are now supported
# - polling speed has been increased sgnificantly. With modbus RTU @ 38400 baud
#   more than 80 transactions per second have been achieved.
# - switched over to pymodbus which is in active development


# Requires:
# - Eclipse Paho for Python - http://www.eclipse.org/paho/clients/python/
# - pymodbus - https://github.com/riptideio/pymodbus
#!/usr/bin/env python

import time
import socket
import paho.mqtt.client as mqtt
import serial
import io
import json
import os
import sys
import signal
import ssl
import math
import struct
import queue
import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .addToHomeAssistant import HassConnector
from .config import load_config

import asyncio

import pymodbus.client as ModbusClient
from pymodbus import (
        FramerType,
        ModbusException,
        pymodbus_apply_logging_config,
)

__version__ = "0.80"
mqtt_port = None
mqc = None
args = None
verbosity = None
addToHass = None
adder = None
loopBreak = None
globaltopic = None
config = None
control = None
client = None

pymodbus_apply_logging_config(level=logging.CRITICAL)
#pymodbus_apply_logging_config(level=logging.INFO)

writeQueue = queue.SimpleQueue()

class Control:
    def __init__(self):
        self.runLoop = True
    def stopLoop(self):
        self.runLoop = False

def signal_handler(signal, frame):
        global control
        print('Exiting ' + sys.argv[0])
        control.stopLoop()

def publish_diagnostics(device):
    if args.diagnostics_rate>0:
        if device.next_due<time.monotonic():
            device.next_due=time.monotonic()+args.diagnostics_rate
            error=0
            try:
                error=(device.error_count / device.poll_count)*100
            except Exception:
                error=0
            if device.poll_count==0:
                error=100
            if mqc.initial_connection_made == True:
                try:
                    mqc.publish(globaltopic + device.name +"/state/diagnostics_errors_percent", str(error), qos=1, retain=True)
                    mqc.publish(globaltopic + device.name +"/state/diagnostics_errors_total", str(device.error_count), qos=1, retain=True)
                except Exception:
                    pass
            device.poll_count=0
            device.error_count=0

def fail_count(poller, failed):
    device = poller.device
    device.poll_count+=1
    if not failed:
        poller.failcounter=0
        if not poller.connected:
            poller.connected = True
            if device.connected != poller.connected:
                device.connected = poller.connected
                if verbosity >=1:
                    print(f"Connected to device: {poller.topic}")
                device.mqc.publish(globaltopic + poller.topic +"/connected", "online", qos=1, retain=True)
    else:
        device.error_count+=1
        if poller.failcounter==1:
            if args.autoremove:
                poller.disabled=True
                if verbosity >=1:
                    print("Poller "+poller.topic+" with ID "+str(poller.unit_id)+" disabled (functioncode: "+str(poller.fc)+", start reference: "+str(poller.address)+", size: "+str(poller.size)+").")
                for p in config.pollers: #also fail all pollers with the same device id
                    if p.unit_id == poller.unit_id:
                        p.failcounter=1
                        p.disabled=True
                        if verbosity >=1:
                            print("Poller "+p.topic+" with ID "+str(p.unit_id)+" disabled (functioncode: "+str(p.fc)+", start reference: "+str(p.address)+", size: "+str(p.size)+").")
            poller.failcounter=2
            poller.connected = False
            if device.connected != poller.connected:
                device.connected = poller.connected
                if verbosity >=1:
                    print(f"Could not connect to device: {poller.topic}")
                device.mqc.publish(globaltopic + poller.topic +"/connected", "offline", qos=1, retain=True)
        else:
            if poller.failcounter<2:
                poller.failcounter+=1

async def poll(poller, client):
        result = None
        failed = False
        data = []
        try:
            await asyncio.sleep(0.002)
            if poller.fc == 3:
                result = await client.read_holding_registers(poller.address, count=poller.size, device_id=poller.unit_id)
                if result.isError():
                    if verbosity >=1:
                        # THIS IS NOT A PYTHON EXCEPTION, but a valid modbus message
                        print(f"Received exception from device ({result})")
                    failed = True
                else:
                    data = result.registers
            elif poller.fc == 1:
                result = await client.read_coils(poller.address, count=poller.size, device_id=poller.unit_id)
                if result.isError():
                    if verbosity >=1:
                        # THIS IS NOT A PYTHON EXCEPTION, but a valid modbus message
                        print(f"Received exception from device ({result})")
                    failed = True
                else:
                    data = result.bits
            elif poller.fc == 2:
                result = await client.read_discrete_inputs(poller.address, count=poller.size, device_id=poller.unit_id)
                if result.isError():
                    if verbosity >=1:
                        # THIS IS NOT A PYTHON EXCEPTION, but a valid modbus message
                        print(f"Received exception from device ({result})")
                    failed = True
                else:
                    data = result.bits
            elif poller.fc == 4:
                result = await client.read_input_registers(poller.address, count=poller.size, device_id=poller.unit_id)
                if result.isError():
                    if verbosity >=1:
                        # THIS IS NOT A PYTHON EXCEPTION, but a valid modbus message
                        print(f"Received exception from device ({result})")
                    failed = True
                else:
                    data = result.registers
            else:
                if verbosity >= 1:
                    print(f"Unknown function code {poller.fc} for poller {poller.topic}")
                failed = True

            if not failed:
                if verbosity>=4:
                    print("Read MODBUS, FC:"+str(poller.fc)+", ref:"+str(poller.address)+", Qty:"+str(poller.size)+", SI:"+str(poller.unit_id))
                    print("Read MODBUS, DATA:"+str(data))
                for ref in poller.references:
                    val = data[ref.relative:(ref.dtype.reg_amount+ref.relative)]
                    check_publish(ref, val)
            else:
                if verbosity>=1:
                    print("Device "+str(poller.unit_id)+" responded with error code:"+str(result).split(',', 3)[2].rstrip(')'))
        except ModbusException as exc:
            if verbosity>=2:
                print(f"ERROR: exception in pymodbus {exc}")
            failed = True
        finally:
            fail_count(poller, failed)

async def check_poll(poller, client):
    if time.monotonic() >= poller.next_due and not poller.disabled:
        await poll(poller, client)
        poller.next_due=time.monotonic()+poller.rate

def check_publish(ref, val):
    if not ref.publish:
        return
    # Only publish messages after the initial connection has been made. If it became disconnected then the offline buffer will store messages,
    # but only after the intial connection was made.
    if mqc.initial_connection_made == True:
        val = ref.dtype.combine(val, ref)
        if ref.lastval != val or args.always_publish:
            ref.lastval = val
            if ref.scale:
                val = val * ref.scale
            try:
                publish_result = mqc.publish(globaltopic+ref.device.name+"/state/"+ref.topic,val,retain=True)
                if verbosity>=4:
                    print("published MQTT topic: " + str(ref.device.name+"/state/"+ref.topic)+" value: " + str(ref.lastval)+" RC:"+str(publish_result.rc))
            except Exception:
                if verbosity>=1:
                    print("Error publishing MQTT topic: " + str(ref.device.name+"/state/"+ref.topic)+"value: " + str(ref.lastval))

def _handle_tz_set(ref, name):
    """In-memory IANA timezone override for a packedtime ref. Validates the
    name, mutates ref.json['timezone'] (read by parsePackedTime/combinePackedTime
    via _refTargetZone), republishes discovery so HA picks up the new
    `timezone` discovery field, and resyncs the companion state. Lost on
    container restart — pair with an HA automation to set the inverter
    clock when this changes."""
    name = (name or '').strip()
    if name:
        try:
            ZoneInfo(name)
        except ZoneInfoNotFoundError:
            if verbosity >= 1:
                print(f"Unknown timezone {name!r} for {ref.topic}; ignoring")
            # Snap the HA text entity back to the current value.
            tz_state_topic = f'{globaltopic}{ref.device.name}/state/{ref.topic}/timezone'
            mqc.publish(tz_state_topic, ref.json.get('timezone', ''), qos=1, retain=True)
            return
        ref.json['timezone'] = name
    else:
        ref.json.pop('timezone', None)

    if verbosity >= 1:
        print(f"Updated timezone for {ref.device.name}/{ref.topic} -> {ref.json.get('timezone', '(none)')}")

    # Force the next checkPublish to emit a fresh datetime in the new zone.
    ref.lastval = None

    # Republish device discovery (the `timezone` field flows through ref.json)
    # and the companion text state.
    if addToHass and adder:
        adder.addAll(config.references)
    else:
        # Discovery isn't enabled, but still keep the state topic in sync
        # in case something else is subscribed.
        tz_state_topic = f'{globaltopic}{ref.device.name}/state/{ref.topic}/timezone'
        mqc.publish(tz_state_topic, ref.json.get('timezone', ''), qos=1, retain=True)


async def writehandler(userdata, msg, client):
    if str(msg.topic) == "homeassistant/status":
        if str(msg.payload.decode("utf-8")) == "online" and addToHass and adder:
            if verbosity >= 1:
                print("Home Assistant online — republishing discovery configs")
            adder.addAll(config.references)
        return
    if str(msg.topic) == globaltopic+"reset-autoremove":
        if not args.autoremove and verbosity>=1:
            print("ERROR: Received autoremove-reset command but autoremove is not enabled. Check flags.")
        if args.autoremove:
            payload = str(msg.payload.decode("utf-8"))
            if payload == "True" or payload == "1":
                if verbosity>=1:
                    print("Reactivating previously disabled pollers (command from MQTT)")
                for p in config.pollers:
                    if p.disabled == True:
                        p.disabled = False
                        p.failcounter = 0
                        if verbosity>=1:
                            print("Reactivated poller "+p.topic+" with ID "+str(p.unit_id)+ " and functioncode "+str(p.fc)+".")

        return
    parts = msg.topic.split("/")
    if len(parts) < 4:
        return
    (prefix,device,function) = parts[:3]
    reference = "/".join(parts[3:])
    if function != 'set':
        return
    myDevice = None
    for iterDevice in config.devices:
        if iterDevice.name == device:
            myDevice = iterDevice
    if myDevice is None: # no such device
        return

    # Companion text entity: "<ref>/timezone" updates the in-memory IANA
    # zone override on the parent packedtime ref instead of writing modbus.
    if reference.endswith('/timezone'):
        parent_topic = reference[:-len('/timezone')]
        parent_ref = next(
            (r for r in config.references
             if r.device is myDevice and r.topic == parent_topic),
            None)
        if parent_ref is None or parent_ref.dtype.name != "packedtime":
            return
        _handle_tz_set(parent_ref, str(msg.payload.decode("utf-8")))
        return

    myRef = myDevice.writable.get(reference)
    if myRef is None: # no such reference
        return
    payload = str(msg.payload.decode("utf-8"))
    await asyncio.sleep(0.005)
    tries = 3
    delay = 0.25
    result = None
    writeType = None
    value = myRef.dtype.parse(str(payload), myRef)

    if value is not None:
        values = value if isinstance(value, list) else [value]
        for n in range(tries):
            try:
                match myRef.write_fc:
                    case 5:
                        writeType = "coils"
                        result = await client.write_coil(int(myRef.address),value,device_id=int(myRef.device.unit_id))
                    case 6:
                        if len(values)>1 or args.avoid_fc6:
                            writeType = "registers"
                            result = await client.write_registers(int(myRef.address),values,device_id=int(myRef.device.unit_id))
                        else:
                            writeType = "register"
                            result = await client.write_register(int(myRef.address),values[0],device_id=int(myRef.device.unit_id))

                if result.isError():
                    if verbosity>=1:
                        # THIS IS NOT A PYTHON EXCEPTION, but a valid modbus message
                        print(f"Writing '{value}' to device {str(myDevice.name)}, ID={str(myDevice.unit_id)} at Reference={str(myRef.address)} ({myRef.topic}) using function code {str(myRef.write_fc)} FAILED! (Devices responded with errorcode{str(result).split(',', 3)[2].rstrip(')')}. ({result})")
                        print(f"Retrying. attempt #{n+1}")
                    await asyncio.sleep(delay)
                    delay *= 1.5
                else:
                    check_publish(myRef, values) # writing was successful => we can assume, that the corresponding state can be set and published
                    if verbosity>=3:
                        print(f"Writing {writeType} values to device {str(myDevice.name)}, ID={str(myDevice.unit_id)} at Reference={str(myRef.address)} successful.")
                    return
            except ModbusException as exc:
                if verbosity >= 1:
                    print(f"Error writing to device {str(myDevice.unit_id)}: {exc}")
    else:
        if verbosity >= 1:
            print(f"Writing to device {str(myDevice.name)}, ID={str(myDevice.unit_id)} at Reference={str(myRef.address)} using function code {str(myRef.write_fc)} not possible. Given value is not valid.")

def messagehandler(mqc,userdata,msg):
    writeQueue.put((userdata, msg))

def connecthandler(mqc, userdata, flags, reason_code, properties):
    if reason_code == "Success" and mqc.is_connected():
        mqc.initial_connection_made = True
        if verbosity>=2:
            print("MQTT Broker connected succesfully: " + args.mqtt_host + ":" + str(mqtt_port))
        mqc.subscribe(globaltopic + "+/set/#")
        mqc.subscribe(globaltopic + "reset-autoremove")
        if verbosity>=2:
            print("Subscribed to MQTT topic: "+globaltopic + "+/set/#")
        mqc.publish(globaltopic + "connected", "online", qos=1, retain=True)
        # Start per-device MQTT clients (each carries its own LWT).
        # First connect only — paho's loop handles subsequent reconnections automatically.
        for device in config.devices:
            if device.mqc is not None and not device.mqtt_started:
                try:
                    device.mqc.connect(args.mqtt_host, mqtt_port, 60)
                    device.mqc.loop_start()
                    device.mqtt_started = True
                except Exception as e:
                    if verbosity >= 1:
                        print(f"Failed to start MQTT client for device {device.name}: {e}")
        if addToHass and adder:
            mqc.subscribe("homeassistant/status")
            adder.addAll(config.references)
    else:
        if verbosity>=1:
            print(f"MQTT Failed to connect: {reason_code}")

def disconnecthandler(mqc, userdata, flags, reason_code, properties=None):
    if verbosity >= 2:
        print("MQTT Disconnected, RC:"+str(reason_code))


def loghandler(mgc, userdata, level, buf):
    if verbosity >= 4:
        print("MQTT LOG:" + buf)

async def async_main(parsed_args):
    global args
    global verbosity
    global addToHass
    global loopBreak
    global globaltopic
    global control
    global config

    control = Control()
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    args=parsed_args
    verbosity=args.verbosity
    loopBreak=args.set_loop_break
    if loopBreak is None:
        loopBreak=0.01
    addToHass=args.add_to_homeassistant

    globaltopic=args.mqtt_topic

    if not globaltopic.endswith("/"):
        globaltopic+="/"

    if verbosity>=0:
        print('Starting spiciermodbus2mqtt V%s with topic prefix \"%s\"' %(__version__, globaltopic))

    config = load_config(args.config)
    for device in config.devices:
        device.next_due = time.monotonic() + args.diagnostics_rate

    #Setup MODBUS Client
    client: ModbusClient.ModbusBaseClient | None = None
    if args.rtu:
        if args.rtu_parity == "none":
                parity = "N"
        if args.rtu_parity == "odd":
                parity = "O"
        if args.rtu_parity == "even":
                parity = "E"
        client = ModbusClient.AsyncModbusSerialClient(args.rtu,framer=FramerType.RTU, stopbits = 1, bytesize = 8, parity = parity, baudrate = int(args.rtu_baud), timeout=args.set_modbus_timeout)

    elif args.tcp:
        client = ModbusClient.AsyncModbusTcpClient(args.tcp, port=args.tcp_port, framer=FramerType.SOCKET)
    else:
        print("You must specify a modbus access method, either --rtu or --tcp")
        sys.exit(1)

    #Setup MQTT Broker
    global mqtt_port
    mqtt_port = args.mqtt_port

    if mqtt_port is None:
        if args.mqtt_use_tls:
            mqtt_port = 8883
        else:
            mqtt_port = 1883

    def apply_mqtt_security(client):
        if args.mqtt_user or args.mqtt_pass:
            client.username_pw_set(args.mqtt_user, args.mqtt_pass)
        if args.mqtt_use_tls:
            if args.mqtt_tls_version == "tlsv1.2":
                tls_version = ssl.PROTOCOL_TLSv1_2
            elif args.mqtt_tls_version == "tlsv1.1":
                tls_version = ssl.PROTOCOL_TLSv1_1
            elif args.mqtt_tls_version == "tlsv1":
                tls_version = ssl.PROTOCOL_TLSv1
            elif args.mqtt_tls_version is None:
                tls_version = None
            else:
                if verbosity >= 2:
                    print("Unknown TLS version - ignoring")
                tls_version = None
            cert_reqs = ssl.CERT_NONE if args.mqtt_insecure else ssl.CERT_REQUIRED
            client.tls_set(ca_certs=args.mqtt_cacerts, certfile=None, keyfile=None, cert_reqs=cert_reqs, tls_version=tls_version)
            if args.mqtt_insecure:
                client.tls_insecure_set(True)

    clientid=globaltopic.rstrip("/").replace("/","_") + "_" + str(int(time.time()))
    global mqc
    mqc=mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=clientid)
    mqc.on_connect=connecthandler
    mqc.on_message=messagehandler
    mqc.on_disconnect=disconnecthandler
    mqc.on_log= loghandler
    mqc.will_set(globaltopic+"connected","offline",qos=2,retain=True)
    mqc.initial_connection_attempted = False
    mqc.initial_connection_made = False
    apply_mqtt_security(mqc)

    if len(config.pollers)<1:
        print("No pollers. Exitting.")
        sys.exit(0)

    global adder
    if addToHass:
        adder = HassConnector(mqc, globaltopic, verbosity>=1)

    # Create a dedicated MQTT client per device, each with its own LWT.
    # On crash the broker fires the LWT immediately; on reconnect the device
    # client's on_connect publishes "offline" again before polling resumes.
    for device in config.devices:
        dev_clientid = globaltopic.rstrip("/").replace("/","_") + "_" + device.name + "_" + str(int(time.time()))
        dev_mqc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=dev_clientid)
        dev_mqc.will_set(globaltopic + device.name + "/connected", "offline", qos=1, retain=True)
        apply_mqtt_security(dev_mqc)
        def _make_on_connect(name, dev):
            def _on_connect(c, userdata, flags, reason_code, properties):
                if reason_code == "Success":
                    # Republish current Modbus state so HA reflects reality after MQTT reconnect.
                    # On first connect device.connected is None (falsy) so we start offline;
                    # failCount() will publish online after the first successful Modbus poll.
                    state = "online" if dev.connected else "offline"
                    c.publish(globaltopic + name + "/connected", state, qos=1, retain=True)
            return _on_connect
        dev_mqc.on_connect = _make_on_connect(device.name, device)
        device.mqc = dev_mqc

    if not client.connected:
        if verbosity >= 1:
            print("Connecting to MODBUS...")
        await client.connect()
        if client.connected:
            if verbosity >= 1:
                print("MODBUS connected successfully")

    #Main Loop
    current_poller = 0
    modbuslaststatus = client.connected
    while control.runLoop:
        time.sleep(loopBreak)

        if not mqc.initial_connection_attempted:
           try:
                print("Connecting to MQTT Broker: " + args.mqtt_host + ":" + str(mqtt_port) + "...")
                mqc.connect(args.mqtt_host, mqtt_port, 60)
                mqc.initial_connection_attempted = True #Once we have connected the mqc loop will take care of reconnections.
                mqc.loop_start()
                if verbosity >= 1:
                    print("MQTT Loop started")
           except Exception:
                if verbosity>=1:
                    print("Socket Error connecting to MQTT broker: " + args.mqtt_host + ":" + str(mqtt_port) + ", check LAN/Internet connection, trying again...")

        if mqc.initial_connection_made: #Don't start polling unless the initial connection to MQTT has been made, no offline MQTT storage will be available until then.
            if client.connected:
                try:
                    if len(config.pollers) > 0:
                        await check_poll(config.pollers[current_poller], client)
                        current_poller = current_poller + 1
                        if current_poller == len(config.pollers):
                            current_poller=0
                    if not writeQueue.empty():
                        writeObj = writeQueue.get(False)
                        await writehandler(writeObj[0],writeObj[1],client)

                    for d in config.devices:
                        publish_diagnostics(d)
                    anyAct=False

                    for p in config.pollers:
                        if p.disabled is not True:
                            anyAct=True

                    if not anyAct:
                        time.sleep(0.010)
                        for p in config.pollers:
                            if p.disabled == True:
                                p.disabled = False
                                p.failcounter = 0
                                if verbosity>=1:
                                    print("Reactivated poller "+p.topic+" with ID "+str(p.unit_id)+ " and functioncode "+str(p.fc)+".")
                except Exception as e:
                    if verbosity>=1:
                        print("Error: "+str(e)+" when polling or publishing, trying again...")
            else:
                if verbosity>=1:
                    if modbuslaststatus != client.connected:
                        modbuslaststatus = client.connected
                        print(f"MODBUS connection lost. Reconnecting...")
                # Close connection before attempting to open, this prevents the failure to get exclusive lock
                client.close()
                await client.connect()
    client.close()
    for device in config.devices:
        if device.mqc:
            device.mqc.publish(globaltopic + device.name + "/connected", "offline", qos=1, retain=True)
            device.mqc.loop_stop()
            device.mqc.disconnect()
    mqc.publish(globaltopic + "connected", "offline", qos=1, retain=True)
    if addToHass and adder:
        adder.removeAll()
    mqc.loop_stop()
    mqc.disconnect()
    sys.exit(0)


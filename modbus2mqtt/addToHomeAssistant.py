import json
from dataTypes import DataTypes


class HassConnector:
    def __init__(self, mqc, globaltopic, verbosity):
        self.mqc = mqc
        self.globaltopic = globaltopic
        self.verbosity = verbosity
        self.referenceList = []

    def addAll(self, referenceList):
        self.referenceList = referenceList
        if self.verbosity:
            print("Publishing Home Assistant MQTT discovery configs")

        # Group references by device
        devices = {}
        for ref in referenceList:
            device_name = ref.device.name
            if device_name not in devices:
                devices[device_name] = {'device': ref.device, 'refs': []}
            devices[device_name]['refs'].append(ref)

        for device_name, data in devices.items():
            self._publishDevice(data['device'], data['refs'])

    def _publishDevice(self, device, refs):
        device_uid = self._makeUniqueId(device.name, device.device_id)

        origin = {
            'name': 'modbus2mqtt',
            'sw': '0.80',
            'url': 'https://github.com/dbergl/modbus2mqtt/'
        }

        dev = {
            'ids': device_uid,
            'name': device.name,
            'mf': 'modbus'
        }

        avty = [
            {
                't': f'{self.globaltopic}{device.name}/connected',
                'pl_avail': 'online',
                'pl_not_avail': 'offline'
            }
        ]

        cmps = {}
        for ref in refs:
            platform = self._detectPlatform(ref)
            component = self._buildComponent(ref, platform)
            if component:
                key = ref.topic.replace('/', '_')
                cmps[key] = component

        if not cmps:
            if self.verbosity:
                print(f"No components for device {device.name}, skipping discovery")
            return

        payload = {
            'o': origin,
            'dev': dev,
            'avty': avty,
            'cmps': cmps
        }

        topic = f'homeassistant/device/{device_uid}/config'
        if self.verbosity:
            print(f"Publishing HA discovery for device {device.name} to {topic}")
        self.mqc.publish(topic, json.dumps(payload), qos=1, retain=False)

    def _makeUniqueId(self, device_name, device_id):
        prefix = self.globaltopic.rstrip('/').replace('/', '_')
        return f'{prefix}_{device_name}_{device_id}'

    def _detectPlatform(self, ref):
        # User-specified platform takes priority
        if 'ha_platform' in ref.json:
            return ref.json['ha_platform']

        # Check the reference's actual combine function, not the poller's default
        # dataType. This correctly handles bool-typed holding registers as well as coils.
        is_bool = (ref.combine == DataTypes.combinebool)
        is_writable = 'w' in ref.rw

        if is_bool:
            return 'switch' if is_writable else 'binary_sensor'
        else:
            return 'number' if is_writable else 'sensor'

    def _buildComponent(self, ref, platform):
        if not getattr(ref, 'publish', True):
            return None
        # Start with user-provided fields; strip our custom key
        component = {k: v for k, v in ref.json.items() if k != 'ha_platform'}

        component['p'] = platform

        # Default name from topic if not user-supplied
        if 'name' not in component:
            component['name'] = ref.topic.replace('_', ' ').replace('/', ' ')

        # Auto-derive topics
        component['stat_t'] = f'{self.globaltopic}{ref.device.name}/state/{ref.topic}'
        if 'w' in ref.rw:
            component['cmd_t'] = f'{self.globaltopic}{ref.device.name}/set/{ref.topic}'

        # Platform-specific payload defaults
        if platform == 'switch':
            component.setdefault('stat_on', 'True')
            component.setdefault('stat_off', 'False')
            component.setdefault('pl_on', 'True')
            component.setdefault('pl_off', 'False')
        elif platform == 'binary_sensor':
            component.setdefault('pl_on', 'True')
            component.setdefault('pl_off', 'False')

        device_uid = self._makeUniqueId(ref.device.name, ref.device.device_id)
        component['uniq_id'] = f'{device_uid}_{ref.topic.replace("/", "_")}'

        return component

    def removeAll(self):
        """Remove all HA discovery configs by publishing empty payloads to discovery topics."""
        seen = set()
        for ref in self.referenceList:
            device_uid = self._makeUniqueId(ref.device.name, ref.device.device_id)
            if device_uid not in seen:
                seen.add(device_uid)
                topic = f'homeassistant/device/{device_uid}/config'
                if self.verbosity:
                    print(f"Removing HA discovery for device {ref.device.name}")
                self.mqc.publish(topic, '', qos=1, retain=False)

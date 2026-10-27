import argparse
import asyncio
import os


def build_parser():
    parser = argparse.ArgumentParser(description='Bridge between ModBus and MQTT')
    parser.add_argument('--mqtt-host',default=os.environ.get('MQTT_HOST', 'localhost'),
                        help='MQTT server address. Defaults to "localhost"')
    parser.add_argument('--mqtt-port', default=os.environ.get('MQTT_PORT', None), type=int,
                        help='Defaults to 8883 for TLS or 1883 for non-TLS')
    parser.add_argument('--mqtt-topic', default=os.environ.get('MQTT_TOPIC', 'modbus/'),
                        help='Topic prefix to be used for subscribing/publishing. Defaults to "modbus/"')
    parser.add_argument('--mqtt-user', default=os.environ.get('MQTT_USERNAME', None),
                        help='Username for authentication (optional)')
    parser.add_argument('--mqtt-pass', default=os.environ.get('MQTT_PASSWORD',''),
                        help='Password for authentication (optional)')
    parser.add_argument('--mqtt-use-tls', action='store_true',
                        help='Use TLS')
    parser.add_argument('--mqtt-insecure', action='store_true',
                        help='Use TLS without providing certificates')
    parser.add_argument('--mqtt-cacerts', default=None,
                        help="Path to keychain including ")
    parser.add_argument('--mqtt-tls-version', default=None,
                        help='TLS protocol version, can be one of tlsv1.2 tlsv1.1 or tlsv1')
    parser.add_argument('--rtu', default=os.environ.get('RTU', None),
                        help='pyserial URL (or port name) for RTU serial port')
    parser.add_argument('--rtu-baud', default=os.environ.get('RTU_BAUD', '9600'), type=int,
                        help='Baud rate for serial port. Defaults to 9600')
    parser.add_argument('--rtu-parity', default='none', choices=['even','odd','none'],
                        help='Parity for serial port. Defaults to none')
    parser.add_argument('--tcp', help='Act as a Modbus TCP client, connecting to host TCP')
    parser.add_argument('--tcp-port', default='502', type=int, help='Port for MODBUS TCP. Defaults to 502')
    parser.add_argument('--set-modbus-timeout',default='1',type=float, help='Response time-out for MODBUS devices')
    parser.add_argument('--config', default=os.environ.get('CONFIG_FILE', None),
                        help='Configuration file. Required!')
    parser.add_argument('--verbosity', default=os.environ.get('LOG_LEVEL','3'), type=int,
                        help='Verbose level, 0=silent, 1=errors only, 2=connections, 3=mb writes, 4=all')
    parser.add_argument('--autoremove',action='store_true',help='Automatically remove poller if modbus communication has failed three times. Removed pollers can be reactivated by sending "True" or "1" to topic modbus/reset-autoremove')
    parser.add_argument('--add-to-homeassistant',action='store_true',help='Add devices to Home Assistant using Home Assistant\'s MQTT-Discovery')
    parser.add_argument('--always-publish',action='store_true',help='Always publish values, even if they did not change.')
    parser.add_argument('--set-loop-break',default=None,type=float, help='Set pause in main polling loop. Defaults to 10ms.')
    parser.add_argument('--diagnostics-rate',default='0',type=int, help='Time in seconds after which for each device diagnostics are published via mqtt. Set to sth. like 600 (= every 10 minutes) or so.')
    parser.add_argument('--avoid-fc6',action='store_true', help='If set, use function code 16 (write multiple registers) even when just writing a single register')
    return parser


async def run(argv=None):
    args = build_parser().parse_args(argv)
    from .modbus2mqtt import async_main
    await async_main(args)


def main(argv=None):
    asyncio.run(run(argv))

import sys
sys.path.pop(0)  # /app holds this file; without this, "import modbus2mqtt" imports it
from modbus2mqtt.cli import main
main()

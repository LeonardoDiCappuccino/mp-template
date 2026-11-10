# This file is executed on every boot (including wake-boot from deepsleep)
#import esp
import sys
if '/util' not in sys.path:
    sys.path.append('/util')  # MicroPython only searches '' and /lib by default
import ble_loader  # first: records the modules that survive between BLE runs

#esp.osdebug(None)
#import webrepl
#webrepl.start()

ble_loader.start()  # BLE loader in a background thread (upload / run scripts, see tools/ble_push.py)
"""Run mpremote and make Ctrl+C reach the board.

mpremote's `run` does not forward Ctrl+C to the device: the host process dies and the
program keeps running on the board. This wrapper waits for mpremote to exit, then sends
Ctrl+C over the serial port itself, so the script gets a real KeyboardInterrupt
(its `except KeyboardInterrupt` runs) and its output is printed.

Usage: python tools/run_usb.py <port> <mpremote args after "connect <port>">...
"""
import subprocess
import sys
import time

import serial


def main() -> int:
    port, args = sys.argv[1], sys.argv[2:]
    proc = subprocess.Popen([sys.executable, "-m", "mpremote", "connect", port, *args])
    interrupted = False
    while True:
        try:
            code = proc.wait()
            break
        except KeyboardInterrupt:
            interrupted = True  # the child got Ctrl+C too and is shutting down
    if not interrupted:
        return code

    time.sleep(0.5)
    try:
        with serial.Serial(port, 115200, timeout=0.2) as s:
            s.write(b"\x03\x03")
            end = time.time() + 1.5
            while time.time() < end:
                data = s.read(256)
                if data:
                    sys.stdout.write(data.decode("utf-8", "replace"))
                    sys.stdout.flush()
            s.write(b"\x02")  # leave raw REPL
    except (serial.SerialException, OSError) as e:
        print(f"\nCould not interrupt the board: {e}")
    return 130


if __name__ == "__main__":
    sys.exit(main())

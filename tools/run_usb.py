"""Run mpremote so that Ctrl+C reaches the program on the board.

mpremote's `run` does not forward Ctrl+C: the PC side dies and the program keeps running
on the board. This runs mpremote in this process with one change: while it waits for the
program's output, Ctrl+C sends Ctrl+C (0x03) to the board and keeps waiting. The program
gets a real KeyboardInterrupt, so its `except KeyboardInterrupt` / `finally` blocks run and
their output is shown. Press Ctrl+C again if the program catches it and keeps going.

Usage: python tools/run_usb.py <port> <mpremote args after "connect <port>">...
"""
import sys

from mpremote import main as mpremote_main
from mpremote.transport import TransportError
from mpremote.transport_serial import SerialTransport


def _follow(self, timeout, data_consumer=None):
    """Same as SerialTransport.follow, but Ctrl+C is forwarded to the board."""

    def until_eof(consumer):
        while True:
            try:
                data = self.read_until(1, b"\x04", timeout=timeout, data_consumer=consumer)
            except KeyboardInterrupt:
                self.serial.write(b"\x03")
                continue
            if not data.endswith(b"\x04"):
                raise TransportError("timeout waiting for EOF reception")
            return

    out, err = bytearray(), bytearray()
    until_eof(data_consumer or out.extend)  # program output (streamed when there is a consumer)
    until_eof(err.extend)                   # error output (traceback of an uncaught exception)
    return bytes(out[:-1]), bytes(err[:-1])


SerialTransport.follow = _follow

if __name__ == "__main__":
    port, args = sys.argv[1], sys.argv[2:]
    sys.argv = ["mpremote", "connect", port, *args]
    sys.exit(mpremote_main.main())

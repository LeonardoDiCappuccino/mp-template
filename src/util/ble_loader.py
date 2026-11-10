"""BLE loader: upload files to the ESP32 and run scripts over Bluetooth LE.

Counterpart of tools/ble_push.py. Usage in boot.py:

    import ble_loader       # import first: records the modules kept between runs
    ble_loader.start()      # advertises + handles commands in a background thread

main.py then runs as usual. ble_loader.serve() is the blocking variant.

Protocol: both directions carry frames  [len u16 BE][type u8][payload]  where
len counts type + payload. Frames may be split over / merged into several GATT
packets, so the receiver reassembles a byte stream.

Requests (host -> device, RX characteristic):
    PUT_BEGIN path | PUT_DATA bytes | PUT_END crc32(u32 BE) | RUN path | STOP
    LS path | RM path | RESET | AUTORUN path ("" clears) | PING | AUTH pin | CRC path
    EVAL source (one REPL input; output streams back, then DONE)
Replies (device -> host, TX characteristic): OK text | ERR text | OUT stdout | DONE text

Security: anyone in radio range can run code unless /ble_pin exists. If it does,
the host must send AUTH <pin> first (the pin travels unencrypted).
"""
import sys

_BASE_MODULES = set(sys.modules)  # modules to keep when purging between runs

import binascii
import bluetooth
import gc
import io
import machine
import os
import struct
import time
import _thread

SERVICE_UUID = "7d3c0001-8b2e-4f6a-9c1d-5e7a2b4f9a10"
RX_UUID = "7d3c0002-8b2e-4f6a-9c1d-5e7a2b4f9a10"  # host writes requests here
TX_UUID = "7d3c0003-8b2e-4f6a-9c1d-5e7a2b4f9a10"  # device notifies replies from here

OP_PUT_BEGIN = 0x01
OP_PUT_DATA = 0x02
OP_PUT_END = 0x03
OP_RUN = 0x04
OP_STOP = 0x05
OP_LS = 0x06
OP_RM = 0x07
OP_RESET = 0x08
OP_AUTORUN = 0x09
OP_PING = 0x0A
OP_AUTH = 0x0B
OP_CRC = 0x0C
OP_EVAL = 0x0D
_OP_DISCONNECTED = 0xFF  # internal marker queued by the IRQ handler

R_OK = 0
R_ERR = 1
R_OUT = 2
R_DONE = 3

AUTORUN_FILE = "/autorun"
PIN_FILE = "/ble_pin"
AUTORUN_DELAY_MS = 5000  # window after boot to clear a broken autorun
_RX_BUFFER = 1024
_SENDER_STACK = 6 * 1024
_OUT_QUEUE_LIMIT = 48  # queued frames; a script that prints faster than Bluetooth carries has to wait
_OUT_FRAME = 240  # max bytes of script output per frame
_STOP_GRACE_MS = 150  # between disconnecting and resetting on STOP

_IRQ_CENTRAL_CONNECT = 1
_IRQ_CENTRAL_DISCONNECT = 2
_IRQ_GATTS_WRITE = 3
_IRQ_MTU_EXCHANGED = 21


def _clean(path):
    path = path.decode() if isinstance(path, (bytes, bytearray)) else path
    if ".." in path.split("/"):
        raise ValueError("bad path")
    return "/" + path.strip("/")


def _makedirs(path):
    cur = ""
    for part in path.split("/")[1:-1]:
        cur += "/" + part
        try:
            os.mkdir(cur)
        except OSError:
            pass


def _exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _file_crc(path):
    crc = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(512)
            if not chunk:
                return crc
            crc = binascii.crc32(chunk, crc)


class _BleOut(io.IOBase):
    """Stream for os.dupterm: mirrors everything printed to the connected host."""

    def __init__(self, loader):
        self._loader = loader

    def write(self, data):
        # `data` may be a view on a buffer that is only valid during this call: copy it now.
        n = len(data)
        loader = self._loader
        try:
            if not loader._stopping:
                for i in range(0, n, _OUT_FRAME):
                    loader._send_output(data[i:i + _OUT_FRAME])
        except Exception:
            pass
        return n

    def readinto(self, _buf):
        return None  # no input; None = "nothing available" (0 would mean EOF)


class Loader:
    def __init__(self, name="MCT-ESP"):
        self._name = name
        self._conn = None
        self._mtu = 23
        self._buf = bytearray()
        self._cmds = []
        self._outq = []  # frames waiting for the sender thread (append / pop(0) are atomic)
        self._epoch = 0  # counts connections, so the sender drops data of a previous one
        self._talking = False  # quiet until the host's first request: it has subscribed by then
        self._stopping = False
        self._running = False
        self._authed = True
        self._pin = None
        self._put_f = None
        self._put_path = ""
        self._crc = 0
        self._out = _BleOut(self)
        self._repl_ns = {"__name__": "__main__"}  # globals of `mpble repl`, kept between inputs

        ble = self._ble = bluetooth.BLE()
        ble.active(True)  # the ESP32 port rejects config() (ENODEV) while inactive
        ble.config(gap_name=name, mtu=512)
        ble.irq(self._irq)
        ((self._rx, self._tx),) = ble.gatts_register_services((
            (
                bluetooth.UUID(SERVICE_UUID),
                (
                    (bluetooth.UUID(RX_UUID), bluetooth.FLAG_WRITE | bluetooth.FLAG_WRITE_NO_RESPONSE),
                    (bluetooth.UUID(TX_UUID), bluetooth.FLAG_NOTIFY),
                ),
            ),
        ))
        # append=True: writes that arrive before the IRQ handler runs are
        # concatenated instead of overwritten (hence the frame length prefix).
        ble.gatts_set_buffer(self._rx, _RX_BUFFER, True)
        if _exists(PIN_FILE):
            with open(PIN_FILE) as f:
                self._pin = f.read().strip().encode()
            self._authed = False
        self._advertise()
        old = _thread.stack_size(_SENDER_STACK)
        try:
            _thread.start_new_thread(self._pump, ())
        finally:
            _thread.stack_size(old)

    @property
    def address(self):
        """Bluetooth address as the host sees it, for `mpble --address`."""
        mac = self._ble.config("mac")[1]  # type: ignore (stub lacks the tuple type)
        return ":".join("%02X" % b for b in mac)

    # ---- BLE plumbing ------------------------------------------------------

    def _advertise(self):
        uuid = bytes(reversed(binascii.unhexlify(SERVICE_UUID.replace("-", ""))))  # little endian
        adv = b"\x02\x01\x06" + bytes((len(uuid) + 1, 0x07)) + uuid
        name = self._name.encode()
        resp = bytes((len(name) + 1, 0x09)) + name
        self._ble.gap_advertise(100000, adv_data=adv, resp_data=resp)  # type: ignore (stub lacks kwargs)

    def _irq(self, event, data):
        if event == _IRQ_CENTRAL_CONNECT:
            self._conn = data[0]
            self._mtu = 23
            self._buf = bytearray()
            self._talking = False
            self._outq.clear()
            self._epoch += 1
            self._authed = self._pin is None
            os.dupterm(self._out, 0)  # type: ignore (missing in stubs)
        elif event == _IRQ_CENTRAL_DISCONNECT:
            self._conn = None
            os.dupterm(None, 0)  # type: ignore (missing in stubs)
            self._cmds.append((_OP_DISCONNECTED, b""))
            self._advertise()
        elif event == _IRQ_MTU_EXCHANGED:
            self._mtu = data[1]
        elif event == _IRQ_GATTS_WRITE and data[1] == self._rx:
            self._feed(self._ble.gatts_read(self._rx))

    def _send(self, typ, payload=b""):
        """Queue a frame. Called from the IRQ handler, the loader thread and the running script
        (via print), so it never sends itself: only _pump does, which keeps frames in one piece."""
        if self._conn is not None and self._talking:
            self._outq.append(struct.pack(">HB", len(payload) + 1, typ) + payload)

    def _send_output(self, chunk):
        q = self._outq
        for _ in range(250):  # at most 0.5 s per print
            if len(q) < _OUT_QUEUE_LIMIT or self._conn is None or self._stopping:
                break
            time.sleep_ms(2)
        else:
            return  # the host does not keep up: drop this output instead of growing without bound
        self._send(R_OUT, chunk)

    def _reply(self, typ, text=""):
        self._send(typ, text.encode())

    def _pump(self):
        """Sender thread: packs queued frames into full notifications (the frames form a byte
        stream, the host reassembles them) so a printing script does not flood the BLE stack."""
        q = self._outq
        pending = bytearray()
        epoch = 0
        while True:
            conn = self._conn
            if conn is None or epoch != self._epoch:
                pending = bytearray()
                epoch = self._epoch
                if conn is None:
                    time.sleep_ms(20)
                    continue
            step = self._mtu - 3
            while q and len(pending) < step:
                pending.extend(q.pop(0))
            if not pending:
                time.sleep_ms(3)
                continue
            try:
                self._ble.gatts_notify(conn, self._tx, bytes(pending[:step]))  # type: ignore (stub has wrong signature)
                pending = pending[step:]
            except OSError:  # stack buffers full, retry shortly
                time.sleep_ms(5)

    # ---- request parsing (runs in the BLE IRQ context) -----------------------

    def _feed(self, data):
        if not self._talking:
            # Notifications sent before the host subscribed are lost, which would cut a frame
            # in two. Output of a script that is already running therefore starts only now.
            self._outq.clear()
            self._epoch += 1
            self._talking = True
        buf = self._buf = self._buf + data
        while len(buf) >= 2:
            n = (buf[0] << 8) | buf[1]
            if n == 0:
                buf = buf[2:]
                continue
            if len(buf) < 2 + n:
                break
            frame = bytes(buf[2:2 + n])
            buf = buf[2 + n:]
            self._dispatch(frame[0], frame[1:])
        self._buf = buf

    def _dispatch(self, op, arg):
        if op == OP_AUTH:
            if self._pin is None or arg == self._pin:
                self._authed = True
                self._reply(R_OK, "authenticated")
            else:
                self._reply(R_ERR, "wrong pin")
                if self._conn is not None:
                    self._ble.gap_disconnect(self._conn)  # type: ignore (stub types handle as memoryview)
        elif not self._authed:
            self._reply(R_ERR, "auth required")
        elif op == OP_PING:
            self._reply(R_OK, "pong")
        elif op == OP_STOP or op == OP_RESET:
            # A running script blocks the main loop, so the only way to stop it
            # is a reset. The loader (and the autorun delay) then start over.
            self._stopping = True  # mute the script: its output must not delay the reply
            self._outq.clear()
            self._reply(R_OK, "resetting")
            time.sleep_ms(100)  # the sender thread delivers the reply
            if self._conn is not None:
                # disconnect cleanly, else the host only notices the reset after a link timeout
                self._ble.gap_disconnect(self._conn)  # type: ignore (stub types handle as memoryview)
            time.sleep_ms(_STOP_GRACE_MS)
            machine.reset()
        elif self._running:
            self._reply(R_ERR, "busy: script running (use stop)")
        else:
            self._cmds.append((op, arg))

    # ---- command execution (main loop) ---------------------------------------

    def _abort_put(self):
        if self._put_f:
            self._put_f.close()
            self._put_f = None
            try:
                os.remove(self._put_path + ".part")
            except OSError:
                pass

    def _execute(self, op, arg):
        if op == _OP_DISCONNECTED:
            self._abort_put()
        elif op == OP_PUT_BEGIN:
            self._abort_put()
            path = _clean(arg)
            _makedirs(path)
            self._put_path = path
            self._put_f = open(path + ".part", "wb")
            self._crc = 0
            self._reply(R_OK)
        elif op == OP_PUT_DATA:
            if not self._put_f:
                return self._reply(R_ERR, "no upload in progress")
            self._put_f.write(arg)
            self._crc = binascii.crc32(arg, self._crc)
        elif op == OP_PUT_END:
            if not self._put_f:
                return self._reply(R_ERR, "no upload in progress")
            self._put_f.close()
            self._put_f = None
            path = self._put_path
            if struct.unpack(">I", arg)[0] != self._crc:
                os.remove(path + ".part")
                return self._reply(R_ERR, "crc mismatch")
            if _exists(path):
                os.remove(path)
            os.rename(path + ".part", path)
            self._reply(R_OK, "%08x" % self._crc)
        elif op == OP_RUN:
            self._run(_clean(arg))
        elif op == OP_LS:
            path = _clean(arg)
            lines = []
            for name, kind, _, size in os.ilistdir(path):  # type: ignore (missing in stubs)
                lines.append("%s%s\t%d" % (name, "/" if kind == 0x4000 else "", size))
            self._reply(R_OK, "\n".join(lines))
        elif op == OP_RM:
            os.remove(_clean(arg))
            self._reply(R_OK)
        elif op == OP_AUTORUN:
            if arg:
                with open(AUTORUN_FILE, "w") as f:
                    f.write(_clean(arg))
            elif _exists(AUTORUN_FILE):
                os.remove(AUTORUN_FILE)
            self._reply(R_OK)
        elif op == OP_CRC:
            self._reply(R_OK, "%08x" % _file_crc(_clean(arg)))
        elif op == OP_EVAL:
            self._eval(arg.decode())
        else:
            self._reply(R_ERR, "unknown op %d" % op)

    def _eval(self, source):
        """One REPL input: an expression prints its value, anything else is executed.
        Variables live in self._repl_ns, so later inputs see earlier ones."""
        self._running = True
        status = "ok"
        try:
            try:
                code = compile(source, "<repl>", "eval")
                is_expression = True
            except SyntaxError:
                code = compile(source, "<repl>", "exec")
                is_expression = False
            result = eval(code, self._repl_ns)  # for "exec" code this runs it and returns None
            if is_expression and result is not None:
                print(repr(result))
        except SystemExit:
            pass
        except BaseException as e:
            sys.print_exception(e)  # type: ignore (missing in stubs)
            status = "error: %s" % e
        self._running = False
        self._reply(R_DONE, status)
        gc.collect()

    def _run(self, path):
        with open(path) as f:
            code = compile(f.read(), path, "exec")
        for name in list(sys.modules):  # re-import modules changed since last run
            if name not in _BASE_MODULES:
                del sys.modules[name]
        gc.collect()
        self._running = True
        self._reply(R_OK, "running " + path)
        status = "ok"
        try:
            exec(code, {"__name__": "__main__"})
        except SystemExit:
            pass
        except BaseException as e:
            sys.print_exception(e)  # type: ignore (missing in stubs)
            status = "error: %s" % e
        self._running = False
        self._reply(R_DONE, status)
        gc.collect()

    def _autorun(self):
        if not _exists(AUTORUN_FILE):
            return
        with open(AUTORUN_FILE) as f:
            path = f.read().strip()
        try:
            self._run(path)
        except Exception as e:
            sys.print_exception(e)  # type: ignore (missing in stubs)

    def serve(self):
        start = time.ticks_ms()
        autorun_pending = True
        while True:
            if self._cmds:
                op, arg = self._cmds.pop(0)
                try:
                    self._execute(op, arg)
                except Exception as e:
                    self._reply(R_ERR, "%s: %s" % (type(e).__name__, e))
            elif autorun_pending and time.ticks_diff(time.ticks_ms(), start) >= AUTORUN_DELAY_MS:
                autorun_pending = False
                self._autorun()
            else:
                time.sleep_ms(10)


_THREAD_STACK = 24 * 1024  # uploaded scripts are compiled in the loader thread

instance = None  # the running Loader; `mpble link` checks it over USB


def start(name="MCT-ESP"):
    """Run the loader in a background thread; boot.py/main.py and the REPL keep working."""
    global instance
    loader = instance = Loader(name)
    print("BLE loader '%s' advertising, address %s" % (name, loader.address))
    old = _thread.stack_size(_THREAD_STACK)
    try:
        _thread.start_new_thread(loader.serve, ())
    finally:
        _thread.stack_size(old)
    return loader


def serve(name="MCT-ESP"):
    """Run the loader in the foreground (blocks; nothing after this call runs)."""
    global instance
    loader = instance = Loader(name)
    print("BLE loader '%s' advertising, address %s" % (name, loader.address))
    loader.serve()

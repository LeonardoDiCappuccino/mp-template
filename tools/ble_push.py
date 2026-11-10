"""Upload files to / run scripts on the ESP32 over BLE (counterpart of src/lib/ble_loader.py).

    mpble scan
    mpble sync src            # upload changed files of ./src to /  (--reset: then restart the board)
    mpble put src/lib/blink.py /lib/blink.py
    mpble exec src/test/praktikum_01.py   # upload as /_run.py, run, stream output
    mpble exec --stop FILE    # same, first ending a program still running (the board restarts)
    mpble run /main.py        # run a file already on the device
    mpble autorun /main.py    # start it on every boot (--clear to remove)
    mpble ls / | rm PATH | stop | reset | ping
    mpble unpair              # remove a Windows pairing (it breaks connections)
    mpble link / unlink       # remember the address of the board on USB / forget it
    mpble repl                # interactive Python prompt on the board

The board is found in this order: --address AA:BB:CC:DD:EE:FF, MCT_BLE_ADDRESS, the address
saved by `mpble link`, a scan for "MCT-ESP". Ctrl+C while a script runs sends STOP (the device
resets). Set --pin or MCT_BLE_PIN if the device has a /ble_pin file.
"""
import argparse
import asyncio
import binascii
import codeop
import os
import re
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

SERVICE_UUID = "7d3c0001-8b2e-4f6a-9c1d-5e7a2b4f9a10"
RX_UUID = "7d3c0002-8b2e-4f6a-9c1d-5e7a2b4f9a10"
TX_UUID = "7d3c0003-8b2e-4f6a-9c1d-5e7a2b4f9a10"

OP_PUT_BEGIN, OP_PUT_DATA, OP_PUT_END, OP_RUN, OP_STOP = 0x01, 0x02, 0x03, 0x04, 0x05
OP_LS, OP_RM, OP_RESET, OP_AUTORUN, OP_PING, OP_AUTH, OP_CRC = 0x06, 0x07, 0x08, 0x09, 0x0A, 0x0B, 0x0C
OP_EVAL = 0x0D
R_OK, R_ERR, R_OUT, R_DONE = 0, 1, 2, 3

SKIP_DIRS = {"__pycache__", ".git"}
LINE_WIDTH = 79
IS_WINDOWS = os.sep == "\\"  # not sys.platform: that is evaluated statically and greys out the other branch


def clock() -> float:
    """Monotonic seconds. Pylance resolves `time` to the MicroPython stub in typings/, which lacks it."""
    return time.monotonic()  # type: ignore (missing in the MicroPython stub)


class DeviceError(Exception):
    pass


class ConnectionLost(DeviceError):
    """The link dropped while waiting for the board (it reset, crashed or went out of range)."""


def fmt_size(n: float) -> str:
    return f"{n:.0f} B" if n < 1024 else f"{n / 1024:.1f} KB"


class Progress:
    """One upload line: a live bar on a terminal, a single summary line otherwise."""

    def __init__(self, label: str, total: int):
        self.label, self.total = label, total
        self.start = clock()
        self.last = 0.0
        self.tty = sys.stdout.isatty()

    def update(self, done: int):
        now = clock()
        if not self.tty or now - self.last < 0.1:
            return
        self.last = now
        frac = min(done / self.total, 1.0) if self.total else 1.0
        rate = done / max(now - self.start, 1e-6)
        line = (f"> {self.label}  [{'#' * int(frac * 20):<20}] {frac:4.0%}  "
                f"{fmt_size(min(done, self.total))}/{fmt_size(self.total)}  {fmt_size(rate)}/s")
        print("\r" + line.ljust(LINE_WIDTH), end="", flush=True)

    def finish(self, note: str):
        secs = clock() - self.start
        line = (f"> {self.label}  {fmt_size(self.total)}  {secs:.1f} s  "
                f"{fmt_size(self.total / max(secs, 1e-6))}/s  {note}")
        print(("\r" + line.ljust(LINE_WIDTH)) if self.tty else line, flush=True)

    def abort(self):
        if self.tty:
            print(flush=True)


class Link:
    def __init__(self, client: BleakClient):
        self.client = client
        self.buf = bytearray()
        self.replies: asyncio.Queue = asyncio.Queue()
        # bleak reports the negotiated ATT MTU; the payload is MTU - 3 bytes.
        self.chunk = max(20, min(client.mtu_size - 3, 509))

    def on_notify(self, _char, data: bytearray):
        self.buf += data
        while len(self.buf) >= 2:
            n = (self.buf[0] << 8) | self.buf[1]
            if len(self.buf) < 2 + n:
                break
            typ, payload = self.buf[2], bytes(self.buf[3:2 + n])
            del self.buf[:2 + n]
            if typ == R_OUT:
                sys.stdout.buffer.write(payload)
                sys.stdout.buffer.flush()
            else:
                self.replies.put_nowait((typ, payload))

    async def send(self, op: int, payload: bytes = b""):
        frame = struct.pack(">HB", len(payload) + 1, op) + payload
        for i in range(0, len(frame), self.chunk):
            await self.client.write_gatt_char(RX_UUID, frame[i:i + self.chunk], response=True)

    async def reply(self, timeout: float = 10) -> bytes:
        typ, payload = await asyncio.wait_for(self.replies.get(), timeout)
        if typ == R_ERR:
            raise DeviceError(payload.decode(errors="replace"))
        return payload

    async def call(self, op: int, payload: bytes = b"", timeout: float = 10) -> str:
        await self.send(op, payload)
        return (await self.reply(timeout)).decode(errors="replace")

    async def put(self, data: bytes, remote: str):
        progress = Progress(remote, len(data))
        try:
            await self.call(OP_PUT_BEGIN, remote.encode())
            step = self.chunk - 3  # frame header: len(2) + type(1) -> keeps data frames in one packet
            for i in range(0, len(data), step):
                await self.send(OP_PUT_DATA, data[i:i + step])
                progress.update(i + step)
            progress.update(len(data))
            crc = await self.call(OP_PUT_END, struct.pack(">I", binascii.crc32(data)), timeout=30)
        except BaseException:
            progress.abort()
            raise
        progress.finish(f"crc {crc} ok")

    async def next_reply(self) -> tuple[int, bytes]:
        """The next reply (output is printed by on_notify). Raises when the link drops meanwhile,
        e.g. the board crashed or went out of range, instead of waiting forever."""
        while True:
            try:
                return await asyncio.wait_for(self.replies.get(), 0.5)
            except (asyncio.TimeoutError, TimeoutError):
                if not self.client.is_connected:
                    raise ConnectionLost("connection lost (the board reset or is out of range)") from None

    async def stop(self, op: int = OP_STOP, patience: float = 4.0) -> bool:
        """Reset the board and wait until it drops the link. True when it did.

        Sent without waiting for an acknowledgement: a board that is busy printing answers late,
        and the reset ends the connection anyway. Repeated if the link stays up."""
        frame = struct.pack(">HB", 1, op)
        for _ in range(3):
            try:
                await self.client.write_gatt_char(RX_UUID, frame, response=False)
            except Exception:
                pass  # link already gone
            end = clock() + patience
            while clock() < end:
                if not self.client.is_connected:
                    return True
                await asyncio.sleep(0.1)
        return False

    async def run(self, remote: str, follow: bool = True):
        try:
            # inside the try: Ctrl+C right after the request went out must still stop the script
            await self.call(OP_RUN, remote.encode())
            if not follow:
                print(f"Started {remote}; it keeps running after disconnect.")
                return
            while True:
                typ, payload = await self.next_reply()
                if typ == R_DONE:
                    status = payload.decode(errors="replace")
                    print(f"\n[{status}]")
                    return
        except (asyncio.CancelledError, KeyboardInterrupt):
            print("\n[stopping]", flush=True)
            print("[stopped, the board restarts]" if await self.stop() else "[no answer: is the board still running?]")
        except ConnectionLost as e:
            print(f"\n[{e}]")

    async def evaluate(self, source: str) -> str:
        """Run one REPL input on the board; its output streams to stdout. Returns "ok" or "error: ..."."""
        await self.send(OP_EVAL, source.encode())
        while True:
            typ, payload = await self.next_reply()
            if typ == R_ERR:
                raise DeviceError(payload.decode(errors="replace"))
            if typ == R_DONE:
                return payload.decode(errors="replace")


def project_root() -> Path:
    """The repository: next to this tool, else the nearest parent of the working directory."""
    here = Path(__file__).resolve().parent.parent
    if (here / "pyproject.toml").exists():
        return here
    for folder in [Path.cwd(), *Path.cwd().parents]:
        if (folder / "pyproject.toml").exists() or (folder / ".git").exists():
            return folder
    return Path.cwd()


def link_file() -> Path:
    """Where `mpble link` remembers the board's address: tools/.mpble-address (git-ignored)."""
    return Path(__file__).resolve().parent / ".mpble-address"


def linked_address() -> str | None:
    try:
        return link_file().read_text().strip() or None
    except OSError:
        return None


def find_serial_port() -> str:
    """The one USB serial adapter that is plugged in (ESP32 boards show up as CP210x/CH340/...)."""
    from serial.tools import list_ports

    ports = [p for p in list_ports.comports() if p.vid is not None]
    if len(ports) == 1:
        return ports[0].device
    if not ports:
        sys.exit("No USB serial device found. Is the board plugged in? (or pass --port)")
    listing = "\n".join(f"  {p.device}  {p.description}" for p in ports)
    sys.exit(f"Several USB serial devices found, pick one with --port or MCT_PORT:\n{listing}")


# Runs on the board (through mpremote, without a soft reset). `import` finds the module that
# boot.py already loaded; `instance` is set once ble_loader.start()/serve() has run.
PROBE_CODE = """\
try:
    import ble_loader as m
    print('MCT_LINK', m.instance.address if m.instance else 'NOT_STARTED')
except ImportError:
    print('MCT_LINK', 'NO_LOADER')
except AttributeError:
    print('MCT_LINK', 'OLD_LOADER')
"""
PROBE_ERRORS = {
    "NO_LOADER": "ble_loader is not on this board. Upload the project over USB first "
                 "(VS Code task: MPY: Deploy src to device).",
    "NOT_STARTED": "ble_loader is on this board but not running. boot.py must call ble_loader.start().",
    "OLD_LOADER": "ble_loader on this board is too old (it cannot report its address). "
                  "Upload the project again.",
}


def link_command(args):
    port = args.port or os.environ.get("MCT_PORT")
    if not port or port == "auto":  # "auto" is what esp32.port in the VS Code settings may say
        port = find_serial_port()
    print(f"Asking the board on {port} for its address ...", file=sys.stderr)
    cmd = [sys.executable, "-m", "mpremote", "connect", port, "resume", "exec", PROBE_CODE]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        sys.exit(f"No answer from {port}. Is another program (serial monitor, mpremote) using the port?")
    found = re.search(r"MCT_LINK (\S+)", result.stdout)
    if not found:
        detail = (result.stderr or result.stdout).strip().splitlines()[-1:] or ["no output"]
        sys.exit(f"Could not talk to the board on {port}: {detail[0]}\n"
                 "Check the port (mpremote devs) and pass it with --port.")
    answer = found.group(1)
    if answer in PROBE_ERRORS:
        sys.exit(f"Cannot link {port}: {PROBE_ERRORS[answer]}")
    if not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", answer):
        sys.exit(f"Unexpected answer from the board: {answer}")
    path = link_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(answer + "\n")
    print(f"Linked {answer} (board on {port}). mpble now connects to it without scanning.")


def unlink_command(_args):
    try:
        link_file().unlink()
        print("Unlinked. mpble scans for the board again.")
    except FileNotFoundError:
        print("No board linked.")


async def find_device(args) -> tuple[str, str]:
    """The address to connect to and where it came from: arg, linked or scan."""
    if args.address:
        return args.address, "arg"
    linked = linked_address()
    if linked:
        return linked, "linked"
    print(f"Scanning for '{args.name}' ...", file=sys.stderr)
    found = await BleakScanner.discover(timeout=args.scan_time, return_adv=True)
    for address, (_, adv) in found.items():
        if adv.local_name == args.name or SERVICE_UUID in [u.lower() for u in adv.service_uuids]:
            print(f"Found {address}  (skip the scan: plug the board in and run `mpble link`)", file=sys.stderr)
            return address, "scan"
    sys.exit(f"Device '{args.name}' not found. Is the loader running? Try: mpble scan")


def remote_name(local: Path, root: Path) -> str:
    return "/" + local.relative_to(root).as_posix()


async def sync(link: Link, root: Path):
    files = [p for p in sorted(root.rglob("*"))
             if p.is_file() and not (SKIP_DIRS & set(p.relative_to(root).parts)) and p.suffix != ".pyc"]
    tty = sys.stdout.isatty()
    print(f"Syncing {len(files)} files from {root} ...")
    start = clock()
    sent = skipped = 0
    for n, path in enumerate(files, 1):
        data, remote = path.read_bytes(), remote_name(path, root)
        if tty:
            print("\r" + f"[{n}/{len(files)}] checking {remote} ...".ljust(LINE_WIDTH), end="", flush=True)
        try:
            same = await link.call(OP_CRC, remote.encode()) == f"{binascii.crc32(data):08x}"
        except DeviceError:
            same = False  # not on the device yet
        if same:
            line = f"= {remote}  unchanged"
            print(("\r" + line.ljust(LINE_WIDTH)) if tty else line, flush=True)
            skipped += 1
            continue
        await link.put(data, remote)
        sent += len(data)
    uploaded = len(files) - skipped
    print(f"Done: {uploaded} uploaded ({fmt_size(sent)}), {skipped} unchanged, "
          f"{clock() - start:.1f} s")


async def windows_pairing(address: str):
    """WinRT pairing info of the device, or None when not on Windows."""
    if not IS_WINDOWS:
        return None
    from winrt.windows.devices.bluetooth import BluetoothLEDevice
    from winrt.windows.devices.enumeration import DeviceInformation

    device = await BluetoothLEDevice.from_bluetooth_address_async(int(address.replace(":", ""), 16))
    if device is None:
        return None
    return (await DeviceInformation.create_from_id_async(device.device_id)).pairing


async def unpair(args):
    address, _ = await find_device(args)
    pairing = await windows_pairing(address)
    if pairing is None or not pairing.is_paired:
        print("not paired")
        return
    result = await pairing.unpair_async()
    print(f"unpaired (status {result.status})")


async def connect(address: str, attempts: int = 3) -> BleakClient:
    """Connect, trying again a few times: right after a stop or reset the board needs a moment."""
    for attempt in range(1, attempts + 1):
        client = BleakClient(address)
        try:
            await client.connect()
            return client
        except (BleakError, asyncio.TimeoutError, TimeoutError):
            try:
                await client.disconnect()
            except Exception:
                pass
            if attempt == attempts:
                raise
            print("Board not reachable yet (still restarting, or holding the link of an mpble that was "
                  "killed; that frees up after ~15 s) ...", file=sys.stderr, flush=True)
            await asyncio.sleep(1.5)
    raise AssertionError("unreachable")


async def main_async(args):
    address, source = await find_device(args)
    stopped = False  # True on the second round of `exec/run --stop`, after the board was reset
    while True:
        print(f"Connecting to {address}{' (linked)' if source == 'linked' else ''} ...", file=sys.stderr)
        try:
            client = await connect(address, attempts=8 if stopped else 2)
        except (BleakError, asyncio.TimeoutError, TimeoutError) as e:
            await explain_connect_error(address, source)
            sys.exit(f"Could not connect to {address}: {e or type(e).__name__}. "
                     "Is the board on, in range, and running the loader? (Another program may hold the link.)")
        try:
            print(f"Connected (MTU {client.mtu_size})", file=sys.stderr)
            restart = await run_command(client, args, stopped)
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
        if not restart:
            return
        stopped = True
        await asyncio.sleep(3)  # the board is rebooting


async def explain_connect_error(address: str, source: str):
    """Print a hint for the usual reasons a connection fails (exits when it knows the cause)."""
    # The loader keeps no pairing keys, so a Windows pairing makes every
    # connection fail ("Unreachable") once Windows tries to encrypt the link.
    pairing = await windows_pairing(address)
    if pairing is not None and pairing.is_paired:
        sys.exit("Windows has the board paired, but the loader doesn't support pairing. Run: mpble unpair")
    if source == "linked":
        print(f"The linked board {address} did not answer. Another board? Run `mpble link` again "
              "or `mpble unlink`.", file=sys.stderr)


async def open_link(address: str, args) -> tuple[BleakClient, Link]:
    client = await connect(address)
    print(f"Connected (MTU {client.mtu_size})", file=sys.stderr)
    link = Link(client)
    await client.start_notify(TX_UUID, link.on_notify)
    if args.pin:
        await link.call(OP_AUTH, args.pin.encode())
    return client, link


def repl_command(args):
    """Interactive Python prompt on the board.

    The prompt runs in the main thread, so Ctrl+C works as expected. The Bluetooth part runs on an
    asyncio loop in a background thread; `run` hands a coroutine over and waits for its result."""
    address, source = asyncio.run(find_device(args))
    print(f"Connecting to {address}{' (linked)' if source == 'linked' else ''} ...", file=sys.stderr)
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()

    def run(coro):
        return asyncio.run_coroutine_threadsafe(coro, loop).result()

    try:
        client, link = run(open_link(address, args))
    except (BleakError, asyncio.TimeoutError, TimeoutError) as e:
        asyncio.run(explain_connect_error(address, source))
        sys.exit(f"Could not connect to {address}: {e or type(e).__name__}")
    try:
        repl_loop(run, link)
    finally:
        try:
            run(client.disconnect())
        except Exception:
            pass
        loop.call_soon_threadsafe(loop.stop)


def repl_loop(run, link: Link):
    print("MicroPython on the board, over Bluetooth. Variables stay between inputs.\n"
          "Ctrl+C: discard the input line, or stop running code (this resets the board, state is lost).\n"
          "Leave with exit() or Ctrl+Z then Enter (Ctrl+D on Linux/macOS).")
    pending: list[str] = []
    while True:
        try:
            line = input("... " if pending else ">>> ")
        except EOFError:
            print()
            return
        except KeyboardInterrupt:
            print("\nKeyboardInterrupt")
            pending.clear()
            continue
        if not pending and line.strip() in ("exit()", "exit", "quit()", "quit"):
            return
        pending.append(line)
        source = "\n".join(pending)
        try:
            incomplete = codeop.compile_command(source) is None  # e.g. "for x in y:" waits for more lines
        except (SyntaxError, ValueError, OverflowError):
            incomplete = False  # let the board report the error
        if incomplete:
            continue
        pending.clear()
        if not source.strip():
            continue
        try:
            run(link.evaluate(source))
        except KeyboardInterrupt:
            print("\nStopping: the board is reset, the REPL state is lost.")
            try:
                run(link.stop())
            except Exception:
                pass
            return
        except DeviceError as e:
            hint = " (the loader on the board is too old for the REPL: upload the project again)" \
                if str(e).startswith("unknown op") else ""
            print(f"device error: {e}{hint}")
            if not link.client.is_connected:
                return


async def run_command(client: BleakClient, args, stopped: bool = False) -> bool:
    """Run one command. Returns True when main_async has to reconnect and call it again
    (`--stop` reset the board that was busy)."""
    link = Link(client)
    await client.start_notify(TX_UUID, link.on_notify)
    if args.pin:
        await link.call(OP_AUTH, args.pin.encode())

    cmd = args.cmd
    if cmd == "ping":
        print(await link.call(OP_PING))
    elif cmd == "put":
        local = Path(args.local)
        await link.put(local.read_bytes(), args.remote or "/" + local.name)
    elif cmd == "sync":
        await sync(link, Path(args.dir))
        if args.reset:
            print("Resetting the board so boot.py/main.py run with the new files ...")
            try:
                await link.call(OP_RESET)
            except Exception:
                pass  # the link drops as the board resets
    elif cmd in ("exec", "run"):
        try:
            if cmd == "exec":
                local = Path(args.local)
                print(f"Uploading {local} as /_run.py", flush=True)
                await link.put(local.read_bytes(), "/_run.py")
                remote = "/_run.py"
            else:
                remote = args.remote
            print(f"Running {remote} (Ctrl+C to stop) ...", flush=True)
            await link.run(remote, follow=not args.detach)
        except DeviceError as e:
            if not (args.stop and not stopped and str(e).startswith("busy")):
                raise
            # the board still runs an earlier program: end it (reset), then start over
            print("A program is still running on the board: stopping it (the board restarts) ...", flush=True)
            if not await link.stop():
                raise DeviceError("the running program did not stop; try `mpble stop` or press the reset button")
            return True
    elif cmd == "ls":
        print(await link.call(OP_LS, args.path.encode()))
    elif cmd == "rm":
        await link.call(OP_RM, args.path.encode())
    elif cmd == "autorun":
        await link.call(OP_AUTORUN, b"" if args.clear or not args.path else args.path.encode())
    elif cmd in ("stop", "reset"):
        # the board resets and drops the link; no acknowledgement is waited for
        if await link.stop(OP_STOP if cmd == "stop" else OP_RESET):
            print("The board restarts.")
        else:
            print("No answer: the board may still be running.")
    return False


async def scan(args):
    found = await BleakScanner.discover(timeout=args.scan_time, return_adv=True)
    for address, (device, adv) in found.items():
        mark = "*" if SERVICE_UUID in [u.lower() for u in adv.service_uuids] else " "
        print(f"{mark} {address}  {adv.rssi:4d} dBm  {adv.local_name or device.name or ''}")
    print("(* = runs the MCT loader)")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--name", default="MCT-ESP", help="advertised device name")
    p.add_argument("--address", default=os.environ.get("MCT_BLE_ADDRESS"),
                   help="skip scanning and connect to this address (env: MCT_BLE_ADDRESS)")
    p.add_argument("--scan-time", type=float, default=5.0)
    p.add_argument("--pin", default=os.environ.get("MCT_BLE_PIN"))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan")
    sub.add_parser("ping")
    sub.add_parser("stop")
    sub.add_parser("reset")
    sub.add_parser("unpair", help="remove a Windows pairing with the board")
    s = sub.add_parser("link", help="remember the address of the board connected via USB",
                       description="Reads the Bluetooth address from the board on the USB serial port and "
                                   "saves it, so mpble connects without scanning. Fails if ble_loader is not "
                                   "loaded on the board. Briefly interrupts a running main.py.")
    s.add_argument("--port", metavar="COM4", help="serial port (default: MCT_PORT, else the one USB serial device)")
    sub.add_parser("unlink", help="forget the linked address (scan again)")
    sub.add_parser("repl", help="interactive Python prompt on the board",
                   description="Type Python and see the result, like the USB REPL but over Bluetooth. "
                               "Expressions print their value, variables stay between inputs. "
                               "Ctrl+C while code runs resets the board.")
    s = sub.add_parser("put"); s.add_argument("local"); s.add_argument("remote", nargs="?")
    s = sub.add_parser("sync"); s.add_argument("dir", nargs="?", default="src"); s.add_argument("--reset", action="store_true")
    stop_help = "end a program that is still running on the board first (the board restarts)"
    s = sub.add_parser("exec"); s.add_argument("local"); s.add_argument("--detach", action="store_true")
    s.add_argument("--stop", action="store_true", help=stop_help)
    s = sub.add_parser("run"); s.add_argument("remote"); s.add_argument("--detach", action="store_true")
    s.add_argument("--stop", action="store_true", help=stop_help)
    s = sub.add_parser("ls"); s.add_argument("path", nargs="?", default="/")
    s = sub.add_parser("rm"); s.add_argument("path")
    s = sub.add_parser("autorun"); s.add_argument("path", nargs="?"); s.add_argument("--clear", action="store_true")
    args = p.parse_args()
    try:
        if args.cmd in ("link", "unlink"):
            return (link_command if args.cmd == "link" else unlink_command)(args)
        if args.cmd == "repl":
            return repl_command(args)
        commands = {"scan": scan, "unpair": unpair}
        asyncio.run(commands.get(args.cmd, main_async)(args))
    except DeviceError as e:
        sys.exit(f"device error: {e}")


if __name__ == "__main__":
    main()

"""The shim must present the deck's REAL serial, not a fixed placeholder.

`hidshim.c` advertised `GHOSTDECKVHID00000` for the ADB-mode device while IOKit reports the deck's
own serial in HID mode, so Studio -- which keys the device it remembers on the serial it enumerates
-- saw the SAME deck as two different devices. Measured on the attached deck: `CurrentDeviceType`
was the real serial against an enumerated `GHOSTDECKVHID00000`, and the UI read "not connected"
while the bridge counted the handle open.

The bridge therefore advertises `--serial` in its `event` reply and the shim adopts it. This test
drives that exchange against a FAKE bridge on a private socket, so it needs no deck, no adb, and no
`/tmp/d200-*` path: the shim source is compiled with its socket path substituted.

Skips where no C compiler or macOS SDK is available -- the shim is Darwin-only in this tree.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHIM_SRC = ROOT / "reference" / "hidshim.c"
# A serial that is obviously synthetic: the public tree must not carry a real deck's identity.
FAKE_SERIAL = "SN-UNDER-TEST-0001"


def _compiler():
    return shutil.which("cc") or shutil.which("clang")


def _sdk():
    try:
        return subprocess.check_output(
            ["xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True, timeout=30
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return ""


needs_toolchain = pytest.mark.skipif(
    _compiler() is None or not _sdk(), reason="needs a C compiler and a macOS SDK"
)


class _FakeBridge:
    """Serves the shim's RPCs on a private socket and answers `event` with a chosen serial.

    `open` models the real bridge's registry: a handle already held stays refused. Without that the
    cross-process collision this file regression-tests cannot be reproduced at all -- a fake that
    accepts everything makes the test pass no matter what the shim numbers.
    """

    def __init__(self, path: Path, serial: str):
        self.serial = serial
        self.handles = {}
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        self.listener.listen(8)
        self._stop = threading.Event()
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                connection, _ = self.listener.accept()
            except OSError:
                return
            try:
                request = json.loads(connection.recv(4096).decode().strip().splitlines()[0])
                operation = request.get("op")
                if operation == "event":
                    reply = {
                        "schemaVersion": 1,
                        "accepted": True,
                        "openHandles": len(self.handles),
                        "outputsAcked": [0, 0],
                        "inputsReceived": [0, 0],
                        "serial": self.serial,
                    }
                elif operation == "open":
                    handle = request.get("handle")
                    with self.lock:
                        if handle in self.handles:
                            reply = {"schemaVersion": 1, "accepted": False,
                                     "error": "handle already open"}
                        else:
                            self.handles[handle] = request.get("interface", 0)
                            reply = {"schemaVersion": 1, "accepted": True,
                                     "capability": "ab" * 32}
                else:
                    reply = {"schemaVersion": 1, "accepted": True, "capability": "ab" * 32}
                connection.sendall((json.dumps(reply) + "\n").encode())
            except Exception:
                pass
            finally:
                connection.close()

    def close(self):
        self._stop.set()
        try:
            self.listener.close()
        except OSError:
            pass
        self.thread.join(timeout=5)


class _Info(ctypes.Structure):
    pass


_Info._fields_ = [
    ("path", ctypes.c_char_p),
    ("vendor_id", ctypes.c_ushort),
    ("product_id", ctypes.c_ushort),
    ("serial_number", ctypes.c_wchar_p),
    ("release_number", ctypes.c_ushort),
    ("manufacturer_string", ctypes.c_wchar_p),
    ("product_string", ctypes.c_wchar_p),
    ("usage_page", ctypes.c_ushort),
    ("usage", ctypes.c_ushort),
    ("interface_number", ctypes.c_int),
    ("next", ctypes.POINTER(_Info)),
]


def _build_shim(directory: Path, socket_path: Path) -> Path:
    """Compile `hidshim.c` against `socket_path`, so the test owns the endpoint."""
    source = SHIM_SRC.read_text(encoding="utf-8")
    assert '"/tmp/d200-adb-bridge.sock"' in source, (
        "the socket path literal moved; this test rewrites it and would silently keep the real one"
    )
    patched = source.replace('"/tmp/d200-adb-bridge.sock"', f'"{socket_path}"')
    local = directory / "hidshim_undertest.c"
    local.write_text(patched, encoding="utf-8")
    output = directory / "libhidshim_undertest.dylib"
    result = subprocess.run(
        [
            _compiler(), "-arch", "arm64", "-dynamiclib", "-O2",
            "-isysroot", _sdk(), str(local), "-o", str(output),
            "-framework", "IOKit", "-framework", "CoreFoundation", "-lpthread",
        ],
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stderr
    return output


def _load(path: Path):
    lib = ctypes.CDLL(str(path))
    lib.hid_enumerate.restype = ctypes.POINTER(_Info)
    lib.hid_enumerate.argtypes = [ctypes.c_ushort, ctypes.c_ushort]
    lib.hid_open.restype = ctypes.c_void_p
    lib.hid_open.argtypes = [ctypes.c_ushort, ctypes.c_ushort, ctypes.c_wchar_p]
    return lib


def _enumerated_serials(lib):
    serials = []
    node = lib.hid_enumerate(0x2207, 0x0019)
    while node:
        serials.append(node.contents.serial_number)
        node = node.contents.next
    return serials


@pytest.fixture()
def short_scratch():
    """An AF_UNIX endpoint lives in 104 bytes, so the socket needs a short path."""
    directory = Path(tempfile.mkdtemp(prefix="hidshim-serial-", dir="/tmp"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@needs_toolchain
def test_the_shim_advertises_the_serial_the_bridge_reports(short_scratch):
    """The defect: a fixed placeholder made one deck look like two devices."""
    endpoint = short_scratch / "b.sock"
    shim = _build_shim(short_scratch, endpoint)
    bridge = _FakeBridge(endpoint, FAKE_SERIAL)
    try:
        lib = _load(shim)
        assert _enumerated_serials(lib) == [FAKE_SERIAL, FAKE_SERIAL]
    finally:
        bridge.close()


@needs_toolchain
def test_hid_open_accepts_the_advertised_serial_and_still_refuses_a_stranger(short_scratch):
    """Opening by the enumerated serial is the path Studio takes; a stranger is still refused."""
    endpoint = short_scratch / "b2.sock"
    shim = _build_shim(short_scratch, endpoint)
    bridge = _FakeBridge(endpoint, FAKE_SERIAL)
    try:
        lib = _load(shim)
        assert _enumerated_serials(lib), "precondition: the serial must be adopted first"
        assert lib.hid_open(0x2207, 0x0019, FAKE_SERIAL), "the advertised serial was refused"
        # Backward compatibility: a caller holding the older placeholder identity still works.
        assert lib.hid_open(0x2207, 0x0019, "GHOSTDECKVHID00000"), "the placeholder was refused"
        assert not lib.hid_open(0x2207, 0x0019, "not-this-deck"), "a stranger's serial was accepted"
    finally:
        bridge.close()


@needs_toolchain
def test_a_second_live_process_can_open_the_same_interface(short_scratch):
    """Handle numbers must be unique across processes, not just within one.

    The bridge's registry is global, but each process that loads the shim numbered its handles from
    1. A second live process -- a restarted Studio, or any other client -- therefore asked for
    `handle 1` and was refused by the live owner, then burned the shim's 10x100ms retry on the SAME
    number and returned NULL with ETIMEDOUT. Measured against the real bridge on the attached deck,
    pre-fix: both a standing client AND a fresh one got `NULL errno=60`; post-fix both get OK.

    Two real processes are needed: one process cannot show a cross-process collision.
    """
    endpoint = short_scratch / "b4.sock"
    shim = _build_shim(short_scratch, endpoint)
    bridge = _FakeBridge(endpoint, FAKE_SERIAL)
    opener = (
        "import ctypes, sys, time\n"
        f"lib = ctypes.CDLL({str(shim)!r}, use_errno=True)\n"
        "lib.hid_open_path.restype = ctypes.c_void_p\n"
        "lib.hid_open_path.argtypes = [ctypes.c_char_p]\n"
        "ctypes.set_errno(0)\n"
        "d = lib.hid_open_path(b'd200-adb://2207:0019/interface/0')\n"
        "print('OK' if d else 'NULL', ctypes.get_errno(), flush=True)\n"
        "time.sleep(float(sys.argv[1]))\n"
    )
    try:
        first = subprocess.Popen([sys.executable, "-c", opener, "10"],
                                 stdout=subprocess.PIPE, text=True)
        first_line = first.stdout.readline().strip()
        assert first_line.startswith("OK"), f"the standing client failed: {first_line}"
        # A fresh process, exactly as a restarted Studio loads the shim.
        second = subprocess.Popen([sys.executable, "-c", opener, "1"],
                                  stdout=subprocess.PIPE, text=True)
        second_line = second.stdout.readline().strip()
        try:
            assert second_line.startswith("OK"), (
                f"a second live process could not open the same interface: {second_line}"
            )
        finally:
            second.kill(); second.wait()
        first.kill(); first.wait()
    finally:
        bridge.close()


@needs_toolchain
def test_the_placeholder_stands_when_the_bridge_reports_no_serial(short_scratch):
    """An older bridge that omits the field must keep working, not empty the serial out."""
    endpoint = short_scratch / "b3.sock"
    shim = _build_shim(short_scratch, endpoint)
    bridge = _FakeBridge(endpoint, "")
    try:
        lib = _load(shim)
        assert _enumerated_serials(lib) == ["GHOSTDECKVHID00000", "GHOSTDECKVHID00000"]
    finally:
        bridge.close()

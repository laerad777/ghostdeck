"""The bridge must reclaim a handle whose owning process is gone.

`hidshim.c` numbers handles per PROCESS from 1 (`static uint64_t next_handle = 1`), while
`BridgeState.handles` is global to the bridge. A client that exits without sending `close` -- killed,
crashed, or simply disconnected -- therefore left an entry that its successor reused by number, and
`handle already open` refused that successor FOREVER: Studio never attached again, and only a bridge
restart cleared it (measured on this host: `openHandles: 7` of leaked entries, and every
`hid_open(2207:0019)` returning NULL with `errno=60`).

Ownership is the peer's pid (`LOCAL_PEERPID`), so the rule is: a handle may be reclaimed when the
process that opened it is provably dead, and never while it is alive.

Device-free: a `DeviceProxy` that was never started, and `BridgeState` driven directly. No adb, no
device, no `/tmp/d200-*` path.

The compressed proof of the shipped fix, measured on the attached deck: after killing Studio, the
restarted copy re-attached (`openHandles` stayed 1 while `inputsReceived` moved `[3,0] -> [5,0]` and
`outputsAcked` `295 -> 583`). Pre-fix the same restart could not open handle 1 at all.
"""

from __future__ import annotations

import importlib.util
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
VENDOR = ROOT / "vendor"
BRIDGE_PATH = VENDOR / "d200-local-bridge.py"

sys.path.insert(0, str(VENDOR))

spec = importlib.util.spec_from_file_location("d200_local_bridge_handleowner", BRIDGE_PATH)
bridge = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = bridge
spec.loader.exec_module(bridge)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def make_state(tmp_path):
    proxy = bridge.DeviceProxy(
        str(tmp_path / "no-such-adb"), "unused",
        tmp_path / "d200-zkgui-proxy", tmp_path / "libd200-zkgui-preload.so",
    )
    return bridge.BridgeState(proxy)


def dead_pid():
    """A pid that is provably gone: spawn a process, reap it, and hand back its number."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def test_a_handle_whose_owner_died_is_reclaimed(tmp_path):
    """The defect: a dead owner's entry refused its successor by number, forever."""
    state = make_state(tmp_path)
    gone = dead_pid()

    assert state.open(1, 0, owner=gone), "precondition: the first client could open handle 1"
    # The owner is gone, and the successor's own shim counter restarts at 1.
    assert state.open(1, 0, owner=os.getpid()), "a dead owner's handle was not reclaimed"


def test_a_handle_whose_owner_is_alive_is_still_refused(tmp_path):
    """The other half: reclaiming must not become a way to steal a live client's handle."""
    state = make_state(tmp_path)
    state.open(2, 0, owner=os.getpid())
    with pytest.raises(bridge.ProtocolError) as excinfo:
        state.open(2, 0, owner=os.getpid() + 1)
    assert "handle already open" in str(excinfo.value)


def test_an_unidentifiable_owner_keeps_its_claim(tmp_path):
    """When the pid cannot be read the existing claim stands -- refusing beats stealing.

    `_pid_alive(None)` is True, so an unreadable owner is never reclaimed. Stealing a live client's
    handle would break a working Studio; refusing (and letting the next `open` succeed once that
    client really exits) is the recoverable direction.
    """
    state = make_state(tmp_path)
    state.open(3, 0, owner=None)
    with pytest.raises(bridge.ProtocolError):
        state.open(3, 0, owner=None)


def test_reclaiming_a_dead_handle_leaves_the_registry_consistent(tmp_path):
    """The reclaimed slot must hold the NEW capability, and the old one must stop working."""
    state = make_state(tmp_path)
    gone = dead_pid()
    old = state.open(4, 0, owner=gone)
    new = state.open(4, 0, owner=os.getpid())

    assert new != old, "the replacement did not get a fresh capability"
    assert state.authorize(4, new) == 0
    with pytest.raises(bridge.ProtocolError):
        state.authorize(4, old)


def test_pid_alive_only_calls_a_reaped_process_dead():
    """`os.kill(pid, 0)` is the probe; anything unreadable must read as alive."""
    assert bridge._pid_alive(os.getpid()) is True
    assert bridge._pid_alive(dead_pid()) is False
    # An unidentifiable owner must never be treated as dead, or a live half would be stolen.
    for junk in (None, 0, -1, "1234", True):
        assert bridge._pid_alive(junk) is True, junk


def test_the_event_reply_advertises_the_deck_serial(tmp_path):
    """The bridge must tell the shim which serial to present, rather than the shim inventing one.

    Measured on the attached deck: the shim enumerated `GHOSTDECKVHID00000` while Studio's own
    `CurrentDeviceType` was the deck's real serial, so one deck appeared under two identities.
    Whether that mismatch is what the UI showed as "not connected" was NOT proven -- Studio imports
    `hid_open_path` (path-keyed), not `hid_open`, and the deck had dropped off USB before an
    end-to-end check could run. This test only pins the fact that was measured: the value is passed
    through from `--serial` instead of being a constant.
    """
    state = make_state(tmp_path)
    state.transport.serial = "SN-UNDER-TEST"
    server = bridge.BridgeServer.__new__(bridge.BridgeServer)
    server.state = state

    reply = server.dispatch({"schemaVersion": 1, "op": "event", "handle": 0, "interface": 0,
                             "timeoutMs": -1})
    assert reply["accepted"] is True
    assert reply["serial"] == "SN-UNDER-TEST"


def test_the_serial_field_is_the_one_the_bridge_was_started_with(tmp_path):
    """The value is read from `--serial`, never stored: nothing here may invent a serial."""
    proxy = bridge.DeviceProxy(
        str(tmp_path / "no-such-adb"), "SN-UNDER-TEST",
        tmp_path / "d200-zkgui-proxy", tmp_path / "libd200-zkgui-preload.so",
    )
    state = bridge.BridgeState(proxy)
    server = bridge.BridgeServer.__new__(bridge.BridgeServer)
    server.state = state
    reply = server.dispatch({"schemaVersion": 1, "op": "event", "handle": 0, "interface": 0,
                             "timeoutMs": -1})
    assert reply["serial"] == "SN-UNDER-TEST"


@pytest.fixture()
def short_scratch():
    """An AF_UNIX endpoint lives in 104 bytes, so the wire test needs a short path."""
    directory = Path(tempfile.mkdtemp(prefix="vendorbridge-handleowner-", dir="/tmp"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


class _FakeConnection:
    """A stand-in whose `getsockopt` is scripted, so both platform branches are reachable."""

    def __init__(self, raw=b"", error=None):
        self.raw = raw
        self.error = error

    def getsockopt(self, level, option, size):
        if self.error is not None:
            raise self.error
        return self.raw


def test_the_linux_peer_credential_branch_reads_the_first_int(monkeypatch):
    """`SO_PEERCRED` is `struct ucred {pid, uid, gid}`; only the pid is the owner.

    This branch exists because the Darwin-only first revision passed on macOS and failed on the
    ubuntu runner, where nothing could name the peer: the reclaim silently did not happen, and the
    wire test caught it. Every unreadable shape must answer None, which keeps the claim.
    """
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(socket, "SO_PEERCRED", 0x11, raising=False)
    import struct as _struct

    assert bridge._peer_pid(_FakeConnection(_struct.pack("3i", 4242, 0, 0))) == 4242
    assert bridge._peer_pid(_FakeConnection(_struct.pack("3i", 0, 0, 0))) is None
    assert bridge._peer_pid(_FakeConnection(b"\x01\x02")) is None
    assert bridge._peer_pid(_FakeConnection(error=OSError(92, "not supported"))) is None
    assert bridge._peer_pid(None) is None


def test_a_platform_without_a_peer_option_degrades_to_no_owner(monkeypatch):
    """A host that cannot name the peer keeps the old behaviour instead of a wrong one."""
    monkeypatch.setattr(sys, "platform", "freebsd14")
    monkeypatch.delattr(socket, "SO_PEERCRED", raising=False)
    assert bridge._peer_pid(_FakeConnection()) is None


def _peer_pid_supported():
    """True when this host can name a unix-socket peer, which is what reclamation keys on.

    Both CI runners answer -- `LOCAL_PEERPID` on macOS, `SO_PEERCRED` on Linux -- but a host that
    answers neither keeps the old leak by design, so this test asserts nothing there rather than
    encoding a platform assumption it cannot meet.
    """
    if sys.platform == "darwin":
        return True
    return type(getattr(socket, "SO_PEERCRED", None)) is int


@pytest.mark.skipif(not _peer_pid_supported(), reason="host cannot name a unix-socket peer")
def test_a_real_disconnect_and_restart_pair_reattaches(short_scratch):
    """End to end over a real socket: killed client, then a successor on the same handle number.

    This is the shape that failed on the deck. The peer pid is what makes it work, so the test
    drives actual connections rather than calling `open` directly.
    """
    endpoint = short_scratch / "bridge.sock"
    tmp_path = short_scratch
    proxy = bridge.DeviceProxy(
        str(tmp_path / "no-such-adb"), "unused",
        tmp_path / "d200-zkgui-proxy", tmp_path / "libd200-zkgui-preload.so",
    )
    server = bridge.BridgeServer(str(endpoint), bridge.BridgeState(proxy))
    import threading

    threading.Thread(
        target=server.serve_forever, kwargs=dict(poll_interval=0.05), daemon=True
    ).start()
    try:
        # A client that opens handle 1 and then dies without closing.
        opener = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import socket, json\n"
                    f"s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
                    f"s.connect({str(endpoint)!r})\n"
                    "s.sendall((json.dumps({'schemaVersion': 1, 'op': 'open', 'handle': 1,"
                    " 'interface': 0, 'timeoutMs': -1}) + '\\n').encode())\n"
                    "print(s.recv(400).decode())\n"
                ),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        out, _ = opener.communicate(timeout=30)
        assert '"accepted":true' in out, out
        # The opener is reaped, so its pid is provably gone before the successor arrives.
        assert bridge._pid_alive(opener.pid) is False, "precondition: the opener really exited"
        # The opener has exited; the successor reuses handle 1, exactly as a restarted Studio does.
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(5)
        client.connect(str(endpoint))
        client.sendall(
            (
                json.dumps(
                    {"schemaVersion": 1, "op": "open", "handle": 1, "interface": 0, "timeoutMs": -1}
                )
                + "\n"
            ).encode()
        )
        answer = json.loads(client.recv(400).decode())
        client.close()
        assert answer.get("accepted") is True, answer
    finally:
        server.shutdown()
        server.server_close()

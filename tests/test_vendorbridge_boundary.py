"""Regression proof for FIX-5-T14 -- the bridge's ordered session-boundary records.

The dispatch's problem: the deck's adb transport died during a playlist run and the bridge's
own diagnostics could only be *inferred* into an order. Every record it emitted described one
moment (`transportRevive`, the video terminal receipt, `bridge_agent_remove_failed`) and none
carried a place in a total order, so "the last successful device interaction and the first
failure" had to be reconstructed by reading lines top to bottom.

What this test holds down, without a deck:

* one `hostMonotonicNs` + strictly increasing `sequence` on every boundary record, so a future
  log can be sorted instead of guessed at;
* the boundary order itself: session start, stop requested, the deck-side restore observed,
  the local proxy process exit, then the teardown summary;
* the teardown summary's attribution -- `stagedAgent: "failed"` when the deck can no longer
  run `rm -f /tmp/d200-color-agent`, which is the line the master's playlist log needed;
* that the proxy-process record reports an exit code and a signal honestly, and
* that none of these records carries the session token, the session directory or a capability.

The peer below is a real AF_UNIX listener speaking the shipped BOOTSTRAP/READY frames, and the
device is a fake `adb` script, so no deck, no real `/tmp/d200-*` path and no real HOME is
touched. What it cannot prove: that the deck-side proxy, the stock UI or the USB gadget behave
as the log suggests -- that needs the hardware, and it is not claimed here.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor"
BRIDGE_PATH = VENDOR / "d200-local-bridge.py"

sys.path.insert(0, str(VENDOR))

spec = importlib.util.spec_from_file_location("d200_local_bridge_boundary", BRIDGE_PATH)
bridge = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bridge
spec.loader.exec_module(bridge)

BUILD_ARTIFACTS = ("d200-zkgui-proxy", "d200-color-agent", "libd200-zkgui-preload.so")
BOUNDARY_EVENTS = ("transportSessionStart", "transportStopRequested", "transportStopObserved",
                   "transportProxyExit", "transportTeardown")
HEX64 = re.compile(r"[0-9a-f]{64}")


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """The admission lock lives under HOME; it must follow this one, never the operator's."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def make_proxy(tmp_path, *, reject=None, forward_port=None):
    """A real `DeviceProxy` whose only device is a fake `adb` script.

    `reject` is a substring of an argument that the fake deck refuses, which is how a device
    that has "gone away" for exactly one command is simulated. `forward_port` is what the fake
    `forward` command reports as the allocated host port, which is how the proxy is pointed at
    this test's own peer instead of the real forwarded port. The fake script only ever sees
    the argument list; it never executes anything, so `/tmp/d200-color-agent` is never opened.
    """
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    for name in BUILD_ARTIFACTS:
        (artifacts / name).write_bytes(b"x" * 32)
    script = tmp_path / "adb"
    script.write_text(
        "#!/bin/sh\n"
        f'for a in "$@"; do case "$a" in *"{reject or "/no-such-marker"}"*) exit 1;; esac; done\n'
        f'for a in "$@"; do [ "$a" = forward ] && {{ echo {forward_port or 0}; exit 0; }}; done\n'
        "exit 0\n"
    )
    script.chmod(0o755)
    return bridge.DeviceProxy(str(script), "unused", artifacts / BUILD_ARTIFACTS[0],
                              artifacts / BUILD_ARTIFACTS[2])


def frame(kind, payload=b"", sequence=0):
    """The shipped 16-byte-header framing, from the host side."""
    return (b"D2PX" + bytes((1, kind, 0, 0)) + struct.pack(">II", len(payload), sequence)
            + payload)


def read_frame(connection):
    header = b""
    while len(header) < 16:
        chunk = connection.recv(16 - len(header))
        if not chunk:
            raise AssertionError("peer closed before a full frame arrived")
        header += chunk
    length = struct.unpack(">I", header[8:12])[0]
    payload = b""
    while len(payload) < length:
        payload += connection.recv(length - len(payload))
    return header[5], payload


def serve_one_session(listener):
    """Answer the shipped handshake once: BOOTSTRAP, read HELLO, READY.

    The peer keeps its own transmit sequence: the host rejects a frame whose sequence does not
    match what it has already consumed, so BOOTSTRAP is 0 and READY is 1.
    """
    connection, _ = listener.accept()
    connection.sendall(frame(bridge.BOOTSTRAP))
    kind, payload = read_frame(connection)
    assert kind == bridge.HELLO and payload
    connection.sendall(frame(bridge.READY, sequence=1))
    return connection


class Peer:
    """The fake device-proxy peer, holding its socket for as long as the test needs it.

    The reference matters: if this object did not keep the accepted connection, it would be
    collected the moment the serving thread returned, the peer end would close, and the
    bridge's reader would see EOF mid-session and start reviving -- which is the failure mode
    this class exists to keep out of the test.
    """

    def __init__(self, listener):
        self.listener = listener
        self.connection = None
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def _serve(self):
        try:
            self.connection = serve_one_session(self.listener)
        except (AssertionError, OSError):
            pass

    def close(self):
        for resource in (self.connection, self.listener):
            if resource is not None:
                try:
                    resource.close()
                except OSError:
                    pass


def listening_socket():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    return listener


def records(captured, events=None):
    parsed = [json.loads(line) for line in captured.splitlines()
              if line.startswith("{") and '"event"' in line]
    if events is not None:
        parsed = [record for record in parsed if record["event"] in events]
    return parsed


def start_acknowledging_restore(proxy, delay=0.05):
    """Simulate the one thing the host reacts to in the deck's restore: RESTORING."""
    def acknowledge():
        time.sleep(delay)
        with proxy.condition:
            proxy.restoring = True
            proxy.condition.notify_all()
    threading.Thread(target=acknowledge, daemon=True).start()


# --- the ordered boundary --------------------------------------------------------------

def test_a_session_boundary_is_one_ordered_absolutely_timestamped_sequence(
        tmp_path, capfd):
    """Start, stop requested, restore observed, process exit, teardown -- in that order."""
    listener = listening_socket()
    peer = Peer(listener).start()
    proxy = make_proxy(tmp_path, forward_port=listener.getsockname()[1])
    try:
        proxy._start_proxy_transport()
        proxy.process = subprocess.Popen(["sleep", "30"], start_new_session=True)
        with proxy.condition:
            assert proxy.ready, "the fake peer must have completed the handshake"
        start_acknowledging_restore(proxy)

        proxy.close()
    finally:
        peer.close()
        if proxy.transport_socket is not None:
            proxy.transport_socket.close()

    boundary = records(capfd.readouterr().err, BOUNDARY_EVENTS)
    assert [record["event"] for record in boundary] == [
        "transportSessionStart", "transportStopRequested", "transportStopObserved",
        "transportProxyExit", "transportTeardown",
    ], boundary

    sequences = [record["sequence"] for record in boundary]
    assert sequences == sorted(sequences) and len(set(sequences)) == len(sequences), (
        "the counter must give every boundary record a unique place in the order"
    )
    clocks = [record["hostMonotonicNs"] for record in boundary]
    assert clocks == sorted(clocks), "host timestamps must not go backwards"
    assert all(record["clock"] == "host-monotonic" for record in boundary)
    assert all(record["pid"] == os.getpid() for record in boundary)
    assert isinstance(boundary[1]["generation"], int)
    assert boundary[2]["restoring"] is True and boundary[2]["waitedSeconds"] >= 0
    assert boundary[3]["signal"] == 15, "a terminated proxy process is reported by signal"
    assert boundary[4]["remoteDir"] == "ok" and boundary[4]["stagedAgent"] == "removed"

    # Nothing in the session-boundary stream may carry the credential or the private
    # session directory, both of which the same process holds at this moment.
    published = capfd.readouterr().err + "\n".join(json.dumps(record) for record in boundary)
    assert proxy.session_token not in published
    assert proxy.remote_dir not in published
    assert HEX64.search(published) is None


def test_the_teardown_record_names_the_step_the_deck_refused(tmp_path, capfd):
    """The playlist log's `bridge_agent_remove_failed` becomes attributable, not inferred.

    A device that is gone by teardown fails the fixed-path agent removal while the session
    directory removal (issued first, while the transport was still there) still succeeds.
    """
    proxy = make_proxy(tmp_path, reject=bridge.STAGED_AGENT)
    proxy.remote_dir_staged = True
    proxy.agent_staged = True

    proxy.close()

    captured = capfd.readouterr().err
    assert "bridge_agent_remove_failed" in captured, "the operator-facing line is unchanged"
    teardown = records(captured, ("transportTeardown",))[0]
    assert teardown["stagedAgent"] == "failed"
    assert teardown["remoteDir"] == "ok"
    assert teardown["forwardRemove"] == "skipped"
    assert teardown["seconds"] >= 0
    with proxy.condition:
        assert proxy.agent_staged is True, "a refused removal must not be recorded as done"


def test_the_proxy_exit_record_reports_the_exit_code_and_the_signal_honestly(tmp_path, capfd):
    """Two different deaths, two different records: a code is not a signal (C-104's lesson)."""
    proxy = make_proxy(tmp_path)

    proxy.process = subprocess.Popen([sys.executable, "-c", "raise SystemExit(3)"])
    proxy._reap_proxy_process()
    proxy.process = subprocess.Popen(["sleep", "30"], start_new_session=True)
    proxy._reap_proxy_process()

    exits = records(capfd.readouterr().err, ("transportProxyExit",))
    assert [record["how"] for record in exits] == ["exited", "killed-after-timeout"]
    assert exits[0]["returnCode"] == 3 and exits[0]["signal"] is None
    assert exits[1]["returnCode"] is None and exits[1]["signal"] == 9
    assert exits[1]["sequence"] > exits[0]["sequence"]


def test_a_second_close_after_a_full_teardown_emits_nothing_new(tmp_path, capfd):
    """Idempotence is a landed guarantee (T4/C-022); the diagnostics must not double up."""
    listener = listening_socket()
    peer = Peer(listener).start()
    proxy = make_proxy(tmp_path, forward_port=listener.getsockname()[1])
    try:
        proxy._start_proxy_transport()
        with proxy.condition:
            assert proxy.ready
        start_acknowledging_restore(proxy)
        proxy.close()
        first = records(capfd.readouterr().err, BOUNDARY_EVENTS)
        proxy.close()
        second = records(capfd.readouterr().err, BOUNDARY_EVENTS)
    finally:
        peer.close()

    assert [record["event"] for record in first] == [
        "transportSessionStart", "transportStopRequested", "transportStopObserved",
        "transportProxyExit", "transportTeardown",
    ]
    assert second == []


def test_teardown_never_revives_the_transport_it_is_closing(tmp_path, capfd, monkeypatch):
    """The reader used to race close() into a revive -- rebuilding the proxy and
    re-staging on the deck while the deck was being handed back to the stock UI.

    `_enable_adb` is the single choke point every revive starts with, so counting its calls
    is a direct measure of "did a revive happen", not a proxy for one.
    """
    listener = listening_socket()
    peer = Peer(listener).start()
    proxy = make_proxy(tmp_path, forward_port=listener.getsockname()[1])
    revives = []
    monkeypatch.setattr(bridge.DeviceProxy, "_enable_adb",
                        lambda self: revives.append(self.session_token))
    try:
        proxy._start_proxy_transport()
        with proxy.condition:
            assert proxy.ready
        start_acknowledging_restore(proxy)

        proxy.close()
        proxy.reader.join(timeout=5)

        assert revives == [], "teardown must not rebuild the transport it is closing"
        assert proxy.reader.is_alive() is False, "the reader must end with the session"
        assert records(capfd.readouterr().err, ("transportRevive",)) == []
    finally:
        peer.close()

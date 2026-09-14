"""Endpoint discipline for the hidshim bridge bring-up (`studio._ensure_bridge`).

The host caller used to remove the bridge socket path unconditionally once its probe said "not
live", which deleted the evidence the bridge's own `BridgeSocketInUse` guard needs. These tests pin
the replacement rule, mirroring `socket_listener_live` in vendor/d200-local-bridge.py: only a
refused connection or a missing path proves no listener owns the endpoint, so only that state may
unlink it. Every other probe outcome is reported, never acted on.

Confinement: every test points `studio.SOCKET` at a scratch path under pytest's tmp_path. The real
/tmp/d200-adb-bridge.sock is never opened, `ghostdeck studio` is never run, no Studio copy is
touched, and the device chain (`_adb_serial`, `_device_ready`) is stubbed out.
"""

from __future__ import annotations

import contextlib
import errno
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ghostdeck import studio  # noqa: E402


class _FakeChild:
    """Stand-in for a spawned bridge. Optionally binds the endpoint, as a real bridge would."""

    def __init__(self, endpoint: Path | None = None):
        self.returncode: int | None = None
        self._listener: socket.socket | None = None
        if endpoint is not None:
            self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._listener.bind(str(endpoint))
            self._listener.listen(4)

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def wait(self, timeout: float | None = None) -> int | None:
        self._close()
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9

    def _close(self) -> None:
        if self._listener is not None:
            self._listener.close()
            self._listener = None


class _TimedOutSocket:
    """A probe whose connect never completes: liveness cannot be excluded."""

    def __init__(self, *args, **kwargs):
        pass

    def settimeout(self, value: float) -> None:
        pass

    def connect(self, path) -> None:
        raise TimeoutError("probe timed out")

    def close(self) -> None:
        pass


class _ExhaustedSocket:
    """fd exhaustion: the construction itself fails, before any try/except in the old code."""

    def __init__(self, *args, **kwargs):
        raise OSError(errno.EMFILE, "Too many open files")


class _FakeSocketModule:
    """Stands in for the `socket` module inside studio, so only the probe is affected."""

    AF_UNIX = socket.AF_UNIX
    SOCK_STREAM = socket.SOCK_STREAM

    def __init__(self, factory):
        self.socket = factory


@pytest.fixture
def scratch():
    """A short /tmp scratch dir: pytest's tmp_path exceeds the AF_UNIX path limit on macOS."""
    directory = Path(tempfile.mkdtemp(prefix="gd-br-"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _bind_listener(path: Path) -> socket.socket:
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(4)
    return listener


def _stale(path: Path) -> None:
    """A socket file with no listener left: the crashed-bridge case."""
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(str(path))
    dead.close()


def _stub_device_chain(monkeypatch, spawned, *, endpoint=None, on_ready=None):
    """Make the bring-up loop reachable without a device, an adb call, or a real bridge.

    `endpoint` is what a spawned bridge binds; None means the spawn never brings one up.
    """
    monkeypatch.setattr(studio, "_adb_serial", lambda: "SERIAL")
    monkeypatch.setattr(studio, "BRIDGE_WAIT", 0.3)
    monkeypatch.setattr(studio, "BRIDGE_READY_TIMEOUT", 0.1)
    monkeypatch.setattr(studio, "BRIDGE_ATTEMPTS", 1)
    monkeypatch.setattr(studio, "BRIDGE_RETRY_DELAY", 0.0)

    def ready(serial, *, timeout):
        if on_ready is not None:
            on_ready()
        return True

    def spawn(serial, log):
        child = _FakeChild(endpoint)
        spawned.append(child)
        return child

    monkeypatch.setattr(studio, "_device_ready", ready)
    monkeypatch.setattr(studio, "_spawn_bridge", spawn)


# --- the contract: classify an endpoint by what a probe can actually prove -------------


def test_a_missing_endpoint_is_proven_dead(scratch, monkeypatch):
    monkeypatch.setattr(studio, "SOCKET", scratch / "b.sock")
    assert studio._socket_state() == (studio._ENDPOINT_DEAD, "endpoint is absent")
    assert studio._socket_live() is False


def test_a_stale_socket_file_is_proven_dead(scratch, monkeypatch):
    sock = scratch / "b.sock"
    _stale(sock)
    monkeypatch.setattr(studio, "SOCKET", sock)
    assert sock.exists()
    state, _ = studio._socket_state()
    assert state == studio._ENDPOINT_DEAD


def test_a_live_listener_is_reported_live(scratch, monkeypatch):
    sock = scratch / "b.sock"
    listener = _bind_listener(sock)
    try:
        monkeypatch.setattr(studio, "SOCKET", sock)
        assert studio._socket_state() == (studio._ENDPOINT_LIVE, "")
        assert studio._socket_live() is True
    finally:
        listener.close()


def test_a_path_that_is_not_a_socket_cannot_be_proven_dead(scratch, monkeypatch):
    """A regular file at the endpoint path is not evidence of a dead listener, so it is spared."""
    sock = scratch / "b.sock"
    sock.write_text("operator data\n", encoding="utf-8")
    monkeypatch.setattr(studio, "SOCKET", sock)
    state, reason = studio._socket_state()
    assert state == studio._ENDPOINT_UNDETERMINABLE
    assert reason, "an undeterminable probe must say why"
    assert studio._socket_live() is False


# --- a live endpoint is never unlinked, however the probe behaves -----------------------


def test_live_endpoint_at_entry_is_used_and_never_unlinked(scratch, monkeypatch):
    """Requirement 4: the already-up fast path returns early, touching nothing."""
    sock = scratch / "b.sock"
    listener = _bind_listener(sock)
    inode = sock.stat().st_ino
    spawned = []
    try:
        monkeypatch.setattr(studio, "SOCKET", sock)
        _stub_device_chain(monkeypatch, spawned)
        studio._ensure_bridge()
        assert sock.exists() and sock.stat().st_ino == inode
        assert spawned == [], "a second bridge was spawned although one was already up"
    finally:
        listener.close()


def test_live_endpoint_appearing_during_the_device_wait_is_not_unlinked(scratch, monkeypatch):
    """The race the unconditional unlink lost: the bridge comes up while we wait for the deck."""
    sock = scratch / "b.sock"
    _stale(sock)                       # dead at entry, so bring-up is permitted to proceed
    holder = []

    def bridge_arrives():
        # A real bridge reclaims a proven-dead path and rebinds it (BridgeServer.__init__).
        sock.unlink()
        holder.append(_bind_listener(sock))   # a foreign bridge claims the endpoint now

    spawned = []
    monkeypatch.setattr(studio, "SOCKET", sock)
    _stub_device_chain(monkeypatch, spawned, on_ready=bridge_arrives)
    try:
        with contextlib.suppress(RuntimeError):
            # Refusing is acceptable here; deleting a live endpoint or spawning over it is not.
            studio._ensure_bridge()
        assert sock.exists(), "the endpoint that became live was deleted"
        assert spawned == [], "a second bridge was spawned on a live endpoint"
        assert studio._socket_live() is True
    finally:
        for listener in holder:
            listener.close()


def test_a_probe_that_cannot_complete_refuses_and_spares_the_endpoint(scratch, monkeypatch):
    """Requirement 3: undeterminable means no unlink, no second bridge, and a clear refusal."""
    sock = scratch / "b.sock"
    listener = _bind_listener(sock)
    inode = sock.stat().st_ino
    spawned = []
    try:
        monkeypatch.setattr(studio, "SOCKET", sock)
        monkeypatch.setattr(studio, "socket", _FakeSocketModule(_TimedOutSocket))
        _stub_device_chain(monkeypatch, spawned)
        with pytest.raises(RuntimeError) as excinfo:
            studio._ensure_bridge()
        message = str(excinfo.value)
        assert str(sock) in message
        assert "not starting a second bridge" in message
        assert "\n" not in message
        assert sock.exists() and sock.stat().st_ino == inode, "a live endpoint was unlinked"
        assert spawned == []
    finally:
        listener.close()


def test_a_non_socket_path_is_not_deleted(scratch, monkeypatch):
    """The old unlink removed whatever was at the path. It must not destroy a non-socket file."""
    sock = scratch / "b.sock"
    sock.write_text("operator data\n", encoding="utf-8")
    spawned = []
    monkeypatch.setattr(studio, "SOCKET", sock)
    _stub_device_chain(monkeypatch, spawned)
    with pytest.raises(RuntimeError) as excinfo:
        studio._ensure_bridge()
    assert "cannot verify whether a bridge is already listening" in str(excinfo.value)
    assert sock.read_text(encoding="utf-8") == "operator data\n"
    assert spawned == []


# --- a proven-dead endpoint is still reclaimed, and fd exhaustion never raises ----------


def test_a_proven_dead_endpoint_is_reclaimed_and_rebound(scratch, monkeypatch):
    """Requirement 1's other half: bring-up must still work over a crashed bridge's leftover."""
    sock = scratch / "b.sock"
    _stale(sock)
    spawned = []
    monkeypatch.setattr(studio, "SOCKET", sock)
    _stub_device_chain(monkeypatch, spawned, endpoint=sock)
    studio._ensure_bridge()
    assert len(spawned) == 1, "a stale endpoint blocked bring-up"
    assert sock.exists(), "the bridge endpoint was not rebound"
    assert sock.is_socket()
    assert studio._socket_live() is True
    spawned[0]._close()


def test_fd_exhaustion_is_reported_and_never_raised(scratch, monkeypatch):
    """Requirement 2: a probe is a state, not an exception, so `studio` cannot die with EMFILE."""
    sock = scratch / "b.sock"
    listener = _bind_listener(sock)
    inode = sock.stat().st_ino
    spawned = []
    try:
        monkeypatch.setattr(studio, "SOCKET", sock)
        monkeypatch.setattr(studio, "socket", _FakeSocketModule(_ExhaustedSocket))
        _stub_device_chain(monkeypatch, spawned)
        state, reason = studio._socket_state()
        assert state == studio._ENDPOINT_UNDETERMINABLE
        assert "Too many open files" in reason
        assert studio._socket_live() is False
        with pytest.raises(RuntimeError) as excinfo:
            studio._ensure_bridge()
        assert not isinstance(excinfo.value, OSError)
        assert "cannot verify whether a bridge is already listening" in str(excinfo.value)
        assert sock.exists() and sock.stat().st_ino == inode
        assert spawned == []
    finally:
        listener.close()


# --- H3: a stale host adb server is restarted, once, and only on the stale path ---------
# Observed on the physical deck: `usb.detect()` reports the deck in ADB mode while the host
# `adb` server has no transport for it, so `adb devices` is empty and every allowlisted device
# command fails. Waiting cannot fix that server, and the bridge's own staging then fails.


def _usb_says(monkeypatch, mode: str, restarts: list, *, serial: str = "SERIAL"):
    """Report `mode` for the deck and record server restarts. Touches no hardware."""
    monkeypatch.setattr(studio.usb, "detect", lambda: {"serial": serial, "mode": mode})
    monkeypatch.setattr(studio.adb, "restart_server", lambda: restarts.append("restart"))


def test_a_stale_adb_server_is_restarted_once_and_the_wait_is_rerun(scratch, monkeypatch):
    """The measured H3 path: USB says ADB, readiness fails, the restart is what makes it answer."""
    sock = scratch / "b.sock"
    restarts: list = []
    _usb_says(monkeypatch, "adb", restarts)
    spawned = []
    monkeypatch.setattr(studio, "SOCKET", sock)
    _stub_device_chain(monkeypatch, spawned, endpoint=sock)
    waits = []

    def ready_only_after_the_restart(serial, *, timeout):
        waits.append(serial)
        return len(waits) > 1

    monkeypatch.setattr(studio, "_device_ready", ready_only_after_the_restart)
    studio._ensure_bridge()
    assert restarts == ["restart"], "the stale adb server was not restarted exactly once"
    assert len(waits) == 2, "the readiness wait was not re-run after the restart"
    assert len(spawned) == 1, "the restart did not let bring-up proceed"
    spawned[0]._close()


def test_an_answering_deck_never_restarts_the_adb_server(scratch, monkeypatch):
    """Requirement: no restart on the healthy path."""
    sock = scratch / "b.sock"
    restarts: list = []
    _usb_says(monkeypatch, "adb", restarts)
    spawned = []
    monkeypatch.setattr(studio, "SOCKET", sock)
    _stub_device_chain(monkeypatch, spawned, endpoint=sock)
    studio._ensure_bridge()
    assert restarts == [], "a working server was restarted"
    assert len(spawned) == 1
    spawned[0]._close()


def test_a_deck_that_usb_does_not_report_as_adb_never_restarts_the_server(scratch, monkeypatch):
    """Only `usb.detect()` reporting ADB proves the server is stale; anything else is not a case."""
    for mode in ("none", "hid"):
        restarts: list = []
        spawned = []
        monkeypatch.setattr(studio, "SOCKET", scratch / f"b-{mode}.sock")
        _usb_says(monkeypatch, mode, restarts)
        _stub_device_chain(monkeypatch, spawned, endpoint=None)
        monkeypatch.setattr(studio, "_device_ready", lambda serial, *, timeout: False)
        with pytest.raises(RuntimeError):
            studio._ensure_bridge()
        assert restarts == [], f"restarted the server for a deck reported as {mode!r}"


def test_the_restart_is_bounded_to_one_per_bring_up(scratch, monkeypatch):
    """A dead deck must not make the retry loop kill a shared server once per attempt."""
    sock = scratch / "b.sock"
    restarts: list = []
    _usb_says(monkeypatch, "adb", restarts)
    spawned = []
    monkeypatch.setattr(studio, "SOCKET", sock)
    _stub_device_chain(monkeypatch, spawned, endpoint=None)
    monkeypatch.setattr(studio, "BRIDGE_ATTEMPTS", 3)
    attempts = []
    monkeypatch.setattr(
        studio, "_device_ready", lambda serial, *, timeout: attempts.append(serial) or False
    )
    with pytest.raises(RuntimeError):
        studio._ensure_bridge()
    assert len(restarts) == 1, f"the restart was not bounded: {len(restarts)} restarts"
    assert len(attempts) == 4, "one wait per attempt plus the one after the restart"
    assert spawned == []


def test_the_failure_message_says_the_restart_was_tried(scratch, monkeypatch):
    """Requirement: the existing message is kept, and it now names what was tried."""
    sock = scratch / "b.sock"
    restarts: list = []
    _usb_says(monkeypatch, "adb", restarts)
    spawned = []
    monkeypatch.setattr(studio, "SOCKET", sock)
    _stub_device_chain(monkeypatch, spawned, endpoint=None)
    monkeypatch.setattr(studio, "_device_ready", lambda serial, *, timeout: False)
    with pytest.raises(RuntimeError) as excinfo:
        studio._ensure_bridge()
    message = str(excinfo.value)
    assert message.startswith("D200 stopped answering device commands after switching to ADB")
    assert "adb kill-server; adb start-server" in message
    assert "\n" not in message, "the message a user sees must stay one line"


def test_the_recovery_did_not_widen_the_device_allowlist():
    """Requirement 2: the recovery is a named call, not a new device-shell permission."""
    assert callable(studio.adb.restart_server)
    for argv in (["kill-server"], ["start-server"], ["-s", "SERIAL", "kill-server"]):
        assert studio.adb.allowed(argv) is False, f"the device allowlist now permits {argv}"
        with pytest.raises(studio.adb.AdbDenied):
            studio.adb.validate(argv)
    # and the device surface it already had still works
    assert studio.adb.allowed(["-s", "SERIAL", "shell", "getprop", "sys.usb.config"]) is True


def test_a_failed_restart_is_reported_without_a_traceback(scratch, monkeypatch):
    """`adb` refusing to restart is a reported reason, not an uncaught error out of bring-up."""
    sock = scratch / "b.sock"
    _usb_says(monkeypatch, "adb", [])

    def boom():
        raise RuntimeError("'adb start-server' failed with status 1: cannot bind")

    monkeypatch.setattr(studio.adb, "restart_server", boom)
    spawned = []
    monkeypatch.setattr(studio, "SOCKET", sock)
    _stub_device_chain(monkeypatch, spawned, endpoint=None)
    monkeypatch.setattr(studio, "_device_ready", lambda serial, *, timeout: False)
    with pytest.raises(RuntimeError) as excinfo:
        studio._ensure_bridge()
    assert "could not be restarted" in str(excinfo.value)
    assert "cannot bind" in str(excinfo.value)
    assert spawned == []


# --- A-153: two of the H3 guards were not pinned by any test -------------------------------


def test_usb_reports_adb_requires_the_matching_serial(scratch, monkeypatch):
    """A-153: another ADB device on the bus is not proof that OUR deck is the ADB device.

    Dropping the serial equality made the host restart the shared adb server for any ADB device --
    a phone on the same bus -- and left the whole suite green.
    """
    sock = scratch / "b.sock"
    restarts: list = []
    _usb_says(monkeypatch, "adb", restarts, serial="SOME-OTHER-DEVICE")
    spawned = []
    monkeypatch.setattr(studio, "SOCKET", sock)
    _stub_device_chain(monkeypatch, spawned, endpoint=None)
    monkeypatch.setattr(studio, "_device_ready", lambda serial, *, timeout: False)
    assert studio._usb_reports_adb("SERIAL") is False
    with pytest.raises(RuntimeError):
        studio._ensure_bridge()
    assert restarts == [], "the shared adb server was restarted for a different device"
    # The same reading is positive only for the serial that was asked about.
    _usb_says(monkeypatch, "adb", restarts, serial="SERIAL")
    assert studio._usb_reports_adb("SERIAL") is True


def test_restart_server_names_a_failing_command(monkeypatch):
    """A-153: the error-reporting contract of the new host-daemon call, pinned directly.

    Deleting the return-code check made the whole "report what was tried" contract disappear while
    the suite stayed green, so it is asserted on the exception text, not on a status code.
    """
    monkeypatch.setattr(studio.adb, "adb_bin", lambda: "/nonexistent/adb")
    seen = []

    def fake_run(argv, **_kwargs):
        seen.append(argv[-1])
        if argv[-1] == "start-server":
            return subprocess.CompletedProcess(argv, 1, "", "cannot bind to 127.0.0.1:5037\n")
        return subprocess.CompletedProcess(argv, 0, "killed\n", "")

    monkeypatch.setattr(studio.adb.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError) as excinfo:
        studio.adb.restart_server()
    message = str(excinfo.value)
    assert "start-server" in message
    assert "cannot bind" in message
    assert "status 1" in message
    assert seen == ["kill-server", "start-server"]


def test_restart_server_succeeds_quietly_when_both_commands_exit_zero(monkeypatch):
    """The accept side of the same contract, so the check is not merely 'always raise'."""
    monkeypatch.setattr(studio.adb, "adb_bin", lambda: "/nonexistent/adb")
    seen = []

    def fake_run(argv, **_kwargs):
        seen.append(argv[-1])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(studio.adb.subprocess, "run", fake_run)
    assert studio.adb.restart_server() is None
    assert seen == ["kill-server", "start-server"]

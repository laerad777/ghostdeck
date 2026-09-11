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
    stale_inode = sock.stat().st_ino
    spawned = []
    monkeypatch.setattr(studio, "SOCKET", sock)
    _stub_device_chain(monkeypatch, spawned, endpoint=sock)
    studio._ensure_bridge()
    assert len(spawned) == 1, "a stale endpoint blocked bring-up"
    assert sock.exists(), "the bridge endpoint was not rebound"
    assert sock.stat().st_ino != stale_inode, "the stale file was reused instead of reclaimed"
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

"""Regression proof that a second bridge never steals a live bridge socket.

FINDER-C C-013: `BridgeServer.__init__` used to `unlink(missing_ok=True)` before
binding and `server_close()` unlinked unconditionally, so starting a second
bridge deleted the running instance's endpoint. These tests drive the real
`BridgeServer` and the real CLI on scratch AF_UNIX paths under /tmp: a planted
live listener stands in for the running bridge. No device, no adb, no
ffmpeg/Studio, and never the real `/tmp/d200-adb-bridge.sock`.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BRIDGE_PATH = ROOT / "vendor" / "d200-local-bridge.py"

# The bridge imports its vendor siblings by name, as it does when run directly.
sys.path.insert(0, str(ROOT / "vendor"))

spec = importlib.util.spec_from_file_location("d200_local_bridge_under_test", BRIDGE_PATH)
bridge = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bridge
spec.loader.exec_module(bridge)


@pytest.fixture()
def scratch():
    directory = Path(tempfile.mkdtemp(prefix="vendorbridge-socket-", dir="/tmp"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def plant_live_listener(path):
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(4)
    return listener


def probe(path):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(1)
        client.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        client.close()


def test_live_listener_is_reported_live(scratch):
    listener = plant_live_listener(scratch / "bridge.sock")
    try:
        assert bridge.socket_listener_live(scratch / "bridge.sock") is True
    finally:
        listener.close()


def test_missing_path_is_not_live(scratch):
    assert bridge.socket_listener_live(scratch / "absent.sock") is False


def test_stale_socket_file_is_not_live(scratch):
    """A bound-then-closed listener leaves the file behind with nobody serving it."""
    path = scratch / "stale.sock"
    listener = plant_live_listener(path)
    listener.close()
    assert path.exists()
    assert bridge.socket_listener_live(path) is False


def test_second_server_refuses_a_live_endpoint_and_leaves_it_bound(scratch):
    path = scratch / "bridge.sock"
    listener = plant_live_listener(path)
    try:
        with pytest.raises(bridge.BridgeSocketInUse):
            bridge.BridgeServer(path, state=None)
        assert path.exists(), "a live endpoint must not be unlinked"
        assert probe(path) is True, "the running listener must still accept connections"
    finally:
        listener.close()


def test_stale_endpoint_is_reclaimed_and_bound(scratch):
    """A refused connection is the proof that lets the stale file go."""
    path = scratch / "stale.sock"
    plant_live_listener(path).close()
    assert path.exists()
    assert bridge.socket_listener_live(path) is False

    server = bridge.BridgeServer(path, state=None)
    try:
        assert probe(path) is True, "the reclaimed path must now be served"
    finally:
        server.server_close()
    assert not path.exists()


def test_non_socket_path_is_refused_not_deleted(scratch):
    """ENOTSOCK is not proof of death, so the path is left untouched."""
    path = scratch / "plain.sock"
    path.write_text("not a socket")
    with pytest.raises(bridge.BridgeSocketInUse):
        bridge.BridgeServer(path, state=None)
    assert path.read_text() == "not a socket"


def test_server_close_removes_its_own_endpoint(scratch):
    path = scratch / "bridge.sock"
    server = bridge.BridgeServer(path, state=None)
    assert path.exists()
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    server.server_close()
    assert not path.exists()


def test_server_close_leaves_a_replaced_endpoint_alone(scratch):
    """A server that lost the endpoint must not delete whoever owns it now."""
    path = scratch / "bridge.sock"
    server = bridge.BridgeServer(path, state=None)
    path.unlink()
    listener = plant_live_listener(path)
    try:
        server.server_close()
        assert path.exists(), "the replacement endpoint must survive"
        assert probe(path) is True
    finally:
        listener.close()


def test_cli_refuses_a_live_endpoint_before_any_device_work(scratch):
    path = scratch / "bridge.sock"
    listener = plant_live_listener(path)
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(BRIDGE_PATH),
                "--socket",
                str(path),
                "--serial",
                "unused",
                "--adb",
                str(scratch / "no-such-adb"),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 1, result.stderr
        assert "bridge_socket_in_use" in result.stderr
        assert path.exists()
        assert probe(path) is True
    finally:
        listener.close()


class RecordingProxy(bridge.DeviceProxy):
    """DeviceProxy with adb replaced by a recorded command list."""

    def __init__(self, *arguments, **keywords):
        super().__init__(*arguments, **keywords)
        self.commands = []

    def _run(self, *arguments, timeout=15):
        self.commands.append((arguments, timeout))
        return None


def make_proxy():
    return RecordingProxy(
        "/nonexistent/adb",
        "unused",
        Path("/nonexistent/d200-zkgui-proxy"),
        Path("/nonexistent/libd200-zkgui-preload.so"),
    )


def test_staged_agent_is_removed_only_by_the_instance_that_staged_it():
    proxy = make_proxy()
    proxy._remove_staged_agent()
    assert proxy.commands == [], "an unstaged agent belongs to some other instance"

    proxy.agent_staged = True
    proxy._remove_staged_agent()
    assert proxy.commands == [(("shell", f"rm -f {bridge.STAGED_AGENT}"), 5)]
    assert proxy.agent_staged is False

    proxy._remove_staged_agent()
    assert len(proxy.commands) == 1, "removal must not repeat"


def test_close_runs_the_staged_agent_removal(scratch):
    proxy = make_proxy()
    order = []
    proxy._remove_remote_dir = lambda: order.append("remote_dir")
    proxy._remove_staged_agent = lambda: order.append("staged_agent")

    proxy.close()

    assert order == ["remote_dir", "staged_agent"]
    assert proxy.closed is True
    assert proxy.commands == [], "close() must not need the device here"


def test_agent_path_is_declared_once_in_the_bridge():
    source = BRIDGE_PATH.read_text(encoding="utf-8")
    assert source.count("'/tmp/d200-color-agent'") == 1
    assert f"STAGED_AGENT = '/tmp/d200-color-agent'" in source

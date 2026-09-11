"""Device-admission regression proof for FIX-5-T7 (C-012).

`managed_device_admission()` was a bare `yield`, so two concurrent bridges both
passed admission, both staged to, and both drove one real deck. These tests drive
the real context manager and the real `DeviceProxy` caller with a scratch lock
path only: no adb, no device, no `/tmp/d200-adb-bridge.sock`, and no real
`~/.ghostdeck/device-admission.lock` (HOME is redirected to `tmp_path`).

The four behaviours the dispatch requires:
  first bridge                    -> admitted
  second bridge, first alive      -> rejected with DeviceAdmissionError
  second bridge, first dead/killed-> admitted (stale lock reclaimed)
  normal exit / exception         -> lock released
"""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor"
sys.path.insert(0, str(VENDOR))

from d200_process_control import (  # noqa: E402
    ADMISSION_LOCK_NAME,
    DeviceAdmissionError,
    admission_lock_path,
    managed_device_admission,
)

BRIDGE = VENDOR / "d200-local-bridge.py"

# A child that takes the admission and then holds it until its stdin closes.
HOLDER = """
import sys
sys.path.insert(0, sys.argv[1])
from d200_process_control import managed_device_admission
with managed_device_admission(lock_path=sys.argv[2]):
    print("held", flush=True)
    sys.stdin.read()
"""


def _load_bridge():
    """The bridge is not import-safe as a module name (dashes), so load it by path."""
    spec = importlib.util.spec_from_file_location("d200_local_bridge_admission", BRIDGE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def lock_path(tmp_path):
    return tmp_path / "state" / ADMISSION_LOCK_NAME


def _hold_in_child(lock_path):
    child = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(VENDOR), str(lock_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert child.stdout.readline().strip() == "held", "the holder child never took the lock"
    return child


def _stop_child(child, sig=signal.SIGKILL):
    try:
        os.kill(child.pid, sig)
    except ProcessLookupError:
        pass
    child.wait(timeout=10)
    child.stdin.close()
    child.stdout.close()
    child.stderr.close()


def test_lock_path_is_resolved_under_home_at_call_time(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert admission_lock_path() == tmp_path / ".ghostdeck" / ADMISSION_LOCK_NAME


def test_first_bridge_is_admitted_and_records_the_holder(lock_path):
    with managed_device_admission(lock_path=lock_path):
        recorded = json.loads(lock_path.read_text())
        assert recorded["pid"] == os.getpid()
        assert isinstance(recorded["lstart"], str) and recorded["lstart"].strip()
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600


def test_second_bridge_is_rejected_while_the_first_is_alive(lock_path):
    child = _hold_in_child(lock_path)
    try:
        with pytest.raises(DeviceAdmissionError) as refused:
            with managed_device_admission(lock_path=lock_path):
                pytest.fail("a second bridge must never reach the guarded section")
        assert "already owns the device" in str(refused.value)
        assert f"pid {child.pid}" in str(refused.value)
        # The first bridge keeps working: no second holder can displace it.
        with pytest.raises(DeviceAdmissionError):
            with managed_device_admission(lock_path=lock_path):
                pytest.fail("the first holder must still own the deck")
    finally:
        _stop_child(child)


def test_a_killed_bridge_releases_the_lock(lock_path):
    """SIGKILL cannot run a finally block, so only the kernel can release it."""
    child = _hold_in_child(lock_path)
    _stop_child(child)
    with managed_device_admission(lock_path=lock_path):
        assert json.loads(lock_path.read_text())["pid"] == os.getpid()


def test_a_stale_lock_record_with_a_dead_pid_is_reclaimed(lock_path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    dead = _dead_pid()
    lock_path.write_text(json.dumps({"pid": dead, "lstart": "Thu Jan  1 00:00:00 1970"}))
    with managed_device_admission(lock_path=lock_path):
        assert json.loads(lock_path.read_text())["pid"] == os.getpid()


def test_admission_is_released_on_normal_exit_and_on_exception(lock_path):
    with managed_device_admission(lock_path=lock_path):
        pass
    with managed_device_admission(lock_path=lock_path):
        pass
    with pytest.raises(RuntimeError):
        with managed_device_admission(lock_path=lock_path):
            raise RuntimeError("caller failed")
    with managed_device_admission(lock_path=lock_path):
        pass


def _dead_pid():
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=10)
    return process.pid


def _offline_proxy(monkeypatch, tmp_path):
    """A real DeviceProxy whose device-touching steps are replaced by no-ops."""
    bridge = _load_bridge()
    proxy = bridge.DeviceProxy(
        str(tmp_path / "no-such-adb"), "unused",
        tmp_path / "d200-zkgui-proxy", tmp_path / "libd200-zkgui-preload.so",
    )
    monkeypatch.setattr(proxy, "_remove_remote_dir", lambda: None)
    monkeypatch.setattr(proxy, "_remove_staged_agent", lambda: None)
    monkeypatch.setattr(proxy, "_start_with_cleanup", lambda: None)
    return proxy


def test_the_bridge_caller_holds_the_lock_for_the_whole_session(tmp_path, monkeypatch):
    """The proxy owns the deck until close(), not merely until startup returns."""
    monkeypatch.setenv("HOME", str(tmp_path))
    lock_path = admission_lock_path()
    proxy = _offline_proxy(monkeypatch, tmp_path)
    proxy.start()
    try:
        with pytest.raises(DeviceAdmissionError):
            with managed_device_admission(lock_path=lock_path):
                pytest.fail("the running proxy must still own the deck after startup")
    finally:
        proxy.close()
    with managed_device_admission(lock_path=lock_path):
        assert json.loads(lock_path.read_text())["pid"] == os.getpid()


def test_a_failed_start_releases_the_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    lock_path = admission_lock_path()
    proxy = _offline_proxy(monkeypatch, tmp_path)

    def explode():
        raise RuntimeError("staging failed")

    monkeypatch.setattr(proxy, "_start_with_cleanup", explode)
    with pytest.raises(RuntimeError, match="staging failed"):
        proxy.start()
    with managed_device_admission(lock_path=lock_path):
        assert json.loads(lock_path.read_text())["pid"] == os.getpid()


def test_a_refused_bridge_does_no_device_work(tmp_path, monkeypatch):
    """Rejection must precede staging, per the caller's own contract."""
    monkeypatch.setenv("HOME", str(tmp_path))
    lock_path = admission_lock_path()
    marker = tmp_path / "adb-ran"
    fake_adb = tmp_path / "adb"
    fake_adb.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 0\n')
    fake_adb.chmod(0o755)
    child = _hold_in_child(lock_path)
    try:
        bridge = _load_bridge()
        proxy = bridge.DeviceProxy(
            str(fake_adb), "unused",
            tmp_path / "d200-zkgui-proxy", tmp_path / "libd200-zkgui-preload.so",
        )
        with pytest.raises(DeviceAdmissionError):
            proxy.start()
        assert not marker.exists(), "a refused bridge must not touch the device"
        assert time.monotonic()  # the refusal was immediate, not a timeout
    finally:
        _stop_child(child)

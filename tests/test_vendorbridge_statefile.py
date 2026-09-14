"""Regression proof for FIX-5-T11 / C-128 -- the bridge's own `--state-file` writer.

C-128 is the unfixed sibling of FIX-5-T1. `publish_video_state` got the
mkstemp/fchmod/os.replace discipline, but `main()`'s `--state-file` writer still
did `state.with_suffix('.tmp').write_text(...)` followed by `.replace(...)`. Two
consequences, both reproduced on the real bridge:

  * a symlink planted at the fixed `.tmp` path chose the file that received the
    record -- an arbitrary-file-write primitive that also kept the victim's 0644;
  * the published record came out world-readable and carries `control.token`, the
    credential that authorises stopping the session.

Everything here runs on scratch paths under pytest's `tmp_path` or a short `/tmp`
fixture: no real device, no real `adb` (every CLI run passes a fake one), never
`/tmp/d200-local-bridge.pid`, never the real `/tmp/d200-adb-bridge.sock`, and HOME
is redirected so the admission lock is a scratch file too.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor"
BRIDGE_PATH = VENDOR / "d200-local-bridge.py"

sys.path.insert(0, str(VENDOR))

import d200_process_control as control  # noqa: E402

spec = importlib.util.spec_from_file_location("d200_local_bridge_statefile", BRIDGE_PATH)
bridge = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bridge
spec.loader.exec_module(bridge)

RECORD = {"pid": 1234, "control": {"kind": "bridge", "socket": "/tmp/x/control.sock",
                                   "token": "0" * 64}}
BUILD_ARTIFACTS = ("d200-zkgui-proxy", "d200-color-agent", "libd200-zkgui-preload.so")


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """No test here may touch the operator's real state root."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture()
def scratch():
    """A short absolute scratch directory, for AF_UNIX limits in the CLI tests."""
    directory = Path(tempfile.mkdtemp(prefix="vendorbridge-statefile-", dir="/tmp"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def victims(parent, name="state.pid"):
    """A victim file plus symlinks planted at both the destination and the temp path."""
    parent.mkdir(parents=True, exist_ok=True)
    victim = parent / "victim"
    victim.write_text("ORIGINAL\n")
    destination = parent / name
    (parent / "state.tmp").symlink_to(victim)
    destination.symlink_to(victim)
    return victim, destination, parent / "state.tmp"


def temp_siblings(parent, name="state.pid"):
    """Every entry in `parent` that is one of the writer's own temp files."""
    return sorted(entry.name for entry in parent.iterdir()
                  if entry.name.startswith(f".{name}."))


def test_a_planted_symlink_at_the_temp_path_is_not_followed(tmp_path):
    """The fixed `.tmp` sibling used to be the file that got overwritten."""
    parent = tmp_path / "scratch"
    victim, destination, planted_temp = victims(parent)
    destination.unlink()  # only the temp path is planted this time

    bridge.write_private_state_file(destination, json.dumps(RECORD))

    assert victim.read_text() == "ORIGINAL\n"
    assert not os.path.islink(destination)
    assert json.loads(destination.read_text()) == RECORD


def test_a_planted_symlink_at_the_destination_is_not_followed(tmp_path):
    """A symlink destination is dropped, never written through, so the victim keeps its bytes."""
    parent = tmp_path / "scratch"
    victim, destination, _planted_temp = victims(parent)

    bridge.write_private_state_file(destination, json.dumps(RECORD))

    assert victim.read_text() == "ORIGINAL\n"
    assert not os.path.islink(destination), "the planted symlink must not survive as the record"
    assert json.loads(destination.read_text()) == RECORD
    assert stat.S_ISREG(os.lstat(destination).st_mode)


def test_the_published_record_is_private_and_keeps_its_schema(tmp_path):
    """0644 carried the control token, which is the credential to stop the session."""
    parent = tmp_path / "scratch"
    parent.mkdir()
    destination = parent / "state.pid"

    bridge.write_private_state_file(destination, json.dumps(RECORD))

    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert sorted(json.loads(destination.read_text())) == ["control", "pid"]


def test_no_temp_sibling_survives_a_successful_write(tmp_path):
    parent = tmp_path / "scratch"
    parent.mkdir()
    destination = parent / "state.pid"

    bridge.write_private_state_file(destination, json.dumps(RECORD))

    assert temp_siblings(parent) == []
    assert sorted(entry.name for entry in parent.iterdir()) == ["state.pid"]


def test_a_failed_write_leaves_no_temp_behind(tmp_path):
    """A directory cannot be replaced, so the write fails after the temp exists."""
    destination = tmp_path / "scratch"
    destination.mkdir()

    with pytest.raises(OSError):
        control._write_private_file(destination, json.dumps(RECORD))

    assert temp_siblings(tmp_path) == []
    assert destination.is_dir(), "the foreign entry is left exactly as it was"


def test_the_teardown_read_never_follows_a_symlink(tmp_path):
    """`finally` must not read a planted path, and junk there is 'not ours', not an error."""
    parent = tmp_path / "scratch"
    parent.mkdir()
    victim = parent / "victim"
    victim.write_text(json.dumps(RECORD))
    planted = parent / "state.pid"
    planted.symlink_to(victim)

    assert bridge.own_state_record(planted) is None

    planted.unlink()
    planted.write_bytes(b"\xff\xfe not json")
    assert bridge.own_state_record(planted) is None

    planted.write_text(json.dumps(RECORD))
    assert bridge.own_state_record(planted) == RECORD


@pytest.fixture()
def deckless_bridge(tmp_path, scratch):
    """A copy of the real bridge with the build artifacts, driven by a fake adb."""
    target = scratch / "deckless"
    target.mkdir()
    for name in ("d200-local-bridge.py", "d200_process_control.py", "d200_video_stream.py"):
        shutil.copy(VENDOR / name, target / name)
    for name in BUILD_ARTIFACTS:
        (target / name).write_bytes(b"x" * 32)
    adb = target / "adb"
    adb.write_text("#!/bin/sh\necho 'error: no devices/emulators found' >&2\nexit 1\n")
    adb.chmod(0o755)
    return target


def run_bridge(deckless_bridge, scratch, state_file, *, home, adb_name="adb", timeout=60):
    return subprocess.run(
        [
            sys.executable, str(deckless_bridge / "d200-local-bridge.py"),
            "--socket", str(scratch / "nope.sock"),
            "--state-file", str(state_file),
            "--serial", "unused",
            "--adb", str(deckless_bridge / adb_name),
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=dict(os.environ, HOME=str(home)),
    )


def test_the_cli_does_not_write_through_a_planted_symlink(deckless_bridge, scratch, isolated_home):
    victim, destination, planted_temp = victims(scratch)

    result = run_bridge(deckless_bridge, scratch, destination, home=isolated_home)

    assert result.returncode == 1, result.stderr
    assert victim.read_text() == "ORIGINAL\n", "the victim file must keep its bytes"
    assert not destination.is_symlink()
    assert not destination.exists(), "the bridge removed the record it had written"


def test_the_cli_publishes_a_private_state_file_while_it_runs(deckless_bridge, scratch,
                                                              isolated_home):
    """The acceptance item the unit tests cannot cover: what the *running* bridge leaves."""
    adb = deckless_bridge / "slow-adb"
    marker = scratch / "adb-ran"
    # Only the first device command is slow, so the bridge is alive long enough to
    # observe its state file without paying that delay again during teardown.
    adb.write_text(
        "#!/bin/sh\n"
        f'[ -f "{marker}" ] || {{ touch "{marker}"; sleep 2; }}\n'
        "echo 'error: no devices/emulators found' >&2\nexit 1\n"
    )
    adb.chmod(0o755)
    destination = scratch / "state.pid"
    process = subprocess.Popen(
        [
            sys.executable, str(deckless_bridge / "d200-local-bridge.py"),
            "--socket", str(scratch / "nope.sock"),
            "--state-file", str(destination),
            "--serial", "unused",
            "--adb", str(adb),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=dict(os.environ, HOME=str(isolated_home)),
    )
    try:
        deadline = time.monotonic() + 10
        while not destination.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert destination.exists(), "the bridge never published its state file"
        published = json.loads(destination.read_text())
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600
        assert sorted(published) == ["control", "pid"]
        assert published["pid"] == process.pid
        assert temp_siblings(scratch) == []
    finally:
        process.wait(timeout=30)
    assert "Traceback" not in process.stdout.read()


# --- C-139: an unusable destination is one reported line, not a traceback ---------

def test_a_directory_at_the_destination_is_reported_not_followed(tmp_path):
    """`_state_kind` classifies a directory as 'foreign', and `unlink()` cannot drop one."""
    parent = tmp_path / "scratch"
    parent.mkdir()
    destination = parent / "state.pid"
    destination.mkdir()

    with pytest.raises(bridge.StateFileError) as raised:
        bridge.write_private_state_file(destination, json.dumps(RECORD))

    assert "cannot be replaced" in str(raised.value)
    assert isinstance(raised.value, RuntimeError), "callers catching RuntimeError keep working"
    assert isinstance(raised.value.__cause__, OSError)
    assert destination.is_dir(), "the foreign entry is left exactly as it was"
    assert temp_siblings(parent) == []


@pytest.mark.parametrize("shape", ["directory", "missing-parent", "unwritable-parent",
                                  "parent-is-a-file", "unsearchable-parent"])
def test_the_cli_reports_an_unusable_state_path_in_one_line(deckless_bridge, scratch,
                                                           isolated_home, shape):
    """The state file is written before `transport.start()`, outside any handler.

    A raw traceback here also meant the bridge went on to stage to the deck even
    though it could not publish the record it was asked for. The last two shapes
    are the ones `_state_kind` could not classify: its `lstat` failure check named
    only `FileNotFoundError`, so a non-directory parent entry (`ENOTDIR`) and an
    unsearchable one (`EACCES`) escaped as tracebacks -- from this writer and from
    the teardown read in `own_state_record`.
    """
    state_file = scratch / "state.pid"
    restore = None
    blocker = None
    if shape == "directory":
        state_file.mkdir()
    elif shape == "missing-parent":
        state_file = scratch / "nope" / "state.pid"
    elif shape == "parent-is-a-file":
        blocker = scratch / "not-a-directory"
        blocker.write_text("not a directory")
        state_file = blocker / "state.pid"
    elif shape == "unsearchable-parent":
        parent = scratch / "locked"
        parent.mkdir()
        parent.chmod(0o000)
        restore = parent
        state_file = parent / "state.pid"
    else:
        parent = scratch / "ro"
        parent.mkdir()
        parent.chmod(0o500)
        restore = parent
        state_file = parent / "state.pid"

    try:
        result = run_bridge(deckless_bridge, scratch, state_file, home=isolated_home)
    finally:
        if restore is not None:
            restore.chmod(0o700)

    assert result.returncode == 1, result.stderr
    assert "bridge_state_file_failed" in result.stderr
    assert "Traceback" not in result.stderr
    assert "bridge_stage_failed" not in result.stderr, (
        "the record could not be published, so the bridge must refuse before any device effect"
    )
    if shape == "directory":
        assert state_file.is_dir()
    if blocker is not None:
        assert blocker.read_text() == "not a directory", "the blocker must be left alone"

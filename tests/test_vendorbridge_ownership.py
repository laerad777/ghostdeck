"""Ownership-identity regression proof for FIX-5-T8 / C-015.

Two halves of the same defect in `vendor/d200_process_control.publish_video_state`,
which runs inside the media send loop:

* **pid reuse.** `_pid_alive(pid)` is `os.kill(pid, 0)`, so a recycled pid answered
  "the owner is alive" for a process that no longer exists, and the publication of
  the *current* player was refused.
* **raising into the send loop.** Every internal failure (a live foreign owner, an
  unwritable or foreign destination) raised `RuntimeError` out of `on_progress`,
  which `VideoStream.send` calls on every FRAME batch.

Ownership is now bound to the FIX-1-T8 identity pair (pid **plus** process start
time) in a 0600 sidecar beside the record, and a non-claim publication skips the
write instead of raising. Reads and writes a temp state path only: no device, no
adb, no real `/tmp/d200-color-host.json`, no real `~/.ghostdeck/state.json`.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vendor"))

import d200_process_control as control
from d200_process_control import PID_MAX, _process_start_time, publish_video_state


@pytest.fixture()
def state_path():
    directory = Path(tempfile.mkdtemp(prefix="vendorbridge-owner-", dir="/tmp"))
    try:
        yield directory / "host.json"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _live_stray():
    """A real live process that is not this one, so 'foreign' is genuine."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


def _dead_pid():
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=30)
    return process.pid


def test_the_identity_oracle_agrees_with_play_py(tmp_path):
    """Same shape and same rules as src/ghostdeck/play.py's `_probe_start_time`."""
    live, reason = _process_start_time(os.getpid())
    assert reason is None and isinstance(live, str) and live.strip()
    assert live == " ".join(live.split())
    assert _process_start_time(_dead_pid()) == (None, None)
    # An out-of-range pid is never handed to ps.
    assert _process_start_time(2 ** 31) == (None, None)
    assert _process_start_time(True) == (None, None)


def test_publishing_binds_the_record_owner_to_pid_and_start_time(state_path):
    publish_video_state({"phase": "active", "pid": os.getpid(), "source": "x"}, claim=True,
                        state_path=state_path)
    sidecar = state_path.with_name(state_path.name + control.OWNER_SUFFIX)
    recorded = json.loads(sidecar.read_text())
    assert recorded["pid"] == os.getpid()
    assert recorded["lstart"] == _process_start_time(os.getpid())[0]
    assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600
    # The published record's own schema is untouched.
    assert json.loads(state_path.read_text()) == {"phase": "active", "pid": os.getpid(), "source": "x"}


def test_a_live_foreign_owner_makes_the_publication_skip(state_path):
    """The C-015 raise-into-the-send-loop half: skip, do not raise, do not clobber."""
    stray = _live_stray()
    try:
        original = json.dumps({"phase": "active", "pid": stray.pid})
        state_path.write_text(original)
        returned = publish_video_state({"phase": "active", "pid": os.getpid()}, state_path=state_path)
        assert returned["pid"] == os.getpid(), "the caller keeps its own state"
        assert state_path.read_text() == original, "the live owner's record must survive"
    finally:
        stray.kill()
        stray.wait(timeout=30)


def test_a_recycled_pid_is_not_mistaken_for_the_recorded_owner(state_path):
    """The C-015 pid-reuse half, which the old `os.kill(pid, 0)` test got wrong.

    The record names a *live* pid (the stray here stands in for the recycled pid),
    but the sidecar's start time is not that process's, so the recorded owner is
    provably gone and the record is stale: the publication must proceed.
    """
    stray = _live_stray()
    try:
        state_path.write_text(json.dumps({"phase": "active", "pid": stray.pid}))
        state_path.with_name(state_path.name + control.OWNER_SUFFIX).write_text(
            json.dumps({"pid": stray.pid, "lstart": "Thu Jan  1 00:00:00 1970"})
        )
        publish_video_state({"phase": "active", "pid": os.getpid()}, state_path=state_path)
        assert json.loads(state_path.read_text())["pid"] == os.getpid()
    finally:
        stray.kill()
        stray.wait(timeout=30)


def test_a_stale_record_whose_pid_is_gone_is_replaced(state_path):
    state_path.write_text(json.dumps({"phase": "active", "pid": _dead_pid()}))
    publish_video_state({"phase": "active", "pid": os.getpid()}, state_path=state_path)
    assert json.loads(state_path.read_text())["pid"] == os.getpid()


def test_an_unanswerable_ps_makes_the_publication_skip_not_raise(state_path, monkeypatch):
    """Identity that cannot be determined is never treated as 'not ours'."""
    stray = _live_stray()
    try:
        original = json.dumps({"phase": "active", "pid": stray.pid})
        state_path.write_text(original)
        state_path.with_name(state_path.name + control.OWNER_SUFFIX).write_text(
            json.dumps({"pid": stray.pid, "lstart": "Fri Sep 11 00:00:00 2026"})
        )
        monkeypatch.setattr(control, "_process_start_time", lambda pid: (None, "ps could not be run"))
        publish_video_state({"phase": "active", "pid": os.getpid()}, state_path=state_path)
        assert state_path.read_text() == original
    finally:
        stray.kill()
        stray.wait(timeout=30)


def test_a_garbage_sidecar_does_not_raise(state_path):
    stray = _live_stray()
    try:
        original = json.dumps({"phase": "active", "pid": stray.pid})
        state_path.write_text(original)
        sidecar = state_path.with_name(state_path.name + control.OWNER_SUFFIX)
        for junk in ("{", "not json", "[1, 2]", "null", "42", '{"pid": 2147483648}', '{"pid": 12, "lstart": 7}'):
            sidecar.write_text(junk)
            publish_video_state({"phase": "active", "pid": os.getpid()}, state_path=state_path)
            assert state_path.read_text() == original, f"junk identity {junk!r} clobbered a live owner"
    finally:
        stray.kill()
        stray.wait(timeout=30)


def test_identity_is_probed_once_per_pid_not_once_per_publication(state_path, monkeypatch):
    """This runs at most once a second inside the send loop, so it must not spawn ps each time."""
    calls = []
    real = control._process_start_time

    def counted(pid):
        calls.append(pid)
        return real(pid)

    monkeypatch.setattr(control, "_process_start_time", counted)
    for _ in range(5):
        publish_video_state({"phase": "active", "pid": os.getpid()}, state_path=state_path)
    assert calls == [os.getpid()]


def test_a_claim_takes_the_record_over_from_a_live_owner(state_path):
    stray = _live_stray()
    try:
        state_path.write_text(json.dumps({"phase": "active", "pid": stray.pid}))
        publish_video_state({"phase": "active", "pid": os.getpid()}, claim=True, state_path=state_path)
        assert json.loads(state_path.read_text())["pid"] == os.getpid()
        sidecar = json.loads(state_path.with_name(state_path.name + control.OWNER_SUFFIX).read_text())
        assert sidecar["pid"] == os.getpid()
    finally:
        stray.kill()
        stray.wait(timeout=30)


@pytest.mark.parametrize("pid", [PID_MAX, 2147483648, 10 ** 20, -1, 0, True, "42", None, 42.0])
def test_no_hostile_recorded_pid_can_raise(state_path, pid):
    state_path.write_text(json.dumps({"phase": "active", "pid": pid}))
    publish_video_state({"phase": "active", "pid": os.getpid()}, claim=True, state_path=state_path)
    assert json.loads(state_path.read_text())["pid"] == os.getpid()


def test_no_hostile_destination_can_raise_from_a_non_claim_publication(state_path):
    """This call is inside the send loop, so every path here must return, not raise."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    # A directory that cannot be unlinked: the file cannot be taken over.
    state_path.mkdir()
    publish_video_state({"phase": "active", "pid": os.getpid()}, state_path=state_path)
    assert state_path.is_dir(), "a foreign destination must be left exactly as it was"
    # A writable record plus an unwritable directory entry point: skip, do not raise.
    state_path.rmdir()
    state_path.write_text(json.dumps({"phase": "active", "pid": _dead_pid()}))
    os.chmod(state_path.parent, 0o500)
    try:
        publish_video_state({"phase": "active", "pid": os.getpid()}, state_path=state_path)
    finally:
        os.chmod(state_path.parent, 0o700)
    assert json.loads(state_path.read_text())["pid"] != os.getpid(), "an unwritable root must not be a lie"


def test_a_claim_still_reports_an_unpublishable_destination(state_path):
    """The startup takeover keeps raising, so a broken state root is visible at startup."""
    state_path.mkdir()
    with pytest.raises(RuntimeError, match="state path is not an owned regular file"):
        publish_video_state({"phase": "active", "pid": os.getpid()}, claim=True, state_path=state_path)

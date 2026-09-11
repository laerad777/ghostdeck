"""Host-side state-file and lock-path hardening: C-111, C-117, C-121, C-126, C-132.

These are the open findings against `vendor/d200_process_control.py` that FIX-5 owns
and had not yet covered:

  C-132  nothing pinned that the published record is written through a *unique*
         temp file, so reverting `_write_private_file` to a predictable
         `<state>.tmp` sibling (planted symlink followed, victim clobbered) kept
         the suite green.
  C-117  the `claim=True` precondition (`phase` must be "active") had no coverage;
         deleting the raise kept the suite green.
  C-121  a *directory* at the published path made every non-claim publication a
         silent no-op that still looked like success, and disabled the ownership
         guard, while the in-code comment claimed the inode was dropped.
  C-126  the identity oracle pinned only `LC_ALL=C`, not `TZ=UTC`, so a timezone
         change between the recorded owner and the check made the guard fail open
         and overwrite a live owner's record. (`A-143` is the same finding.)
  C-111  the device-admission lock opened a fixed path with no `O_NOFOLLOW`, then
         truncated and wrote it: a planted symlink made every bridge start destroy
         an operator file of the attacker's choosing.

Device-free: only `ps`, scratch paths under pytest's `tmp_path`, and a redirected
HOME (so the real `~/.ghostdeck/device-admission.lock` can never be opened). No adb,
no device, no `/tmp/d200-*` path.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor"
sys.path.insert(0, str(VENDOR))

import d200_process_control as control  # noqa: E402
from d200_process_control import (  # noqa: E402
    ADMISSION_LOCK_NAME, DeviceAdmissionError, managed_device_admission,
    publish_video_state,
)

CLAIM = "playback claim must be active"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("TZ", raising=False)
    return home


@pytest.fixture()
def state_path():
    directory = Path(tempfile.mkdtemp(prefix="vendorbridge-hoststate-", dir="/tmp"))
    try:
        yield directory / "host.json"
    finally:
        import shutil

        shutil.rmtree(directory, ignore_errors=True)


def live_stray():
    """A real live process that is not this one, so 'foreign owner' is genuine."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


def start_time_under(tz, pid, monkeypatch):
    """`ps -o lstart=` for `pid` as the oracle reports it under `tz`."""
    if tz is None:
        monkeypatch.delenv("TZ", raising=False)
    else:
        monkeypatch.setenv("TZ", tz)
    return control._process_start_time(pid)[0]


# --------------------------------------------------------------------------- C-132


def test_the_published_record_never_writes_through_a_predictable_temp_sibling(state_path):
    """C-132: a planted `<state>.tmp` must not become the file that receives the record.

    The name is the one a `path.with_name(path.name + '.tmp')` writer would use, so
    this assertion is what fails if the writer ever loses its unique mkstemp name.
    """
    victim = state_path.with_name("victim.txt")
    victim.write_text("SECRET-VICTIM-DATA\n")
    predictable = state_path.with_name(state_path.name + ".tmp")
    predictable.symlink_to(victim)
    state_path.symlink_to(victim)  # and a second symlink at the destination itself

    publish_video_state({"phase": "active", "pid": os.getpid()}, claim=True,
                        state_path=state_path)

    assert victim.read_text() == "SECRET-VICTIM-DATA\n", "the victim file was written through"
    assert not predictable.exists() or predictable.is_symlink()
    assert not os.path.islink(state_path), "the published path must be a real file"
    assert stat.S_ISREG(state_path.lstat().st_mode)
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    assert json.loads(state_path.read_text())["pid"] == os.getpid()


def test_no_writer_temp_file_survives_a_publication(state_path):
    publish_video_state({"phase": "active", "pid": os.getpid()}, claim=True,
                        state_path=state_path)
    leftovers = [entry.name for entry in state_path.parent.iterdir()
                 if entry.name.startswith("." + state_path.name + ".")]
    assert leftovers == []


# --------------------------------------------------------------------------- C-117


@pytest.mark.parametrize("phase", ["terminal", "idle", "", None, 0])
def test_a_claim_requires_an_active_phase_and_changes_nothing(state_path, phase):
    """C-117: the precondition had no coverage; deleting it kept the suite green."""
    state_path.write_text(json.dumps({"phase": "terminal", "pid": 4242}))
    before = state_path.read_bytes()

    with pytest.raises(RuntimeError) as refused:
        publish_video_state({"phase": phase, "pid": os.getpid()}, claim=True,
                            state_path=state_path)

    assert CLAIM in str(refused.value)
    assert state_path.read_bytes() == before, "a refused claim must not touch the record"


def test_a_claim_with_the_active_phase_still_publishes(state_path):
    publish_video_state({"phase": "active", "pid": os.getpid()}, claim=True, state_path=state_path)
    assert json.loads(state_path.read_text())["phase"] == "active"


# --------------------------------------------------------------------------- C-121


def test_a_directory_at_the_state_path_is_reported_not_reported_as_success(state_path, capsys):
    """C-121: `unlink()` cannot drop a directory, so nothing is published.

    The call still must not raise into the media send loop (FIX-5-T8's contract),
    but it must not look like a successful publication either.
    """
    state_path.mkdir()
    capsys.readouterr()

    returned = publish_video_state({"phase": "active", "pid": os.getpid()},
                                   state_path=state_path)

    assert returned["phase"] == "active", "the caller's own state is still returned"
    assert state_path.is_dir(), "a foreign directory is never removed"
    diagnostic = capsys.readouterr().err
    assert "statePublicationSkipped" in diagnostic, (
        "a publication that did not happen must be reported, not implied as success")
    assert "regular file" in diagnostic


def test_a_claim_on_a_directory_still_raises(state_path):
    state_path.mkdir()
    with pytest.raises(RuntimeError) as refused:
        publish_video_state({"phase": "active", "pid": os.getpid()}, claim=True,
                            state_path=state_path)
    assert "owned regular file" in str(refused.value)
    assert state_path.is_dir()


def test_a_symlink_and_a_fifo_are_still_replaced(state_path):
    """The branch that *can* drop the inode keeps publishing a fresh file."""
    victim = state_path.with_name("victim.txt")
    victim.write_text("KEEP\n")
    state_path.symlink_to(victim)

    publish_video_state({"phase": "active", "pid": os.getpid()}, claim=True, state_path=state_path)

    assert victim.read_text() == "KEEP\n"
    assert stat.S_ISREG(state_path.lstat().st_mode)


# --------------------------------------------------------------------------- C-126


def test_the_identity_oracle_is_timezone_invariant(monkeypatch):
    """C-126/A-143: the recorded string is compared later, so its zone must be pinned."""
    readings = [start_time_under(tz, os.getpid(), monkeypatch)
                for tz in ("UTC", "Asia/Seoul", "Etc/GMT-9", None)]

    assert all(readings), readings
    assert len(set(readings)) == 1, f"the oracle answers differently per zone: {readings}"


@pytest.mark.parametrize("recorded_zone,checked_zone",
                         [("UTC", "UTC"), ("UTC", "Asia/Seoul"), ("Asia/Seoul", "UTC"),
                          ("UTC", "Etc/GMT-9"), ("Etc/GMT-9", "UTC")])
def test_a_zone_change_between_record_and_check_cannot_clobber_a_live_owner(
        state_path, monkeypatch, recorded_zone, checked_zone):
    """The fail-open itself: a live owner's record must survive a TZ difference."""
    stray = live_stray()
    try:
        recorded = start_time_under(recorded_zone, stray.pid, monkeypatch)
        assert recorded
        state_path.write_text(json.dumps({"phase": "active", "pid": stray.pid}))
        state_path.with_name(state_path.name + control.OWNER_SUFFIX).write_text(
            json.dumps({"pid": stray.pid, "lstart": recorded}))

        start_time_under(checked_zone, os.getpid(), monkeypatch)
        publish_video_state({"phase": "active", "pid": os.getpid()}, state_path=state_path)

        assert json.loads(state_path.read_text())["pid"] == stray.pid, (
            "the publication overwrote a live owner's record after a zone change")
    finally:
        stray.kill()
        stray.wait(timeout=30)


def test_a_dead_owner_is_still_replaced_after_a_zone_change(state_path, monkeypatch):
    """The guard must stay a *liveness* check, not a permanent refusal."""
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=30)
    state_path.write_text(json.dumps({"phase": "active", "pid": dead.pid}))
    state_path.with_name(state_path.name + control.OWNER_SUFFIX).write_text(
        json.dumps({"pid": dead.pid, "lstart": "Thu Jan  1 00:00:00 1970"}))

    start_time_under("Asia/Seoul", os.getpid(), monkeypatch)
    publish_video_state({"phase": "active", "pid": os.getpid()}, state_path=state_path)

    assert json.loads(state_path.read_text())["pid"] == os.getpid()


# --------------------------------------------------------------------------- C-111


@pytest.fixture()
def lock_path(isolated_home):
    return isolated_home / ".ghostdeck" / ADMISSION_LOCK_NAME


def test_a_symlink_at_the_lock_path_is_never_written_through(lock_path):
    """C-111: the lock is opened, fchmod'd, ftruncated and written on every start."""
    lock_path.parent.mkdir(parents=True)
    victim = lock_path.parent.parent / "operator-notes.txt"
    victim.write_text("OPERATOR DATA\n")
    lock_path.symlink_to(victim)

    with managed_device_admission(lock_path=lock_path):
        adopted = lock_path.lstat()

    assert victim.read_text() == "OPERATOR DATA\n", "the planted target was truncated"
    assert stat.S_ISREG(adopted.st_mode), "the lock must be this process's own regular file"
    assert stat.S_IMODE(adopted.st_mode) == 0o600
    assert adopted.st_uid == os.geteuid()


def test_a_fifo_at_the_lock_path_is_replaced_not_adopted(lock_path):
    lock_path.parent.mkdir(parents=True)
    os.mkfifo(lock_path)

    with managed_device_admission(lock_path=lock_path):
        assert stat.S_ISREG(lock_path.lstat().st_mode)


def test_a_directory_at_the_lock_path_refuses_with_a_typed_error(lock_path):
    lock_path.parent.mkdir(parents=True)
    lock_path.mkdir()

    with pytest.raises(DeviceAdmissionError):
        with managed_device_admission(lock_path=lock_path):
            pytest.fail("a directory must never be adopted as the lock file")

    assert lock_path.is_dir(), "a foreign directory is left for the operator"


def test_a_normal_admission_is_unchanged(lock_path):
    with managed_device_admission(lock_path=lock_path):
        recorded = json.loads(lock_path.read_text())
        assert recorded["pid"] == os.getpid()
    with managed_device_admission(lock_path=lock_path):
        assert json.loads(lock_path.read_text())["pid"] == os.getpid()

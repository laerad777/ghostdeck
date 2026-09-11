"""A-125: `ghostdeck quit` must never signal a pid it cannot prove is our virtual-HID keeper.

The vhid record used to be believed on `pid_alive()` alone, so a recycled pid was SIGTERMed and
SIGKILLed and `quit` exited 0 as if it had cleaned up its own keeper (finding A-125, a live kill by
FINDER-A). `play.py` already carries the identity discipline this file pins for `vhid.py`: the pid is
recorded together with its `ps -o lstart=` start time, and the record is only ours while the live
start time still matches.

Every test runs with a temp `HOME`, and the only processes ever signalled are ones this file started
with `subprocess.Popen`. `usb.virtual_hid_enumerated()` is stubbed so a real D200 attached to the
host is never enumerated -- `vhid.status()` reaches it on the `ghostdeck status` path.
"""

from __future__ import annotations

import fcntl
import json
import multiprocessing as mp
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from ghostdeck import HID_PID, HID_VID
from ghostdeck import state, usb, vhid


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A temp ~/.ghostdeck, so no test can read or write the real one."""
    path = tmp_path / ".ghostdeck"
    for name in ("HOME", "HOME_DIR"):
        monkeypatch.setattr(state, name, path)
    for name, value in (
        ("STATE_PATH", path / "state.json"),
        ("PLUGIN_DIR", path / "plugins"),
        ("BIN_DIR", path / "bin"),
    ):
        monkeypatch.setattr(state, name, value)
    # Never touch the bus: a real deck may be attached to this host.
    monkeypatch.setattr(usb, "virtual_hid_enumerated", lambda: False)
    return path


def _sleep():
    """A process this test owns. Never signal a process you did not start."""
    return subprocess.Popen(["/bin/sleep", "60"])


def _kill(*procs):
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def _gone(pid):
    """True when the pid has left the process table.

    `Popen.returncode` cannot prove a kill here: `quit()` reaps through `state.pid_alive()`'s
    `waitpid`, and subprocess maps a lost child status to returncode 0 rather than -15. The process
    table is the honest witness.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


class _NoSpawnSubprocess:
    """`subprocess` with only `Popen` replaced.

    Patching `vhid.subprocess.Popen` directly would patch the global module and break
    `subprocess.run`, which the identity probe itself uses.
    """

    def __init__(self, spawn):
        self._spawn = spawn
        self.calls = []

    def __getattr__(self, name):
        return getattr(subprocess, name)

    def run(self, *args, **kwargs):
        return subprocess.run(*args, **kwargs)

    def Popen(self, *args, **kwargs):
        self.calls.append(args)
        return self._spawn(*args, **kwargs)


def _seed_vhid(pid):
    state.save({"vhid_pid": pid, "vhid": {"pid": pid, "experimental": True, "status": "up"}})


# --------------------------------------------------------------------------- the probe itself


def test_the_probe_reads_the_live_start_time(home):
    proc = _sleep()
    try:
        lstart, reason = vhid._probe_start_time(proc.pid)
        assert reason is None
        assert lstart
        assert "\n" not in lstart
    finally:
        _kill(proc)


def test_the_probe_keeps_cannot_answer_distinct_from_no_such_process(home):
    """A missing `ps` is `reason`, a dead pid is `(None, None)`; `quit` treats them differently."""
    proc = _sleep()
    try:
        pid = proc.pid
    finally:
        _kill(proc)
    assert vhid._probe_start_time(pid) == (None, None)

    original = vhid.subprocess.run

    def _boom(*_args, **_kwargs):
        raise OSError("no ps on this host")

    try:
        vhid.subprocess.run = _boom
        lstart, reason = vhid._probe_start_time(os.getpid())
    finally:
        vhid.subprocess.run = original
    assert lstart is None
    assert reason and "ps" in reason


def test_the_identity_probe_is_timezone_stable(home, monkeypatch):
    """A-124 mirrored for vhid: pinning only LC_ALL is not enough.

    On this host `ps -o lstart=` renders in the caller's zone, so a record written under one TZ and
    re-read under another used to compare unequal -- which is how the identity check misfires.
    """
    proc = _sleep()
    try:
        monkeypatch.setenv("TZ", "Asia/Seoul")
        seoul = vhid._probe_start_time(proc.pid)
        monkeypatch.setenv("TZ", "UTC")
        utc = vhid._probe_start_time(proc.pid)
        assert seoul == utc, (seoul, utc)
        assert seoul[0] is not None and seoul[1] is None
    finally:
        _kill(proc)


def test_a_timezone_change_between_record_and_quit_keeps_the_keeper_reachable(home, monkeypatch):
    """The end-to-end A-124 shape: a TZ difference must not turn our keeper into a stranger."""
    proc = _sleep()
    try:
        monkeypatch.setenv("TZ", "Asia/Seoul")
        _seed_vhid(proc.pid)
        vhid._record_identity(proc.pid)
        written = json.loads(vhid._identity_path().read_text())
        assert written["pid"] == proc.pid
        monkeypatch.setenv("TZ", "UTC")
        assert vhid._keeper_identity(proc.pid)[0] is True
        assert vhid.quit()["status"] == "down"
        assert _gone(proc.pid), "the keeper was dropped just because TZ changed"
    finally:
        _kill(proc)


# --------------------------------------------------------------------------- A-125: quit


def test_quit_never_signals_a_pid_it_cannot_prove_is_ours(home):
    """The A-125 repro: a live unrelated pid in vhid_pid, with no recorded identity for it."""
    victim = _sleep()
    try:
        _seed_vhid(victim.pid)
        with pytest.raises(RuntimeError, match="cannot verify"):
            vhid.quit()
        assert victim.poll() is None, "an unrelated process was signalled"
        # Its fate is unknown, so the only handle on it is preserved rather than erased.
        assert state.load()["vhid_pid"] == victim.pid
    finally:
        _kill(victim)


def test_quit_does_not_signal_a_pid_whose_recorded_identity_differs(home):
    """A sidecar mismatch must never signal, and must not discard an unaccounted-for live process.

    The contract changed with A-145. It used to answer "not ours" from the mismatch alone, which
    cleared the record and exited 0 while leaving a live process nobody could reach again. A
    mismatch is not proof (the sidecar can be stale), so the honest answer is "cannot verify":
    nothing is signalled and the record that is the only handle on the process is kept.
    """
    victim = _sleep()
    other = _sleep()
    try:
        vhid._write_identity({"pid": other.pid, "lstart": "Thu Jan  1 00:00:00 1970"})
        _seed_vhid(victim.pid)
        with pytest.raises(RuntimeError, match="cannot verify"):
            vhid.quit()
        assert victim.poll() is None
        assert other.poll() is None
        assert state.load()["vhid_pid"] == victim.pid
        assert vhid._identity_path().exists()
    finally:
        _kill(victim, other)


def test_quit_signals_the_keeper_it_recorded(home):
    """The positive path: a process whose identity we recorded is still cleaned up."""
    keeper = _sleep()
    pid = keeper.pid
    try:
        _seed_vhid(pid)
        vhid._record_identity(pid)
        assert vhid._keeper_identity(pid)[0] is True
        assert vhid.quit()["status"] == "down"
        assert _gone(pid), "our own keeper was not stopped"
        assert state.load()["vhid_pid"] is None
        assert not vhid._identity_path().exists()
    finally:
        _kill(keeper)


def test_quit_clears_a_dead_record_without_needing_an_identity(home):
    """The common stale-state case stays quiet: a dead pid is classifiable, not an error."""
    proc = _sleep()
    pid = proc.pid
    _kill(proc)
    _seed_vhid(pid)
    assert vhid.quit()["status"] == "down"
    assert state.load()["vhid_pid"] is None


# --------------------------------------------------------------------------- A-125: status / start


def test_status_reports_down_without_erasing_an_unverifiable_record(home):
    victim = _sleep()
    try:
        _seed_vhid(victim.pid)
        record = vhid.status()
        assert record["status"] == "down"
        assert record["pid"] == victim.pid
        assert state.load()["vhid_pid"] == victim.pid
        assert victim.poll() is None
    finally:
        _kill(victim)


def test_status_is_up_only_for_a_keeper_whose_identity_matches(home):
    me = os.getpid()
    _seed_vhid(me)
    assert vhid.status()["status"] == "down"  # live, but nothing proves it is ours
    vhid._record_identity(me)
    assert vhid.status()["status"] == "up"
    vhid._write_identity({"pid": me, "lstart": "Thu Jan  1 00:00:00 1970"})
    assert vhid.status()["status"] == "down"


def test_start_reuses_only_a_keeper_it_can_prove_is_ours(home, monkeypatch):
    """`start()` trusted any live pid too, so a recycled pid was adopted as 'the keeper'."""
    shim = _NoSpawnSubprocess(_unexpected_spawn)
    monkeypatch.setattr(vhid, "subprocess", shim)
    me = os.getpid()
    _seed_vhid(me)
    vhid._record_identity(me)
    assert vhid.start()["status"] == "up"
    assert shim.calls == []


def _unexpected_spawn(*_args, **_kwargs):
    raise AssertionError("start() spawned a keeper it should not have needed")


def test_start_does_not_adopt_a_live_pid_it_cannot_verify(home, monkeypatch):
    """A stranger holding the recorded pid must not be adopted, and must not be signalled."""

    class _WouldSpawn(Exception):
        pass

    def _refuse(*_args, **_kwargs):
        raise _WouldSpawn

    shim = _NoSpawnSubprocess(_refuse)
    monkeypatch.setattr(vhid, "subprocess", shim)
    victim = _sleep()
    try:
        _seed_vhid(victim.pid)
        with pytest.raises(_WouldSpawn):
            vhid.start()
        assert shim.calls, "start() adopted the stranger instead of spawning a real keeper"
        assert victim.poll() is None
    finally:
        _kill(victim)


def test_write_vhid_records_identity_on_up_and_clears_it_on_down(home):
    me = os.getpid()
    vhid._write_vhid(me, visible=False, iohid=False, status="up")
    recorded = vhid._load_identity()
    assert recorded == {"pid": me, "lstart": vhid._probe_start_time(me)[0]}
    vhid._write_vhid(None, visible=False, iohid=False, status="down")
    assert vhid._load_identity() is None


def test_the_identity_sidecar_is_0600_and_leaves_no_temp_file(home):
    proc = _sleep()
    try:
        vhid._record_identity(proc.pid)
        sidecar = vhid._identity_path()
        assert sidecar.name == "vhid.pid"
        assert stat_mode(sidecar) == 0o600
        assert not [n for n in os.listdir(home) if n.startswith(".vhid.pid.")]
    finally:
        _kill(proc)


def stat_mode(path):
    return os.stat(path).st_mode & 0o777


def test_the_recorded_identity_rejects_a_junk_sidecar(home):
    """A malformed sidecar must be "no identity", never a false positive."""
    for payload in ("{not json", "[]", '{"pid": "1", "lstart": 0}', '{"pid": 1}', '{"lstart": "x"}'):
        vhid._identity_path().parent.mkdir(parents=True, exist_ok=True)
        vhid._identity_path().write_text(payload)
        assert vhid._load_identity() is None


def _state_lock_is_held() -> bool:
    """True when this process already holds the state lock.

    `flock` treats two descriptors for the same file as independent, so a second descriptor that is
    *denied* proves the first is still held. That makes "was the read inside the lock?" a
    deterministic question instead of a timing one.
    """
    state.ensure_dirs()
    fd = os.open(state.STATE_PATH.parent / state.LOCK_NAME, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def test_write_vhid_reads_and_writes_inside_the_state_lock(home, monkeypatch):
    """A-104, deterministically: the load must happen under the same lock as the write.

    `save()` takes the lock, but `_write_vhid` used to call `load()` *before* it, so two writers
    could both read, both mutate, and the later save discard the other's update. Asserting on a
    concurrent race would be a timing test that passes on both revisions (with a constant victim
    value, every stale snapshot already contains the committed value), so the invariant is asserted
    directly instead: at the moment `_write_vhid` reads the document it must already hold the lock.
    """
    waits = []
    real_load = state.load

    def recording_load():
        waits.append(_state_lock_is_held())
        return real_load()

    monkeypatch.setattr(state, "load", recording_load)
    vhid._write_vhid(4242, visible=False, iohid=False, status="up")
    assert waits, "_write_vhid did not read the state document at all"
    assert all(waits), f"a state read ran outside the lock: {waits}"


def test_write_vhid_does_not_discard_a_concurrent_writers_update(home, monkeypatch):
    """A-104, on the real failure: another writer's whole update is lost.

    A second writer commits `play_pid` in the window between `_write_vhid`'s read and its write.
    Under the fix that window cannot exist (the read holds the lock), so both updates survive; with
    the read outside the lock the stale snapshot clobbers `play_pid` back to None.
    """
    real_load = state.load
    started: list = []

    def load_then_race():
        snapshot = real_load()
        if not started:
            # Marked before the worker starts, so the worker's own `load()` is inert.
            started.append(True)
            thread = threading.Thread(target=lambda: state.update(play_pid=9999), daemon=True)
            started.append(thread)
            thread.start()
            # Give the other writer every chance to commit while we are "between" read and write.
            thread.join(timeout=0.75)
        return snapshot

    monkeypatch.setattr(state, "load", load_then_race)
    vhid._write_vhid(4242, visible=False, iohid=False, status="up")
    worker = started[1]
    worker.join(timeout=10)
    assert not worker.is_alive(), "the other writer never completed"

    data = state.load()
    assert data["play_pid"] == 9999, "a concurrent writer's update was silently discarded"
    assert data["play"]["pid"] == 9999
    # ...and the vhid write itself survived the other direction.
    assert data["vhid"]["pid"] == data["vhid_pid"] == 4242


def test_concurrent_vhid_writers_agree_and_never_tear(home):
    """Two processes writing the record at once: the spellings agree and the JSON is never torn."""
    from ghostdeck import vhid as vhid_mod

    def writer(pid):
        for _ in range(30):
            vhid_mod._write_vhid(pid, visible=False, iohid=False, status="up")

    ctx = mp.get_context("fork")
    procs = [ctx.Process(target=writer, args=(701,)), ctx.Process(target=writer, args=(702,))]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=60)
    assert [proc.exitcode for proc in procs] == [0, 0]

    data = state.load()
    assert data["vhid"]["pid"] in (701, 702)
    assert data["vhid"]["pid"] == data["vhid_pid"], "the two spellings disagree"
    assert data["vhid"]["status"] == "up"
    assert data["vhid"]["iohid"] is False and data["vhid_iohid"] is False
    assert data["vhid"]["visible"] is False and data["vhid_visible"] is False
    assert data["vhid_vid"] == HID_VID
    assert data["vhid_pid_usb"] == HID_PID
    # Never mid-write: whatever is on disk is a whole document.
    json.loads(vhid_mod._identity_path().parent.joinpath("state.json").read_text())


def test_write_vhid_keeps_the_flat_ids_it_owns(home):
    """`vhid_vid`/`vhid_pid_usb` have no nested counterpart, so they must still be written (A-104)."""
    vhid._write_vhid(4242, visible=True, iohid=True, status="up")
    data = state.load()
    assert data["vhid_vid"] == HID_VID
    assert data["vhid_pid_usb"] == HID_PID
    assert data["vhid"]["visible"] is True and data["vhid_visible"] is True
    assert data["vhid"]["iohid"] is True and data["vhid_iohid"] is True


def test_write_vhid_does_not_disturb_another_section(home):
    """A merge, not a clobber: the play section is not part of the vhid write."""
    state.save({"play_pid": 4242, "play": {"pid": 4242}})
    vhid._write_vhid(5150, visible=False, iohid=False, status="up")
    data = state.load()
    assert data["play_pid"] == 4242 and data["play"]["pid"] == 4242
    assert data["vhid"]["pid"] == 5150


def test_is_up_agrees_with_status(home):
    me = os.getpid()
    _seed_vhid(me)
    assert vhid.is_up() is False
    vhid._record_identity(me)
    assert vhid.is_up() is True


# --------------------------------------------------------------------------- A-145
# A sidecar naming a DIFFERENT pid made `_keeper_identity` answer False without ever probing the
# state pid, so a read-only `status()` erased vhid_pid and the sidecar of a LIVE keeper that nothing
# then signalled, and `quit()` took the identical branch and left the keeper unreachable.


def test_a_stale_sidecar_cannot_erase_a_live_keepers_record(home):
    """A-145 acceptance: state names a live pid, the sidecar names another one."""
    keeper = _sleep()
    other = _sleep()
    try:
        _seed_vhid(keeper.pid)
        vhid._write_identity({"pid": other.pid, "lstart": "Thu Jan  1 00:00:00 1970"})
        record = vhid.status()
        assert record["status"] == "down"
        assert record["pid"] == keeper.pid
        assert "different pid" in (record.get("unverified") or "")
        # The whole point: the record is the only handle on a process whose fate is unknown.
        assert state.load()["vhid_pid"] == keeper.pid, "a live keeper's record was erased"
        assert vhid._identity_path().exists()
        assert keeper.poll() is None and other.poll() is None
    finally:
        _kill(keeper, other)


def test_quit_refuses_to_discard_a_live_pid_behind_a_stale_sidecar(home):
    """Same state, on the mutating path: refuse and report rather than pretend to have cleaned up."""
    keeper = _sleep()
    other = _sleep()
    try:
        _seed_vhid(keeper.pid)
        vhid._write_identity({"pid": other.pid, "lstart": "Thu Jan  1 00:00:00 1970"})
        with pytest.raises(RuntimeError, match="cannot verify"):
            vhid.quit()
        assert keeper.poll() is None, "the keeper was signalled on a sidecar mismatch alone"
        assert other.poll() is None
        assert state.load()["vhid_pid"] == keeper.pid
    finally:
        _kill(keeper, other)


def test_a_sidecar_mismatch_is_still_discarded_when_that_pid_is_dead(home):
    """The bound on the change: a dead pid behind a mismatched sidecar is still classifiable.

    `_keeper_identity` may only answer False when `ps` ran and reports no such process -- otherwise
    the A-145 fix would leak records forever.
    """
    dead = _sleep()
    other = _sleep()
    pid = dead.pid
    _kill(dead)
    try:
        _seed_vhid(pid)
        vhid._write_identity({"pid": other.pid, "lstart": "Thu Jan  1 00:00:00 1970"})
        assert vhid._keeper_identity(pid)[0] is False
        assert vhid.quit()["status"] == "down"
        assert state.load()["vhid_pid"] is None
    finally:
        _kill(other)


def test_the_recorder_removes_a_stale_sidecar_it_cannot_replace(home, monkeypatch):
    """When the start time cannot be read, the sidecar must not be left naming another process."""
    me = os.getpid()
    vhid._record_identity(me)
    assert vhid._load_identity()["pid"] == me
    monkeypatch.setattr(vhid, "_probe_start_time", lambda pid: (None, "ps could not be run"))
    vhid._record_identity(os.getppid())
    assert vhid._load_identity() is None, "a sidecar naming another pid was left behind"


# --------------------------------------------------------------------------- A-136

def test_the_report_descriptor_has_exactly_one_definition():
    """Both virtual-HID bindings must hand the same bytes to IOHIDUserDeviceCreate (A-136).

    It was copied verbatim into `vhid.py` and `iohid.py` with nothing tying the copies together; an
    edit to one side would have made only the fallback binding wrong.
    """
    from ghostdeck import iohid

    assert vhid._DESCRIPTOR is iohid.REPORT_DESCRIPTOR
    assert len(vhid._DESCRIPTOR) == 25
    assert vhid._DESCRIPTOR.hex() == "0600ff0901a101150026ff00750895400901810209019102c0"
    assert "0x95, 0x40" not in Path(vhid.__file__).read_text(encoding="utf-8"), (
        "vhid.py carries a second copy of the descriptor again"
    )


# --------------------------------------------------------------------------- A-155
# `iohid` is the no-pyobjc binding, and it is macOS-only. `_cf()`/`_iokit()` used to load the
# frameworks with nothing catching the failure, so on any host without CoreFoundation/IOKit
# `iohid.create()` raised `OSError` instead of answering None -- which made the offline suite's own
# `test_iohid_create_does_not_raise` an ERROR (not a skip) on the `ubuntu-latest` CI job, i.e. that
# job was red by construction. The platform is a *value* now; these two tests are the two directions.


def test_create_answers_none_instead_of_raising_without_corefoundation(monkeypatch):
    """The non-Apple host, modelled: no `find_library` result and nothing at the framework path.

    Device-free and platform-independent: the two things a Linux runner lacks are replaced here, so
    the assertion can run on this macOS host -- which is the only platform the defect cannot show on.
    """
    from ghostdeck import iohid

    def _missing(_path):
        raise OSError(
            "libCoreFoundation.so.0: cannot open shared object file: No such file or directory"
        )

    monkeypatch.setattr(iohid, "find_library", lambda _name: None)
    monkeypatch.setattr(iohid.ctypes.cdll, "LoadLibrary", _missing)

    assert iohid._cf() is None
    assert iohid._iokit() is None
    assert iohid.create() is None, "create() must answer None, never raise, where there is no IOKit"
    iohid.pump(0)  # the run-loop helper must be inert too, not an `in_dll` TypeError


@pytest.mark.skipif(sys.platform != "darwin", reason="CoreFoundation is macOS-only")
def test_a_working_corefoundation_is_still_loaded(monkeypatch):
    """The other direction: the guard must not turn a working host into None (A-155).

    A `try/except` that swallowed too much would make the real macOS path silently stop working, and
    every test above would still pass because they all model the failure.
    """
    from ghostdeck import iohid

    cf = iohid._cf()
    assert cf is not None, "the framework loads on this host and must still be returned"
    assert iohid._iokit() is not None
    # And `create()` is unchanged on the platform it exists for: a device or the documented None.
    device = iohid.create()
    assert device is None or device

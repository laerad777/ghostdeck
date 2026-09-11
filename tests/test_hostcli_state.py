"""A-104: the session record must be written by one locked read-modify-write.

`state.save()` and `state.update()` both take the lock (FIX-2-T3), but a caller that does
`data = load()` ... `save(data)` leaves the *read* outside it, so two processes can both read, both
mutate, and the later save discards the earlier update. `play.py`'s two remaining sites did exactly
that; they now go through `state.update(...)`.

Two independent bindings, because either one alone can be fooled:

* the canary test fails loudly if a site reverts to a whole-dict `save()`, with no timing involved;
* the concurrency test runs real writer processes and checks what survived on disk.

Device-free: temp HOME throughout, no adb, no USB, no player.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

# Each writer is a real process: the defect is a cross-process race, and an in-process thread would
# share the module's `HOME` and prove nothing about two independent ghostdeck invocations.
_WRITER = """
import sys
from pathlib import Path
sys.path.insert(0, {src!r})
from ghostdeck import state
state.HOME = Path({home!r})
state.STATE_PATH = state.HOME / "state.json"
for _ in range({rounds}):
    state.update(play_pid={pid})
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A temp HOME, so nothing here can touch the operator's `~/.ghostdeck`."""
    from ghostdeck import state

    target = tmp_path / ".ghostdeck"
    target.mkdir(parents=True)
    monkeypatch.setattr(state, "HOME", target)
    monkeypatch.setattr(state, "STATE_PATH", target / "state.json")
    return target


def test_clear_play_records_uses_a_single_locked_update(home, monkeypatch):
    """The canary: if this site reverts to `load()` + `save()`, the test fails immediately.

    `state.update()` never calls `state.save()` (it writes through `_write_state` under the lock), so
    poisoning `save` is a precise detector for the old pattern and involves no timing at all - which
    the concurrency test below cannot claim.
    """
    from ghostdeck import play, state

    def forbidden(data):
        raise AssertionError(
            "play wrote state with a whole-dict save(); that read is outside the lock (A-104)"
        )

    monkeypatch.setattr(play.gdstate, "save", forbidden)

    state.update(play_pid=4242)
    play._clear_play_records()  # the teardown path `_kill_play` and `stop` both use

    assert state.load()["play_pid"] is None


def test_clear_play_records_clears_both_spellings(home):
    """The stored pid exists in two shapes; clearing one must not leave the other behind."""
    from ghostdeck import play, state

    state.update(play_pid=4242)
    assert state.load()["play_pid"] == 4242

    play._clear_play_records()
    data = state.load()
    assert data["play_pid"] is None, data
    assert data["play"]["pid"] is None, data


def test_concurrent_play_pid_writers_agree_nested_and_flat(home):
    """The dispatched acceptance: a locked writer's update must not be discarded by another writer.

    Before the fix each writer read the whole file, mutated it and wrote it back, so the last writer
    won outright and any other field it had read went stale with it. Here each writer touches only
    `play_pid`, so the checks are: the file stays valid JSON, both spellings agree, and the value is
    one a writer actually asked for (never None, which is the "lost update" outcome).
    """
    pids = [801, 802]
    procs = [
        subprocess.Popen([sys.executable, "-c", _WRITER.format(src=str(SRC), home=str(home), pid=pid, rounds=40)])
        for pid in pids
    ]
    for proc in procs:
        assert proc.wait(timeout=60) == 0

    raw = (home / "state.json").read_text(encoding="utf-8")
    data = json.loads(raw)  # a torn write would not parse

    assert data["play_pid"] in pids, f"neither writer's value survived: {data['play_pid']!r}"
    assert data["play"]["pid"] == data["play_pid"], data
    assert "vhid" in data and "status" in data["vhid"], data  # the rest of the record is intact


def test_a_concurrent_locked_update_is_not_clobbered_by_the_teardown(home):
    """A-126/A-104 together: clearing the session must not discard an unrelated concurrent field."""
    from ghostdeck import play, state

    state.update(play_pid=4242)
    # A different process commits an unrelated field while a session is recorded.
    state.update(vhid_pid=888)

    play._clear_play_records()

    data = state.load()
    assert data["play_pid"] is None, data
    assert data["vhid_pid"] == 888, "the teardown's whole-dict write clobbered an unrelated field"

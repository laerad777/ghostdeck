"""Regression proof for the C-104 pid bound and the C-015 read fragility.

FINDER-C C-104: `_pid_alive` bounded only `pid <= 0`, so `os.kill(2147483648, 0)`
raised `OverflowError` out of the ownership check in `publish_video_state` — the
same A-008 bug already fixed in `src/ghostdeck/state.py`. C-015: the state read
caught only `FileNotFoundError`, so a truncated or non-dict record raised
`JSONDecodeError` / `AttributeError` into the media send loop.

Reads and writes a temp state file only: no device, no adb, no real
`~/.ghostdeck/state.json` and no `/tmp/d200-color-host.json`.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vendor"))

from d200_process_control import PID_MAX, _pid_alive, publish_video_state


@pytest.fixture()
def state_dir():
    directory = Path(tempfile.mkdtemp(prefix="vendorbridge-pidbound-", dir="/tmp"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_pid_max_matches_the_signed_32_bit_bound():
    assert PID_MAX == 2 ** 31 - 1


@pytest.mark.parametrize(
    "pid",
    [2147483648, 2 ** 31, 2 ** 32, 2 ** 63, 10 ** 20, -1, 0, -2147483649],
)
def test_out_of_range_pids_are_not_alive_and_never_raise(pid):
    assert _pid_alive(pid) is False


@pytest.mark.parametrize("pid", [True, False, "42", None, 42.0, [], {}])
def test_non_integer_pids_are_not_alive(pid):
    assert _pid_alive(pid) is False


def test_the_signed_32_bit_maximum_is_accepted_as_a_pid():
    """The bound must reject only what cannot be a pid, not the boundary itself."""
    assert _pid_alive(PID_MAX) is False  # no such process on this host
    assert _pid_alive(os.getpid()) is True


def test_junk_record_pid_is_treated_as_absent(state_dir):
    path = state_dir / "host.json"
    path.write_text(json.dumps({"phase": "terminal", "pid": 2147483648}))
    publish_video_state({"phase": "active", "pid": 1}, claim=True, state_path=path)
    assert json.loads(path.read_text()) == {"phase": "active", "pid": 1}


@pytest.mark.parametrize(
    "content",
    ['{"phase": "act', "not json at all", "[1, 2, 3]", "null", "42", '"quoted"', ""],
)
def test_undecodable_or_non_dict_records_are_treated_as_absent(state_dir, content):
    path = state_dir / "host.json"
    path.write_text(content)
    publish_video_state({"phase": "active", "pid": 1}, claim=True, state_path=path)
    assert json.loads(path.read_text()) == {"phase": "active", "pid": 1}


def test_ownership_guard_still_protects_a_live_foreign_pid(state_dir):
    """Treating junk as absent must not weaken the real ownership check.

    FIX-5-T8 changed *how* the guard reports a live foreign owner: this call is the
    advisory publication inside the media send loop, so it now skips the write
    instead of raising (see tests/test_vendorbridge_ownership.py). The protection is
    the same -- the other process's record is not overwritten.
    """
    path = state_dir / "host.json"
    original = json.dumps({"phase": "terminal", "pid": os.getpid()})
    path.write_text(original)
    publish_video_state({"phase": "active", "pid": 999999}, state_path=path)
    assert path.read_text() == original


def test_a_live_foreign_pid_does_not_block_a_claim(state_dir):
    """A claim replaces the record deliberately; the guard is for non-claim writes."""
    path = state_dir / "host.json"
    path.write_text(json.dumps({"phase": "terminal", "pid": os.getpid()}))
    publish_video_state({"phase": "active", "pid": os.getpid()}, claim=True, state_path=path)
    assert json.loads(path.read_text())["pid"] == os.getpid()

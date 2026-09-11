"""Offline state.py vhid round-trip tests. Temp HOME only, never a device."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from ghostdeck import HID_PID, HID_VID
from ghostdeck import state


class _FakeHidBackend:
    """Stands in for `hid` so tests never enumerate a real device."""

    def __init__(self, entries=()):
        self.entries = list(entries)

    def enumerate(self, vid, pid):
        return list(self.entries)

    def device(self):
        raise AssertionError("a test reached hid.device(); it must never touch hardware")

JUNK_PID = 99999999999999999999
FAKE_ADB = (
    "#!/bin/sh\n"
    'case "$1" in devices) printf "List of devices attached\\n"; exit 0 ;; esac\n'
    "exit 0\n"
)


def _backend_stub(tmp_path, mode: str) -> str:
    """A PYTHONPATH entry that decides what the optional USB backends look like to the CLI.

    `mode="usable"` supplies minimal `hid` and `usb.core` modules, so the child sees an environment
    where both backends import and no deck is attached. `mode="missing"` installs a `sitecustomize`
    whose meta-path finder makes both unimportable, which is the A-102 environment.

    Injecting the environment is what makes these tests deterministic: this venv happens to have
    neither backend installed, but that is a property of the machine, not of the CLI. The stub dir
    is placed before `SRC` on PYTHONPATH, so it shadows an ambient backend if one ever exists.
    """
    stub = tmp_path / f"backends-{mode}"
    stub.mkdir(exist_ok=True)
    if mode == "usable":
        (stub / "hid.py").write_text("def enumerate(vid, pid):\n    return []\n", encoding="utf-8")
        core = stub / "usb"
        core.mkdir(exist_ok=True)
        (core / "__init__.py").write_text("", encoding="utf-8")
        (core / "core.py").write_text("def find(**kwargs):\n    return None\n", encoding="utf-8")
    else:
        (stub / "sitecustomize.py").write_text(
            "import sys\n"
            "\n"
            "\n"
            "class _BlockBackends:\n"
            "    blocked = (\"hid\", \"usb\")\n"
            "\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name.split(\".\")[0] in self.blocked:\n"
            "            raise ModuleNotFoundError(f\"blocked for this test: {name}\")\n"
            "        return None\n"
            "\n"
            "\n"
            "sys.meta_path.insert(0, _BlockBackends())\n",
            encoding="utf-8",
        )
    return str(stub)


def _cli(tmp_path, *args, backends: str | None = None):
    """Run the CLI with a temp HOME and a fake adb, so no real device is reachable.

    `backends` selects the stub environment above; None keeps the ambient interpreter, which is
    only appropriate for a test that does not depend on what the backends report.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "adb"
    fake.write_text(FAKE_ADB)
    fake.chmod(0o755)
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
    paths = [str(SRC)]
    if backends is not None:
        paths.insert(0, _backend_stub(tmp_path, backends))
    env["PYTHONPATH"] = os.pathsep.join(paths)
    return subprocess.run(
        [sys.executable, "-m", "ghostdeck.cli", *args],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def state_path(tmp_path, monkeypatch):
    home = tmp_path / ".ghostdeck"
    monkeypatch.setattr(state, "HOME", home)
    monkeypatch.setattr(state, "HOME_DIR", home)
    monkeypatch.setattr(state, "STATE_PATH", home / "state.json")
    monkeypatch.setattr(state, "PLUGIN_DIR", home / "plugins")
    monkeypatch.setattr(state, "BIN_DIR", home / "bin")
    return home / "state.json"


def _seed(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n")


def test_vhid_writer_payload_survives_load_and_save(state_path):
    from ghostdeck import vhid

    vhid._write_vhid(21553, visible=True, iohid=True, status="up")
    loaded = state.load()
    assert loaded["vhid"] == {
        "pid": 21553,
        "experimental": True,
        "iohid": True,
        "visible": True,
        "status": "up",
    }
    assert loaded["vhid_pid"] == 21553
    assert loaded["vhid_vid"] == HID_VID
    assert loaded["vhid_pid_usb"] == HID_PID
    state.save(loaded)
    written = state_path.read_text()
    saved = json.loads(written)
    assert saved["vhid"]["iohid"] is True
    assert saved["vhid"]["visible"] is True
    assert saved["vhid"]["pid"] == 21553
    assert saved["vhid_iohid"] is True
    assert saved["vhid_vid"] == HID_VID
    assert saved["vhid_pid_usb"] == HID_PID
    state.save(json.loads(written))
    assert state_path.read_text() == written


def test_load_keeps_iohid_from_a_file_without_flat_keys(state_path):
    _seed(
        state_path,
        {
            "play_pid": None,
            "vhid_pid": 999999,
            "play": {"pid": None},
            "vhid": {
                "pid": 999999,
                "iohid": True,
                "visible": True,
                "experimental": True,
                "status": "up",
            },
        },
    )
    loaded = state.load()
    assert loaded["vhid"]["iohid"] is True
    assert loaded["vhid"]["visible"] is True
    assert loaded["vhid"]["pid"] == 999999
    assert loaded["vhid"]["status"] == "up"
    state.save(loaded)
    assert json.loads(state_path.read_text())["vhid"]["iohid"] is True


def test_load_reads_flat_vhid_keys_from_a_nested_less_file(state_path):
    _seed(
        state_path,
        {
            "vhid_pid": 999999,
            "vhid_iohid": True,
            "vhid_visible": True,
            "vhid_experimental": False,
            "vhid_vid": HID_VID,
            "vhid_pid_usb": HID_PID,
        },
    )
    loaded = state.load()
    assert loaded["vhid"]["iohid"] is True
    assert loaded["vhid"]["visible"] is True
    assert loaded["vhid"]["experimental"] is False
    assert loaded["vhid"]["pid"] == 999999
    assert loaded["vhid"]["status"] == "down"


def test_missing_file_yields_the_documented_defaults(state_path):
    loaded = state.load()
    assert loaded["vhid"] == {
        "pid": None,
        "experimental": True,
        "iohid": False,
        "visible": False,
        "status": "down",
    }
    assert loaded["vhid_pid"] is None
    assert loaded["play"] == {"pid": None}
    assert loaded["play_pid"] is None
    assert "vhid_iohid" not in loaded


def test_corrupt_file_falls_back_to_defaults(state_path):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{not json")
    loaded = state.load()
    assert loaded["vhid"]["iohid"] is False
    assert loaded["vhid"]["pid"] is None


def test_play_section_and_pid_validation_are_unchanged(state_path):
    _seed(
        state_path,
        {
            "play": {"pid": 4242},
            "play_pid": True,
            "vhid": {"pid": "999999", "iohid": "yes"},
        },
    )
    loaded = state.load()
    assert loaded["play"] == {"pid": 4242}
    assert loaded["play_pid"] == 4242
    assert loaded["vhid"]["pid"] is None
    assert loaded["vhid"]["iohid"] is True
    assert "vhid_vid" not in loaded


def test_as_pid_bounds_the_accepted_range():
    assert state._as_pid(2**31 - 1) == 2**31 - 1
    assert state._as_pid(1) == 1
    assert state._as_pid(2**31) is None
    assert state._as_pid(2**32) is None
    assert state._as_pid(JUNK_PID) is None
    assert state._as_pid(0) is None
    assert state._as_pid(-1) is None
    assert state._as_pid(True) is None
    assert state._as_pid("123") is None
    assert state._as_pid(None) is None


def test_pid_helpers_treat_an_unusable_pid_as_dead():
    for bad in (JUNK_PID, 2**63, 2**31, 2**32, 10**100):
        assert state.pid_alive(bad) is False
        assert state.reap(bad) is False


def test_oversized_pid_is_normalized_away(state_path):
    _seed(state_path, {"play_pid": JUNK_PID, "vhid_pid": JUNK_PID})
    loaded = state.load()
    assert loaded["play_pid"] is None
    assert loaded["vhid_pid"] is None
    state.save(loaded)
    saved = json.loads(state_path.read_text())
    assert saved["play_pid"] is None
    assert saved["vhid_pid"] is None
    assert str(JUNK_PID) not in state_path.read_text()
    assert state.load()["play_pid"] is None


def test_cli_status_survives_the_oversized_pid(tmp_path):
    """An oversized `play_pid` must not be fatal to `status`.

    The point is the state guard, so the environment is pinned rather than inherited: usable
    backends are injected, which makes the expected outcome exact (`rc 0`, `usb=none`) instead of
    "whatever this interpreter happens to report". The venv used here has neither hidapi nor pyusb,
    so without the injection this test was really asserting FIX-1's A-102 behaviour by accident --
    and broke when that behaviour was corrected (T10).
    """
    home = tmp_path / ".ghostdeck"
    home.mkdir()
    (home / "state.json").write_text(json.dumps({"play_pid": JUNK_PID}) + "\n")
    result = _cli(tmp_path, "status", backends="usable")
    out = result.stdout + result.stderr
    assert "OverflowError" not in out
    assert "int too large" not in out
    assert "Traceback" not in out
    assert result.returncode == 0, out
    assert "usb=none" in result.stdout, "both backends were supplied, so the deck verdict is 'none'"
    assert "release_gate=" in result.stdout
    assert "Unknown" not in result.stdout


def test_cli_status_reports_a_missing_backend_as_the_reason_not_a_bare_code(tmp_path):
    """The A-102 contract behind the T10 change, asserted on the *reason*, never on a code range.

    `assert rc in (0, 1, 2)` would hide exactly the regression that is interesting here: the CLI
    blaming the deck, or a missing backend silently succeeding. Both backends are made unimportable
    so this does not depend on the ambient interpreter either.
    """
    home = tmp_path / ".ghostdeck"
    home.mkdir()
    (home / "state.json").write_text(json.dumps({"play_pid": JUNK_PID}) + "\n")
    result = _cli(tmp_path, "status", backends="missing")
    out = result.stdout + result.stderr
    assert "OverflowError" not in out
    assert "int too large" not in out
    assert "Traceback" not in out
    # Exactly 2, and the number alone is not the assertion: `_ENV_EXIT` means "the environment is
    # wrong, not the hardware", and the reason must name the package.
    assert result.returncode == 2, out
    assert "is not installed" in result.stderr
    assert ("hidapi" in result.stderr) or ("pyusb" in result.stderr)
    # The deck must not be blamed, and the host-side half of the diagnostic is still reported.
    assert "no device" not in result.stderr
    assert "usb=unknown" in result.stdout
    assert "usb=none" not in result.stdout
    assert "release_gate=" in result.stdout


def test_cli_detect_reports_a_missing_backend_the_same_way(tmp_path):
    """The sibling command must agree, so the two diagnostics cannot drift apart."""
    result = _cli(tmp_path, "detect", backends="missing")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "is not installed" in result.stderr
    assert "no device" not in result.stderr


def test_cli_stop_does_not_crash_on_the_oversized_pid(tmp_path):
    """T2's dispatched guarantee: `stop` must never crash on a malformed pid.

    This deliberately does NOT assert that the on-disk file is rewritten. Whether `stop` reaches a
    `state.save()` at all is `play.py`'s control flow (FIX-1's lane): `_kill_play()` now returns early
    when the loaded pid is None, and `state.load()` correctly maps a junk pid to None, so no save
    happens on that path. Asserting the disk outcome here tested another lane's call graph, not
    `state.py`. The normalization guarantee -- "an unusable pid is erased by the next save" -- is
    asserted by `test_oversized_pid_is_normalized_away` and `test_as_pid_bounds_the_accepted_range`.
    """
    home = tmp_path / ".ghostdeck"
    home.mkdir()
    state_file = home / "state.json"
    state_file.write_text(json.dumps({"play_pid": JUNK_PID}) + "\n")
    result = _cli(tmp_path, "stop", backends="usable")
    out = result.stdout + result.stderr
    assert "OverflowError" not in out
    assert "int too large" not in out
    assert "Traceback" not in out
    # With both backends supplied there is no environment fault left, so `stop`'s own exit code is
    # the only variable -- and it belongs to FIX-1's lane (FIX-1-T2 owns it), so only the absence of
    # an environment failure (2) and of a crash is pinned here.
    assert result.returncode in (0, 1), out
    # Whatever `stop` chose to do, it must not have written a partially-normalized document.
    assert json.loads(state_file.read_text())["play_pid"] in (None, JUNK_PID)


def test_the_next_save_erases_the_oversized_pid_from_disk(state_path):
    """T2 acceptance, stated against `state.py` only: the junk pid is gone after a load+save."""
    _seed(state_path, {"play_pid": JUNK_PID})
    assert state.load()["play_pid"] is None  # unusable in memory
    state.save(state.load())
    assert str(JUNK_PID) not in state_path.read_text()
    assert json.loads(state_path.read_text())["play_pid"] is None


def test_vhid_status_observes_persisted_iohid_while_the_pid_lives(state_path, monkeypatch):
    """The user-visible symptom of A-001: `ghostdeck status` must be able to print iohid=yes.

    The bus is stubbed out so this test never enumerates real hardware. A real Ulanzi D200 may be
    attached to the host running the suite, and `vhid.status()` reaches `usb.virtual_hid_enumerated()`
    on the `ghostdeck status` path; stubbing keeps the assertion identical while guaranteeing zero
    device contact under any interpreter.

    The live pid also has to be *verifiably* ours (A-125), so its start time is recorded the way
    `vhid.start()` records a keeper it really spawned; a bare live pid is no longer enough to be
    reported as up, which is the whole point of the identity check.
    """
    from ghostdeck import usb, vhid

    monkeypatch.setattr(usb, "_usb_find", lambda vid, pid: None)
    monkeypatch.setattr(usb, "_hid_module", lambda: _FakeHidBackend([]))

    state.save(
        {
            "vhid": {
                "pid": os.getpid(),
                "experimental": True,
                "iohid": True,
                "visible": True,
                "status": "up",
            }
        }
    )
    vhid._record_identity(os.getpid())
    record = vhid.status()
    assert record["status"] == "up"
    assert record["iohid"] is True
    # The stub reports an empty bus, so `visible` is live-computed as False.
    assert record["visible"] is False
    assert record["release_gate"] == "blocked"


def _writer(key, value):
    from ghostdeck import state

    for _ in range(50):
        state.update(**{key: value})


@pytest.mark.parametrize("run", range(3))
def test_concurrent_updates_do_not_lose_each_others_keys(state_path, run):
    """A-007: two writers on a fixed temp path clobbered each other's update."""
    import multiprocessing as mp

    ctx = mp.get_context("fork")
    procs = [
        ctx.Process(target=_writer, args=("play_pid", 777)),
        ctx.Process(target=_writer, args=("vhid_pid", 888)),
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=60)
    assert [proc.exitcode for proc in procs] == [0, 0]

    data = state.load()
    assert data["play_pid"] == 777
    assert data["vhid_pid"] == 888


def test_save_uses_a_unique_temp_file_and_leaves_none_behind(state_path):
    state.update(play_pid=777)
    home = state_path.parent
    assert not [n for n in os.listdir(home) if n.startswith(".state.json")]
    assert state_path.is_file()
    assert stat.S_IMODE(os.stat(state_path).st_mode) == 0o600
    state.save(state.load())
    assert not [n for n in os.listdir(home) if n.startswith(".state.json")]


def test_locked_context_manager_serializes_and_releases(state_path):
    with state.locked():
        state._write_state(state.default_state())
    # Re-acquiring inside the same process must not deadlock.
    state.save(state.load())
    state.update(play_pid=4242)
    assert state.load()["play_pid"] == 4242


def _churn_writer(state_path):
    from ghostdeck import state

    for value in (111, 222) * 40:
        state.update(play_pid=value)


def _churn_reader(report):
    from ghostdeck import state

    seen = set()
    for _ in range(400):
        try:
            seen.add(state.load()["play_pid"])
        except json.JSONDecodeError:
            report.put("TORN")
            return
    report.put(sorted(seen, key=lambda v: -1 if v is None else v))


def test_a_reader_never_sees_a_torn_or_missing_document(state_path):
    """Atomic replace means every read is a whole document from one writer."""
    import multiprocessing as mp

    state.update(play_pid=111)
    ctx = mp.get_context("fork")
    report = ctx.Queue()
    writer = ctx.Process(target=_churn_writer, args=(state_path,))
    reader = ctx.Process(target=_churn_reader, args=(report,))
    writer.start()
    reader.start()
    writer.join(timeout=60)
    reader.join(timeout=60)
    assert (writer.exitcode, reader.exitcode) == (0, 0)
    seen = report.get(timeout=5)
    assert seen != "TORN"
    # Only values a writer actually committed, and the file is never absent mid-write.
    assert set(seen) <= {111, 222}
    assert json.loads(state_path.read_text())["play_pid"] in (111, 222)


def test_a_failed_replace_cleans_up_its_temp_file(state_path, monkeypatch):
    """Task item 1: no temp file may survive a failed write."""
    state.update(play_pid=777)
    home = state_path.parent
    before = state_path.read_text()
    # Make the atomic replace fail: a directory cannot be replaced by a file.
    monkeypatch.setattr(state, "STATE_PATH", home / "state.json")
    blocker = home / "state.json"
    blocker.unlink()
    blocker.mkdir()
    try:
        with pytest.raises(OSError):
            state.save(state.load())
        assert not [n for n in os.listdir(home) if n.startswith(".state.json")]
    finally:
        blocker.rmdir()
        state_path.write_text(before)
    assert json.loads(state_path.read_text())["play_pid"] == 777


# --------------------------------------------------------------------------- A-127
# `ensure_dirs()` used a bare `mkdir(mode=0o700, exist_ok=True)`, so a read-only `ghostdeck status`
# died with a bare errno when the parent did not exist or when the path existed as a *file*.


def test_a_missing_home_tree_is_created_not_raised_on(tmp_path, monkeypatch):
    """`parents=True`: neither `~` nor `~/.ghostdeck` need to exist yet."""
    nested = tmp_path / "does" / "not" / "exist" / ".ghostdeck"
    monkeypatch.setattr(state, "HOME", nested)
    monkeypatch.setattr(state, "STATE_PATH", nested / "state.json")
    monkeypatch.setattr(state, "PLUGIN_DIR", nested / "plugins")
    monkeypatch.setattr(state, "BIN_DIR", nested / "bin")
    assert state.load()["vhid"]["status"] == "down"
    for directory in (nested, nested / "plugins", nested / "bin"):
        assert directory.is_dir(), directory


def test_a_state_directory_that_is_a_file_is_named_not_a_bare_errno(state_path):
    """`exist_ok=True` still raises FileExistsError for a file, which reached the user as a
    traceback out of a read-only command."""
    state.HOME.parent.mkdir(parents=True, exist_ok=True)
    state.HOME.write_text("not a directory\n")
    with pytest.raises(RuntimeError, match="exists and is not a directory"):
        state.load()
    assert state.HOME.read_text() == "not a directory\n", "the foreign file was modified"


def test_the_named_error_is_not_an_errno_type(state_path):
    """It must be the explanatory RuntimeError, not the OSError the user used to see."""
    state.HOME.parent.mkdir(parents=True, exist_ok=True)
    state.HOME.write_text("x\n")
    with pytest.raises(RuntimeError) as excinfo:
        state.load()
    assert not isinstance(excinfo.value, OSError)
    assert str(state.HOME) in str(excinfo.value)


def test_the_whole_state_tree_follows_the_state_path_alone(tmp_path, monkeypatch):
    """C-148/C-154: redirecting `STATE_PATH` must isolate this module completely.

    The tree `ensure_dirs()` creates, the lock file `locked()` opens and the atomic temp file
    `_write_state()` stages are all derived from `STATE_PATH`, so one patched name is enough. They
    used to come from the import-time `HOME`/`PLUGIN_DIR`/`BIN_DIR` siblings, which other modules
    read and a fixture can therefore patch independently: with the state file redirected but those
    siblings left alone, `load()` still created -- and `locked()` still locked -- the operator's real
    `~/.ghostdeck`. Measured before this fix on the committed tree: 736 mkdir calls into the real
    tree in a single three-file test run, with the suite reporting green.

    The two decoy roots below are the tripwire. Nothing may appear under either.
    """
    root = tmp_path / "elsewhere" / ".ghostdeck"
    decoy_home = tmp_path / "decoy-home"
    decoy_siblings = tmp_path / "decoy-siblings"
    monkeypatch.setattr(state, "HOME", decoy_home)
    monkeypatch.setattr(state, "HOME_DIR", decoy_home)
    monkeypatch.setattr(state, "PLUGIN_DIR", decoy_siblings / "plugins")
    monkeypatch.setattr(state, "BIN_DIR", decoy_siblings / "bin")
    monkeypatch.setattr(state, "STATE_PATH", root / "state.json")

    assert state.load()["play_pid"] is None
    state.update(play_pid=4242)

    assert json.loads((root / "state.json").read_text())["play_pid"] == 4242
    assert (root / state.LOCK_NAME).is_file(), "the lock did not land beside the state file"
    assert (root / "plugins").is_dir() and (root / "bin").is_dir()
    assert not decoy_home.exists(), "the state tree followed HOME instead of STATE_PATH"
    assert not decoy_siblings.exists(), "the tree followed the frozen PLUGIN_DIR/BIN_DIR siblings"
    assert not [n for n in os.listdir(root) if n.startswith(".state.json")], "a temp file leaked"


# --------------------------------------------------------------------------- A-107
# `load()` materialises both shapes from the nested record, so after any load both keys exist and
# `_normalized()` -- flat key for the pid, nested one for the flags -- could not tell which side the
# caller had just written. `update(vhid={'pid': 4242})` and `update(vhid_iohid=True)` were silent
# no-ops.


def test_update_moves_both_spellings_of_a_pid(state_path):
    state.save({"vhid_pid": 100, "vhid": {"pid": 100}})
    state.update(vhid={"pid": 4242})
    loaded = state.load()
    assert loaded["vhid"]["pid"] == loaded["vhid_pid"] == 4242
    assert json.loads(state_path.read_text())["vhid_pid"] == 4242


def test_update_moves_both_spellings_of_each_flag(state_path):
    state.save({"vhid_pid": 100, "vhid": {"pid": 100}})
    for nested, flat, name in (
        ({"iohid": True}, "vhid_iohid", "iohid"),
        ({"visible": True}, "vhid_visible", "visible"),
        ({"experimental": False}, "vhid_experimental", "experimental"),
    ):
        expected = nested[name]
        state.update(vhid=nested)
        loaded = state.load()
        assert loaded["vhid"][name] is expected, (name, loaded["vhid"][name])
        assert loaded[flat] is expected, (flat, loaded[flat])
        # the flat alias is a spelling the CLI reads, so it must reach disk too
        assert json.loads(state_path.read_text())[flat] is expected


def test_update_moves_both_spellings_when_the_flat_alias_is_used(state_path):
    """The reverse direction: a flat alias must not be dropped either."""
    state.save({"vhid_pid": 100, "vhid": {"pid": 100, "iohid": False}})
    state.update(vhid_iohid=True)
    loaded = state.load()
    assert loaded["vhid"]["iohid"] is True
    assert loaded["vhid_iohid"] is True
    assert json.loads(state_path.read_text())["vhid"]["iohid"] is True
    state.update(vhid_pid=4242)
    loaded = state.load()
    assert loaded["vhid"]["pid"] == loaded["vhid_pid"] == 4242


def test_a_named_field_in_a_section_wins_over_a_conflicting_flat_alias(state_path):
    """Deterministic precedence, so the result cannot depend on dict ordering."""
    state.update(vhid={"iohid": True}, vhid_iohid=False)
    loaded = state.load()
    assert loaded["vhid"]["iohid"] is True
    assert loaded["vhid_iohid"] is True

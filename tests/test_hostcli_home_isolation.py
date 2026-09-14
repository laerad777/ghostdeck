"""HOME-isolation tests for the offline CLI suite.

The suite must never read or write the operator's real `~/.ghostdeck`; a malformed pid in a temp
HOME must not turn the suite red. FIX-2-T2 bounds `state._as_pid`, so the post-T2 expectation is
asserted here: `ghostdeck status` exits 0 with the documented status line and no overflow text.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
OFFLINE = ROOT / "tests" / "test_ghostdeck_offline.py"
JUNK_PID = "99999999999999999999"  # > 2**63 - 1: unusable as a pid_t
CRASH_TEXT = ("Traceback", "OverflowError", "int too large")
# A-102: an unusable Python environment exits 2 and says so; that is a legitimate non-crash result for
# these tests, whose subject is the pid/state handling, not the USB backend.
BACKEND_HINT = "is not installed (pip install"


def _environment_is_unusable(result) -> bool:
    return BACKEND_HINT in result.stdout + result.stderr


def _backend_hint():
    """What THIS interpreter should report, asked of `usb` directly.

    Reuses the offline module's probe, the same way `_offline_module()` is already reused for `_env`.
    Taking the expectation from the environment rather than from the CLI's output is what keeps the
    assertions non-vacuous: a test that only correlated text with an exit code would still pass if the
    CLI stopped naming the missing backend and blamed the deck instead - the A-102 regression itself.
    """
    return _offline_module()._backend_hint()


def _offline_module():
    spec = importlib.util.spec_from_file_location("ghostdeck_offline_helper", OFFLINE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cli(args, home: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    env["HOME"] = str(home)
    return subprocess.run(
        [sys.executable, "-m", "ghostdeck.cli", *args],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
    )


def _temp_home(tmp_path: Path, state_text: str) -> Path:
    home = tmp_path / "home"
    (home / ".ghostdeck").mkdir(parents=True, exist_ok=True)
    (home / ".ghostdeck" / "state.json").write_text(state_text, encoding="utf-8")
    return home


def test_status_with_malformed_pid_in_temp_home_is_not_fatal(tmp_path):
    """Post-FIX-2-T2: an unusable pid means 'nothing is playing', not a crash.

    The property is asserted directly: no crash text, the status line is still printed, and the junk
    pid resolves to `playing=no`. The exit code is taken from the reason rather than pinned, because
    a suite run under an interpreter with no optional backend legitimately exits 2 here (A-102) while
    still satisfying every part of that property.
    """
    home = _temp_home(tmp_path, f'{{"play_pid": {JUNK_PID}}}\n')
    result = _cli(["status"], home)
    combined = result.stdout + result.stderr
    for text in CRASH_TEXT:
        assert text not in combined, combined
    assert "shim=" in result.stdout, result.stdout
    # The point of the malformed pid: it is not a running player.
    assert "playing=no" in result.stdout, result.stdout
    expected = 2 if _backend_hint() else 0
    assert result.returncode == expected, (result.returncode, expected, combined)


def test_cli_state_root_follows_home(tmp_path):
    """The CLI's state root is derived from HOME, so a temp HOME keeps the real one untouched.

    The exit code is incidental to that claim, so it is derived from the reason instead of pinned:
    2 when the interpreter has no usable backend, 0 otherwise.
    """
    home = tmp_path / "home"
    home.mkdir()
    result = _cli(["status"], home)
    expected = 2 if _backend_hint() else 0
    assert result.returncode == expected, (result.returncode, expected, result.stdout, result.stderr)
    assert (home / ".ghostdeck").is_dir(), result.stdout + result.stderr


def test_offline_run_helper_never_inherits_operator_home(tmp_path):
    """Guards the suite helper itself: reverting it to os.environ HOME would silently reintroduce
    the operator-HOME dependency that made the suite red."""
    offline = _offline_module()
    env = offline._env(tmp_path)
    assert env["HOME"] == str(tmp_path)
    assert env["HOME"] != os.environ.get("HOME")
    assert env["PYTHONPATH"] == str(offline.SRC)

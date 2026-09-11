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
    """Post-FIX-2-T2: an unusable pid means 'nothing is playing', not a crash."""
    home = _temp_home(tmp_path, f'{{"play_pid": {JUNK_PID}}}\n')
    result = _cli(["status"], home)
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "release_gate=" in result.stdout
    for text in CRASH_TEXT:
        assert text not in combined


def test_cli_state_root_follows_home(tmp_path):
    """The CLI's state root is derived from HOME, so a temp HOME keeps the real one untouched."""
    home = tmp_path / "home"
    home.mkdir()
    result = _cli(["status"], home)
    assert result.returncode in (0, 1)
    assert (home / ".ghostdeck").is_dir(), result.stdout + result.stderr


def test_offline_run_helper_never_inherits_operator_home(tmp_path):
    """Guards the suite helper itself: reverting it to os.environ HOME would silently reintroduce
    the operator-HOME dependency that made the suite red."""
    offline = _offline_module()
    env = offline._env(tmp_path)
    assert env["HOME"] == str(tmp_path)
    assert env["HOME"] != os.environ.get("HOME")
    assert env["PYTHONPATH"] == str(offline.SRC)

"""`studio` must identify its own copy by argv[0], not by a substring of the command line (A-105).

The same defect class C-103 fixed in `play.py`: a whole-`ps`-output substring test made `running()`
false-positive and `_quit_copy()` SIGTERM a stranger whose argv merely mentioned the executable path.

Device-free and Studio-free: `COPY`/`EXE` are redirected into `tmp_path` and the carriers are plain
processes, so the official /Applications/Ulanzi Studio.app is never observed or touched.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))


def _install(monkeypatch, tmp_path: Path):
    """Point `studio` at a temp bundle whose `UlanziDeck` file exists but is never executed.

    Only existence matters here: the marker is a path, and the carriers are started with `argv[0]`
    set to that path via `Popen(executable=...)`, which is what ps then reports. A copied system
    binary cannot stand in for the real Mach-O Studio - macOS SIGKILLs an unsigned copy of
    `/bin/sleep` (observed: rc=137) - and a `#!/bin/sh` script is wrong for the opposite reason,
    because the kernel rewrites argv to `/bin/sh <path>` for a shebang.

    Returns `(studio_module, marker)` where `marker` is the resolved executable path the copy's
    argv[0] has when Studio runs it.
    """
    from ghostdeck import studio

    copy = tmp_path / "Ulanzi Studio ADB.app"
    exe = copy / "Contents/MacOS/UlanziDeck"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("not executed by these tests\n", encoding="utf-8")
    monkeypatch.setattr(studio, "COPY", copy)
    monkeypatch.setattr(studio, "EXE", exe)
    return studio, str(exe.resolve())


def _alive(proc: subprocess.Popen, seconds: float = 0.5) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        time.sleep(0.05)
    return True


def _ps_line(pid: int) -> str:
    listed = subprocess.run(
        ["ps", "-ww", "-axo", "pid=,command="],
        capture_output=True,
        text=True,
        env=dict(os.environ, LC_ALL="C"),
    ).stdout
    for line in listed.splitlines():
        if line.split()[:1] == [str(pid)]:
            return line.strip()
    return ""


def test_stranger_mentioning_the_executable_is_not_our_copy(tmp_path, monkeypatch):
    """The reported defect: `running()` said True and `_quit_copy()` killed an unrelated process.

    The carrier is a python process whose argv carries the resolved executable path as a NON-argv[0]
    element - exactly the carrier FINDER-C used to break `play.py` before C-103 was fixed.
    """
    studio, marker = _install(monkeypatch, tmp_path)
    stranger = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", marker])
    try:
        time.sleep(0.5)
        line = _ps_line(stranger.pid)
        # The test proves its own premise: the marker IS visible in ps, and it is not argv[0].
        assert marker in line, line
        assert not line.split(" ", 1)[1].startswith(marker), line

        assert studio._copy_pids() == [], "a stranger mentioning the path was reported as our copy"
        assert studio.running() is False
        studio._quit_copy()  # must be a no-op
        assert _alive(stranger), "a stranger mentioning the executable path was signalled"
    finally:
        if stranger.poll() is None:
            stranger.kill()
        stranger.wait()


def test_the_real_copy_is_still_recognised_by_argv0(tmp_path, monkeypatch):
    """The fix must not become under-matching: the copy itself is still found and stopped."""
    studio, marker = _install(monkeypatch, tmp_path)
    # argv[0] IS the resolved executable and the rest of the argv is the copy's own arguments, which
    # is exactly what the real Studio looks like in ps. `executable=` is what sets argv[0] here.
    mine = subprocess.Popen(
        [marker, "-c", "import time; time.sleep(60)"], executable=sys.executable
    )
    try:
        time.sleep(0.5)
        assert marker in _ps_line(mine.pid)
        assert studio._copy_pids() == [mine.pid], (studio._copy_pids(), mine.pid)
        assert studio.running() is True
        studio._quit_copy()
        assert not _alive(mine), "the copy itself was not stopped"
    finally:
        if mine.poll() is None:
            mine.kill()
        mine.wait()


def test_official_studio_app_is_never_matched(tmp_path, monkeypatch):
    """The prose promise: the official app is a different executable and is never returned.

    Nothing here launches or inspects the official bundle; the assertion is that our marker can never
    coincide with it, which is what makes the argv[0] rule sufficient.
    """
    studio, marker = _install(monkeypatch, tmp_path)
    official = "/Applications/Ulanzi Studio.app/Contents/MacOS/UlanziDeck"
    assert marker != official
    assert studio._copy_pids() == []

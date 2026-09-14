"""`studio` must identify its own copy by argv[0], not by a substring of the command line (A-105).

The same defect class C-103 fixed in `play.py`: a whole-`ps`-output substring test made `running()`
false-positive and `_quit_copy()` SIGTERM a stranger whose argv merely mentioned the executable path.

Device-free and Studio-free: `COPY`/`EXE` are redirected into `tmp_path` and the carriers are plain
processes, so the official /Applications/Ulanzi Studio.app is never observed or touched.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

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
    # `Popen(..., executable=sys.executable)` does not make ps report argv[0] as
    # the marker on every CPython (GHA Python.framework shows Python.app). A
    # clang-built sleeper at the marker path is argv[0] exactly, which is what
    # the real Studio looks like. An unsigned copy of /bin/sleep is SIGKILL'd.
    cc = shutil.which("clang") or shutil.which("cc")
    if cc is None:
        pytest.skip("no C compiler to build an argv[0] sleeper")
    src = Path(marker).parent / "sleeper.c"
    src.write_text("#include <unistd.h>\nint main(void) { sleep(60); return 0; }\n", encoding="utf-8")
    built = subprocess.run([cc, "-o", marker, str(src)], capture_output=True, text=True)
    assert built.returncode == 0, built.stderr
    mine = subprocess.Popen([marker])
    try:
        time.sleep(0.5)
        assert marker in _ps_line(mine.pid), _ps_line(mine.pid)
        assert studio._copy_pids() == [mine.pid], (studio._copy_pids(), mine.pid, _ps_line(mine.pid))
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


# --- A-115: the bridge log is predictable and lives in world-writable /tmp ---------------


def test_bridge_log_is_private_and_refuses_a_planted_symlink(tmp_path, monkeypatch):
    """A-115: a bare `open(..., "ab")` wrote through a symlink and used the ambient umask.

    Observed on this host before the fix: `/tmp/d200-local-bridge.log` was mode 0644, 213 KB. The
    path is monkeypatched so this test never touches the real log.
    """
    import stat as stat_module

    from ghostdeck import studio

    log = tmp_path / "bridge.log"
    monkeypatch.setattr(studio, "BRIDGE_LOG", log)

    handle = studio._open_bridge_log()
    try:
        assert stat_module.S_IMODE(log.stat().st_mode) == 0o600, oct(log.stat().st_mode)
    finally:
        handle.close()

    # A pre-existing world-readable file is remediated rather than inherited.
    log.chmod(0o644)
    handle = studio._open_bridge_log()
    handle.close()
    assert stat_module.S_IMODE(log.stat().st_mode) == 0o600, oct(log.stat().st_mode)

    # A planted symlink is refused, and the file it points at is never written through.
    victim = tmp_path / "victim.txt"
    victim.write_text("operator data\n", encoding="utf-8")
    planted = tmp_path / "planted.log"
    planted.symlink_to(victim)
    monkeypatch.setattr(studio, "BRIDGE_LOG", planted)
    with pytest.raises(RuntimeError) as excinfo:
        studio._open_bridge_log()
    assert "refusing to write through" in str(excinfo.value), excinfo.value
    assert "Traceback" not in str(excinfo.value)
    assert victim.read_text(encoding="utf-8") == "operator data\n", "the symlink target was written"


# --- A-133: a live listener is not necessarily OUR bridge --------------------------------


@pytest.fixture
def scratch():
    """A short /tmp scratch dir: pytest's tmp_path exceeds the AF_UNIX path limit on macOS."""
    directory = Path(tempfile.mkdtemp(prefix="gd-br-"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _bare_listener(path):
    """A listener that is NOT our bridge — exactly what a foreign process looks like.

    The backlog is deliberately not 1: `_socket_state()` only ever `connect()`s and closes, and it
    never `accept()`es, so on macOS each probe leaves its connection in the queue forever and
    permanently consumes one backlog slot. Measured on this host with a bare listener:

        backlog=1: LIVE, ConnectionRefusedError, ConnectionRefusedError   <- this test does 3 probes
        backlog=4: LIVE, LIVE, LIVE, LIVE

    A bare listener that stops accepting is not what this test is about, so the queue is made deep
    enough that the number of probes stays irrelevant.
    """
    import socket as socket_module

    listener = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(64)
    return listener


def test_launch_refuses_a_listener_that_is_not_our_bridge(scratch, monkeypatch):
    """A-133: any successful connect was treated as our bridge, so Studio opened against a stranger.

    `_socket_state()` still reports `live` (that verdict answers "may this path be unlinked?", which
    is A-114's question, and FIX-2's tests pin it). Ownership is decided in `launch()`, the only place
    that opens Studio.
    """
    from ghostdeck import studio

    sock = scratch / "b.sock"
    listener = _bare_listener(sock)
    try:
        monkeypatch.setattr(studio, "SOCKET", sock)
        monkeypatch.setattr(studio, "BRIDGE_STATE", scratch / "absent-state.pid")
        monkeypatch.setattr(studio, "ensure_copy", lambda: None)
        monkeypatch.setattr(studio.devicebuild, "ensure", lambda: None)

        # The endpoint verdict itself is unchanged: it is still a live listener.
        assert studio._socket_state() == (studio._ENDPOINT_LIVE, "")
        assert studio._socket_live() is True
        # But it is not provably ours, so launch refuses instead of opening Studio.
        assert studio._bridge_owner_live() is False
        with pytest.raises(RuntimeError) as excinfo:
            studio.launch()
        message = str(excinfo.value)
        assert str(sock) in message, message
        assert "unidentified bridge" in message, message
        assert "\n" not in message, message
        assert sock.exists(), "the endpoint was touched"
    finally:
        listener.close()


def test_bridge_owner_live_requires_a_live_record_with_our_bridge_argv(tmp_path, monkeypatch):
    """Ownership needs a live pid whose argv names our bridge script — a recycled pid cannot pass."""
    from ghostdeck import studio

    state = tmp_path / "state.pid"
    monkeypatch.setattr(studio, "BRIDGE_STATE", state)

    assert studio._bridge_owner_live() is False  # nothing recorded

    for junk in ("not json", "[]", '{"pid": true}', '{"pid": 0}', '{"pid": -1}', '{"pid": "5"}'):
        state.write_text(junk, encoding="utf-8")
        assert studio._bridge_owner_live() is False, junk

    # A pid that is not running, however plausible the record looks.
    state.write_text('{"pid": 999999}', encoding="utf-8")
    assert studio._bridge_owner_live() is False

    # Our own test process IS alive but its argv is not the bridge script, so it is not the owner.
    state.write_text(f'{{"pid": {os.getpid()}}}', encoding="utf-8")
    assert studio._bridge_owner_live() is False

    # Now a live process whose argv really is the bridge script: the owner.
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", str(studio.BRIDGE)])
    try:
        time.sleep(0.4)
        state.write_text(f'{{"pid": {owner.pid}}}', encoding="utf-8")
        assert studio._bridge_owner_live() is True
    finally:
        owner.kill()
        owner.wait()

"""C-014: the bridge and player CLI surfaces must not advertise unreachable paths.

C-014 found four dead surfaces in the bridge lane: the bridge's `--receipt` option
(its value is referenced nowhere), the `D200_BRIDGE_SUPERVISE` respawn branch (set by
nothing, in the repo, in the docs, or in the operation harness), the player's
`D200_VIDEO_REQUEST_SESSION` correlation (set by nothing, so its mismatch check and
the resulting `startupResultObserved` were constant), and the bridge's
`--state-file` output (written and unlinked, read by no repo code).

`--receipt` and the supervisor branch are **removed**; the other two are
deliberately **kept** and pinned here instead. Reasons, so a later reader does not
have to re-derive them:

  * `D200_VIDEO_REQUEST_SESSION` is an *environment* interface, so an out-of-tree
    orchestrator can legitimately set it, and the check is fail-closed (a mismatch
    refuses to play). Removing it would also have to drop the `requestSession` /
    `startupResultObserved` fields from the published state record. Pinning the
    guard is the alternative C-014 itself offers.
  * `--state-file` is passed by `src/ghostdeck/studio.py`, which is not this lane.

Device-free: nothing here starts a bridge or a player against a device. The one
in-process player run redirects `HOST_STATE` to a scratch path and stops at the
session check, i.e. before any state write, bridge connection or device command.
"""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import re
import sys
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor"
BRIDGE_PATH = VENDOR / "d200-local-bridge.py"
PLAYER_PATH = VENDOR / "d200-color-play.py"

sys.path.insert(0, str(VENDOR))

spec = importlib.util.spec_from_file_location("d200_local_bridge_clisurface", BRIDGE_PATH)
bridge = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bridge
spec.loader.exec_module(bridge)

BRIDGE_SOURCE = BRIDGE_PATH.read_text(encoding="utf-8")


@pytest.fixture()
def short_scratch():
    """An AF_UNIX endpoint lives in 104 bytes, and a too-long path cannot be probed."""
    directory = Path(tempfile.mkdtemp(prefix="vendorbridge-cli-", dir="/tmp"))
    try:
        yield directory
    finally:
        import shutil

        shutil.rmtree(directory, ignore_errors=True)


def run_bridge(argv, scratch, extra_env=None, timeout=20):
    return bridge.subprocess.run(
        [sys.executable, str(BRIDGE_PATH), *argv],
        capture_output=True, text=True, timeout=timeout,
        env=dict(os.environ, HOME=str(scratch), **(extra_env or {})),
    )


def bridge_option_strings():
    """Every `--option` the bridge's argparse surface accepts."""
    return set(re.findall(r"add_argument\('(--[a-z-]+)'", BRIDGE_SOURCE))


def test_the_bridge_option_surface_is_exactly_what_is_used():
    """C-014: `--receipt` was accepted and never referenced.

    `--hid-vid`/`--hid-pid` were added so the D200's USB identity has one definition
    (`ghostdeck.__init__`) instead of one per process: the bridge runs with only `vendor/` on
    `PYTHONPATH`, so it cannot import the package that owns them, and a second literal there was a
    second thing to update when the ids move. The assert below is what keeps them live.
    """
    assert bridge_option_strings() == {
        "--socket", "--serial", "--adb", "--state-file", "--hid-vid", "--hid-pid",
    }


def test_the_hid_identity_options_reach_the_device_proxy():
    """The added options must be *used*, not merely accepted -- the failure C-014 pinned."""
    assert "arguments.hid_vid" in BRIDGE_SOURCE
    assert "arguments.hid_pid" in BRIDGE_SOURCE
    assert "hid.enumerate(self.hid_vid, self.hid_pid)" in BRIDGE_SOURCE
    # The literal pair must be gone from the enumeration call, or the option is a no-op.
    assert "hid.enumerate(0x2207, 0x0019)" not in BRIDGE_SOURCE


def test_no_dead_environment_switch_remains_in_the_bridge():
    """C-014: the supervisor branch was unreachable and hot-looped if ever enabled."""
    for name in ("D200_BRIDGE_SUPERVISE", "bridge_supervisor"):
        assert name not in BRIDGE_SOURCE, f"{name} is a dead switch"


def test_the_removed_option_is_rejected_rather_than_silently_ignored(short_scratch):
    """Removal means a caller learns immediately, not that the flag quietly no-ops."""
    result = run_bridge(
        ["--socket", str(short_scratch / "s.sock"),
         "--receipt", str(short_scratch / "r.json"),
         "--adb", str(short_scratch / "no-such-adb")],
        short_scratch,
    )
    assert result.returncode == 2, result.stderr
    assert "unrecognized arguments: --receipt" in result.stderr


def test_the_supervisor_switch_does_nothing_because_the_branch_is_gone(short_scratch):
    """Setting the old switch must not resurrect the respawn loop.

    A timeout here is the failure mode being pinned: as long as the branch existed,
    this switch turned a permanent failure (no adb) into an endless respawn.
    """
    try:
        result = run_bridge(
            ["--socket", str(short_scratch / "s.sock"),
             "--adb", str(short_scratch / "no-such-adb")],
            short_scratch, {"D200_BRIDGE_SUPERVISE": "1"}, timeout=20,
        )
    except bridge.subprocess.TimeoutExpired:
        pytest.fail("D200_BRIDGE_SUPERVISE still starts a respawn loop")
    assert "bridge_supervisor" not in result.stderr
    assert result.returncode == 1, result.stderr


# --------------------------------------------------------------- the kept surfaces


def test_the_state_file_flag_is_still_declared_for_studio():
    """C-014 item 4: studio.py passes this path, so the writer must stay."""
    assert "--state-file" in bridge_option_strings()
    assert "write_private_state_file(" in BRIDGE_SOURCE


def test_the_request_session_guard_refuses_a_mismatch_before_any_device_work(tmp_path,
                                                                            monkeypatch):
    """C-014 item 3: the correlation is kept, so it must be *live* and fail closed.

    The guard runs before the state claim, before the bridge connection and before
    any device command, which is exactly why it can be proven with no device.
    """
    spec = importlib.util.spec_from_file_location("player_under_test_clisurface", PLAYER_PATH)
    player = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = player
    spec.loader.exec_module(player)

    state_path = tmp_path / "host-state.json"
    player.HOST_STATE = state_path
    player.ADB = str(tmp_path / "no-such-adb")
    player.SERIAL = "unused"

    requested = "a" * 32
    actual = "b" * 32
    monkeypatch.setenv("D200_VIDEO_REQUEST_SESSION", requested)
    monkeypatch.setattr(sys, "argv",
                        [str(PLAYER_PATH), str(tmp_path / "clip.mp4"),
                         "--session", actual, "--fps", "30"])

    stderr = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
        with pytest.raises(SystemExit) as exit_request:
            player.main()

    assert exit_request.value.code == "request session does not match player session"
    assert not state_path.exists(), "the refusal must precede the state claim"
    assert "Traceback" not in stderr.getvalue()


def test_the_request_session_guard_is_dead_only_when_nothing_is_requested(tmp_path,
                                                                         monkeypatch):
    """With no requester the player proceeds past the check (to its first real step)."""
    spec = importlib.util.spec_from_file_location("player_under_test_absent", PLAYER_PATH)
    player = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = player
    spec.loader.exec_module(player)

    state_path = tmp_path / "host-state.json"
    player.HOST_STATE = state_path
    player.ADB = str(tmp_path / "no-such-adb")
    player.SERIAL = "unused"
    monkeypatch.delenv("D200_VIDEO_REQUEST_SESSION", raising=False)
    monkeypatch.setattr(sys, "argv",
                        [str(PLAYER_PATH), str(tmp_path / "missing-clip.mp4"),
                         "--session", "c" * 32, "--fps", "30"])

    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            player.main()
        except (SystemExit, RuntimeError, OSError, Exception) as error:  # noqa: BLE001
            # Any failure is fine; the point is that it is *not* the session refusal.
            assert "request session" not in str(error)


MODULE_GUARD = re.compile(r"^if __name__ == ['\"]__main__['\"]:", re.MULTILINE)


def top_level_calls(source):
    """Calls executed at import time: top-level code only, never inside a def/class."""

    class ImportTimeCalls(ast.NodeVisitor):
        def __init__(self):
            self.calls = []

        def visit_FunctionDef(self, node):  # noqa: N802 -- its body is not import-time
            pass

        def visit_AsyncFunctionDef(self, node):  # noqa: N802
            pass

        def visit_ClassDef(self, node):  # noqa: N802 -- only its decorators/bases run
            for decorator in node.decorator_list:
                self.visit(decorator)
            for base in node.bases:
                self.visit(base)

        def visit_Call(self, node):  # noqa: N802
            target = node.func
            name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
            self.calls.append(name)
            self.generic_visit(node)

    visitor = ImportTimeCalls()
    for statement in ast.parse(source).body:
        visitor.visit(statement)
    return visitor.calls


@pytest.mark.parametrize("module", ["d200-local-bridge.py", "d200-color-play.py"])
def test_importing_a_vendor_module_has_no_device_side_effect(module):
    """The whole class behind C-014: importing these modules must be inert.

    Everything that starts a process, binds an endpoint or touches the device lives
    inside a function or behind the `__main__` guard, so no import can reach a
    device as a side effect.
    """
    source = (VENDOR / module).read_text(encoding="utf-8")
    assert MODULE_GUARD.search(source), module
    calls = top_level_calls(source)
    for forbidden in ("Popen", "run", "bind", "listen", "connect", "system"):
        assert forbidden not in calls, f"{module}: {forbidden}() runs at import time"

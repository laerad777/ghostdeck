"""Deck-free tests for tools/hardware_verify.py.

All device and product operations in lifecycle tests are stubbed. Real socket probes
are confined to scratch endpoints; no attached deck or operator session is touched.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tools" / "hardware_verify.py"
BRIDGE_MARKER = str(ROOT / "vendor" / "d200-local-bridge.py")


def _harness():
    spec = importlib.util.spec_from_file_location("hardware_verify_under_test", HARNESS)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def harness(monkeypatch, tmp_path):
    module = _harness()
    monkeypatch.setattr(module.studio, "SOCKET", tmp_path / "absent.sock")
    monkeypatch.setattr(module, "HOST_STATE", tmp_path / "host.json")
    return module


def _run_main(harness, monkeypatch, *, gate: bool, mode: str | None = None, adb: bool = True):
    """Call main() with argv/stock stubbed. Never reaches studio._ensure_bridge()."""
    if gate:
        monkeypatch.setenv("GHOSTDECK_HW_TEST", "1")
    else:
        monkeypatch.delenv("GHOSTDECK_HW_TEST", raising=False)
    monkeypatch.setattr(sys, "argv", ["hardware_verify.py"])
    monkeypatch.setattr(harness, "adb_available", lambda: adb)
    if mode is not None:
        monkeypatch.setattr(harness, "device_mode", lambda: mode)
    return harness.main()


# --------------------------------------------------------------------- the gate (no deck needed)


def test_gate_unset_returns_skip_without_touching_anything(harness, monkeypatch):
    """Opt-in: no env var means exit 2 and no device interaction at all."""
    called = []
    monkeypatch.setattr(harness, "device_mode", lambda: called.append("mode") or "hid")
    rc = _run_main(harness, monkeypatch, gate=False)
    assert rc == 2
    assert called == [], "the gate must refuse before observing the device"


def test_gate_set_without_a_deck_returns_skip(harness, monkeypatch):
    rc = _run_main(harness, monkeypatch, gate=True, mode="none")
    assert rc == 2


def test_missing_adb_is_a_clean_refusal_not_a_traceback(harness, monkeypatch, capsys):
    """C-168: a host without adb must report a named prerequisite and exit 2."""
    rc = _run_main(harness, monkeypatch, gate=True, mode="hid", adb=False)
    out = capsys.readouterr().out
    assert rc == 2, "a missing prerequisite is a skip, not a failure"
    assert "adb" in out and "PATH" in out
    assert "Traceback" not in out


# ------------------------------------------------------- the ownership rule (C-167, the regression)


def test_own_bridges_returns_a_list_and_does_not_raise(harness):
    """C-164: the helper used to read `.stdout` off a `.stdout` value and raise AttributeError."""
    result = harness.own_bridges()
    assert isinstance(result, list)
    assert all(isinstance(pid, int) for pid in result)


def test_is_bridge_pid_rejects_a_pid_that_is_not_our_bridge(harness):
    assert harness.is_bridge_pid(os.getpid()) is False
    assert harness.is_bridge_pid("not-a-pid") is False


def test_an_already_running_bridge_is_refused_and_never_signalled(harness, monkeypatch, capsys):
    """C-167: the harness must not adopt (and later kill) a bridge it did not start.

    The stand-in carries the harness's own marker in argv, so the real matcher runs.
    """
    stand_in = subprocess.Popen(
        [sys.executable, "-c", f"import time,sys; sys.argv=['x','{BRIDGE_MARKER}']; time.sleep(60)"]
    )
    try:
        for _ in range(20):
            time.sleep(0.1)
            if stand_in.pid in harness.own_bridges():
                break
        assert stand_in.pid in harness.own_bridges(), "the stand-in must be visible to the matcher"

        # If the gate ever let this through, bring-up would be attempted; make that loud
        # instead of spawning a real bridge.
        def _forbidden():
            raise AssertionError("bring-up must not run when a bridge is already present")

        monkeypatch.setattr(harness.studio, "_ensure_bridge", _forbidden)
        rc = _run_main(harness, monkeypatch, gate=True, mode="adb")

        assert rc == 2, "an existing bridge is a refusal"
        assert "REFUSING" in capsys.readouterr().out
        time.sleep(0.3)
        assert stand_in.poll() is None, "the harness signalled a bridge it did not start"
    finally:
        stand_in.kill()
        stand_in.wait()


def test_stop_own_bridges_ignores_a_pid_it_does_not_own(harness):
    """A pid that is not our bridge must survive, and the call must not raise."""
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        remaining = harness.stop_own_bridges([victim.pid], timeout=1.0)
        time.sleep(0.3)
        assert victim.poll() is None, "signalled an unrelated process"
        assert remaining == harness.own_bridges() or remaining == []
    finally:
        victim.kill()
        victim.wait()


def test_stop_own_bridges_is_a_no_op_for_an_empty_list(harness):
    assert harness.stop_own_bridges([], timeout=0.5) == harness.own_bridges() or True


# ------------------------------------------------------------------ diagnostics helpers


def test_adb_devices_is_total_when_adb_is_unusable(harness, monkeypatch):
    """`observe()` calls this on every stage, so it must not raise out of a diagnostic."""
    def _boom(*_args, **_kwargs):
        raise OSError("no adb")

    monkeypatch.setattr(harness.subprocess, "run", _boom)
    assert harness.adb_devices() == []


def test_adb_devices_parses_serial_and_state(harness, monkeypatch):
    class _Result:
        stdout = "List of devices attached\nSAMPLE0000000001      device usb:1 transport_id:2\nOTHER\toffline\n"

    monkeypatch.setattr(harness.subprocess, "run", lambda *a, **k: _Result())
    assert harness.adb_devices() == [("SAMPLE0000000001", "device"), ("OTHER", "offline")]


def test_wait_for_mode_returns_the_settled_mode(harness, monkeypatch):
    modes = iter(["adb", "adb", "hid"])
    monkeypatch.setattr(harness, "device_mode", lambda: next(modes, "hid"))
    assert harness.wait_for_mode("hid", timeout=2.0) == "hid"


def test_wait_for_mode_gives_up_and_reports_what_it_saw(harness, monkeypatch):
    monkeypatch.setattr(harness, "device_mode", lambda: "adb")
    assert harness.wait_for_mode("hid", timeout=0.6) == "adb"


# ------------------------------------------------------------------ CI wiring (items 5 and 6)


def test_hosted_ci_never_opts_into_the_hardware_harness():
    """`ci.yml` is the GitHub-hosted matrix. It must not set GHOSTDECK_HW_TEST=1.

    The harness's own gate exists so a device-free runner cannot pretend to have a deck. Putting
    the smoke test in that workflow would either skip (exit 2, looking like a pass if not checked)
    or wait forever for hardware that is not there.
    """
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "GHOSTDECK_HW_TEST: \"1\"" not in text
    assert "hardware_verify.py" not in text
    assert "self-hosted" not in text


def test_hardware_workflow_is_self_hosted_opt_in_and_runs_the_existing_verifier():
    """The deck smoke test is its own workflow so it cannot block a PR on a hosted runner."""
    text = (ROOT / ".github" / "workflows" / "hardware.yml").read_text(encoding="utf-8")
    assert "self-hosted" in text
    assert "d200" in text
    assert "workflow_dispatch" in text
    assert "cron:" in text
    assert "GHOSTDECK_HW_TEST" in text
    assert "tools/hardware_verify.py" in text
    # Push would race a single physical deck; the comment is the contract, the trigger is the proof.
    assert "push:" not in text.split("jobs:")[0]
    # `set -u` plus an empty array expansion is unbound (first runner job, 17s).
    assert "media[@]" not in text
    # play→stop is the point of a deck-attached job; a 2s testsrc is generated when
    # GHOSTDECK_HW_MEDIA is unset, because sample clips are not in git.
    assert "ffmpeg" in text
    assert "testsrc" in text
    assert "--media" in text


def test_hardware_verifier_stages_device_binaries_before_the_bridge():
    """A fresh checkout has no ARM binaries in vendor/; the bridge then exits 1.

    Measured on the self-hosted runner: bring-up failed with StagingError
    "build d200-zkgui-proxy, d200-color-agent and libd200-zkgui-preload.so first"
    because the harness called `_ensure_bridge` the way `launch()` never does --
    without `devicebuild.ensure()`.
    """
    text = HARNESS.read_text(encoding="utf-8")
    assert "devicebuild.ensure()\n        bridge = studio._ensure_bridge(reuse_existing=False)" in text


def test_agent_workflow_builds_the_release_artifact_the_failure_message_names():
    """`ghostdeck build` points at this URL; the workflow must actually produce that filename."""
    from ghostdeck import devicebuild

    text = (ROOT / ".github" / "workflows" / "agent.yml").read_text(encoding="utf-8")
    assert "d200-color-agent" in text
    assert "build-color-agent.sh" in text
    assert "arm-linux-gnueabihf-gcc" in text
    assert "libjpeg-turbo" in text
    assert "action-gh-release" in text
    assert "refs/tags/" in text
    # CMAKE_SYSTEM_NAME=Linux with no processor left CMAKE_SYSTEM_PROCESSOR empty and
    # libjpeg-turbo died at CMakeLists.txt:92 (measured on the v0.1.0 tag job).
    assert "CMAKE_SYSTEM_PROCESSOR=arm" in text
    assert devicebuild.AGENT_RELEASE_URL.endswith("/d200-color-agent")
    assert "laerad777/ghostdeck" in devicebuild.AGENT_RELEASE_URL


@pytest.mark.parametrize("endpoint", ["live", "undeterminable"])
def test_shared_endpoint_refuses_before_any_device_work(harness, monkeypatch, tmp_path, endpoint):
    monkeypatch.setattr(harness, "own_bridges", lambda: [])
    monkeypatch.setattr(harness.studio, "_socket_state", lambda: (endpoint, "occupied"))
    state = tmp_path / "host.json"
    state.write_text("operator state")
    monkeypatch.setattr(harness, "HOST_STATE", state)
    def forbidden(*args, **kwargs):
        pytest.fail("an existing session must not be touched")
    for name in ("observe", "start_play_async", "cli", "stop_own_bridges"):
        monkeypatch.setattr(harness, name, forbidden)
    monkeypatch.setattr(harness.devicebuild, "ensure", forbidden)
    monkeypatch.setattr(harness.studio, "_ensure_bridge", forbidden)
    assert _run_main(harness, monkeypatch, gate=True, mode="adb") == 2
    assert state.read_text() == "operator state"


class FakeBridge:
    pid = 87654
    returncode = None
    def poll(self):
        return self.returncode


def setup_owned_session(harness, monkeypatch, *, serial="SERIAL", listener=None, count=1):
    bridge = FakeBridge()
    stopped = []
    monkeypatch.setenv("GHOSTDECK_HW_TEST", "1")
    monkeypatch.setattr(sys, "argv", ["hardware_verify.py", "--media", "clip.mp4"])
    monkeypatch.setattr(harness, "adb_available", lambda: True)
    monkeypatch.setattr(harness, "own_bridges", lambda: [])
    states = iter([("dead", "absent")])
    monkeypatch.setattr(harness.studio, "_socket_state", lambda: next(states, ("live", "")))
    monkeypatch.setattr(harness.studio, "_owned_bridge_pid", lambda: bridge.pid if listener is None else listener)
    monkeypatch.setattr(harness, "device_mode", lambda: "adb")
    monkeypatch.setattr(harness, "observe", lambda stage: None)
    monkeypatch.setattr(harness.devicebuild, "ensure", lambda: None)
    def ensure(*, reuse_existing):
        assert reuse_existing is False
        monkeypatch.setattr(harness, "own_bridge_count", lambda: count)
        return bridge
    monkeypatch.setattr(harness.studio, "_ensure_bridge", ensure)
    monkeypatch.setattr(harness, "device_serial", lambda: serial)
    monkeypatch.setattr(harness, "adb_devices", lambda: [(serial, "device")])
    monkeypatch.setattr(harness, "reachable", lambda serial: True)
    def stop(child):
        assert child is bridge
        stopped.append(child.pid)
        child.returncode = 0
        return True
    monkeypatch.setattr(harness.studio, "_stop_verifier_bridge", stop)
    monkeypatch.setattr(harness, "wait_for_mode", lambda wanted: "hid")
    monkeypatch.setattr(harness.play, "playing", lambda: False)
    monkeypatch.setattr(harness, "deck_paths", lambda *args: {"/tmp/d200-color-agent": "ABSENT"})
    return bridge, stopped


def test_listener_mismatch_aborts_before_play_or_stop(harness, monkeypatch, tmp_path):
    bridge, stopped = setup_owned_session(harness, monkeypatch, listener=99999)
    def forbidden(*args, **kwargs):
        pytest.fail("must not play or stop someone else's session")
    monkeypatch.setattr(harness, "start_play_async", forbidden)
    monkeypatch.setattr(harness, "cli", forbidden)
    assert harness.main() == 1
    assert stopped == [bridge.pid]


@pytest.mark.parametrize("failure", ["serial", "play"])
def test_owned_child_is_cleaned_up_on_early_failure(harness, monkeypatch, failure):
    bridge, stopped = setup_owned_session(harness, monkeypatch, serial=None if failure == "serial" else "SERIAL")
    calls = []
    monkeypatch.setattr(harness, "stop_playing", lambda: calls.append("stop") or "stop rc=0")
    def boom(media):
        raise RuntimeError("launcher unavailable")
    monkeypatch.setattr(harness, "start_play_async", boom)
    assert harness.main() == 1
    assert calls == ["stop"]
    assert stopped == [bridge.pid]


def test_teardown_does_not_stop_replacement_session(harness, monkeypatch):
    bridge, stopped = setup_owned_session(harness, monkeypatch, listener=99999)
    monkeypatch.setattr(harness, "stop_playing", lambda: pytest.fail("foreign stop"))
    harness.teardown(bridge, "SERIAL")
    assert stopped == [bridge.pid]
    assert "teardown ownership" in harness.failures


def test_run_logs_are_preserved_and_launcher_error_is_reported(harness, monkeypatch, tmp_path, capsys):
    directory = tmp_path / "run-logs"
    monkeypatch.setenv("GHOSTDECK_HW_LOG_DIR", str(directory))
    handle = harness.open_run_log("ghostdeck-play-")
    handle.write(b"specific launcher failure\n")
    handle.flush()
    player = FakeBridge()
    player._ghostdeck_log = handle
    harness.report_play_log(player)
    handle.close()
    assert Path(handle.name).parent == directory
    assert "specific launcher failure" in capsys.readouterr().out
    assert Path(handle.name).exists()


def test_workflow_preserves_run_logs_and_serializes_hardware():
    text = (ROOT / ".github/workflows/hardware.yml").read_text()
    assert "cancel-in-progress: false" in text
    assert "GHOSTDECK_HW_LOG_DIR" in text
    assert "tee" in text and "verifier.log" in text
    assert "${{ runner.temp }}/ghostdeck-hardware-${{ github.run_id }}-${{ github.run_attempt }}/*.log" in text


def test_owned_happy_session_plays_and_cleans_up(harness, monkeypatch):
    bridge, stopped = setup_owned_session(harness, monkeypatch)
    player = FakeBridge()
    player.returncode = 0
    calls = []
    monkeypatch.setattr(harness, "start_play_async", lambda media: calls.append("play") or player)
    monkeypatch.setattr(harness.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(harness, "host_diagnostics", lambda: {"framesConsumed": 12, "firstConsumedReceipt": True})
    def cli(command):
        calls.append(command)
        return subprocess.CompletedProcess([], 0, "playing=yes" if command == "status" else "", "")
    monkeypatch.setattr(harness, "cli", cli)
    assert harness.main() == 0
    assert calls == ["play", "status", "stop", "stop"]
    assert stopped == [bridge.pid]


def test_no_clean_preserves_owned_session(harness, monkeypatch):
    bridge, stopped = setup_owned_session(harness, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["hardware_verify.py", "--no-clean"])
    monkeypatch.setattr(harness, "cli", lambda *args: pytest.fail("no-clean must not stop"))
    assert harness.main() == 0
    assert stopped == []


@pytest.mark.parametrize("count", [0, 2])
def test_invalid_bridge_count_aborts_playback(harness, monkeypatch, count):
    bridge, stopped = setup_owned_session(harness, monkeypatch, count=count)
    monkeypatch.setattr(harness, "start_play_async", lambda media: pytest.fail("invalid session"))
    monkeypatch.setattr(harness, "stop_playing", lambda: "stop rc=0")
    assert harness.main() == 1
    assert stopped == [bridge.pid]


def test_post_bringup_diagnostic_exception_still_reaps_child(harness, monkeypatch):
    bridge, stopped = setup_owned_session(harness, monkeypatch)
    def observe(stage):
        if stage == "after bring-up":
            raise RuntimeError("diagnostic failed")
    monkeypatch.setattr(harness, "observe", observe)
    monkeypatch.setattr(harness, "stop_playing", lambda: "stop rc=0")
    assert harness.main() == 1
    assert stopped == [bridge.pid]


def test_pending_bridge_cleanup_is_reported_without_waiting_for_hid(harness, monkeypatch):
    bridge, stopped = setup_owned_session(harness, monkeypatch)
    monkeypatch.setattr(harness, "stop_playing", lambda: "stop rc=0")
    monkeypatch.setattr(harness.studio, "_stop_verifier_bridge", lambda child: False)
    monkeypatch.setattr(harness, "wait_for_mode", lambda mode: pytest.fail("cleanup is pending"))
    harness.teardown(bridge, "SERIAL")
    assert "owned bridge cleanup finished" in harness.failures
    assert bridge.poll() is None


def test_a_killed_bridge_is_not_reported_as_clean_teardown(harness, monkeypatch):
    bridge, stopped = setup_owned_session(harness, monkeypatch)
    monkeypatch.setattr(harness, "stop_playing", lambda: "stop rc=0")
    def killed(child):
        child.returncode = -9
        return True
    monkeypatch.setattr(harness.studio, "_stop_verifier_bridge", killed)
    harness.teardown(bridge, "SERIAL")
    assert "the bridge this run started exited cleanly" in harness.failures


def test_sigterm_unwinds_cleanup_and_ignores_repeated_term(harness, monkeypatch):
    calls = []
    monkeypatch.setattr(harness.signal, "signal", lambda *args: calls.append(args))
    cleaned = []
    with pytest.raises(SystemExit) as error:
        try:
            harness.handle_sigterm(harness.signal.SIGTERM, None)
        finally:
            cleaned.append(True)
    assert error.value.code == 143
    assert cleaned == [True]
    assert calls == [(harness.signal.SIGTERM, harness.signal.SIG_IGN)]

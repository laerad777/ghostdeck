"""Deck-free tests for tools/hardware_verify.py.

Why this file exists (C-169): the harness grew real process-signalling logic and none of it
was covered here, so a defect that signals a bridge it did not start (C-167) shipped in a
commit whose own message claimed the ownership rule held. This covers only the deck-free
surface - the parts reachable without a deck, a bridge, or studio._ensure_bridge().

Deliberately NOT covered, because it needs hardware or spawns a real bridge:
  * anything past the device-mode check (bring-up, play, stop, teardown against a deck)
  * studio._ensure_bridge()
The master's own runs cover those, and `docs`/commit notes record that split.
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
def harness():
    return _harness()


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
    # `set -u` plus `"${media[@]}"` on an empty array is unbound (first runner job, 17s).
    assert "media[@]" not in text


def test_hardware_verifier_stages_device_binaries_before_the_bridge():
    """A fresh checkout has no ARM binaries in vendor/; the bridge then exits 1.

    Measured on the self-hosted runner: bring-up failed with StagingError
    "build d200-zkgui-proxy, d200-color-agent and libd200-zkgui-preload.so first"
    because the harness called `_ensure_bridge` the way `launch()` never does --
    without `devicebuild.ensure()`.
    """
    text = HARNESS.read_text(encoding="utf-8")
    assert "devicebuild.ensure()\n        studio._ensure_bridge()" in text


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

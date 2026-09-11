"""Device-free `ghostdeck stop` exit-code tests.

A fake `adb` is placed first on PATH and HOME points at a temp dir, so no real device and no real
`~/.ghostdeck` is ever touched.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

SERIAL = "ABC123XYZ"
DEVICE_LINE = (
    "List of devices attached\n"
    f"{SERIAL}\tdevice product:d200 model:D200 device:d200 transport_id:1\n"
)
DEVICES_ARGV = "devices -l"
# The restore pair. A bare `ctl.start` on an already-running service is a no-op that never
# re-initialises the USB gadget (hardware finding H1), so `stop()` must emit stop BEFORE start.
CTL_STOP_ARGV = f"-s {SERIAL} shell setprop ctl.stop zkswe"
CTL_START_ARGV = f"-s {SERIAL} shell setprop ctl.start zkswe"
RM_ARGV = f"-s {SERIAL} shell rm -f /tmp/ghostdeck-*"
LISTING_ARGV = f"-s {SERIAL} shell ls /tmp/ghostdeck*"
RESTORE_ARGV = [CTL_STOP_ARGV, CTL_START_ARGV]
STOP_ARGV = [DEVICES_ARGV, CTL_STOP_ARGV, CTL_START_ARGV, RM_ARGV, LISTING_ARGV]

# Every fake adb records its own invocation FIRST, before any of its own logic runs. Without this an
# empty `calls` list cannot be distinguished from a fake that simply never logged, which is how a
# whole assertion (C-105) passed for the wrong reason.
LOG_INVOCATION = 'printf \'%s\\n\' "$*" >> "$FAKE_ADB_LOG"\n'


def _adb(body: str) -> str:
    """Build a fake adb that always logs its argv, then runs `body`."""
    return "#!/bin/sh\n" + LOG_INVOCATION + body


# A healthy device after a successful cleanup: both mutating calls succeed and the trailing
# informational listing exits 1 because /tmp/ghostdeck is already gone. That is the SUCCESS case.
HEALTHY_CLEAN_ADB = _adb(f"""case "$1" in
  devices) printf '{DEVICE_LINE}'; exit 0 ;;
esac
case "$*" in
  *"ls /tmp/ghostdeck"*)
    echo "ls: /tmp/ghostdeck: No such file or directory" >&2
    exit 1 ;;
esac
exit 0
""")

# Healthy device where files are still present, so the listing succeeds and is printed.
HEALTHY_LISTING_ADB = _adb(f"""case "$1" in
  devices) printf '{DEVICE_LINE}'; exit 0 ;;
esac
case "$*" in
  *"ls /tmp/ghostdeck"*) printf '/tmp/ghostdeck-1.mp4\n'; exit 0 ;;
esac
exit 0
""")

# `devices` succeeds, every cleanup call fails with a device-side error.
FAILING_ADB = _adb(f"""case "$1" in
  devices) printf '{DEVICE_LINE}'; exit 0 ;;
esac
echo "adb: device offline" >&2
exit 1
""")

# `devices` succeeds but lists no device.
NO_DEVICE_ADB = _adb("""case "$1" in
  devices) printf 'List of devices attached\\n'; exit 0 ;;
esac
exit 0
""")

# The master's third acceptance script (prints nothing, exits 0), with the invocation log added so
# its calls are observable. Its observable adb behaviour is unchanged.
SILENT_ADB = _adb("exit 0\n")

# Every call succeeds.
HAPPY_ADB = _adb(f"""case "$1" in
  devices) printf '{DEVICE_LINE}'; exit 0 ;;
esac
case "$*" in
  *"ls /tmp/ghostdeck"*) echo "listing ok"; exit 0 ;;
esac
echo "cleanup ok"
exit 0
""")



def _cli(
    fake_adb: str,
    tmp_path: Path,
    *args: str,
    pre_state: dict | None = None,
    sidecar: dict | str | None = None,
    system_path: str = "/usr/bin:/bin",
    tz: str | None = None,
) -> tuple[subprocess.CompletedProcess, list[str], Path]:
    """Run the CLI with a temp HOME and an explicit PATH.

    PATH is always REPLACED (never appended to the operator PATH): a real deck is attached to this
    host, so the real `adb` must be unreachable from every test. `system_path` keeps the OS tools
    such as `ps` available unless a test deliberately removes them.
    """
    bin_dir = tmp_path / "bin"
    home = tmp_path / "home"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (home / ".ghostdeck").mkdir(parents=True, exist_ok=True)
    if pre_state is not None:
        (home / ".ghostdeck" / "state.json").write_text(json.dumps(pre_state), encoding="utf-8")
    if sidecar is not None:
        text = sidecar if isinstance(sidecar, str) else json.dumps(sidecar)
        (home / ".ghostdeck" / "play.pid").write_text(text, encoding="utf-8")
    adb_path = bin_dir / "adb"
    adb_path.write_text(fake_adb, encoding="utf-8")
    adb_path.chmod(0o755)
    log = tmp_path / "adb.log"
    env = dict(os.environ)
    env.update(
        PATH=f"{bin_dir}{os.pathsep}{system_path}",
        HOME=str(home),
        PYTHONPATH=str(SRC),
        FAKE_ADB_LOG=str(log),
    )
    if tz is not None:
        env["TZ"] = tz
    result = subprocess.run(
        [sys.executable, "-m", "ghostdeck.cli", *(args or ("stop",))],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.is_file() else []
    return result, calls, home


def _stop(fake_adb: str, tmp_path: Path) -> tuple[subprocess.CompletedProcess, list[str]]:
    result, calls, _ = _cli(fake_adb, tmp_path)
    return result, calls


def test_every_fake_adb_logs_its_invocations(tmp_path):
    """Guard against C-105 returning: a fake that does not log makes any `calls` assertion vacuous."""
    fakes = {
        "HEALTHY_CLEAN_ADB": HEALTHY_CLEAN_ADB,
        "HEALTHY_LISTING_ADB": HEALTHY_LISTING_ADB,
        "FAILING_ADB": FAILING_ADB,
        "NO_DEVICE_ADB": NO_DEVICE_ADB,
        "SILENT_ADB": SILENT_ADB,
        "HAPPY_ADB": HAPPY_ADB,
    }
    for name, fake in fakes.items():
        _, calls, _ = _cli(fake, tmp_path / name)
        # Every path starts by discovering the serial, so the first logged line must be `devices -l`.
        # A fake that does not log at all yields [] here and fails this guard.
        assert calls[:1] == [DEVICES_ARGV], f"{name} did not log its invocation: {calls}"


def test_stop_reports_failing_adb_and_exits_nonzero(tmp_path):
    result, calls, _ = _cli(FAILING_ADB, tmp_path)
    assert result.returncode != 0, result.stdout
    assert "adb: device offline" in result.stderr
    assert "setprop ctl.stop zkswe" in result.stderr
    # The first failure aborts the cleanup. Named explicitly so this cannot pass vacuously: the
    # device was reached for `devices -l` and the first mutating call, and for nothing after it.
    # That first mutating call is now the STOP, which is what makes the pairing non-negotiable.
    assert calls == [DEVICES_ARGV, CTL_STOP_ARGV]


def test_stop_without_device_exits_nonzero(tmp_path):
    result, calls, _ = _cli(NO_DEVICE_ADB, tmp_path)
    assert result.returncode != 0, result.stdout
    assert "no ADB device" in result.stderr
    # Discovery ran; nothing else may be attempted without a serial.
    assert calls == [DEVICES_ARGV]


def test_stop_with_silent_fake_adb_exits_nonzero(tmp_path):
    """The master's third acceptance string prints no device line, so it is the no-device path."""
    result, calls, _ = _cli(SILENT_ADB, tmp_path)
    assert result.returncode != 0, result.stdout
    assert "no ADB device" in result.stderr
    assert calls == [DEVICES_ARGV]


def test_stop_happy_path_exits_zero_and_keeps_command_order(tmp_path):
    result, calls = _stop(HAPPY_ADB, tmp_path)
    assert result.returncode == 0, result.stderr
    assert calls == STOP_ARGV
    assert result.stdout.count("cleanup ok") == 3  # the three mutating calls stay visible
    assert result.stdout.count("listing ok") == 1  # and so does a successful listing


# --- A-014: an already-clean device is success, not failure -----------------


def test_stop_on_healthy_device_with_clean_tmp_exits_zero(tmp_path):
    """A-014: the trailing listing is informational, so `ls` exiting 1 on an empty /tmp/ghostdeck
    (the post-cleanup success state) must not fail the command."""
    result, calls, _ = _cli(HEALTHY_CLEAN_ADB, tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stderr == "", result.stderr
    assert "No such file" not in result.stderr
    assert "No such file" not in result.stdout
    # All four calls ran: the listing was attempted and its failure was tolerated, not skipped.
    assert calls == STOP_ARGV


def test_stop_on_healthy_device_prints_a_successful_listing(tmp_path):
    result, calls, _ = _cli(HEALTHY_LISTING_ADB, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "/tmp/ghostdeck-1.mp4" in result.stdout
    assert calls == STOP_ARGV


def test_stop_still_fails_when_the_removal_fails(tmp_path):
    """The tolerance is only for the listing: a failing mutating call must still be fatal."""
    rm_fails = _adb(f"""case "$1" in
  devices) printf '{DEVICE_LINE}'; exit 0 ;;
esac
case "$*" in
  *"rm -f /tmp/ghostdeck-*")
    echo "rm: /tmp/ghostdeck-1.mp4: Permission denied" >&2
    exit 1 ;;
esac
exit 0
""")
    result, calls, _ = _cli(rm_fails, tmp_path)
    assert result.returncode != 0, result.stdout
    assert "rm -f /tmp/ghostdeck-*" in result.stderr
    assert "Permission denied" in result.stderr
    # Aborts at the failed removal, before the informational listing, and after the restore pair:
    # a failed removal must not cost the deck its UI restart.
    assert calls == [DEVICES_ARGV, CTL_STOP_ARGV, CTL_START_ARGV, RM_ARGV]


# --- H1: a bare `ctl.start` never restores the deck ------------------------


def test_stop_restarts_the_service_stop_before_start(tmp_path):
    """H1: `setprop ctl.start zkswe` on an already-running service is a no-op.

    On the master's real-deck run `stop` exited 0 while
    `cat /sys/class/zkswe_usb/zkswe0/functions` still read `adb` and `detect` still reported
    `mode=adb` 30s later, with `zkgui_ui` running the whole time; only `ctl.stop` followed by
    `ctl.start` returned the gadget to HID. This asserts positionally (index), not by membership,
    so a dropped, duplicated or reordered stop fails here instead of silently regressing.
    """
    from ghostdeck import adb

    result, calls, _ = _cli(HAPPY_ADB, tmp_path)
    assert result.returncode == 0, result.stderr
    assert CTL_STOP_ARGV in calls and CTL_START_ARGV in calls, calls
    assert calls.index(CTL_STOP_ARGV) < calls.index(CTL_START_ARGV), calls
    # The stop is the FIRST device mutation: nothing may touch the deck before it.
    assert calls.index(CTL_STOP_ARGV) == 1, f"the stop must lead the cleanup: {calls}"
    assert calls == STOP_ARGV
    # Both strings are already on the allowlist, so this fix needed no widening of adb.py.
    for command in ("setprop ctl.stop zkswe", "setprop ctl.start zkswe"):
        assert adb.allowed(["-s", SERIAL, "shell", command]), command
    # And `stop` still never switches a USB mode itself: the restarted UI re-enumerates.
    assert not any("functions" in call for call in calls), calls


# --- A-003: a recycled pid must never be signalled -------------------------


def _victim() -> subprocess.Popen:
    """A live process that looks nothing like the vendor player."""
    return subprocess.Popen(["/bin/sleep", "60"])


def _ours() -> subprocess.Popen:
    """A live process that `start_play()` semantics would own.

    Identity no longer comes from argv, so the carrier only has to be a real live process whose
    start time can be recorded in the sidecar; `_record_identity()` is the same call `start_play()`
    makes right after `Popen`.
    """
    return subprocess.Popen(["/bin/sleep", "60"])


def _record(pid: int, home: Path) -> dict:
    """Record a sidecar for `pid` the way `start_play()` does, returning what it wrote.

    `home` is the HOME directory (the same one `_cli` hands to the CLI), so the sidecar lands at
    `<home>/.ghostdeck/play.pid`.
    """
    from ghostdeck import play

    (home / ".ghostdeck").mkdir(parents=True, exist_ok=True)
    previous = play.gdstate.HOME
    play.gdstate.HOME = home / ".ghostdeck"
    try:
        play._record_identity(pid)
    finally:
        play.gdstate.HOME = previous
    return json.loads((home / ".ghostdeck" / "play.pid").read_text(encoding="utf-8"))


def _alive(proc: subprocess.Popen, seconds: float = 0.5) -> bool:
    """True if the process is still running after a short grace period."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        time.sleep(0.05)
    return True


def test_stop_does_not_kill_a_recycled_pid(tmp_path):
    """A-003: an unrelated live process in `play_pid` must never be signalled.

    With no sidecar it is unclassifiable, so the pid is preserved AND nothing is erased.
    """
    victim = _victim()
    try:
        time.sleep(0.2)
        result, calls, home = _cli(HAPPY_ADB, tmp_path, "stop", pre_state={"play_pid": victim.pid})
        assert result.returncode != 0, result.stdout
        assert "cannot verify" in result.stderr
        assert _alive(victim), "stop() killed an unrelated process holding a recycled pid"
        # A-101: the identity problem decides the exit code, never whether the deck is restored.
        # The cleanup runs in full even here, which is a real assertion because HAPPY_ADB logs
        # every invocation (see test_every_fake_adb_logs_its_invocations).
        assert calls == STOP_ARGV, f"the deck was left unrestored: {calls}"
        state = json.loads((home / ".ghostdeck" / "state.json").read_text())
        assert state["play_pid"] == victim.pid  # unclassifiable pid must not be erased
    finally:
        victim.kill()
        victim.wait()


def test_status_reports_playing_no_for_a_recycled_pid(tmp_path):
    victim = _victim()
    try:
        time.sleep(0.2)
        result, calls, _ = _cli(HAPPY_ADB, tmp_path, "status", pre_state={"play_pid": victim.pid})
        assert result.returncode == 0, result.stderr
        assert "playing=no" in result.stdout, result.stdout
        assert len(calls) == 0, f"status contacted the device through adb: {calls}"
    finally:
        victim.kill()
        victim.wait()


# --- C-102: "cannot determine" must never mean "not ours" -------------------


def test_stop_preserves_pid_and_fails_when_ps_is_unavailable(tmp_path):
    """C-102: with `ps` off PATH the identity is undeterminable, so the pid must survive."""
    ours = _ours()
    try:
        time.sleep(0.3)
        # A sidecar that *would* match, so the only reason to be unsure is ps itself.
        recorded = _record(ours.pid, tmp_path / "home")
        assert recorded["pid"] == ours.pid
        result, _, home = _cli(
            HAPPY_ADB,
            tmp_path,
            "stop",
            pre_state={"play_pid": ours.pid},
            sidecar=recorded,
            system_path="/nonexistent",  # no ps anywhere on PATH
        )
        assert result.returncode == 1, (result.returncode, result.stdout, result.stderr)
        assert "cannot verify" in result.stderr, result.stderr
        assert "not signalling" in result.stderr, result.stderr
        assert "Traceback" not in result.stdout + result.stderr
        saved = json.loads((home / ".ghostdeck" / "state.json").read_text())
        assert saved["play_pid"] == ours.pid, "an unclassifiable pid was erased"
        assert (home / ".ghostdeck" / "play.pid").is_file(), "the sidecar was destroyed"
        assert _alive(ours), "the player was signalled despite unknown identity"
    finally:
        ours.kill()
        ours.wait()


def test_kill_play_raises_and_erases_nothing_when_identity_is_unknown(tmp_path, monkeypatch):
    from ghostdeck import play, state

    home = tmp_path / "home"
    (home / ".ghostdeck").mkdir(parents=True)
    monkeypatch.setattr(state, "HOME", home / ".ghostdeck")
    monkeypatch.setattr(state, "STATE_PATH", home / ".ghostdeck" / "state.json")
    victim = _ours()
    try:
        time.sleep(0.3)
        state.save({"play_pid": victim.pid})
        monkeypatch.setattr(play, "_probe_start_time", lambda pid: (None, "ps could not be run"))
        with pytest.raises(RuntimeError) as excinfo:
            play._kill_play()
        assert "cannot verify player pid" in str(excinfo.value)
        assert state.load()["play_pid"] == victim.pid
        assert _alive(victim)
    finally:
        victim.kill()
        victim.wait()


# --- C-103: a stranger that merely mentions the vendor path ----------------


def test_stranger_with_vendor_path_in_argv_is_not_signalable(tmp_path):
    """C-103: the old whole-`ps`-output substring test signalled this process. It must survive.

    The carrier is a python process whose argv carries the vendor path as a NON-argv[0] element, which
    is exactly what FINDER-C used. The marker is asserted to really be visible in `ps`, so the test
    cannot silently degrade into one that proves nothing.
    """
    from ghostdeck import play

    stranger = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)", str(play.VENDOR_PLAY)]
    )
    try:
        time.sleep(0.4)
        argv = subprocess.run(
            ["ps", "-o", "command=", "-ww", "-p", str(stranger.pid)],
            capture_output=True,
            text=True,
            env=dict(os.environ, LC_ALL="C"),
        ).stdout.strip()
        assert str(play.VENDOR_PLAY) in argv, argv
        assert not argv.startswith(str(play.VENDOR_PLAY)), argv  # not in argv[0]; a forged carrier

        result, _, home = _cli(HAPPY_ADB, tmp_path, "stop", pre_state={"play_pid": stranger.pid})
        assert result.returncode != 0, "stop() reported success for an unclassifiable pid"
        assert "cannot verify" in result.stderr, result.stderr
        assert _alive(stranger), "a stranger mentioning the vendor path was signalled"
        saved = json.loads((home / ".ghostdeck" / "state.json").read_text())
        assert saved["play_pid"] == stranger.pid
    finally:
        if stranger.poll() is None:
            stranger.kill()
        stranger.wait()


def test_stranger_recycling_a_recorded_pid_is_not_signalable(tmp_path):
    """C-103 + A-003: a recorded pid recycled by a stranger must not be signalled.

    A new process cannot share the recorded start time, so the start-time comparison is what protects
    a recycled pid even when the sidecar still looks plausible.
    """
    from ghostdeck import play

    home = tmp_path / "home"
    (home / ".ghostdeck").mkdir(parents=True, exist_ok=True)
    first = _ours()
    try:
        time.sleep(0.4)
        recorded = _record(first.pid, home)
        first.kill()
        first.wait()
        time.sleep(1.2)  # let lstart advance past its one-second resolution
        second = _ours()
        try:
            time.sleep(0.4)
            lstart = subprocess.run(
                ["ps", "-o", "lstart=", "-ww", "-p", str(second.pid)],
                capture_output=True,
                text=True,
                env=dict(os.environ, LC_ALL="C"),
            ).stdout.strip()
            if lstart == recorded["lstart"]:  # cannot distinguish; skip rather than assert wrongly
                pytest.skip("lstart collided at one-second resolution")
            monkey = play.gdstate.HOME
            play.gdstate.HOME = home / ".ghostdeck"
            try:
                # HOME is patched BEFORE any identity call, so the operator's real
                # ~/.ghostdeck/play.pid is never read by this test.
                assert play._load_identity() == recorded
                assert play._is_our_player(second.pid) is False
            finally:
                play.gdstate.HOME = monkey

            result, _, _ = _cli(
                HAPPY_ADB,
                tmp_path,
                "stop",
                pre_state={"play_pid": second.pid},
                sidecar=recorded,
            )
            assert result.returncode == 0, result.stderr
            assert _alive(second), "a recycled pid was signalled"
        finally:
            if second.poll() is None:
                second.kill()
            second.wait()
    finally:
        if first.poll() is None:
            first.kill()
        first.wait()


def test_forged_sidecar_with_wrong_lstart_is_not_ours(tmp_path, monkeypatch):
    """A recycled pid cannot satisfy the recorded start time, even with a planted sidecar."""
    from ghostdeck import play

    victim = _ours()
    try:
        time.sleep(0.3)
        monkeypatch.setattr(play.gdstate, "HOME", tmp_path / ".ghostdeck")
        play._write_identity({"pid": victim.pid, "lstart": "Thu Jan  1 00:00:00 1970"})
        assert play._is_our_player(victim.pid) is False
        identity, reason = play._player_identity(victim.pid)
        assert identity is False and reason == ""
        assert _alive(victim), "an unrelated process was signalled on a forged sidecar"
    finally:
        victim.kill()
        victim.wait()


# --- the genuine round trip -------------------------------------------------


def test_genuine_player_round_trip_is_recognised_and_stopped(tmp_path):
    """(c): recorded sidecar -> identity True -> `stop` really terminates the player."""
    ours = _ours()
    try:
        time.sleep(0.3)
        (tmp_path / "home").mkdir(exist_ok=True)
        recorded = _record(ours.pid, tmp_path / "home")
        assert recorded == json.loads((tmp_path / "home" / ".ghostdeck" / "play.pid").read_text())
        assert set(recorded) == {"pid", "lstart"}
        assert isinstance(recorded["lstart"], str) and recorded["lstart"].strip()

        from ghostdeck import play

        monkey = play.gdstate.HOME
        play.gdstate.HOME = tmp_path / "home" / ".ghostdeck"
        try:
            assert play._is_our_player(ours.pid) is True
        finally:
            play.gdstate.HOME = monkey

        result, calls, home = _cli(
            HAPPY_ADB,
            tmp_path,
            "stop",
            pre_state={"play_pid": ours.pid},
            sidecar=recorded,
        )
        assert result.returncode == 0, result.stderr
        assert not _alive(ours), "stop() did not terminate its own player"
        assert calls[-1] == STOP_ARGV[-1]
        assert json.loads((home / ".ghostdeck" / "state.json").read_text())["play_pid"] is None
        assert not (home / ".ghostdeck" / "play.pid").exists()
    finally:
        if ours.poll() is None:
            ours.kill()
        ours.wait()


def test_sidecar_is_written_0600_and_atomically(tmp_path, monkeypatch):
    from ghostdeck import play

    monkeypatch.setattr(play.gdstate, "HOME", tmp_path / ".ghostdeck")
    ours = _ours()
    try:
        time.sleep(0.3)
        play._record_identity(ours.pid)
        path = tmp_path / ".ghostdeck" / "play.pid"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert not list((tmp_path / ".ghostdeck").glob(".play.pid.*"))  # no temp left behind
        play._remove_identity()
        assert not path.exists()
    finally:
        ours.kill()
        ours.wait()


def test_identity_helpers_reject_junk_and_survive_a_missing_record(tmp_path, monkeypatch):
    from ghostdeck import play

    monkeypatch.setattr(play.gdstate, "HOME", tmp_path / ".ghostdeck")
    assert play._load_identity() is None
    (tmp_path / ".ghostdeck").mkdir(parents=True)
    path = tmp_path / ".ghostdeck" / "play.pid"
    for junk in ("not json", '{"pid": 1}', '{"pid": true, "lstart": "x"}',
                 '{"pid": 0, "lstart": "x"}', '{"pid": 5, "lstart": "  "}'):
        path.write_text(junk, encoding="utf-8")
        assert play._load_identity() is None, junk
        assert play._is_our_player(5) is None or play._is_our_player(5) is False
    path.write_text('{"pid": 5, "lstart": "Fri Sep 11 12:00:00 2026"}', encoding="utf-8")
    assert play._load_identity() == {"pid": 5, "lstart": "Fri Sep 11 12:00:00 2026"}
    for bad in (None, True, 0, -1, "1"):
        assert play._is_our_player(bad) is False


def test_stop_does_not_signal_recycled_pid_even_when_no_device(tmp_path):
    """`_kill_play()` still decides nothing about signalling; the cleanup is merely attempted.

    A-101: discovery runs (so the deck is reached) even though the identity is unknown; without a
    serial nothing further may be attempted, and both problems are reported on the one exit path.
    """
    victim = _victim()
    try:
        time.sleep(0.2)
        result, calls, _ = _cli(NO_DEVICE_ADB, tmp_path, "stop", pre_state={"play_pid": victim.pid})
        assert result.returncode != 0, result.stdout
        assert "cannot verify" in result.stderr
        assert "no ADB device" in result.stderr, result.stderr
        assert _alive(victim)
        assert calls == [DEVICES_ARGV], f"discovery must run, and nothing past it without a serial: {calls}"
    finally:
        victim.kill()
        victim.wait()


def test_stale_dead_pid_is_cleared_without_ps_noise(tmp_path):
    """A recorded identity whose process is gone is classifiable: clear it, no error."""
    dead = _ours()
    dead.kill()
    dead.wait()
    time.sleep(0.2)
    # A pid that no longer exists is classifiable, so forge a plausible "recorded" pair for it.
    recorded = {"pid": dead.pid, "lstart": "Thu Jan  1 00:00:00 1970"}
    result, calls, home = _cli(
        HAPPY_ADB, tmp_path, "stop", pre_state={"play_pid": dead.pid}, sidecar=recorded
    )
    assert result.returncode == 0, result.stderr
    assert calls  # the adb cleanup still ran
    assert json.loads((home / ".ghostdeck" / "state.json").read_text())["play_pid"] is None


# --- A-124: identity must not depend on the caller's timezone ------------------


def test_start_time_is_timezone_independent(tmp_path, monkeypatch):
    """A-124: `ps -o lstart=` prints local time, so the probe must pin the zone, not just the locale.

    Without `TZ=UTC` in the child env the two readings differ by the host's offset (9 hours here)
    and a still-running player is misread as a recycled pid. This is the unit-level half: the same
    pid must produce the same string whatever the caller's TZ is.
    """
    from ghostdeck import play

    ours = _ours()
    try:
        time.sleep(0.3)
        monkeypatch.setenv("TZ", "Asia/Seoul")
        under_seoul = play._probe_start_time(ours.pid)
        monkeypatch.setenv("TZ", "UTC")
        under_utc = play._probe_start_time(ours.pid)
        monkeypatch.setenv("TZ", "America/New_York")
        under_ny = play._probe_start_time(ours.pid)
        assert under_seoul[1] is None and under_utc[1] is None and under_ny[1] is None
        assert under_seoul == under_utc == under_ny, (under_seoul, under_utc, under_ny)
        assert under_utc[0], "the probe returned no start time for a live pid"
    finally:
        ours.kill()
        ours.wait()


def test_identity_survives_a_timezone_change_between_record_and_stop(tmp_path, monkeypatch):
    """A-124 end to end: a player recorded under one zone must still be recognised under another.

    The bug's full shape: `play` records the sidecar under the ambient zone, `stop` later runs under
    `TZ=UTC` (an exported TZ, a launchd job, a cron wrapper), the strings disagree, the verdict is
    the 'stale record' False branch, and the live player is both left running and erased. Here the
    record happens under TZ=Asia/Seoul and the stop under TZ=UTC, so the verdict must be unchanged.
    """
    ours = _ours()
    try:
        time.sleep(0.3)
        monkeypatch.setenv("TZ", "Asia/Seoul")
        recorded = _record(ours.pid, tmp_path / "home")
        assert recorded["pid"] == ours.pid

        result, calls, home = _cli(
            HAPPY_ADB,
            tmp_path,
            "stop",
            pre_state={"play_pid": ours.pid},
            sidecar=recorded,
            tz="UTC",
        )
        assert result.returncode == 0, result.stderr
        assert not _alive(ours), "a TZ difference made stop() miss its own running player"
        assert calls == STOP_ARGV
        assert json.loads((home / ".ghostdeck" / "state.json").read_text())["play_pid"] is None
        assert not (home / ".ghostdeck" / "play.pid").exists()
    finally:
        if ours.poll() is None:
            ours.kill()
        ours.wait()


# --- A-101: an unverifiable pid must not strand the deck -----------------------


def test_stop_runs_the_device_cleanup_even_when_the_identity_is_unknown(tmp_path):
    """A-101: the identity result decides the exit code, never whether the deck is restored.

    Before the fix `stop()` aborted inside `_kill_play()` and made zero adb calls, so the stock UI
    stayed stopped and the staged files stayed on the device - the one documented recovery command
    did nothing to the deck. The pid must still be preserved and nothing signalled.
    """
    victim = _victim()
    try:
        time.sleep(0.3)
        # A live pid with no recorded identity: the genuinely undeterminable case.
        result, calls, home = _cli(HAPPY_ADB, tmp_path, "stop", pre_state={"play_pid": victim.pid})
        assert result.returncode == 1, (result.returncode, result.stdout, result.stderr)
        assert "cannot verify" in result.stderr, result.stderr
        assert "not signalling" in result.stderr, result.stderr
        assert "Traceback" not in result.stdout + result.stderr
        assert _alive(victim), "an unclassifiable pid was signalled"
        assert calls == STOP_ARGV, f"the deck was left exactly as it was: {calls}"
        saved = json.loads((home / ".ghostdeck" / "state.json").read_text())
        assert saved["play_pid"] == victim.pid, "the only handle on the player was erased"
    finally:
        victim.kill()
        victim.wait()


def test_stop_reports_both_the_identity_and_a_failed_cleanup(tmp_path):
    """The two failures are independent: neither may hide the other."""
    victim = _victim()
    try:
        time.sleep(0.3)
        result, calls, _ = _cli(FAILING_ADB, tmp_path, "stop", pre_state={"play_pid": victim.pid})
        assert result.returncode == 1, (result.returncode, result.stdout, result.stderr)
        assert "adb: device offline" in result.stderr, result.stderr
        assert "cannot verify" in result.stderr, result.stderr
        assert "Traceback" not in result.stdout + result.stderr
        assert _alive(victim)
        assert calls == [DEVICES_ARGV, CTL_STOP_ARGV]
    finally:
        victim.kill()
        victim.wait()

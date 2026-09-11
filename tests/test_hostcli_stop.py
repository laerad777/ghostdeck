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
# The deck line shape, captured from the ATTACHED HARDWARE in FIX-1-T15. It carries only
# `usb:<...> transport_id:<n>` and NO product/model/device fields. Do not add them: fixtures that
# fabricated those fields were written to satisfy `_deck_serial()`'s old filter, which is exactly how
# 465 green tests certified a `stop` that could not work on the real deck. Deck identity now comes
# from the USB layer (stubbed per test below), so the shape here must stay the real one.
DEVICE_LINE = (
    "List of devices attached\n"
    f"{SERIAL}      device usb:18092032X transport_id:4\n"
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
    deck_serial: str | None = SERIAL,
) -> tuple[subprocess.CompletedProcess, list[str], Path]:
    """Run the CLI with a temp HOME, an explicit PATH, and a stubbed USB layer.

    PATH is always REPLACED (never appended to the operator PATH): a real deck is attached to this
    host, so the real `adb` must be unreachable from every test. `system_path` keeps the OS tools
    such as `ps` available unless a test deliberately removes them.

    `deck_serial` is what `usb.detect()` reports — the deck's primary identity signal since FIX-1-T15.
    It is STUBBED rather than left to the host, because otherwise every one of these tests would
    depend on whether this machine happens to have a D200 attached and a pyusb to see it. Pass None
    to model an environment where the USB layer has no verdict (no deck, or no backend).
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
    # The USB layer is stubbed in the CHILD, via a sitecustomize on PYTHONPATH, because these tests
    # assert real exit codes and therefore need a real subprocess.
    verdict = (
        {"serial": deck_serial, "vid": 0x18D1, "pid": 0xD002, "mode": "adb"}
        if deck_serial
        else {"serial": None, "vid": None, "pid": None, "mode": "none"}
    )
    shim = tmp_path / "shim"
    shim.mkdir(exist_ok=True)
    (shim / "sitecustomize.py").write_text(
        "# Test-only stub of the USB layer (FIX-1-T15).\n"
        "import ghostdeck.usb as _usb\n"
        f"_usb.detect = lambda: {verdict!r}\n",
        encoding="utf-8",
    )
    log = tmp_path / "adb.log"
    env = dict(os.environ)
    env.update(
        PATH=f"{bin_dir}{os.pathsep}{system_path}",
        HOME=str(home),
        PYTHONPATH=f"{shim}{os.pathsep}{SRC}",
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
    result, calls, _ = _cli(NO_DEVICE_ADB, tmp_path, deck_serial=None)
    assert result.returncode != 0, result.stdout
    assert "no ADB device" in result.stderr
    # Discovery ran; nothing else may be attempted without a serial.
    assert calls == [DEVICES_ARGV]


def test_stop_with_silent_fake_adb_exits_nonzero(tmp_path):
    """The master's third acceptance string prints no device line, so it is the no-device path."""
    result, calls, _ = _cli(SILENT_ADB, tmp_path, deck_serial=None)
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
        # The two properties this test exists for: an unclassifiable pid is not a running player, and
        # `status` is a read-only command that never touches the device.
        assert "playing=no" in result.stdout, result.stdout
        # T16 made `status` consult `adb devices -l`, because it cannot report a wedged transport
        # otherwise, so the original `len(calls) == 0` no longer describes the command. The property it
        # protected is KEPT and made more precise than it was: the only adb invocation is that
        # host-side inventory read, nothing is addressed to a device with `-s`, and `playing` is still
        # decided from local state alone rather than by asking the deck. (A-134 - `vhid.status()`
        # rewriting state.json - is a different mechanism and is untouched by this.)
        assert calls == [DEVICES_ARGV], f"status must not go beyond the device inventory: {calls}"
        assert not any("-s " in call for call in calls), f"nothing may be sent to a device: {calls}"
        # The exit code follows the reason (A-102). It is 0 here because `_cli` stubs the USB layer
        # with a positive deck verdict, so this child is never on the missing-backend path - the
        # expectation must track the CHILD's environment, not the pytest interpreter's.
        assert result.returncode == 0, (result.returncode, result.stderr)
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
            # A-144: read the start time with the PRODUCT's own oracle, not a second hand-rolled `ps`
            # call. `recorded` came from `_probe_start_time`, which pins `LC_ALL=C, TZ=UTC`; a local
            # read that pins only the locale returns a LOCAL-time string, so under a non-UTC ambient
            # zone the two sides always differ and this collision guard could never fire. The test
            # then fell through to `assert _alive(second)`, which in a genuine one-second collision
            # is exactly the state the product is entitled to produce - a spurious red that the
            # "any red test is a real regression" rule would read as a product defect.
            monkey = play.gdstate.HOME
            play.gdstate.HOME = home / ".ghostdeck"
            try:
                lstart = play._probe_start_time(second.pid)[0]
                if lstart is not None and lstart == recorded["lstart"]:
                    pytest.skip("lstart collided at one-second resolution")
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
        result, calls, _ = _cli(
            NO_DEVICE_ADB, tmp_path, "stop", pre_state={"play_pid": victim.pid}, deck_serial=None
        )
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


# --- A-116: a wedged adb server must not hang the recovery command -------------


def _wedged_adb(tmp_path: Path, *, hang_listing: bool = False) -> Path:
    """A PATH dir whose `adb` answers `devices -l` and then never returns from a `shell` call.

    `devices -l` answering normally is exactly what used to let `stop()` reach the unbounded call.
    The hang is `exec sleep 30`, so the shell is replaced rather than left with an orphaned child,
    and `subprocess.run`'s timeout kills the blocking process itself.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "adb"
    shell_branch = (
        'case "$*" in\n  *"ls /tmp/ghostdeck"*) exec sleep 30 ;;\nesac\nexit 0\n'
        if hang_listing
        else "exec sleep 30\n"
    )
    stub.write_text(
        "#!/bin/sh\n"
        f'case "$1" in\n  devices) printf \'{DEVICE_LINE}\'; exit 0 ;;\nesac\n' + shell_branch,
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return bin_dir


def test_cleanup_device_times_out_on_a_wedged_adb_server(tmp_path, monkeypatch):
    """A-116: `subprocess.run` without a timeout blocked `stop` forever with the deck still hijacked.

    `_ADB_TIMEOUT` is shortened so the assertion can be about the *shape* of the failure (a named
    timeout, not a hang) without the test itself waiting the production 30s.
    """
    from ghostdeck import play

    bin_dir = _wedged_adb(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}/usr/bin:/bin")
    monkeypatch.setattr(play, "_ADB_TIMEOUT", 3.0)
    # In-process, so the USB layer is patched in place: the deck must be identified before the
    # wedged `shell` call is reached, or the refusal would pre-empt the timeout under test.
    monkeypatch.setattr(
        play.usb, "detect", lambda: {"serial": SERIAL, "vid": 0x18D1, "pid": 0xD002, "mode": "adb"}
    )

    start = time.monotonic()
    with pytest.raises(RuntimeError) as excinfo:
        play._cleanup_device()
    elapsed = time.monotonic() - start

    message = str(excinfo.value)
    assert "timed out" in message, message
    assert "setprop ctl.stop zkswe" in message, message
    assert elapsed < 10, f"the timeout did not bound the call: {elapsed:.1f}s"


def test_cleanup_device_tolerates_a_listing_that_times_out(tmp_path, monkeypatch):
    """The listing is informational, so its timeout must be as non-fatal as its exit code (A-116).

    Every mutating call succeeds here; only the trailing `ls` hangs. `stop()` must still report the
    deck as restored rather than turning a successful restore into a failure.
    """
    from ghostdeck import play

    bin_dir = _wedged_adb(tmp_path, hang_listing=True)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}/usr/bin:/bin")
    monkeypatch.setattr(play, "_ADB_TIMEOUT", 3.0)
    monkeypatch.setattr(
        play.usb, "detect", lambda: {"serial": SERIAL, "vid": 0x18D1, "pid": 0xD002, "mode": "adb"}
    )

    start = time.monotonic()
    play._cleanup_device()  # must not raise
    elapsed = time.monotonic() - start
    assert elapsed < 10, f"the listing timeout did not bound the call: {elapsed:.1f}s"


# --- A-137: the cleanup must target the deck, not the first device listed ------


# A phone listed BEFORE the deck, and both lines field-less (the real shape from T15). Position alone
# must not decide: the deck is chosen because the USB layer names its serial, not because it is
# second, and `serial_from_devices()` returning the first match is how every mutating cleanup command
# went to the phone while `stop` exited 0.
TWO_DEVICE_ADB = _adb(f"""case "$1" in
  devices) printf 'List of devices attached\\nPHONE123      device usb:10000001X transport_id:1\\n{SERIAL}      device usb:18092032X transport_id:4\\n'; exit 0 ;;
esac
exit 0
""")

# Only a phone, and the USB layer has no verdict either: nothing identifies the deck, so `stop` must
# refuse rather than grab the one device that happens to be attached. `deck_serial=None` models that
# environment explicitly (see `_cli`); a deck-verdict-without-a-matching-adb-entry is the separate H3
# case, pinned by its own test below.
PHONE_ONLY_ADB = _adb("""case "$1" in
  devices) printf 'List of devices attached\\nPHONE123      device usb:10000001X transport_id:1\\n'; exit 0 ;;
esac
exit 0
""")


# --- FIX-1-T15: the real deck line, and what identifies it --------------------


def test_deck_line_without_product_fields_still_resolves(tmp_path):
    """H1/T15 regression pin: the attached deck emits NO product/model fields.

    Its verbatim shape is `<serial>      device usb:18092032X transport_id:4` (serial redacted: this\n    file is in the public tree and must not carry the lab's identity). `stop` refuses
    when it cannot identify the deck, so a filter that needs those fields broke `stop` on real
    hardware while the whole suite stayed green on fixtures that invented them. The assertion below
    is on the fixture itself: if someone reintroduces the fabricated fields, this fails first.
    """
    assert "product:" not in DEVICE_LINE and "model:" not in DEVICE_LINE
    assert "transport_id:4" in DEVICE_LINE  # the real line's only identifying content

    # And the deck is still identified: by the USB layer's verdict, which matched VID/PID.
    result, calls, _ = _cli(HAPPY_ADB, tmp_path)
    assert result.returncode == 0, result.stderr
    assert calls == STOP_ARGV, calls
    assert any(SERIAL in call for call in calls), calls


def test_deck_is_identified_by_the_usb_layer_not_by_adb_fields(tmp_path):
    """Both accepted signals, and the refusal when neither fires.

    With the USB verdict present the field-less line resolves; with the same field-less line but no
    USB verdict there is nothing to identify the deck, so `stop` must refuse and touch no device.
    """
    identified, calls, _ = _cli(HAPPY_ADB, tmp_path / "a", deck_serial=SERIAL)
    assert identified.returncode == 0, identified.stderr
    assert calls == STOP_ARGV, calls

    unidentified, calls, _ = _cli(HAPPY_ADB, tmp_path / "b", deck_serial=None)
    assert unidentified.returncode != 0, unidentified.stdout
    assert "no ADB device" in unidentified.stderr, unidentified.stderr
    assert calls == [DEVICES_ARGV], f"nothing may be sent without a positive identification: {calls}"


def test_adb_l_identity_fields_are_still_an_accepted_path(tmp_path):
    """Some builds DO report the fields, so that stays an accepted signal (T15 requirement 2)."""
    fields_adb = _adb(f"""case "$1" in
  devices) printf 'List of devices attached\\n{SERIAL}      device product:d200 model:D200 device:d200 transport_id:4\\n'; exit 0 ;;
esac
exit 0
""")
    result, calls, _ = _cli(fields_adb, tmp_path, deck_serial=None)
    assert result.returncode == 0, result.stderr
    assert calls == STOP_ARGV, calls


def test_a_stale_adb_server_gets_one_bounded_restart(tmp_path):
    """H3/T15: USB says ADB while `adb devices` is empty gets ONE server restart, then a refusal.

    The restart is the same recovery `studio._ensure_bridge()` already performs; without it `stop`
    concluded "no ADB device reachable" the instant the server had not caught up, which the master
    observed repeatedly on this host. Bounded to one restart: the fake treats every re-list as empty,
    and only a single kill/start pair may appear.
    """
    empty_listing = _adb("""case "$1" in
  devices) printf 'List of devices attached\\n'; exit 0 ;;
esac
exit 0
""")
    result, calls, _ = _cli(empty_listing, tmp_path, deck_serial=SERIAL)
    assert result.returncode != 0, result.stdout
    assert "no ADB device" in result.stderr, result.stderr
    assert calls.count("kill-server") == 1, f"exactly one restart, not a loop: {calls}"
    assert calls.count("start-server") == 1, calls
    assert calls.count(DEVICES_ARGV) == 2, f"one initial list, one re-check: {calls}"
    assert not any("shell" in call for call in calls), f"nothing reached a device: {calls}"


def test_a_failed_server_restart_does_not_prevent_the_refusal(tmp_path):
    """A restart that cannot run is no help, not a fatal error (the master's transient 255 note)."""
    restart_fails = _adb("""case "$1" in
  devices) printf 'List of devices attached\\n'; exit 0 ;;
  start-server) echo "error: cannot connect to daemon" >&2; exit 255 ;;
esac
exit 0
""")
    result, calls, _ = _cli(restart_fails, tmp_path, deck_serial=SERIAL)
    assert result.returncode != 0, result.stdout
    assert "no ADB device" in result.stderr, result.stderr
    assert "Traceback" not in result.stdout + result.stderr


def test_the_restart_recovers_a_server_that_then_sees_the_deck(tmp_path):
    """The point of the H3 recovery: `stop` works even when the server catches up only on retry.

    The fake lists nothing until `start-server` has run, which is the stale-server shape the master
    observed. Without the bounded restart `stop` refuses and leaves the deck in ADB; with it, the
    deck is restored and every mutating call reaches it.
    """
    state_file = tmp_path / "server-caught-up"
    flaky_adb = _adb(f"""case "$1" in
  devices) if [ -f "{state_file}" ]; then printf 'List of devices attached\\n{SERIAL}      device usb:18092032X transport_id:4\\n'; else printf 'List of devices attached\\n'; fi; exit 0 ;;
  start-server) : > "{state_file}"; exit 0 ;;
esac
echo "cleanup ok"
exit 0
""")
    result, calls, _ = _cli(flaky_adb, tmp_path, deck_serial=SERIAL)

    assert result.returncode == 0, result.stderr
    assert calls.count("kill-server") == 1 and calls.count("start-server") == 1, calls
    # After the restart the deck is identified, so the whole cleanup runs against it.
    assert calls[-1] == LISTING_ARGV, calls
    for argv in (CTL_STOP_ARGV, CTL_START_ARGV, RM_ARGV):
        assert argv in calls, f"{argv} never ran, so the deck was not restored: {calls}"


def test_stop_targets_the_deck_when_a_phone_is_listed_first(tmp_path):
    """A-137: the deck is chosen by identity, never by its position in `devices -l`.

    Here BOTH lines are field-less, so only the USB layer distinguishes them: it names the deck's
    serial, which is nowhere near the phone's. Every mutating call must go to the deck and none to
    the phone.
    """
    result, calls, _ = _cli(TWO_DEVICE_ADB, tmp_path)
    assert result.returncode == 0, result.stderr
    assert calls == STOP_ARGV, calls
    assert not any("PHONE123" in call for call in calls), f"the phone was mutated: {calls}"


def test_stop_refuses_when_no_attached_device_is_the_deck(tmp_path):
    """With only a phone attached the deck is absent, and the command must say so rather than guess."""
    result, calls, _ = _cli(PHONE_ONLY_ADB, tmp_path, deck_serial=None)
    assert result.returncode != 0, result.stdout
    assert "no ADB device" in result.stderr, result.stderr
    assert "D200" in result.stderr, result.stderr  # names the deck as the missing device
    assert calls == [DEVICES_ARGV], f"nothing may be sent to an unidentified device: {calls}"
    assert not any("PHONE123" in call for call in calls), calls


# --- T16: a deck that is attached but whose adb transport is not answering ---------------

# The wedged shape, captured from the attached hardware: the serial IS listed, in the state `adb`
# reports as `offline` (enumerated on USB as ADB, adbd not answering). It differs from DEVICE_LINE in
# that one token and nothing else, which the guard below pins so neither fixture can drift.
OFFLINE_LINE = DEVICE_LINE.replace("device usb:", "offline usb:")

OFFLINE_ADB = _adb(f"""case "$1" in
  devices) printf '{OFFLINE_LINE}'; exit 0 ;;
esac
exit 0
""")

# A phone in `device` state first, then the wedged deck: A-137's ordering crossed with T16's state.
WEDGED_DECK_AFTER_PHONE_ADB = _adb(f"""case "$1" in
  devices) printf 'List of devices attached\\nPHONE123      device usb:10000001X transport_id:1\\n{SERIAL}      offline usb:18092032X transport_id:2\\n'; exit 0 ;;
esac
exit 0
""")


def _device_line(text: str) -> list[str]:
    """The device line of a fake `devices` banner, as tokens (the header and blank lines dropped)."""
    return [line for line in text.splitlines() if line.strip()][-1].split()


def test_the_offline_fixture_is_the_online_one_with_only_the_state_changed():
    """Guard the pair below the way T15's guard protects DEVICE_LINE.

    The two tests that follow are only a comparison of "usable" against "attached but wedged" if the
    fixtures are otherwise identical. A future edit that changed the serial or the fields in one of
    them would make that comparison meaningless while both tests still passed.
    """
    online = _device_line(DEVICE_LINE)
    offline = _device_line(OFFLINE_LINE)
    assert len(online) == len(offline), (online, offline)
    assert (online[1], offline[1]) == ("device", "offline"), (online, offline)
    assert [online[0], *online[2:]] == [offline[0], *offline[2:]], (online, offline)


def test_stop_names_a_wedged_deck_instead_of_calling_it_absent(tmp_path):
    """T16 (HIGH): on the real deck `stop` exited 1 with "no attached device identifies as the D200"
    while the deck WAS attached and WAS listed - as `offline`. That message sent the user to check a
    cable for a deck that is enumerated on USB and only needs a power cycle.

    The message half of the fix: name the transport and the one remedy that works. Nothing host-side
    restored the wedged deck, so a hint that does not say "power-cycle/replug" is not a recovery
    path.
    """
    result, calls, _ = _cli(OFFLINE_ADB, tmp_path, deck_serial=SERIAL)
    assert result.returncode != 0, result.stdout
    assert SERIAL in result.stderr, result.stderr
    assert "offline" in result.stderr, result.stderr
    assert "power-cycle" in result.stderr and "replug" in result.stderr, result.stderr
    assert "no attached device identifies as the D200" not in result.stderr, result.stderr
    assert "Traceback" not in result.stdout + result.stderr
    # Not just the wording: a transport that cannot run a command must not be sent one.
    assert calls == [DEVICES_ARGV], f"nothing may be sent to a wedged transport: {calls}"


def test_stop_distinguishes_an_offline_deck_from_an_absent_one(tmp_path):
    """The defect was a misdiagnosis, so the two states must not produce the same message.

    Before this, `_parse_devices()` dropped every line whose state was not exactly `device`, so a
    wedged deck and no deck at all were indistinguishable to every caller.
    """
    wedged, _, _ = _cli(OFFLINE_ADB, tmp_path / "wedged", deck_serial=SERIAL)
    absent, _, _ = _cli(NO_DEVICE_ADB, tmp_path / "absent", deck_serial=None)
    assert wedged.returncode != 0 and absent.returncode != 0
    assert "offline" in wedged.stderr and "offline" not in absent.stderr, (wedged.stderr, absent.stderr)
    assert "no attached device identifies as the D200" in absent.stderr, absent.stderr
    assert wedged.stderr != absent.stderr


def test_stop_reports_an_unattributable_offline_device_truthfully(tmp_path):
    """With no USB verdict the deck cannot be positively identified - but something IS attached.

    This is the real venv, which has neither hidapi nor pyusb: `usb.detect()` has no verdict, so the
    deck cannot be proven, and the old message claimed nothing was attached while `adb` was listing
    it. The honest form names what is attached, refuses to touch it, and still gives the remedy.
    """
    result, calls, _ = _cli(OFFLINE_ADB, tmp_path, deck_serial=None)
    assert result.returncode != 0, result.stdout
    assert SERIAL in result.stderr, result.stderr
    assert "offline" in result.stderr, result.stderr
    assert "positively identified" in result.stderr, result.stderr
    assert "no attached device identifies as the D200" not in result.stderr, result.stderr
    assert "power-cycle" in result.stderr, result.stderr
    # It must say that it did not act, and it must not have acted.
    assert "Nothing is sent to an unidentified device" in result.stderr, result.stderr
    assert calls == [DEVICES_ARGV], calls


def test_stop_refuses_without_mutating_the_phone_when_the_wedged_deck_is_listed_second(tmp_path):
    """A-137 held with a phone first AND the deck wedged: the phone must not become the target.

    The distinction added for T16 must not weaken the protection: the offline state is a diagnosis,
    never a reason to act on whatever line happens to be first.
    """
    result, calls, _ = _cli(WEDGED_DECK_AFTER_PHONE_ADB, tmp_path, deck_serial=SERIAL)
    assert result.returncode != 0, result.stdout
    assert calls == [DEVICES_ARGV], f"nothing may be sent: {calls}"
    assert not any("PHONE123" in call for call in calls), f"the phone was mutated: {calls}"
    assert SERIAL in result.stderr and "offline" in result.stderr, result.stderr


def test_the_healthy_path_is_unchanged_by_the_transport_distinction(tmp_path):
    """The other half of the pair: a `device`-state deck still restores, unchanged.

    T16 must not turn a working deck into an error; the state token is the only gate.
    """
    result, calls, _ = _cli(HAPPY_ADB, tmp_path, deck_serial=SERIAL)
    assert result.returncode == 0, result.stderr
    assert calls == STOP_ARGV, calls
    assert result.stderr == "", result.stderr


# --- A-126: the session record must outlive the deck's effects ----------------


def test_stop_keeps_the_player_record_when_the_deck_step_fails(tmp_path):
    """A-126: `stop()` erased the pid and the sidecar before touching the device, so a failed stop
    left a hijacked deck and nothing on disk recording that a player had ever run.

    The player was stopped (it is ours, and identity is determinable) but the deck was not restored,
    so the record that describes the session must still be there for the user and for a later run.
    """
    ours = _ours()
    try:
        time.sleep(0.3)
        recorded = _record(ours.pid, tmp_path / "home")
        result, calls, home = _cli(
            NO_DEVICE_ADB,
            tmp_path,
            "stop",
            pre_state={"play_pid": ours.pid},
            sidecar=recorded,
            deck_serial=None,
        )
        assert result.returncode != 0, result.stdout
        assert "no ADB device" in result.stderr, result.stderr
        assert not _alive(ours), "our own player must still be stopped"
        saved = json.loads((home / ".ghostdeck" / "state.json").read_text())
        assert saved["play_pid"] == ours.pid, "the session record was destroyed before the deck came back"
        assert (home / ".ghostdeck" / "play.pid").is_file(), "the sidecar was destroyed"
    finally:
        if ours.poll() is None:
            ours.kill()
        ours.wait()


def test_stop_erases_the_record_once_the_deck_is_restored(tmp_path):
    """The other side of A-126: a successful stop still leaves no stale session behind."""
    ours = _ours()
    try:
        time.sleep(0.3)
        recorded = _record(ours.pid, tmp_path / "home")
        result, calls, home = _cli(
            HAPPY_ADB, tmp_path, "stop", pre_state={"play_pid": ours.pid}, sidecar=recorded
        )
        assert result.returncode == 0, result.stderr
        assert calls == STOP_ARGV, calls
        assert json.loads((home / ".ghostdeck" / "state.json").read_text())["play_pid"] is None
        assert not (home / ".ghostdeck" / "play.pid").exists()
    finally:
        if ours.poll() is None:
            ours.kill()
        ours.wait()

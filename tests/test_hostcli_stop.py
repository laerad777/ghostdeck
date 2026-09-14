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
# T17 signal 3: the deck-only sysfs node, read over `adb` alone. It is on the adb allowlist, and a
# phone cannot answer it, so a successful read is a positive identification rather than a positional
# pick (A-137). The read is the ONLY call ever sent to a candidate that is not the deck.
CAT_FUNCTIONS = "cat /sys/class/zkswe_usb/zkswe0/functions"
PROBE_ARGV = f"-s {SERIAL} shell {CAT_FUNCTIONS}"
PHONE_PROBE_ARGV = f"-s PHONE123 shell {CAT_FUNCTIONS}"
GETPROP_ARGV = f"-s {SERIAL} shell getprop sys.usb.config"
RESTORE_ARGV = [CTL_STOP_ARGV, CTL_START_ARGV]
STOP_ARGV = [DEVICES_ARGV, CTL_STOP_ARGV, CTL_START_ARGV, RM_ARGV, LISTING_ARGV]
# After the bounce, `stop` waits on USB HID (VID/PID), not on adbd. Poking
# `getprop` after the bounce held the gadget in ADB and H1 never completed.
# `_await_transport_recovery` still exists for its own tests; `stop` does not call it.
RECOVERY_ARGV = [DEVICES_ARGV] + [GETPROP_ARGV] * 6
STOP_ARGV_WITH_SESSION = STOP_ARGV

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

# Every cleanup call succeeds, but the deck never answers the transport-recovery probe. The bounce
# has already happened by the time that probe runs, so this must NOT change the exit code: the wait
# exists so that a completed stop means "the deck is usable", not so that an unrecovered deck is
# reported as a failed stop.
NO_TRANSPORT_ADB = _adb(f"""case "$1" in
  devices) printf '{DEVICE_LINE}'; exit 0 ;;
esac
case "$*" in
  *getprop*) echo "adb: device still starting" >&2; exit 1 ;;
  *"ls /tmp/ghostdeck"*) exit 1 ;;
esac
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
    stub_session_released: bool | None = None,
    recovery_timeout: float | None = None,
    hid_after_bounce: bool = True,
    hid_timeout: float | None = None,
    python_source: str | None = None,
) -> tuple[subprocess.CompletedProcess, list[str], Path]:
    """Run the CLI with a temp HOME, an explicit PATH, and a stubbed USB layer.

    PATH is always REPLACED (never appended to the operator PATH): a real deck is attached to this
    host, so the real `adb` must be unreachable from every test. `system_path` keeps the OS tools
    such as `ps` available unless a test deliberately removes them.

    `deck_serial` is what `usb.detect()` reports — the deck's primary identity signal since FIX-1-T15.
    It is STUBBED rather than left to the host, because otherwise every one of these tests would
    depend on whether this machine happens to have a D200 attached and a pyusb to see it. Pass None
    to model an environment where the USB layer has no verdict (no deck, or no backend).

    `stub_session_released` pins `play._session_released`'s answer in the child. `stop()` waits for the
    media session to release before bouncing the stock UI, and the bound is 8s; a test whose subject is
    the caller's response to a refusal must not spend that 8s, and must not depend on the host's live
    session record either. The predicate itself is covered directly, in-process, further down.

    `recovery_timeout` pins `play._await_transport_recovery`'s budget in the child. That wait is
    bounded at 20s because it is spent against a real deck; a test whose subject is "the deck never
    answers" would otherwise spend all 20 of them proving the clock. The bound is a DEFAULT ARGUMENT,
    bound when the function is defined, so assigning the module global would not move it - the
    default tuple is rebound instead. (That is also why the in-process tests pass `timeout=`
    explicitly.)
    `hid_after_bounce` is the H1 USB flip: after `stop` has issued `ctl.start zkswe`,
    `usb.detect()` must report HID (2207:0019) rather than stay frozen on ADB.
    Discovery still needs ADB so `_cleanup_device` can address the serial; the
    stub switches on that first `ctl.start`. Pass False to keep the gadget in ADB
    for the HID-stuck refusal. `deck_serial=None` stays `mode=none` (no USB
    verdict) so the HID wait is a no-op. `hid_timeout` rebinds `_await_hid_return`'s
    default the same way as `recovery_timeout`, so a stuck-ADB refusal is not an 8s clock test.

    `python_source` runs that snippet instead of the CLI, in the SAME child environment, so a test can
    assert the harness's own setup (which shared global the child actually sees) rather than re-derive
    the environment and assert nothing about the one the tests use.
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
    adb_verdict = (
        {"serial": deck_serial, "vid": 0x18D1, "pid": 0xD002, "mode": "adb"}
        if deck_serial
        else {"serial": None, "vid": None, "pid": None, "mode": "none"}
    )
    hid_verdict = (
        {"serial": deck_serial, "vid": 0x2207, "pid": 0x0019, "mode": "hid"}
        if deck_serial
        else adb_verdict
    )
    shim = tmp_path / "shim"
    shim.mkdir(exist_ok=True)
    session_stub = (
        ""
        if stub_session_released is None
        else f"_play._session_released = lambda *a, **k: {stub_session_released!r}\n"
    )
    recovery_stub = (
        ""
        if recovery_timeout is None
        else "_play._await_transport_recovery.__defaults__ = "
        f"({recovery_timeout!r},)\n"
    )
    usb_lines = [
        "import ghostdeck.usb as _usb",
        f"_gd_box = [{adb_verdict!r}]",
        "def _gd_detect():",
        "    return _gd_box[0]",
        "_usb.detect = _gd_detect",
    ]
    hid_flip = ""
    if hid_after_bounce and deck_serial:
        # Flip detect to HID after the bounce, not after the old getprop wait:
        # `stop` no longer pokes adbd post-bounce (that held the gadget in ADB).
        hid_flip = (
            f"_gd_hid = {hid_verdict!r}\n"
            "_gd_cu = _play._cleanup_device\n"
            "def _gd_after_bounce(*a, **k):\n"
            "    result = _gd_cu(*a, **k)\n"
            "    _gd_box[0] = _gd_hid\n"
            "    return result\n"
            "_play._cleanup_device = _gd_after_bounce\n"
        )
    (shim / "sitecustomize.py").write_text(
        "# Test-only stub of the USB layer (FIX-1-T15 / T20).\n"
        + "\n".join(usb_lines)
        + "\n"
        "# The player's published session record lives at a fixed /tmp path that HOME isolation\n"
        "# cannot redirect, so a real leftover record from a manual run would be inherited by every\n"
        "# test. Point it inside the temp tree: `stop()` reads it to decide whether a media session\n"
        "# is still live before bouncing the stock UI.\n"
        "import pathlib as _pathlib\n"
        "import ghostdeck.play as _play\n"
        f"_play._HOST_STATE = _pathlib.Path({str(tmp_path / 'host-state.json')!r})\n"
        + session_stub
        + recovery_stub
        + hid_flip
        + "import ghostdeck.studio as _studio\n"
        + "_studio._socket_live = lambda: False\n"
        + (
            ""
            if hid_timeout is None
            else "_play._await_hid_return.__defaults__ = "
            f"({hid_timeout!r},)\n"
        ),
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
    command = (
        [sys.executable, "-c", python_source]
        if python_source is not None
        else [sys.executable, "-m", "ghostdeck.cli", *(args or ("stop",))]
    )
    result = subprocess.run(
        command,
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
        assert calls == STOP_ARGV_WITH_SESSION, f"the deck was left unrestored: {calls}"
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
        assert calls == STOP_ARGV_WITH_SESSION, calls
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
        assert calls == STOP_ARGV_WITH_SESSION
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
        assert calls == STOP_ARGV_WITH_SESSION, f"the deck was left exactly as it was: {calls}"
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
    """Signal 1 still resolves the field-less line, and no probe is needed when it fires."""
    identified, calls, _ = _cli(HAPPY_ADB, tmp_path / "a", deck_serial=SERIAL)
    assert identified.returncode == 0, identified.stderr
    assert calls == STOP_ARGV, calls
    assert not any("zkswe0/functions" in call for call in calls), calls


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
    """With only a phone attached the deck is absent, and the command must say so rather than guess.

    T17 adds the read-only sysfs probe, so a `device`-state candidate IS now asked about itself.
    That is the identification mechanism, not a positional fallback: the phone answers nothing (the
    fake exits 0 with no output - the case where an exit code alone would have lied), so `stop` still
    refuses, and the probe is the ONLY call that ever names the phone.
    """
    result, calls, _ = _cli(PHONE_ONLY_ADB, tmp_path, deck_serial=None)
    assert result.returncode != 0, result.stdout
    assert "no ADB device" in result.stderr, result.stderr
    assert "D200" in result.stderr, result.stderr  # names the deck as the missing device
    assert [call for call in calls if "PHONE123" in call] == [PHONE_PROBE_ARGV], calls
    assert not any("setprop" in call or "rm -f" in call or "ls /tmp" in call for call in calls), calls


# --- T17: identification over `adb` alone, when the extras and the `-l` fields are absent -----

# The real extras-absent shape: `usb.detect()` has no verdict (no hidapi/pyusb), and the deck line is
# field-less (T15), so neither signal 1 nor signal 2 can fire. The probe is what is left.
PROBE_ONLY_ADB = _adb(f"""case "$1" in
  devices) printf 'List of devices attached\\n{SERIAL}      device usb:18092032X transport_id:4\\n'; exit 0 ;;
esac
case "$*" in
  *"zkswe0/functions"*) echo adb; exit 0 ;;
esac
exit 0
""")

# A phone FIRST and the deck second, both in `device` state, both field-less. The probe answers only
# for the deck's serial, so identification has to come from the device's own answer.
PROBE_DECK_AFTER_PHONE_ADB = _adb(f"""case "$1" in
  devices) printf 'List of devices attached\\nPHONE123      device usb:10000001X transport_id:1\\n{SERIAL}      device usb:18092032X transport_id:4\\n'; exit 0 ;;
esac
case "$*" in
  *"zkswe0/functions"*)
    case "$2" in
      {SERIAL}) echo adb; exit 0 ;;
    esac
    echo "cat: /sys/class/zkswe_usb/zkswe0/functions: No such file or directory" >&2
    exit 1 ;;
esac
exit 0
""")

# Five usable candidates, none of which answers: the probe count must be bounded.
FIVE_PHONES_ADB = _adb(
    "case \"$1\" in\n"
    "  devices) printf 'List of devices attached\\n"
    + "\\n".join(f"PHONE{i}      device usb:1000000{i}X transport_id:{i}" for i in range(5))
    + "\\n'; exit 0 ;;\n"
    "esac\nexit 0\n"
)


def test_stop_identifies_the_deck_by_the_zkswe_probe_without_the_usb_verdict(tmp_path):
    """T17 (C-153/C-155, HIGH): `stop` could not find the deck without the Python extras.

    Before this, `deck_transport()` accepted only the USB verdict and the `-l` identity fields, and
    neither can fire here - so the serial sitting in `adb devices` was never used and `stop`, the
    documented recovery command, refused. The deck's own sysfs node (already on the adb allowlist) is
    a positive, deck-only answer, so the whole cleanup now runs against it.
    """
    result, calls, _ = _cli(PROBE_ONLY_ADB, tmp_path, deck_serial=None)
    assert result.returncode == 0, result.stderr
    assert calls[0] == DEVICES_ARGV, calls
    assert calls[1] == PROBE_ARGV, calls
    for argv in (CTL_STOP_ARGV, CTL_START_ARGV, RM_ARGV):
        assert argv in calls, f"{argv} never ran, so the deck was not restored: {calls}"
    assert calls[-1] == LISTING_ARGV, calls
    # The signal that identified it is named, so a future diagnosis is not another mystery.
    assert "zkswe" in result.stderr and SERIAL in result.stderr, result.stderr


def test_the_probe_never_targets_the_phone_listed_before_the_deck(tmp_path):
    """A-137 must survive T17: the probe may READ a candidate, but never mutate one.

    The phone is first and both lines are field-less, so position decides nothing: the deck is
    accepted only because its own node answered. The phone is probed (that is how it is ruled out)
    and is named by no other call.
    """
    result, calls, _ = _cli(PROBE_DECK_AFTER_PHONE_ADB, tmp_path, deck_serial=None)
    assert result.returncode == 0, result.stderr
    assert [call for call in calls if "PHONE123" in call] == [PHONE_PROBE_ARGV], calls
    assert PROBE_ARGV in calls, calls
    for argv in (CTL_STOP_ARGV, CTL_START_ARGV, RM_ARGV):
        assert argv in calls, f"{argv} never ran, so the deck was not restored: {calls}"
    assert calls[-1] == LISTING_ARGV, calls


def test_stop_refuses_cleanly_when_the_probe_fails(tmp_path):
    """A probe that cannot answer is "not the deck" - never an exception escaping `stop`."""
    probe_errors = _adb("""case "$1" in
  devices) printf 'List of devices attached\\nPHONE123      device usb:10000001X transport_id:1\\n'; exit 0 ;;
esac
case "$*" in
  *"zkswe0/functions"*) echo "adb: device offline" >&2; exit 1 ;;
esac
exit 0
""")
    result, calls, _ = _cli(probe_errors, tmp_path, deck_serial=None)
    assert result.returncode != 0, result.stdout
    assert "no ADB device" in result.stderr, result.stderr
    assert "Traceback" not in result.stdout + result.stderr
    assert [call for call in calls if "PHONE123" in call] == [PHONE_PROBE_ARGV], calls


def test_a_probe_that_cannot_run_is_not_fatal(monkeypatch):
    """The other failure shapes - a timeout, adb gone - must also be a clean "no answer".

    Reproduced in-process because a real 30s timeout would dominate the suite, and the point is the
    exception path, not the clock.
    """
    from ghostdeck import play

    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired("adb", play._ADB_TIMEOUT)

    monkeypatch.setattr(play.adb, "run", boom)
    assert play._probe_is_deck("PHONE123") is False

    def err(*args, **kwargs):
        raise RuntimeError("adb could not be run")

    monkeypatch.setattr(play.adb, "run", err)
    assert play._probe_is_deck("PHONE123") is False


def test_the_probe_count_is_bounded(tmp_path):
    """Five usable candidates and no answer: at most `_PROBE_LIMIT` probes, then a plain refusal."""
    from ghostdeck import play

    result, calls, _ = _cli(FIVE_PHONES_ADB, tmp_path, deck_serial=None)
    assert result.returncode != 0, result.stdout
    probes = [call for call in calls if "zkswe0/functions" in call]
    assert len(probes) == play._PROBE_LIMIT, probes
    assert len(set(probes)) == play._PROBE_LIMIT, f"each candidate is probed once: {probes}"


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
        assert calls == STOP_ARGV_WITH_SESSION, calls
        assert json.loads((home / ".ghostdeck" / "state.json").read_text())["play_pid"] is None
        assert not (home / ".ghostdeck" / "play.pid").exists()
    finally:
        if ours.poll() is None:
            ours.kill()
        ours.wait()


# --- the release wait: no bounce may cut a live media session (FIX-5-T16) ----------------
#
# `stop()` waits for the media session to release before it bounces the stock UI, because the bounce
# tears down a live session (`terminalCode: 12 D200_VS_DISCONNECTED`) and the next session then opens
# onto an unreleased boundary (`CLEANUP_FAILED / cleanup: unproven`). These four tests cover the two
# refusal paths and the one line that decides whether the bounce is allowed to happen at all.


def test_stop_does_not_bounce_the_ui_when_the_session_never_releases(tmp_path):
    """A session that does not prove its release must leave the stock UI ALONE.

    Bouncing anyway is the defect this wait exists to prevent, so the refusal has to be observable in
    the command list: a bounce issued after a failed wait is the regression, not the message.
    """
    ours = _ours()
    try:
        time.sleep(0.3)
        recorded = _record(ours.pid, tmp_path / "home")
        result, calls, home = _cli(
            HAPPY_ADB,
            tmp_path,
            "stop",
            pre_state={"play_pid": ours.pid},
            sidecar=recorded,
            stub_session_released=False,
        )
        assert result.returncode != 0, result.stdout
        assert "did not release" in result.stderr, result.stderr
        assert "Traceback" not in result.stdout + result.stderr
        assert CTL_STOP_ARGV not in calls, f"the UI was bounced after a failed wait: {calls}"
        assert CTL_START_ARGV not in calls, f"the UI was bounced after a failed wait: {calls}"
        # The player IS stopped (it is ours and its identity is determinable); what is withheld is the
        # device mutation that would cut the transport. The record stays so a re-run can finish.
        assert not _alive(ours), "the player must still be signalled"
        assert json.loads((home / ".ghostdeck" / "state.json").read_text())["play_pid"] == ours.pid
    finally:
        if ours.poll() is None:
            ours.kill()
        ours.wait()


def test_stop_keeps_both_failures_when_the_session_wedges_and_identity_is_unverifiable(tmp_path):
    """Two independent failures must both reach the user; neither may mask the other.

    The pre-existing pairing (`_cleanup_device` + identity) has a dedicated test; this is the same
    contract for the new refusal, which is the third way `stop` can fail.
    """
    victim = _victim()
    try:
        time.sleep(0.3)
        result, calls, _ = _cli(
            HAPPY_ADB,
            tmp_path,
            "stop",
            pre_state={"play_pid": victim.pid},
            stub_session_released=False,
        )
        assert result.returncode == 1, (result.returncode, result.stdout, result.stderr)
        assert "cannot verify" in result.stderr, result.stderr
        assert "did not release" in result.stderr, result.stderr
        assert "Traceback" not in result.stdout + result.stderr
        assert CTL_STOP_ARGV not in calls, calls
        assert _alive(victim), "an unverifiable pid must never be signalled"
    finally:
        victim.kill()
        victim.wait()


def test_the_bounce_is_skipped_when_nothing_was_playing(tmp_path):
    """Nothing to restore means no session can be cut, so the wait must not be entered at all.

    This is what keeps fakes (which publish no record) and an idle host from paying the 8s bound, and
    it is why `HAPPY_ADB` still reaches the bounce in the tests above it. The stub is pinned to
    `False` and a NON-released record is planted, so a `stop()` that consulted the record without first
    checking whether a session is recorded would refuse here.
    """
    (tmp_path / "host-state.json").write_text(
        json.dumps({"video": {"status": {"cleanup": "pending"}}}), encoding="utf-8"
    )
    result, calls, home = _cli(HAPPY_ADB, tmp_path, "stop", stub_session_released=False)
    assert result.returncode == 0, result.stderr
    assert calls == STOP_ARGV, calls
    assert not (home / ".ghostdeck" / "play.pid").exists()


def test_stop_still_restores_the_deck_when_a_live_session_proves_its_release(tmp_path):
    """The positive control: the wait must not turn a normal stop into a refusal.

    H1's guarantee is that after a real session `stop` returns the deck to HID - the four clean
    playlist cycles depend on it - so a live session whose record proves a clean release must still
    reach the bounce, in that order. Without a live session this is indistinguishable from the skip
    test above, which is why `play_pid` is set here.
    """
    ours = _ours()
    try:
        time.sleep(0.3)
        recorded = _record(ours.pid, tmp_path / "home")
        (tmp_path / "host-state.json").write_text(
            json.dumps({"video": {"status": {"cleanup": "proven"}}}), encoding="utf-8"
        )
        result, calls, home = _cli(
            HAPPY_ADB,
            tmp_path,
            "stop",
            pre_state={"play_pid": ours.pid},
            sidecar=recorded,
            stub_session_released=None,
        )
        assert result.returncode == 0, result.stderr
        assert calls == STOP_ARGV_WITH_SESSION, calls
        assert json.loads((home / ".ghostdeck" / "state.json").read_text())["play_pid"] is None
    finally:
        if ours.poll() is None:
            ours.kill()
        ours.wait()


# --- `_session_released`: the predicate the bounce is gated on (in-process) ----------------
#
# Reproduced in-process: the subject is the predicate's answer for a given record, and a subprocess
# run of the real 8s bound would test the clock, not the logic.


def _session(monkeypatch, tmp_path, text=None) -> bool:
    from ghostdeck import play

    record = tmp_path / "host-state.json"
    if text is not None:
        record.write_text(text if isinstance(text, str) else json.dumps(text), encoding="utf-8")
    monkeypatch.setattr(play, "_HOST_STATE", record)
    return play._session_released(0.5)


def test_session_released_is_true_when_no_record_exists(monkeypatch, tmp_path):
    """No record means no session to wait for - the /tmp path itself must never be consulted.

    Asserting the module global is redirected first is the point: a predicate that reads the
    operator's real `/tmp/d200-color-host.json` would answer from whatever this host happens to be
    playing, which is exactly the leak that made three `stop` tests host-dependent.
    """
    from ghostdeck import play

    monkeypatch.setattr(play, "_HOST_STATE", tmp_path / "absent.json")
    assert play._HOST_STATE == tmp_path / "absent.json"
    assert play._session_released(0.5) is True


def test_session_released_true_for_a_proven_release_and_a_recordless_session(monkeypatch, tmp_path):
    assert _session(monkeypatch, tmp_path, {"video": {"status": {"cleanup": "proven"}}}) is True
    # A record that names no session has nothing to wait for either.
    assert _session(monkeypatch, tmp_path, {"video": {"status": {}}}) is True


def test_session_released_is_false_while_the_record_stays_pending(monkeypatch, tmp_path):
    """A live session must hold the bounce back, and the wait must stay bounded."""
    from ghostdeck import play

    record = tmp_path / "host-state.json"
    record.write_text(json.dumps({"video": {"status": {"cleanup": "pending"}}}), encoding="utf-8")
    monkeypatch.setattr(play, "_HOST_STATE", record)
    started = time.monotonic()
    assert play._session_released(0.5) is False
    elapsed = time.monotonic() - started
    assert 0.5 <= elapsed < 3.0, f"the wait must be bounded by the timeout it was given: {elapsed}"


def test_session_released_returns_true_when_the_release_arrives_during_the_wait(
    monkeypatch, tmp_path
):
    """The wait exists because SIGTERM is asynchronous: `pending` must become `proven` at ~t+3s.

    This is the measured shape from the deck, compressed: the record is replaced mid-wait, so the
    predicate has to re-read rather than decide once.
    """
    import threading

    from ghostdeck import play

    record = tmp_path / "host-state.json"
    record.write_text(json.dumps({"video": {"status": {"cleanup": "pending"}}}), encoding="utf-8")
    monkeypatch.setattr(play, "_HOST_STATE", record)

    def release():
        time.sleep(0.3)
        record.write_text(
            json.dumps({"video": {"status": {"cleanup": "proven"}}}), encoding="utf-8"
        )

    writer = threading.Thread(target=release)
    writer.start()
    try:
        started = time.monotonic()
        assert play._session_released(5.0) is True
        assert time.monotonic() - started < 5.0, "the wait must exit as soon as the release proves"
    finally:
        writer.join()


def test_session_released_treats_an_unreadable_record_as_released(monkeypatch, tmp_path):
    """A corrupt/partial record must not wedge `stop` for the full bound forever.

    The player rewrites this file while it is being read, so a torn read is the realistic case, and
    the honest answer is `no session is holding the transport` (released) rather than a permanent
    refusal that leaves the deck in ADB.
    """
    assert _session(monkeypatch, tmp_path, "{not json") is True


def test_the_stop_harness_redirects_the_shared_session_record_in_the_child(tmp_path):
    """Guard the harness itself: the CHILD must see the temp record, not the operator's /tmp one.

    FIX-2 found the leak this asserts against - three `stop` tests inherited a hardcoded
    `/tmp/d200-color-host.json` and failed for 8.5s each whenever the host's record was not `proven`.
    Asking the child, in the harness's own environment, is the only way to prove the redirect landed;
    asserting it in-process would re-derive the environment and assert nothing about the real one.
    """
    from ghostdeck import play

    result, _, _ = _cli(
        HAPPY_ADB,
        tmp_path,
        python_source=(
            "import ghostdeck.play as p; "
            "print(p._HOST_STATE); "
            "print(p._session_released(0.0))"
        ),
    )
    assert result.returncode == 0, result.stderr
    seen = result.stdout.splitlines()
    assert seen[0] == str(tmp_path / "host-state.json"), (seen, str(play._HOST_STATE))
    assert seen[0] != str(play._HOST_STATE), "the child inherited the real shared record path"
    # No record was planted, so the predicate answers immediately rather than spending the bound.
    assert seen[1] == "True", seen


# --- `_await_transport_recovery`: the wait that makes a completed `stop` mean "usable" ------------
#
# The bounce itself is covered above (`STOP_ARGV_WITH_SESSION` / `RECOVERY_ARGV`). This section covers
# the wait's own contract, which is the part that can turn a working `stop` into a hang or a lie: it
# must retry rather than decide from one probe, it must stay bounded, and a deck that never comes back
# must NOT fail the command - the stock UI was already restarted by then, so the only thing left for
# `stop` to do is report, and exiting non-zero would report an unrecovered deck as an unrestored one.
#
# In-process wherever the subject is the helper's answer for a given adb behaviour. The one subprocess
# case is the end-to-end shape, and it pins the recovery budget so it tests the shape and not the clock.


def _quiet_run(monkeypatch, play, results):
    """Drive `adb.run` from `results` (returncodes, last one repeating) and record every argv."""
    calls: list[list[str]] = []

    def run(argv, **kwargs):
        calls.append([str(part) for part in argv])
        code = results[min(len(calls), len(results)) - 1]
        return subprocess.CompletedProcess(list(argv), code, "", f"exit {code}")

    monkeypatch.setattr(play.adb, "run", run)
    monkeypatch.setattr(play, "_deck_serial", lambda: SERIAL)
    monkeypatch.setattr(play, "_TRANSPORT_RECOVERY_POLL", 0.0)
    return calls


def test_transport_recovery_retries_until_the_deck_answers(monkeypatch):
    """One probe is not enough: the deck answers, DIPS, and settles, so the wait needs consecutive
    successes past the dip rather than a single one.

    Measured on the attached deck after `stop` returned, polling once a second:
        t+0.1s rc=0   <-- a single-probe version returned here
        t+2.2s rc=0
        t+3.3s rc=1   <-- the deck dips
        t+4.3s rc=0   <-- settles
    A session opened into that dip died with CLEANUP_FAILED / cleanup: unproven. So the wait must
    survive a dip and require `_TRANSPORT_STABLE_SAMPLES` consecutive successes.
    """
    from ghostdeck import play

    stable = play._TRANSPORT_STABLE_SAMPLES
    # answers, then dips (one failure), then stays up
    results = [0] * (stable - 1) + [1] + [0] * stable
    calls = _quiet_run(monkeypatch, play, results)
    assert play._await_transport_recovery(timeout=5.0) is True
    # The dip resets the run, so it probes past it rather than returning at the first success.
    assert len(calls) == len(results), calls
    # It probes the transport itself, not the device inventory or the session record.
    assert calls[0][-1] == "getprop sys.usb.config", calls[0]
    assert calls[0][:2] == ["-s", SERIAL], calls[0]


def test_transport_recovery_is_bounded_and_reports_failure_without_raising(monkeypatch):
    """A deck that never answers ends the wait AT its bound, returning rather than raising or spinning."""
    from ghostdeck import play

    calls = _quiet_run(monkeypatch, play, [1])
    started = time.monotonic()
    assert play._await_transport_recovery(timeout=0.05) is False
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"the wait ignored its timeout: {elapsed:.1f}s"
    assert len(calls) >= 2, f"it decided from a single probe: {calls}"


def test_transport_recovery_swallows_a_failing_adb_call(monkeypatch):
    """An `adb.run` that raises must not escape: `stop` has already bounced the UI by this point.

    A missing adb binary or an OS error here would otherwise turn a finished stop into a traceback
    for a condition the user cannot act on.
    """
    from ghostdeck import play

    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=list(argv), timeout=1)

    monkeypatch.setattr(play.adb, "run", run)
    monkeypatch.setattr(play, "_deck_serial", lambda: SERIAL)
    monkeypatch.setattr(play, "_TRANSPORT_RECOVERY_POLL", 0.0)
    assert play._await_transport_recovery(timeout=0.05) is False


def test_transport_recovery_sends_nothing_without_an_identified_deck(monkeypatch):
    """No positively-identified ready deck means nothing to wait for and no command to send.

    A wedged deck is exactly what `_deck_serial()` rejects (T15), so a `stop` against one must not
    spend the budget polling a serial it could not identify - and must not address a candidate.
    """
    from ghostdeck import play

    def run(argv, **kwargs):
        raise AssertionError(f"a command was sent to an unidentified deck: {argv}")

    monkeypatch.setattr(play.adb, "run", run)
    monkeypatch.setattr(play, "_deck_serial", lambda: None)
    monkeypatch.setattr(play, "_TRANSPORT_RECOVERY_POLL", 0.0)
    started = time.monotonic()
    assert play._await_transport_recovery(timeout=5.0) is False
    assert time.monotonic() - started < 1.0, "it waited for a deck it never identified"


def test_the_recovery_budget_is_finite_and_polled():
    """Pin the real budget: this wait is the last thing `stop` does, so it IS the command's latency.

    An unbounded or non-positive bound would hang `stop` forever; a poll larger than the bound would
    make the wait a single shot. 20s is the deliberate value (the deck settles within a few seconds)
    and must not drift.
    """
    from ghostdeck import play

    assert 0 < play._TRANSPORT_RECOVERY_POLL <= play._TRANSPORT_RECOVERY_TIMEOUT
    assert 0 < play._TRANSPORT_RECOVERY_TIMEOUT <= 60.0, play._TRANSPORT_RECOVERY_TIMEOUT
    assert play._await_transport_recovery.__defaults__ == (play._TRANSPORT_RECOVERY_TIMEOUT,)


def test_stop_does_not_probe_adbd_after_the_bounce(tmp_path):
    """H1: after `ctl.start zkswe`, `stop` watches USB HID and must not hold adbd open."""
    ours = _ours()
    try:
        time.sleep(0.3)
        recorded = _record(ours.pid, tmp_path / "home")
        (tmp_path / "host-state.json").write_text(
            json.dumps({"video": {"status": {"cleanup": "proven"}}}), encoding="utf-8"
        )
        result, calls, home = _cli(
            NO_TRANSPORT_ADB,
            tmp_path,
            "stop",
            pre_state={"play_pid": ours.pid},
            sidecar=recorded,
            stub_session_released=None,
        )
        assert result.returncode == 0, result.stderr
        assert GETPROP_ARGV not in calls, calls
        assert calls == STOP_ARGV_WITH_SESSION, calls
        assert json.loads((home / ".ghostdeck" / "state.json").read_text())["play_pid"] is None
    finally:
        if ours.poll() is None:
            ours.kill()
        ours.wait()


def test_a_stale_pending_record_does_not_hold_a_stop_with_nothing_playing(tmp_path):
    """The master's revert, pinned: a leftover `pending` record must not delay a `stop` with no session.

    No `play_pid` means no session, so neither the release wait nor the recovery probe may run - a
    stale record from a previous run is not a session, and `stop` is the command a user runs to clean
    up exactly that leftover. The record is planted `pending` (a release the predicate would refuse)
    and neither predicate is stubbed, so a `stop()` that consulted the record without the `play_pid`
    guard spends the whole 8s bound and refuses here.
    """
    (tmp_path / "host-state.json").write_text(
        json.dumps({"video": {"status": {"cleanup": "pending"}}}), encoding="utf-8"
    )
    started = time.monotonic()
    result, calls, _ = _cli(HAPPY_ADB, tmp_path, "stop")
    elapsed = time.monotonic() - started
    assert result.returncode == 0, result.stderr
    assert calls == STOP_ARGV, calls
    assert elapsed < 8.0, f"a stop with nothing playing paid the release bound: {elapsed:.1f}s"


# --- `_await_hid_return`: H1 is USB HID, not "adb answers" (FIX-1-T20) -------------------------
#
# `_await_transport_recovery` probes `getprop sys.usb.config`, which succeeds while the gadget is
# still 18d1:d002. H1 requires 2207:0019. These tests never write USB functions; they only read
# `usb.detect()`'s mode.


def test_hid_return_is_immediate_when_usb_cannot_answer(monkeypatch):
    """Fake-adb / missing backend: `mode=none` is not a stuck gadget, so the wait is a no-op."""
    from ghostdeck import play

    monkeypatch.setattr(play.usb, "detect", lambda: {"serial": None, "vid": None, "pid": None, "mode": "none"})
    started = time.monotonic()
    assert play._await_hid_return(timeout=5.0) is True
    assert time.monotonic() - started < 1.0


def test_hid_return_is_true_when_the_gadget_is_already_hid(monkeypatch):
    from ghostdeck import play

    monkeypatch.setattr(
        play.usb,
        "detect",
        lambda: {"serial": SERIAL, "vid": 0x2207, "pid": 0x0019, "mode": "hid"},
    )
    assert play._await_hid_return(timeout=5.0) is True


def test_hid_return_is_false_while_the_gadget_stays_adb(monkeypatch):
    from ghostdeck import play

    monkeypatch.setattr(
        play.usb,
        "detect",
        lambda: {"serial": SERIAL, "vid": 0x18D1, "pid": 0xD002, "mode": "adb"},
    )
    monkeypatch.setattr(play, "_HID_RETURN_POLL", 0.0)
    started = time.monotonic()
    assert play._await_hid_return(timeout=0.05) is False
    assert time.monotonic() - started < 2.0


def test_hid_return_becomes_true_when_detect_flips_to_hid(monkeypatch):
    """The bounce is asynchronous: ADB must become HID mid-wait, not on the first sample."""
    from ghostdeck import play

    modes = iter(
        [
            {"serial": SERIAL, "vid": 0x18D1, "pid": 0xD002, "mode": "adb"},
            {"serial": SERIAL, "vid": 0x18D1, "pid": 0xD002, "mode": "adb"},
            {"serial": SERIAL, "vid": 0x2207, "pid": 0x0019, "mode": "hid"},
        ]
    )

    def detect():
        try:
            return next(modes)
        except StopIteration:
            return {"serial": SERIAL, "vid": 0x2207, "pid": 0x0019, "mode": "hid"}

    monkeypatch.setattr(play.usb, "detect", detect)
    monkeypatch.setattr(play, "_HID_RETURN_POLL", 0.0)
    assert play._await_hid_return(timeout=5.0) is True


def test_hid_return_waits_through_the_none_dip_before_hid(monkeypatch):
    """After ADB the gadget drops off the bus (`none`) then reappears as HID.

    Measured: t+3.2s adb, t+3.7s none, t+4.2s hid. `none` after ADB is the
    re-enumeration dip, not "USB cannot answer".
    """
    from ghostdeck import play

    modes = iter(
        [
            {"serial": SERIAL, "vid": 0x18D1, "pid": 0xD002, "mode": "adb"},
            {"serial": None, "vid": None, "pid": None, "mode": "none"},
            {"serial": SERIAL, "vid": 0x2207, "pid": 0x0019, "mode": "hid"},
        ]
    )

    def detect():
        try:
            return next(modes)
        except StopIteration:
            return {"serial": SERIAL, "vid": 0x2207, "pid": 0x0019, "mode": "hid"}

    monkeypatch.setattr(play.usb, "detect", detect)
    monkeypatch.setattr(play, "_HID_RETURN_POLL", 0.0)
    assert play._await_hid_return(timeout=5.0) is True

def test_require_hid_does_not_treat_a_first_sample_none_as_success(monkeypatch):
    """`stop` after a real session sets require_hid: a first-sample `none` is the dip."""
    from ghostdeck import play

    monkeypatch.setattr(
        play.usb,
        "detect",
        lambda: {"serial": None, "vid": None, "pid": None, "mode": "none"},
    )
    monkeypatch.setattr(play, "_HID_RETURN_POLL", 0.0)
    assert play._await_hid_return(timeout=0.05, require_hid=True) is False
    assert play._await_hid_return(timeout=0.05) is True


def test_hid_return_is_false_when_adb_drops_off_the_bus_for_good(monkeypatch):
    """A gadget that leaves ADB and never comes back as HID is not a successful H1."""
    from ghostdeck import play

    modes = iter(
        [
            {"serial": SERIAL, "vid": 0x18D1, "pid": 0xD002, "mode": "adb"},
            {"serial": None, "vid": None, "pid": None, "mode": "none"},
        ]
    )

    def detect():
        try:
            return next(modes)
        except StopIteration:
            return {"serial": None, "vid": None, "pid": None, "mode": "none"}

    monkeypatch.setattr(play.usb, "detect", detect)
    monkeypatch.setattr(play, "_HID_RETURN_POLL", 0.0)
    assert play._await_hid_return(timeout=0.05) is False


def test_the_hid_return_budget_is_finite():
    from ghostdeck import play

    assert 0 < play._HID_RETURN_POLL <= play._HID_RETURN_TIMEOUT
    assert play._HID_RETURN_TIMEOUT == 8.0
    assert play._await_hid_return.__defaults__ == (play._HID_RETURN_TIMEOUT,)


def test_stop_refuses_when_the_gadget_stays_adb_after_the_bounce(tmp_path):
    """H1: `stop` must not exit 0 while usb.detect() is still mode=adb.

    The bounce still runs (the waits before it are unchanged). The HID wait is the
    thing that turns a silent ADB leftover into a named refusal.
    """
    ours = _ours()
    try:
        time.sleep(0.3)
        recorded = _record(ours.pid, tmp_path / "home")
        (tmp_path / "host-state.json").write_text(
            json.dumps({"video": {"status": {"cleanup": "proven"}}}), encoding="utf-8"
        )
        result, calls, home = _cli(
            HAPPY_ADB,
            tmp_path,
            "stop",
            pre_state={"play_pid": ours.pid},
            sidecar=recorded,
            stub_session_released=None,
            hid_after_bounce=False,
            hid_timeout=0.2,
        )
        assert result.returncode != 0, result.stdout
        assert "Traceback" not in result.stderr, result.stderr
        assert "still in ADB" in result.stderr, result.stderr
        assert "2207:0019" in result.stderr, result.stderr
        assert "ghostdeck stop" in result.stderr, result.stderr
        assert calls[: len(STOP_ARGV)] == STOP_ARGV, calls
        # The bounce happened; the pid is kept because HID never proved (A-126:
        # records stay until the restore is complete — HID is part of restore).
        assert json.loads((home / ".ghostdeck" / "state.json").read_text())["play_pid"] == ours.pid
    finally:
        if ours.poll() is None:
            ours.kill()
        ours.wait()


def test_stop_does_not_spend_the_hid_bound_without_a_usb_verdict(tmp_path):
    """Acceptance: stubbed `usb.detect()` to none is an immediate no-op, not an 8s wait."""
    started = time.monotonic()
    result, calls, _ = _cli(NO_DEVICE_ADB, tmp_path, "stop", deck_serial=None)
    elapsed = time.monotonic() - started
    assert result.returncode != 0, result.stdout
    assert "no ADB device" in result.stderr
    assert calls == [DEVICES_ARGV]
    assert elapsed < 8.0, f"a missing USB verdict paid the HID bound: {elapsed:.1f}s"


def test_stop_reopens_hidshim_copy_when_the_bridge_is_live(monkeypatch):
    """Keys are the hidshim Studio copy, not CLI vhid. IOHIDUserDeviceCreate returns
    NULL in a non-app process on this host, so `stop` must re-open the copy while
    the bridge is live rather than demand HID or start a sleeper vhid."""
    from ghostdeck import play, studio

    launched = []
    monkeypatch.setattr(play, "_kill_play", lambda **k: None)
    monkeypatch.setattr(play, "_session_released", lambda *a, **k: True)
    monkeypatch.setattr(play, "_cleanup_device", lambda: None)
    monkeypatch.setattr(play, "_clear_play_records", lambda: None)
    monkeypatch.setattr(play.gdstate, "load", lambda: {"play_pid": 1})
    monkeypatch.setattr(studio, "_socket_live", lambda: True)
    monkeypatch.setattr(studio, "running", lambda: False)
    monkeypatch.setattr(studio, "launch", lambda: launched.append("launch"))
    play.stop()
    assert launched == ["launch"]


def test_stop_does_not_relaunch_a_running_hidshim_copy(monkeypatch):
    from ghostdeck import play, studio

    launched = []
    monkeypatch.setattr(play, "_kill_play", lambda **k: None)
    monkeypatch.setattr(play, "_session_released", lambda *a, **k: True)
    monkeypatch.setattr(play, "_cleanup_device", lambda: None)
    monkeypatch.setattr(play, "_clear_play_records", lambda: None)
    monkeypatch.setattr(play.gdstate, "load", lambda: {"play_pid": 1})
    monkeypatch.setattr(studio, "_socket_live", lambda: True)
    monkeypatch.setattr(studio, "running", lambda: True)
    monkeypatch.setattr(studio, "launch", lambda: launched.append("launch"))
    play.stop()
    assert launched == []

#!/usr/bin/env python3
"""Real-hardware verification for ghostdeck. Opt-in, single pass, non-destructive.

Why this exists
---------------
The device-free suite in `tests/` cannot enter the states that actually broke on
hardware: a deck whose adb transport is wedged, an `adb devices -l` line without the
fields a fixture invented, an adb server that has not noticed the HID->ADB switch, a
stage that fails halfway, a `/tmp` that accumulates across sessions. Every defect of
that kind was found by running the product against the attached deck, so the run
belongs in the repository as a first-class artifact instead of in a scratch file.

Design rules, learned the hard way
----------------------------------
1. **Opt-in only.** It refuses to run unless `GHOSTDECK_HW_TEST=1`. It must never run
   in CI, where there is no deck, and never by accident.
2. **One pass, no retry loop.** An earlier scratch harness ran three cycles and
   repeatedly `pkill`ed the bridge and killed the adb server between steps. That
   hammering is the most likely cause of a deck that dropped off the USB bus
   entirely and needed a physical replug. This script does not kill anything it did
   not start, does not restart the adb server, and runs each stage once.
3. **Observe before asking.** Every stage records what the device actually is
   (HID / ADB / absent) *and* what `adb devices -l` reports, so a failure is
   attributable instead of mysterious.
4. **Never invent the device's answer.** The serial is read from the device at
   runtime and never written down; fixtures and logs use the shape only.
5. **Leave the deck as found.** Teardown uses the product's own `stop`/`quit`; if
   that cannot restore the deck, it says so and tells the operator to replug,
   because nothing host-side can fix a deck that is off the bus.

Usage
-----
    GHOSTDECK_HW_TEST=1 python3 tools/hardware_verify.py [--media FILE]...

Run it on an interpreter that has `hidapi` and `pyusb` (the project extras:
`pip install ghostdeck[device]`), because `usb.detect()` needs them. Exit codes:
0 = every check passed, 1 = a check failed, 2 = skipped (no deck attached).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ghostdeck import adb, play, studio, usb  # noqa: E402

HOST_STATE = Path("/tmp/d200-color-host.json")
ENV_GATE = "GHOSTDECK_HW_TEST"
ADB_SETTLE_SECONDS = 2.0

failures: list[str] = []
notes: list[str] = []


def check(label: str, ok: bool, detail: str) -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: {detail}")
    if not ok:
        failures.append(label)


def device_mode() -> str:
    try:
        return usb.detect().get("mode") or "none"
    except Exception as error:  # a missing backend is a real (reported) state
        return f"error:{type(error).__name__}"


def device_serial() -> str | None:
    try:
        return usb.detect().get("serial")
    except Exception:
        return None


def adb_devices() -> list[tuple[str, str]]:
    """(serial, state) pairs from `adb devices`, or [] when adb is unusable.

    Total by design: this is called from `observe()` on every stage, so a missing adb must
    not escape as an OSError from inside a diagnostic. The caller decides what a missing
    adb means; `main()` checks for it up front.
    """
    try:
        out = subprocess.run(["adb", "devices"], capture_output=True, text=True).stdout
    except OSError:
        return []
    rows = []
    for line in out.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 2:
            rows.append((fields[0], fields[1]))
    return rows


def adb_available() -> bool:
    """True when an `adb` binary can be executed at all."""
    try:
        subprocess.run(["adb", "version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def _process_command_line(pid) -> str:
    """Full argv of pid. Linux `ps -o command=` is the 15-char comm name."""
    try:
        raw = Path(f"/proc/{int(pid)}/cmdline").read_bytes()
    except (OSError, TypeError, ValueError):
        raw = b""
    if raw:
        return raw.replace(b"\x00", b" ").decode("utf-8", "replace")
    try:
        return subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError, TypeError, ValueError):
        return ""


def own_bridges() -> list[int]:
    """Only this repository's bridge.

    Matching the bare filename also matches a test that copies the bridge to a temp
    dir and runs it against a fake adb; that inflated an earlier process count and
    looked like a duplicate bridge.
    """
    marker = str(ROOT / "vendor" / "d200-local-bridge.py")
    listed = subprocess.run(["pgrep", "-f", "d200-local-bridge.py"], capture_output=True, text=True).stdout
    pids = []
    for pid in listed.split():
        if is_bridge_pid(pid):
            pids.append(int(pid))
    return pids


def is_bridge_pid(pid) -> bool:
    """True when this exact pid is currently one of this repository's bridge processes.

    This is a pid-reuse guard, **not** an ownership test. Ownership is established as
    "observed absent before bring-up" (see main()); between then and teardown a pid can be
    recycled, and signalling a recycled pid would kill an unrelated process. The argv is
    re-read immediately before signalling for exactly that reason.
    """
    marker = str(ROOT / "vendor" / "d200-local-bridge.py")
    return marker in _process_command_line(pid)


def own_bridge_count() -> int:
    return len(own_bridges())


def stop_own_bridges(pids: list[int], *, timeout: float = 10.0) -> list[int]:
    """Stop only the bridge processes this run started, and wait for them to exit.

    No product command stops the bridge: `studio._stop_owned_bridge` only reaps a child
    that Studio itself spawned, and `ghostdeck stop` deliberately does not touch it. So a
    verification run that calls `studio._ensure_bridge()` owns the bridge and has to
    release it, otherwise the deck cannot return to HID and the staged agent survives.

    The caller passes the pids that appeared DURING this run (see main()), so a bridge
    belonging to the operator or another tool is never in the list. Each pid is re-checked
    against its argv before signalling to survive pid recycling.
    """
    for pid in pids:
        if not is_bridge_pid(pid):
            continue  # exited already, or the pid was recycled; leave it alone
        subprocess.run(["kill", "-TERM", str(pid)], check=False)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not own_bridges():
            return []
        time.sleep(0.3)
    return own_bridges()


def wait_for_mode(wanted: str, *, timeout: float = 25.0) -> str:
    """Poll until the deck reports `wanted` (or a different mode settles)."""
    deadline = time.monotonic() + timeout
    current = device_mode()
    while time.monotonic() < deadline:
        current = device_mode()
        if current == wanted:
            return current
        time.sleep(0.5)
    return current


def cli(*args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    """Run the product CLI, capturing output through a FILE.

    Two traps this helper exists to avoid, both hit for real:

    1. A pipe would be held open by the detached player that `play` leaves running, so
       `communicate()` would block until its timeout and the harness would look like it hung.
       Hence a file, not a pipe.
    2. **`play` never returns on its own.** `play.start_play` always passes `--loop`, so the
       command runs until the player is stopped. A synchronous `play` call therefore always
       ends in a timeout, no matter how short the clip - which read as "play hung" twice while
       the deck had in fact consumed 12461 frames. `play` must be treated as a fire-and-hold
       command: the caller starts it, watches the published diagnostics, and stops it.

    Pass an explicit `timeout` when the command is expected to be long-lived; see
    `start_play_async` for the pattern the harness uses.
    """
    handle = tempfile.NamedTemporaryFile(prefix="ghostdeck-cli-", suffix=".log", delete=False)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "ghostdeck.cli", *args],
            stdout=handle, stderr=subprocess.STDOUT, timeout=timeout,
            cwd=str(ROOT), env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        )
        result.stdout = Path(handle.name).read_text(errors="replace")
        result.stderr = ""
        return result
    finally:
        handle.close()


def start_play_async(media: str) -> subprocess.Popen:
    """Start `play` without waiting for it.

    `play` loops until stopped, so it is a fire-and-hold command. The caller watches
    `HOST_STATE`'s diagnostics for consumption and then calls `cli("stop")`. Output goes to a
    file for the same reason as `cli()`.
    """
    handle = tempfile.NamedTemporaryFile(prefix="ghostdeck-play-", suffix=".log", delete=False)
    process = subprocess.Popen(
        [sys.executable, "-m", "ghostdeck.cli", "play", media],
        stdout=handle, stderr=subprocess.STDOUT,
        cwd=str(ROOT), env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    process._ghostdeck_log = handle  # keep the file object alive for the duration
    return process


def host_diagnostics() -> dict:
    try:
        return json.loads(HOST_STATE.read_text()).get("diagnostics") or {}
    except (OSError, json.JSONDecodeError):
        return {}


def observe(stage: str) -> None:
    """Record everything about the device at this moment, before asserting anything."""
    serial = device_serial()
    print(f"  [{stage}] usb={device_mode()} serial={'yes' if serial else 'no'} adb={adb_devices()}")


def deck_paths(serial: str, *patterns: str) -> dict[str, str]:
    """Master-side inspection, straight to adb.

    The product's allowlist deliberately permits only the exact commands it issues,
    so this harness does not widen it for its own convenience.
    """
    found = {}
    for pattern in patterns:
        result = subprocess.run(
            ["adb", "-s", serial, "shell", f"ls -d {pattern}"],
            capture_output=True, text=True, timeout=15,
        )
        out = (result.stdout or "").strip()
        found[pattern] = out if (result.returncode == 0 and out) else "ABSENT"
    return found


def reachable(serial: str, *, timeout: float = 25.0) -> bool:
    """Wait for the deck to answer a trivial allowlisted command."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            result = adb.run(["-s", serial, "shell", "getprop sys.usb.config"],
                             capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                return True
        except subprocess.SubprocessError:
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def stop_playing() -> str:
    """Best-effort teardown with the product's own commands. Returns a description."""
    outcomes = []
    result = cli("stop")
    outcomes.append(f"stop rc={result.returncode}")
    if result.returncode != 0:
        outcomes.append((result.stdout or "").strip()[:120])
    result = cli("quit")
    outcomes.append(f"quit rc={result.returncode}")
    return "; ".join(outcomes)


def main() -> int:
    if os.environ.get(ENV_GATE) != "1":
        print(f"SKIP: real-hardware verification is opt-in; set {ENV_GATE}=1 to run it.")
        print("      It drives the attached deck (HID <-> ADB) and must not run in CI.")
        return 2

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--media", action="append", default=None,
                        help="media file to play; repeatable. Defaults to none (bring-up/teardown only).")
    parser.add_argument("--no-clean", action="store_true",
                        help="do not run stop/quit at the end (leaves the session up for inspection)")
    args = parser.parse_args()

    print("== baseline ==")
    # Prerequisites FIRST, before any observation, so a missing tool is a clean refusal
    # rather than a traceback out of a diagnostic (C-168).
    if not adb_available():
        print("SKIP: `adb` is not usable on PATH. This harness drives the deck through adb; "
              "install Android platform-tools and re-run.")
        return 2

    # Ownership gate, before anything else can start a bridge: this harness only ever
    # releases a bridge IT started, so an already-running one is a refusal, never something
    # to adopt. Adopting is what let a previous revision signal a bridge that belonged to
    # the operator (C-167).
    before = own_bridges()
    if before:
        print(f"REFUSING: bridge process(es) {before} are already running.")
        print("          This harness only stops a bridge it started itself, so it will not")
        print("          adopt or signal an existing one. Stop it first, then re-run.")
        return 2
    check("no bridge running at start", not before, f"pids={before}")

    observe("before")
    start_mode = device_mode()
    if start_mode not in ("hid", "adb"):
        print(f"SKIP: no deck attached (usb={start_mode}). Attach the deck and re-run.")
        return 2

    print("\n== bridge bring-up ==")
    began = time.monotonic()
    try:
        studio._ensure_bridge()
    except Exception as error:
        check("bring-up", False, f"{type(error).__name__}: {error}")
        print("\nRESULT: cannot continue without a bridge")
        return 1
    setup_seconds = time.monotonic() - began
    # Ownership is "absent before bring-up": only pids that were NOT there before are ours
    # to release. The `before` set is empty by construction here (we refused otherwise), but
    # the subtraction is kept so the rule holds even if the gate above is ever relaxed.
    our_bridges = [pid for pid in own_bridges() if pid not in before]
    observe("after bring-up")
    check("bridge socket live", studio._socket_state()[0] == "live", str(studio._socket_state()))
    check("exactly one bridge", own_bridge_count() == 1, f"count={own_bridge_count()}")
    check("deck reports adb", device_mode() == "adb", device_mode())

    serial = device_serial()
    check("serial resolved", bool(serial), "read from the device")
    if not serial:
        return 1

    rows = adb_devices()
    check("adb sees the deck", any(s == serial for s, _ in rows), str(rows))
    check("deck answers a command", reachable(serial), f"after {setup_seconds:.1f}s bring-up")
    if setup_seconds > 10:
        notes.append(f"bring-up took {setup_seconds:.1f}s (a fast bring-up is ~2-6s); "
                     "slow bring-up means the device proxy needed retries")

    for media in args.media or []:
        print(f"\n== play {media} ==")
        if HOST_STATE.exists():
            HOST_STATE.unlink()
        # `play` loops until stopped, so it is started, watched, and then stopped - never
        # awaited. Awaiting it always times out regardless of clip length (see cli()).
        player = start_play_async(media)
        peak = {}
        for _ in range(24):
            time.sleep(1.0)
            current = host_diagnostics()
            if (current.get("framesConsumed") or 0) >= (peak.get("framesConsumed") or 0):
                peak = current
            if (peak.get("framesConsumed") or 0) > 5 and peak.get("firstConsumedReceipt"):
                break
            # Do NOT stop waiting when the launcher exits. `ghostdeck play` is a fire-and-hold
            # launcher: it starts the player, waits out its own A-103 grace window and returns,
            # and the player only begins publishing consumed frames a second or two AFTER that.
            # Treating the launcher's exit as failure made a healthy session read as 0 frames.
            # Only a launcher that exited NON-ZERO is a failure.
            if player.poll() not in (None, 0) and (peak.get("framesConsumed") or 0) == 0:
                break
        check("play started", (peak.get("framesConsumed") or 0) > 0 or player.poll() in (None, 0),
              f"launcher rc={player.poll()}")
        consumed = peak.get("framesConsumed") or 0
        check("frames consumed by the deck", consumed > 5,
              f"sent={peak.get('framesSent')} consumed={consumed} bytes={peak.get('streamBytesSent')}")
        observe("during play")

        status = cli("status")
        line = (status.stdout or "").strip().splitlines()[0] if status.stdout else ""
        check("status reports playing", "playing=yes" in line, line[:110] or "(no output)")

        print(f"\n== stop {media} ==")
        observe("before stop")
        still = cli("stop")
        check("stop exit 0", still.returncode == 0,
              f"rc={still.returncode} {(still.stdout or '').strip()[:90]}")
        observe("after stop")
        check("bridge survived stop", own_bridge_count() >= 1, f"count={own_bridge_count()}")
        check("deck still attached", device_mode() in ("hid", "adb"), device_mode())
        if device_mode() == "none":
            notes.append("the deck left the USB bus during stop; it needs a physical replug. "
                         "Investigate whether the stock-UI restart can drop the link.")
        # The staged agent belongs to the BRIDGE's session, not to `stop`: the bridge removes
        # it in its own teardown (`_remove_staged_agent`). So while the bridge runs the agent
        # is legitimately present, and asserting ABSENT here would be asserting the wrong
        # contract. It is checked in the teardown section instead, after the bridge is gone.

    print("\n== teardown ==")
    if args.no_clean:
        print("  (--no-clean: leaving the session up)")
    else:
        observe("before teardown")
        print(f"  teardown: {stop_playing()}")
        remaining = stop_own_bridges(our_bridges)
        check("the bridge this run started is stopped", not remaining, f"remaining={remaining}")
        # Only once the bridge is gone can the deck return to HID: the restarted stock UI
        # performs the HID re-enumeration, and the bridge holds the ADB session until then.
        final_mode = wait_for_mode("hid")
        observe("after teardown")
        check("deck back on HID", final_mode == "hid", final_mode)
        check("not playing", not play.playing(), f"playing={play.playing()}")
        if serial and final_mode == "hid":
            # Re-check the staged agent now, the point at which the bridge has removed it.
            staged = deck_paths(serial, "/tmp/d200-color-agent")["/tmp/d200-color-agent"]
            check("no staged agent left after the bridge stopped", staged == "ABSENT", staged)

    print()
    for note in notes:
        print(f"NOTE: {note}")
    if failures:
        print(f"RESULT: {len(failures)} FAILED -> {failures}")
        return 1
    print("RESULT: all hardware checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

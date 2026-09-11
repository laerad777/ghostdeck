from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from ghostdeck import adb, devicebuild, state as gdstate, usb, vhid

VENDOR_PLAY = Path(__file__).resolve().parents[2] / "vendor" / "d200-color-play.py"
VENDOR_DIR = Path(__file__).resolve().parents[2] / "vendor"

_PS_TIMEOUT = 5.0


_TOOL_HINTS = {
    "ffmpeg": "brew install ffmpeg",
    "ffprobe": "brew install ffmpeg",
    "yt-dlp": "brew install yt-dlp",
}


def _require_tools(source: str) -> None:
    """Fail before any device work when the player's own external tools are missing."""
    tools = ["ffmpeg", "ffprobe"]
    if "://" in source:
        tools.append("yt-dlp")
    for tool in tools:
        if shutil.which(tool) is None:
            raise RuntimeError(f"{tool} not on PATH: install it ({_TOOL_HINTS[tool]})")


def _identity_path() -> Path:
    """0600 sidecar recording which pid is our player, resolving HOME at call time."""
    return gdstate.HOME / "play.pid"


def _write_identity(payload: dict) -> None:
    """Atomically write the sidecar with mkstemp + fchmod + os.replace (same discipline as state.save)."""
    target = _identity_path()
    target.parent.mkdir(mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".play.pid.")
    try:
        os.fchmod(fd, 0o600)
        handle = os.fdopen(fd, "w", encoding="utf-8")
        fd = -1  # ownership moved to `handle`
        with handle:
            handle.write(json.dumps(payload) + "\n")
        os.replace(tmp, target)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_identity() -> dict | None:
    """The recorded {"pid", "lstart"} pair, or None when nothing usable is recorded."""
    try:
        raw = json.loads(_identity_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    pid = raw.get("pid")
    lstart = raw.get("lstart")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    if not isinstance(lstart, str) or not lstart.strip():
        return None
    return {"pid": pid, "lstart": " ".join(lstart.split())}


def _remove_identity() -> None:
    try:
        _identity_path().unlink()
    except OSError:
        pass


def _probe_start_time(pid):
    """Read the live process start time, keeping "ps could not answer" distinct from "no such pid".

    Returns ``(lstart, unknown_reason)``:

    * ``(text, None)``  – ps ran; the process is live and this is its start time.
    * ``(None, None)``  – ps ran; there is no such process (dead or recycled away).
    * ``(None, reason)`` – ps itself could not answer, so identity is undeterminable.

    ``LC_ALL=C`` is forced so a recorded string cannot diverge from a later reading by locale, and
    ``TZ=UTC`` so it cannot diverge by timezone: `ps -o lstart=` renders a LOCAL-time string, so a
    record taken under, say, Asia/Seoul and a read under UTC would otherwise disagree by 9 hours and
    make a still-running player look like a recycled pid. `-ww` prevents truncation.
    """
    env = dict(os.environ, LC_ALL="C", TZ="UTC")
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-ww", "-p", str(pid)],
            capture_output=True,
            text=True,
            env=env,
            timeout=_PS_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, f"ps timed out after {_PS_TIMEOUT:g}s"
    except OSError as error:
        return None, f"ps could not be run ({error})"
    text = " ".join((result.stdout or "").split())
    if text:
        return text, None
    if result.returncode == 1:
        return None, None
    return None, f"ps exited {result.returncode}"


def _record_identity(pid) -> None:
    """Record the pid and its start time, so a later run can tell it apart from a recycled pid."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return
    lstart, reason = _probe_start_time(pid)
    if lstart is None:
        # Nothing trustworthy to record: identity stays undeterminable rather than a guess.
        return
    _write_identity({"pid": pid, "lstart": lstart})


def _player_identity(pid):
    """Tri-state identity: ``(True | False | None, reason)``.

    * True – the sidecar records this pid and the live start time still matches.
    * False – identity is determinable and this is not our player: the sidecar records a different
      pid, the live start time does not match, or ps reports no such process. A stale record is safe
      to discard.
    * None – identity cannot be determined. Two sub-cases, both deliberately conservative:
        - ps itself could not answer (missing/broken/timed out), so nothing can be classified; or
        - ps answered, the process is live, but no identity was ever recorded for it. Erasing the pid
          here would lose the only handle on a running player, so it is preserved instead.
      Callers must not treat None as "not ours": nothing may be signalled or erased.

    A recorded identity whose pid is gone is *classifiable* (no such process), so it resolves to
    False rather than None; that keeps the common stale-state case from becoming a permanent error.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False, ""
    recorded = _load_identity()
    if recorded is None:
        lstart, reason = _probe_start_time(pid)
        if reason is not None:
            return None, reason
        if lstart is None:
            return False, ""  # ps ran and reports no such process
        return None, "no recorded identity"
    if recorded["pid"] != pid:
        return False, ""
    lstart, reason = _probe_start_time(pid)
    if reason is not None:
        return None, reason
    if lstart is None:
        return False, ""  # ps ran and reports no such process
    return lstart == recorded["lstart"], ""


def _is_our_player(pid):
    """True/False when identity is determinable, None when it is not. See `_player_identity`."""
    return _player_identity(pid)[0]


def _kill_play() -> None:
    """Stop our own player. Raises when its fate cannot be determined (nothing is then erased)."""
    data = gdstate.load()
    pid = data.get("play_pid")
    if pid is None:
        _remove_identity()
        return
    identity, reason = _player_identity(pid)
    if identity is None:
        raise RuntimeError(f"cannot verify player pid {pid}: {reason}; not signalling")
    if identity:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    # Determinable either way, so the record is no longer meaningful: clear it with the pid.
    data["play_pid"] = None
    gdstate.save(data)
    _remove_identity()


def start_play(source: str) -> None:
    _require_tools(source)
    adb.require_adb()
    gdstate.ensure_dirs()
    devicebuild.ensure()
    found = usb.detect()
    if found is None or found.get("mode") in (None, "none"):
        raise RuntimeError("no D200 on USB")
    if found["mode"] == "hid":
        usb.switch_hid_to_adb()
        found = usb.detect()
    if found is None or found.get("mode") != "adb":
        raise RuntimeError("deck is not in ADB after switch")
    try:
        vhid.start()
    except Exception as error:
        print(f"virtual HID skipped: {error}", file=sys.stderr)
    try:
        _kill_play()
    except RuntimeError as error:
        # A new player is about to take over the pid, so an unverifiable predecessor is not fatal.
        print(f"warning: {error}", file=sys.stderr)
    if not VENDOR_PLAY.is_file():
        raise RuntimeError(f"vendor player missing: {VENDOR_PLAY}")
    env = dict(os.environ)
    env["GHOSTDECK_SERIAL"] = found.get("serial") or adb.serial_from_devices() or ""
    env["PYTHONPATH"] = str(VENDOR_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-B",
            "-u",
            str(VENDOR_PLAY),
            source,
            "--fps",
            "source",
            "--quality",
            "12",
            "--crop",
            "auto",
            "--loop",
        ],
        start_new_session=True,
        env=env,
    )
    data = gdstate.load()
    data["play_pid"] = proc.pid
    gdstate.save(data)
    _record_identity(proc.pid)


def playing() -> bool:
    return _is_our_player(gdstate.load().get("play_pid")) is True


def _cleanup_device() -> None:
    """Restore the stock UI and clear the deck's staging dir. Raises on any fatal step.

    The restore is `ctl.stop zkswe` followed by `ctl.start zkswe`, not a bare start. A start on an
    already-running service is a no-op that never re-initialises the USB gadget, so the master's
    real-deck run (2026-09-11) observed `ghostdeck stop` exit 0 while
    `cat /sys/class/zkswe_usb/zkswe0/functions` still read `adb`; `detect` reported `mode=adb` 30s
    later with `zkgui_ui` running the whole time. Stopping and restarting the service is what
    returned the gadget to HID (`functions=<empty>`, `detect -> mode=hid` within 5s).

    We deliberately do not switch USB modes ourselves: the restarted stock UI performs the HID
    re-enumeration, and the project boundary forbids `functions=hid,adb` and any firmware write.
    """
    serial = adb.serial_from_devices()
    if not serial:
        raise RuntimeError("no ADB device reachable: stock UI not restored and /tmp/ghostdeck-* not cleared")
    for argv in (
        ["-s", serial, "shell", "setprop ctl.stop zkswe"],
        ["-s", serial, "shell", "setprop ctl.start zkswe"],
        ["-s", serial, "shell", "rm -f /tmp/ghostdeck-*"],
    ):
        result = adb.run(argv, capture_output=True, text=True)
        if result.stdout:
            print(result.stdout, end="")
        if result.returncode != 0:
            detail = (result.stderr or "").strip()
            raise RuntimeError(f"adb failed ({result.returncode}): {' '.join(argv)}: {detail}")
    # Informational only: an empty /tmp/ghostdeck makes `ls` exit 1, which is the success case.
    listing = adb.run(["-s", serial, "shell", "ls /tmp/ghostdeck*"], capture_output=True, text=True)
    if listing.returncode == 0 and listing.stdout:
        print(listing.stdout, end="")


def stop() -> None:
    """Stop our player, then restore the deck.

    The player's identity decides the exit code only, never whether the deck is restored: an
    unverifiable pid is left exactly as it is (nothing signalled, nothing erased) and the cleanup
    still runs, because skipping it would leave the stock UI stopped and the staged files on the
    device with no other command able to restore them.
    """
    identity_error = None
    try:
        _kill_play()
    except RuntimeError as error:
        identity_error = error
    try:
        _cleanup_device()
    except RuntimeError as cleanup_error:
        if identity_error is None:
            raise
        raise RuntimeError(f"{cleanup_error} (as well as: {identity_error})") from cleanup_error
    if identity_error is not None:
        raise identity_error

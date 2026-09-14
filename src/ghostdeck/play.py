from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ghostdeck import adb, devicebuild, state as gdstate, studio, tree, usb

VENDOR_PLAY = tree.candidate_root() / "vendor" / "d200-color-play.py"
VENDOR_DIR = tree.candidate_root() / "vendor"

_PS_TIMEOUT = 5.0
# A wedged adb server must not turn `stop` - the one documented recovery command - into a hang
# (A-116). Every device call is bounded; the informational listing's timeout is tolerated exactly
# like its non-zero exit code already is.
_ADB_TIMEOUT = 30.0
# A player that dies immediately (a bad source, a missing device-side tool) must not be reported as
# a successful start (A-103). The window is a grace period, not a health check: it is long enough to
# catch an interpreter that starts and exits, and short enough to stay invisible to the user.
_PLAY_GRACE = 1.0
# `stop()` must not bounce the deck's stock UI while a media session is still live. Measured on the
# attached deck: after SIGTERM the session keeps reporting `state=3 cleanup=pending` for ~2s and only
# reaches `state=9 cleanup=proven` at about t+3s. The UI restart used to land inside that window and
# tore the transport down (`terminalCode: 12 D200_VS_DISCONNECTED`), leaving the next session to open
# onto an unreleased boundary (`CLEANUP_FAILED / cleanup: unproven`, tens of frames instead of ~600).
# This is a bounded wait for the release, not a health check and not a retry loop.
_SESSION_RELEASE_TIMEOUT = 8.0
_SESSION_RELEASE_POLL = 0.25
# The deck also needs time to serve commands again AFTER the stock-UI bounce, before a new session can
# open. Measured by sweeping the gap between two sessions that share one bridge:
#     settle 0s  -> second session 79 frames,  terminalCode 8  (CLEANUP_FAILED, cleanup unproven)
#     settle 5s  -> second session 293 frames, terminalCode 0  (healthy)
#     settle 15s -> second session 427 frames, terminalCode 0  (healthy)
# So `stop()` waits for the transport to answer again before it returns, which makes the command's
# completion mean "the deck is usable" rather than only "the restart was issued".
_TRANSPORT_RECOVERY_TIMEOUT = 20.0
_TRANSPORT_RECOVERY_POLL = 0.5
# How many consecutive successful probes make the transport "settled". One is not enough: the stock UI
# answers at t+0.1s, the deck dips at t+3.3s, and it settles at t+4.3s, so a single success returns
# before the dip. Six samples at 0.5s covers that gap with margin.
_TRANSPORT_STABLE_SAMPLES = 6
# H1: after a real session the stock-UI bounce must re-enumerate the gadget as HID
# (2207:0019). `_await_transport_recovery` only proves adbd answers, which it does
# while the gadget is still 18d1:d002. Bound matches the original H1 observation
# (`detect -> mode=hid` within 5s); the poll is USB, not adb.
_HID_RETURN_TIMEOUT = 8.0
_HID_RETURN_POLL = 0.25
# One HID sample is not H1: after stop rc=0 in 7.2s, detect was `none` then `adb`.
# Four samples at 0.25s is 1s of consecutive HID past the dip.
_HID_STABLE_SAMPLES = 4
# The player publishes the session record here (same path `vendor/d200-color-play.py` writes).
# Module-level so a test can monkeypatch it: it lives in /tmp, which HOME isolation cannot redirect.
_HOST_STATE = Path("/tmp/d200-color-host.json")
# `adb devices -l` identifies some builds of the deck in their product/model/device fields (A-137),
# matched as whole fields so `model:D200X` cannot be mistaken for the deck. This is an ADDITIONAL
# accepted path only: the attached deck emits none of these fields (T15 - its line is
# `<serial>      device usb:18092032X transport_id:4`), so the primary signal is the USB
# layer's VID/PID-matched verdict in `_deck_serial()`.
_DECK_FIELDS = ("product:d200", "model:D200", "device:d200")
# The only `adb devices` transport state that can execute a command. Every other state means the deck
# is attached but its adbd is not answering - `offline` is the one the wedged deck reported - and T16
# showed that state being reported to the user as "no attached device identifies as the D200".
TRANSPORT_READY = "device"

# Signal 3 (C-153/C-155): the deck's own sysfs node, already on the adb allowlist (`_EXACT_SHELL`).
# A phone does not expose it, so a read that answers is a POSITIVE identification - A-137 still holds,
# because the probe asks each device about itself instead of picking one by position. This is what
# lets `stop` find the deck with `adb` alone, on a host without the optional Python extras (no
# hidapi/pyusb, so no USB verdict) and with the field-less real deck line (T15, so no `-l` identity).
_DECK_PROBE = ("cat", "/sys/class/zkswe_usb/zkswe0/functions")
# Bounded so a host with many attached devices cannot turn `stop` - the recovery command - into a
# probe loop. Only candidates adb calls `device` are probed at all.
_PROBE_LIMIT = 4


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


def _validate_source(source: str) -> None:
    """Reject a source that is neither an existing file nor a URL before any device work (A-103).

    SOURCE is handed straight to the player's argv, so a typo used to travel all the way to a
    child process that died on its first read - and was still reported as a successful start.
    """
    if "://" in source:
        return
    if not Path(source).is_file():
        raise RuntimeError(f"source is not a file and is not a URL: {source}")


def _adb_entries() -> list[tuple[str, str, list[str]]]:
    """Every device `adb devices -l` lists, as ``(serial, state, remaining fields)``, in adb's order.

    The transport state is kept rather than filtered on, because the caller has to tell a usable
    device from one that is attached but not answering (T16): both are listed by `adb`, they are
    different user-visible problems, and their remedies are different. The per-line fields are kept
    because the line's shape is firmware-dependent: the attached deck emits only
    ``usb:<...> transport_id:<n>`` (T15), while other builds report product/model/device.
    """
    try:
        result = adb.run(["devices", "-l"], capture_output=True, text=True, timeout=_ADB_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"adb timed out after {_ADB_TIMEOUT:g}s: devices -l (is the adb server wedged?)"
        ) from None
    except OSError as error:
        raise RuntimeError(f"adb could not be run: {error}") from None
    entries = []
    for line in (result.stdout or "").splitlines():
        fields = line.split()
        # `serial  state  <fields...>`. The `List of devices attached` header is skipped by name, the
        # same way `adb.serial_from_devices()` skips it; a device-less line is not a candidate.
        if len(fields) < 2 or fields[0] == "List":
            continue
        entries.append((fields[0], fields[1], fields[2:]))
    return entries


def _transport_state(entries: list[tuple[str, str, list[str]]], serial: str) -> str:
    """The transport state `adb` reports for `serial`, or "" when it lists no such device."""
    for listed, state, _ in entries:
        if listed == serial:
            return state
    return ""


def _probe_is_deck(serial: str) -> bool:
    """True when `serial` answers the deck-only zkswe sysfs node (signal 3, C-153/C-155).

    The node is queried with the command already on the adb allowlist, so this reaches the device
    through the same default-deny gate as every other device command, and it is a read: nothing is
    changed on a candidate that turns out not to be the deck. That is the whole point of the probe -
    identification without mutation, so a phone attached alongside is tested and then left alone.

    Success requires BOTH a zero exit and non-empty output. Exit 0 alone is not proof that the node
    answered: a remote shell that swallows the failure, or an `adb` that does not propagate the
    remote status, also exits 0. The node prints its function list (the master observed `adb` on the
    attached deck while it was in ADB mode), and a device with no such node prints nothing.

    Any failure to run the probe at all is "this candidate did not answer". The probe is an
    identification attempt, so it must never be what makes `stop` - the documented recovery command -
    raise.
    """
    try:
        result = adb.run(
            ["-s", serial, "shell", *_DECK_PROBE],
            capture_output=True,
            text=True,
            timeout=_ADB_TIMEOUT,
        )
    except Exception:
        return False
    return result.returncode == 0 and bool((result.stdout or "").strip())


def deck_transport(*, restart: bool = True) -> tuple[str | None, str, list[tuple[str, str]]]:
    """``(serial, state, blocked)`` for one `adb devices -l` listing.

    Three outcomes, which T16 showed were being collapsed into "no device":

    * ``(serial, "device", [])`` - the deck is attached and usable.
    * ``(serial, "<other>", [])`` - the deck is attached, identified by its own USB identity (or its
      own `-l` fields), and listed by `adb`, but its transport cannot run a command. The wedged deck
      on the real host reported ``offline``: enumerated on USB as ADB with adbd not answering, and no
      host-side action recovered it (a 160s wait, three `kill-server`/`start-server` cycles, `adb
      reconnect` and a USB reset all failed; only a physical power cycle did).
    * ``(None, "", blocked)`` - the deck was not positively identified. `blocked` still lists every
      device that is attached but not answering, so a caller can tell the truth instead of claiming
      nothing is attached. It is a diagnosis only: nothing is ever sent to those serials (A-137).

    The deck is identified by what it reports about *itself*, never by its position in the device
    list - positional selection was A-137, where the cleanup went to a phone and still exited 0. So a
    phone attached alongside is never classified as the deck, whatever the order of the lines, and
    `blocked` is never used as a fallback for acting on a device.

    Accepted signals, unchanged from `_deck_serial()`:

    1. **The USB layer's verdict.** `usb.detect()` matches the deck on its USB identity (VID/PID
       2207:0019 HID or 18d1:d002 ADB), so an unrelated device cannot produce it. The serial must
       also appear in `adb devices`, because a serial the server does not know cannot be addressed
       with `adb -s`. This is the signal that works on the real deck.
    2. **The deck's own `-l` identity fields**, for firmware that reports them. The attached deck does
       NOT: its line is `<serial>      device usb:18092032X transport_id:4` (T15). So this can only
       ever be an *additional* accepted path, never the only one.
    3. **The deck's own sysfs node, read over `adb` alone** (`_probe_is_deck`, C-153/C-155). This is
       the signal that still works when the extras are absent (no USB verdict) AND the line is
       field-less (no `-l` identity): the two signals above both need something more than `adb`.
       Only candidates adb calls `device` are probed, at most `_PROBE_LIMIT`, and it is a read, so an
       unidentified candidate is never mutated (A-137).

    `restart=False` skips the bounded H3 server restart below. The read-only reporting commands
    (`detect`, `status`) pass it, because a diagnostic that resets the adb server changes the state
    it is reporting on (A-134); `stop()` keeps the restart, since a stale server there means the deck
    never gets restored.
    """
    entries = _adb_entries()
    try:
        found = usb.detect()
    except Exception:  # a USB backend that cannot answer is simply no verdict
        found = None
    deck = found.get("serial") if found and found.get("mode") == "adb" else None
    if deck:
        state = _transport_state(entries, str(deck))
        if state:
            return str(deck), state, []
        if restart:
            # H3: the USB layer has the deck in ADB but the host server lists no transport for it at
            # all - not even an `offline` one - so the server has not picked the deck up. Exactly one
            # restart, never a loop; a restart that cannot run is no help rather than a fatal error.
            try:
                adb.restart_server()
            except RuntimeError as error:
                print(f"adb server restart unavailable: {error}", file=sys.stderr)
            else:
                entries = _adb_entries()
                state = _transport_state(entries, str(deck))
                if state:
                    return str(deck), state, []
    for serial, state, fields in entries:
        if any(field in _DECK_FIELDS for field in fields):
            return serial, state, []
    # Signal 3: ask each usable candidate about its own deck-only node. Nothing here falls back to
    # "the first attached device" - a candidate is accepted only when it positively answers, so a
    # phone attached alongside (whatever the order of the lines) is probed and then rejected.
    probed = 0
    for serial, state, _ in entries:
        if probed >= _PROBE_LIMIT:
            break
        if state != TRANSPORT_READY:
            continue
        probed += 1
        if _probe_is_deck(serial):
            # Name the signal in the diagnosis: before T17 this path did not exist, and a future
            # "which signal saw it?" must not be another mystery.
            print(f"deck identified by the zkswe sysfs probe: {serial}", file=sys.stderr)
            return serial, state, []
    # adb-only, so this works with the extras absent too: the T16 distinction between "attached but
    # not answering" and "absent" must not depend on the USB layer that just failed to answer.
    blocked = [(serial, state) for serial, state, _ in entries if state != TRANSPORT_READY]
    return None, "", blocked


def _deck_serial() -> str | None:
    """The serial of a *usable* attached D200, or None when nothing positively identifies one.

    Delegates the identification and the attached-but-unusable distinction to `deck_transport()`; the
    `state` test below is the whole difference, and it is what keeps a wedged deck from being handed
    to a caller that is about to send it a command.
    """
    serial, state, _ = deck_transport()
    if serial is None or state != TRANSPORT_READY:
        return None
    return serial


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


def _clear_play_records() -> None:
    """Erase the stored pid and the identity sidecar together (A-126).

    A-104: this is a single locked read-modify-write. The previous `load()`-then-`save()` left the
    read outside the lock, so a concurrent writer's update that landed in between was discarded.

    Called only once nothing can still act on the record, so the record that describes a session
    outlives the session's own effects on the deck.
    """
    gdstate.update(play_pid=None)
    _remove_identity()


def _kill_play(*, keep_record: bool = False) -> None:
    """Stop our own player. Raises when its fate cannot be determined (nothing is then erased).

    `keep_record=True` leaves the pid and the sidecar in place after a determinable outcome so the
    caller can erase them once its own work has succeeded (A-126); nothing is ever erased when the
    identity is undeterminable, whatever the caller asks for.
    """
    data = gdstate.load()
    pid = data.get("play_pid")
    if pid is None:
        # No session is recorded, so an orphaned sidecar describes nothing and is just litter.
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
    # Determinable either way, so the record is no longer meaningful. It is cleared here unless the
    # caller still needs it to outlive its own later steps.
    if not keep_record:
        _clear_play_records()


def start_play(source: str) -> None:
    _require_tools(source)
    adb.require_adb()
    # Before any device work: an unusable SOURCE must fail cheaply and name the user's own input,
    # not travel to a child process that dies on its first read (A-103).
    _validate_source(source)
    # T19: the player's first device-side act is `connect_bridge(BRIDGE_SOCKET)` (before
    # `videoOpen`), so with no bridge listening it dies inside the child with a raw
    # `ConnectionRefusedError` and the user is told nothing actionable. Refuse here - after the two
    # zero-cost checks on the user's own invocation, and before `ensure_dirs`/`devicebuild.ensure`/
    # `usb.detect`, so a missing bridge costs nothing and no device is touched. `studio` owns this
    # check because the bridge is studio's; `play` must not start one (see `studio.require_bridge`).
    studio.require_bridge()
    gdstate.ensure_dirs()
    devicebuild.ensure()
    found = usb.detect()
    if found is None or found.get("mode") in (None, "none"):
        # A missing backend is not a missing deck (A-102): `detect()` attaches the hint when it
        # could not reach a hardware conclusion, and reporting "no D200 on USB" here would blame
        # the hardware for an incomplete Python environment.
        dependency = (found or {}).get("dependency")
        if dependency:
            raise usb.MissingDependency(dependency)
        raise RuntimeError("no D200 on USB")
    if found["mode"] == "hid":
        usb.switch_hid_to_adb()
        found = usb.detect()
    if found is None or found.get("mode") != "adb":
        raise RuntimeError("deck is not in ADB after switch")
    try:
        _kill_play()
    except RuntimeError as error:
        # A new player is about to take over the pid, so an unverifiable predecessor is not fatal.
        print(f"warning: {error}", file=sys.stderr)
    if not VENDOR_PLAY.is_file():
        raise RuntimeError(f"vendor player missing: {VENDOR_PLAY}")
    env = dict(os.environ)
    env["GHOSTDECK_SERIAL"] = found.get("serial") or _deck_serial() or ""
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
    # A-103: a player that died on startup must not be recorded as a running session, and the
    # command must not exit 0 - the user would be told playback started while nothing is playing.
    # Nothing is written before this check, so there is no stale record to clear on the way out.
    try:
        returncode = proc.wait(timeout=_PLAY_GRACE)
    except subprocess.TimeoutExpired:
        returncode = None
    if returncode is not None:
        raise RuntimeError(
            f"player exited with status {returncode} before it started; nothing is playing"
        )
    data = gdstate.update(play_pid=proc.pid)
    if data.get("play_pid") != proc.pid:
        # A-104: fail loudly rather than silently reporting a session that was never recorded.
        raise RuntimeError(f"could not record the player pid {proc.pid} in {gdstate.STATE_PATH}")
    _record_identity(proc.pid)


def playing() -> bool:
    return _is_our_player(gdstate.load().get("play_pid")) is True


def _adb_mutate(argv: list[str]) -> None:
    """Run one device-mutating command, bounded by `_ADB_TIMEOUT` (A-116).

    `subprocess.run` without a timeout blocks indefinitely, so a wedged adb server made `stop` - the
    one documented recovery command - hang with the deck still hijacked. A timeout is reported in
    the same shape as a non-zero exit, because the step did not happen either way.
    """
    try:
        result = adb.run(argv, capture_output=True, text=True, timeout=_ADB_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"adb timed out after {_ADB_TIMEOUT:g}s: {' '.join(argv)} (is the adb server wedged?)"
        ) from None
    if result.stdout:
        print(result.stdout, end="")
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        raise RuntimeError(f"adb failed ({result.returncode}): {' '.join(argv)}: {detail}")


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

    The target device is chosen by its own identity fields rather than by position, and every call
    is bounded, so neither a second attached device (A-137) nor a wedged adb server (A-116) can make
    this command silently fail to restore the deck. A deck that is attached but not answering is
    named as such rather than reported as absent (T16).
    """
    serial, state, blocked = deck_transport()
    if serial is None:
        if blocked:
            # T16: something IS attached, so "no attached device identifies as the D200" would be
            # false and would send the user to check a cable. Name what is attached, say plainly that
            # nothing was sent to it (it is not positively identified as the deck), and give the only
            # remedy that exists. This branch is reached on the real host whenever the USB backend is
            # unusable - the venv has neither hidapi nor pyusb - with a wedged deck on the bus.
            listed = ", ".join(f"{found_serial} ({found_state})" for found_serial, found_state in blocked)
            raise RuntimeError(
                f"no attached device is positively identified as the D200, but {listed} is attached "
                f"with a transport that cannot run a command. Nothing is sent to an unidentified "
                f"device, so the stock UI is not restored and /tmp/ghostdeck-* is not cleared. If "
                f"that is the deck, no host-side step recovers it (measured on the wedged deck: a "
                f"160s wait, three kill-server/start-server cycles, `adb reconnect` and a USB reset "
                f"all failed) - power-cycle or replug the deck, then re-run `ghostdeck stop`"
            )
        raise RuntimeError(
            "no ADB device reachable: no attached device identifies as the D200, so the stock UI is "
            "not restored and /tmp/ghostdeck-* is not cleared"
        )
    if state != TRANSPORT_READY:
        # The deck IS attached and IS identified; its adb transport just cannot run a command. Saying
        # "no attached device identifies as the D200" here was false and sent the user to check a
        # cable. The remedy is physical: measured on the wedged deck, nothing host-side restored it.
        raise RuntimeError(
            f"the deck ({serial}) is attached in ADB mode but its adb transport is {state}, so it "
            f"cannot run a command: the stock UI is not restored and /tmp/ghostdeck-* is not cleared. "
            f"No host-side step recovers this (measured on the wedged deck: a 160s wait, three "
            f"kill-server/start-server cycles, `adb reconnect` and a USB reset all failed) - "
            f"power-cycle or replug the deck, then re-run `ghostdeck stop`"
        )
    for argv in (
        ["-s", serial, "shell", "setprop ctl.stop zkswe"],
        ["-s", serial, "shell", "setprop ctl.start zkswe"],
        ["-s", serial, "shell", "rm -f /tmp/ghostdeck-*"],
    ):
        _adb_mutate(argv)
    # Informational only: an empty /tmp/ghostdeck makes `ls` exit 1, which is the success case, and a
    # listing that times out is equally unable to change the outcome.
    try:
        listing = adb.run(
            ["-s", serial, "shell", "ls /tmp/ghostdeck*"],
            capture_output=True,
            text=True,
            timeout=_ADB_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return
    if listing.returncode == 0 and listing.stdout:
        print(listing.stdout, end="")


def _session_released(timeout: float = _SESSION_RELEASE_TIMEOUT) -> bool:
    """True once the media session has released, so the stock UI can safely be restarted.

    `stop()` sends SIGTERM and then bounces the deck's stock UI. The bounce tears down a live media
    session, so it must not happen until the player has actually let go. Measured on the attached
    deck: the record sits at `state=3 cleanup=pending` for ~2s after SIGTERM and reaches
    `state=9 cleanup=proven` at ~t+3s.

    Reads the published session record rather than asking the bridge, because the record is already
    the contract `_cleanup_device()` and the CLI report from, and it needs no bridge round trip.

    When there is **no record at all** there is no session to wait for, so this returns True
    immediately. That is not just an optimisation: a stop with nothing playing (or any run against a
    fake adb, which never publishes a record) must not block for the whole timeout.

    Returns False only when a record exists and never proofs a release within the bound. The caller
    then leaves the stock UI alone: a deck still holding an ADB session with its UI running is a far
    smaller failure than a transport cut mid-stream.
    """
    if not _HOST_STATE.is_file():
        return True
    deadline = time.monotonic() + timeout
    while True:
        released = True
        try:
            record = json.loads(_HOST_STATE.read_text())
            status = (record.get("video") or {}).get("status") or {}
            # A record that names no session has nothing to wait for either.
            released = status.get("cleanup") == "proven" or not status
        except (OSError, json.JSONDecodeError):
            released = True
        if released:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_SESSION_RELEASE_POLL)


def _await_transport_recovery(timeout: float = _TRANSPORT_RECOVERY_TIMEOUT) -> bool:
    """Wait until the deck can serve a command again after the stock-UI bounce.

    Bounded, best-effort, and never fatal: if the transport does not come back, `stop` has still done
    its job (the UI was restarted) and the deck's own state is the thing to report. A timeout here
    must not turn a successful stop into a failure, which is why this returns a bool nobody raises on.

    **It waits for the transport to be STABLE, not merely to answer once.** The stock UI comes back up
    before the gadget settles, and the deck goes away again a few seconds later. Measured after `stop`
    returned, polling once a second:

        t+0.1s  getprop rc=0   /proc/modules rc=0     <-- a single probe would succeed here
        t+2.2s  getprop rc=0   /proc/modules rc=0
        t+3.3s  getprop rc=1   /proc/modules rc=1     <-- the deck dips
        t+4.3s  getprop rc=0   /proc/modules rc=0     <-- and settles here

    Returning at t+0.1s (which the first-success version did) let a new session open into that dip and
    die with `CLEANUP_FAILED / cleanup: unproven`. Requiring several consecutive successes past the dip
    is what makes `stop` returning mean "the deck is usable".
    """
    serial = _deck_serial()
    if not serial:
        return False
    deadline = time.monotonic() + timeout
    consecutive = 0
    while time.monotonic() < deadline:
        try:
            result = adb.run(
                ["-s", serial, "shell", "getprop sys.usb.config"],
                capture_output=True, text=True, timeout=_ADB_TIMEOUT,
            )
            consecutive = consecutive + 1 if result.returncode == 0 else 0
        except (subprocess.SubprocessError, OSError):
            consecutive = 0
        if consecutive >= _TRANSPORT_STABLE_SAMPLES:
            return True
        time.sleep(_TRANSPORT_RECOVERY_POLL)
    return False


def _usb_mode() -> str | None:
    """The USB layer's current gadget mode, or None when it cannot answer.

    `usb.detect()` matches VID/PID on the bus (18d1:d002 ADB, 2207:0019 HID). A
    missing/broken backend is not a hardware verdict (A-102), so this returns
    None rather than inventing `adb`/`hid`. Callers that cannot see the bus
    skip the HID-return proof instead of hanging or blaming the deck.
    """
    try:
        found = usb.detect()
    except Exception:
        return None
    if not found:
        return None
    mode = found.get("mode")
    if mode in (None, "none"):
        return None
    return str(mode)


def _await_hid_return(timeout: float = _HID_RETURN_TIMEOUT, *, require_hid: bool = False) -> bool:
    """True once `usb.detect()` reports a *stable* HID, proving the gadget left ADB.

    The bounce (`ctl.stop`/`ctl.start zkswe`) is what re-initialises the gadget;
    this only reads VID/PID. It does not write USB functions.

    Immediate True when the USB layer never reports ADB (`none`, missing backend,
    already HID), unless `require_hid` is set.

    After a real session (`require_hid=True`) a single HID sample is not H1:
    `stop` exited 0 in 7.2s on a hid blip, then detect was `none` and then `adb`.
    Require `_HID_STABLE_SAMPLES` consecutive HID reads. `none` after ADB is the
    re-enumeration dip (t+3.7s none, t+4.2s hid), not "USB cannot answer".
    """
    mode = _usb_mode()
    if not require_hid and (mode is None or mode == "hid"):
        return True
    deadline = time.monotonic() + timeout
    consecutive = 0
    while True:
        mode = _usb_mode()
        consecutive = consecutive + 1 if mode == "hid" else 0
        if consecutive >= _HID_STABLE_SAMPLES:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_HID_RETURN_POLL)


def _hid_stuck_message() -> str:
    return (
        "the stock UI was restarted but the USB gadget is still in ADB "
        "(usb.detect() mode=adb, VID/PID 18d1:d002 not 2207:0019) after "
        f"{_HID_RETURN_TIMEOUT:.0f}s; the bounce did not return the deck to HID. "
        "Replug or power-cycle the deck, then re-run `ghostdeck stop`"
    )


def stop() -> None:
    """Stop our player, then restore the deck.

    The player's identity decides the exit code only, never whether the deck is restored: an
    unverifiable pid is left exactly as it is (nothing signalled, nothing erased) and the cleanup
    still runs *when Studio is not holding the gadget*, because skipping it would leave the stock
    UI stopped with no other command able to restore them.

    The parent `launch-studio-adb.py --play` path only stops the loop player. It never restarts
    `zkswe` while the copied Studio and the local bridge are up. Ghostdeck `stop` used to bounce
    anyway, which is what made a working parent-style session look broken: the bounce drops HID,
    transportRevive yanks ADB back, and the keys go with the gadget.
    """
    identity_error = None
    record_is_disposable = True
    session_was_playing = gdstate.load().get("play_pid") is not None
    keep_gadget = studio._socket_live()
    try:
        _kill_play(keep_record=True)
    except RuntimeError as error:
        identity_error = error
        record_is_disposable = False
    # The stock-UI restart in `_cleanup_device()` tears down a live media session, so wait for the
    # player to release first (see `_session_released`). Only a session that was actually playing can
    # be holding the transport, so a stop with nothing playing skips the wait entirely.
    if session_was_playing and not _session_released():
        if identity_error is not None:
            raise RuntimeError(
                f"the media session did not release within {_SESSION_RELEASE_TIMEOUT:.0f}s, so the "
                f"stock UI was left alone rather than cut the transport mid-stream; "
                f"re-run `ghostdeck stop` (as well as: {identity_error})"
            )
        raise RuntimeError(
            f"the media session did not release within {_SESSION_RELEASE_TIMEOUT:.0f}s, so the stock "
            f"UI was left alone rather than cut the transport mid-stream; re-run `ghostdeck stop`"
        )
    if keep_gadget:
        try:
            if not studio.running():
                studio.launch()
        except Exception as error:
            print(f"hidshim Studio copy skipped: {error}", file=sys.stderr)
    else:
        try:
            _cleanup_device()
        except RuntimeError as cleanup_error:
            if identity_error is None:
                raise
            raise RuntimeError(f"{cleanup_error} (as well as: {identity_error})") from cleanup_error
        if session_was_playing and not _await_hid_return(require_hid=True):
            hid_error = RuntimeError(_hid_stuck_message())
            if identity_error is not None:
                raise RuntimeError(
                    f"{hid_error} (as well as: {identity_error})"
                ) from hid_error
            raise hid_error
    if record_is_disposable:
        _clear_play_records()
    if identity_error is not None:
        raise identity_error

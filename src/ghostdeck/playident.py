"""Player identity, deck identification, and restoring the stock UI.

Split out of `play.py` so the lifecycle (`start_play` / `stop`) is not mixed with the pid
sidecar, the vendor-player scan, or the zkswe bounce. Names that tests patch live on
`ghostdeck.play`; this module reads them from there at call time so a split cannot silently
break a monkeypatch.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ghostdeck import adb, state as gdstate, tree, usb


class _PlayNS:
    def __getattr__(self, name):
        import ghostdeck.play as play
        return getattr(play, name)


P = _PlayNS()

VENDOR_PLAY = tree.candidate_root() / "vendor" / "d200-color-play.py"
VENDOR_DIR = tree.candidate_root() / "vendor"
_PS_TIMEOUT = 5.0
# A wedged adb server must not turn `stop` - the one documented recovery command - into a hang
# (A-116). Every device call is bounded; the informational listing's timeout is tolerated exactly
# like its non-zero exit code already is.
_ADB_TIMEOUT = 30.0
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



def _adb_entries() -> list[tuple[str, str, list[str]]]:
    """Every device `adb devices -l` lists, as ``(serial, state, remaining fields)``, in adb's order.

    The transport state is kept rather than filtered on, because the caller has to tell a usable
    device from one that is attached but not answering (T16): both are listed by `adb`, they are
    different user-visible problems, and their remedies are different. The per-line fields are kept
    because the line's shape is firmware-dependent: the attached deck emits only
    ``usb:<...> transport_id:<n>`` (T15), while other builds report product/model/device.
    """
    try:
        result = adb.run(["devices", "-l"], capture_output=True, text=True, timeout=P._ADB_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"adb timed out after {P._ADB_TIMEOUT:g}s: devices -l (is the adb server wedged?)"
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
            ["-s", serial, "shell", *P._DECK_PROBE],
            capture_output=True,
            text=True,
            timeout=P._ADB_TIMEOUT,
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
       Only candidates adb calls `device` are probed, at most `P._PROBE_LIMIT`, and it is a read, so an
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
        if any(field in P._DECK_FIELDS for field in fields):
            return serial, state, []
    # Signal 3: ask each usable candidate about its own deck-only node. Nothing here falls back to
    # "the first attached device" - a candidate is accepted only when it positively answers, so a
    # phone attached alongside (whatever the order of the lines) is probed and then rejected.
    probed = 0
    for serial, state, _ in entries:
        if probed >= P._PROBE_LIMIT:
            break
        if state != P.TRANSPORT_READY:
            continue
        probed += 1
        if _probe_is_deck(serial):
            # Name the signal in the diagnosis: before T17 this path did not exist, and a future
            # "which signal saw it?" must not be another mystery.
            print(f"deck identified by the zkswe sysfs probe: {serial}", file=sys.stderr)
            return serial, state, []
    # adb-only, so this works with the extras absent too: the T16 distinction between "attached but
    # not answering" and "absent" must not depend on the USB layer that just failed to answer.
    blocked = [(serial, state) for serial, state, _ in entries if state != P.TRANSPORT_READY]
    return None, "", blocked


def _deck_serial() -> str | None:
    """The serial of a *usable* attached D200, or None when nothing positively identifies one.

    Delegates the identification and the attached-but-unusable distinction to `deck_transport()`; the
    `state` test below is the whole difference, and it is what keeps a wedged deck from being handed
    to a caller that is about to send it a command.
    """
    serial, state, _ = P.deck_transport()
    if serial is None or state != P.TRANSPORT_READY:
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
            timeout=P._PS_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, f"ps timed out after {P._PS_TIMEOUT:g}s"
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
    lstart, reason = P._probe_start_time(pid)
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
        lstart, reason = P._probe_start_time(pid)
        if reason is not None:
            return None, reason
        if lstart is None:
            return False, ""  # ps ran and reports no such process
        return None, "no recorded identity"
    if recorded["pid"] != pid:
        return False, ""
    lstart, reason = P._probe_start_time(pid)
    if reason is not None:
        return None, reason
    if lstart is None:
        return False, ""  # ps ran and reports no such process
    return lstart == recorded["lstart"], ""


def _is_our_player(pid):
    """True/False when identity is determinable, None when it is not. See `_player_identity`."""
    return _player_identity(pid)[0]



def is_vendor_player_argv(tokens: list[str]) -> bool:
    """True only for `python -u <vendor/d200-color-play.py> ...`, not a prompt that mentions the path."""
    marker = str(P.VENDOR_PLAY)
    for i, tok in enumerate(tokens):
        if tok == marker:
            return i > 0 and tokens[i - 1] == "-u"
    return False


def _vendor_player_pids() -> list[int]:
    env = dict(os.environ, LC_ALL="C")
    try:
        result = subprocess.run(
            ["ps", "-axww", "-o", "pid=,command="],
            capture_output=True,
            text=True,
            env=env,
            timeout=P._PS_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    pids: list[int] = []
    for line in (result.stdout or "").splitlines():
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if is_vendor_player_argv(parts[1].split()):
            pids.append(pid)
    return pids


def _signal_vendor_players() -> None:
    for pid in _vendor_player_pids():
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.time() + 1.0
    while time.time() < deadline:
        if not _vendor_player_pids():
            return
        time.sleep(0.1)
    for pid in _vendor_player_pids():
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

def _signal_host_stated_player() -> None:
    """SIGTERM the pid published in host json if it is the vendor player.

    A stale state.json pid must not leave that process holding the deck.
    The vendor script path is matched as a whole argv token, not a substring.
    """
    try:
        payload = json.loads(P._HOST_STATE.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return
    pid = payload.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return
    env = dict(os.environ, LC_ALL="C")
    try:
        result = subprocess.run(
            ["ps", "-o", "command=", "-ww", "-p", str(pid)],
            capture_output=True,
            text=True,
            env=env,
            timeout=P._PS_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return
    tokens = (result.stdout or "").split()
    if str(P.VENDOR_PLAY) not in tokens:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass

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
        _signal_vendor_players()
        return
    identity, reason = _player_identity(pid)
    if identity is None:
        raise RuntimeError(f"cannot verify player pid {pid}: {reason}; not signalling")
    if identity:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    _signal_vendor_players()
    # Determinable either way, so the record is no longer meaningful. It is cleared here unless the
    # caller still needs it to outlive its own later steps.
    if not keep_record:
        P._clear_play_records()

def _adb_mutate(argv: list[str]) -> None:
    """Run one device-mutating command, bounded by `_ADB_TIMEOUT` (A-116).

    `subprocess.run` without a timeout blocks indefinitely, so a wedged adb server made `stop` - the
    one documented recovery command - hang with the deck still hijacked. A timeout is reported in
    the same shape as a non-zero exit, because the step did not happen either way.
    """
    try:
        result = adb.run(argv, capture_output=True, text=True, timeout=P._ADB_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"adb timed out after {P._ADB_TIMEOUT:g}s: {' '.join(argv)} (is the adb server wedged?)"
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
    serial, state, blocked = P.deck_transport()
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
    if state != P.TRANSPORT_READY:
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
        P._adb_mutate(argv)
    # Informational only: an empty /tmp/ghostdeck makes `ls` exit 1, which is the success case, and a
    # listing that times out is equally unable to change the outcome.
    try:
        listing = adb.run(
            ["-s", serial, "shell", "ls /tmp/ghostdeck*"],
            capture_output=True,
            text=True,
            timeout=P._ADB_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return
    if listing.returncode == 0 and listing.stdout:
        print(listing.stdout, end="")


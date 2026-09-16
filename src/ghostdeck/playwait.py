"""Bounded waits after `stop`: session release, transport recovery, HID return.

Split out of `play.py` so the lifecycle is not mixed with the measured settle times. Names
that tests patch live on `ghostdeck.play`; this module reads them from there at call time.
"""
from __future__ import annotations

import json
import subprocess
import time

from ghostdeck import adb, usb


class _PlayNS:
    def __getattr__(self, name):
        import ghostdeck.play as play
        return getattr(play, name)


P = _PlayNS()

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



def _record_owner_alive(record: dict) -> bool:
    """True while the process that published `record` is still running.

    Tri-state would be better, but the caller only needs the one direction that is provable: a pid
    `ps` cannot find is gone. An unreadable `ps` answers True so an unanswerable probe never looks
    like a release -- the wait is what keeps `stop` from cutting a live session mid-stream.
    """
    pid = record.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return True
    start, unknown = P._probe_start_time(pid)
    if unknown is not None:
        return True
    return start is not None


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

    A record whose owner is gone counts as released. This is the case that used to hang: a player
    killed before it could write `cleanup=proven` leaves the record at `phase=active`
    `cleanup=pending` forever, so waiting on the proof alone made `stop` fail on every retry (measured:
    two consecutive `stop` runs both exited 1 with "did not release within 8s", while the recorded pid
    23530 was already dead). A dead process cannot still be holding the transport, which is the whole
    thing this wait protects.

    Returns False only when a record exists, its owner is still alive, and it never proofs a release
    within the bound. The caller then leaves the stock UI alone: a deck still holding an ADB session
    with its UI running is a far smaller failure than a transport cut mid-stream.
    """
    if not P._HOST_STATE.is_file():
        return True
    deadline = time.monotonic() + timeout
    while True:
        released = True
        try:
            record = json.loads(P._HOST_STATE.read_text())
            status = (record.get("video") or {}).get("status") or {}
            # A record that names no session has nothing to wait for either.
            released = (
                status.get("cleanup") == "proven"
                or not status
                or not P._record_owner_alive(record)
            )
        except (OSError, json.JSONDecodeError):
            released = True
        if released:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(P._SESSION_RELEASE_POLL)


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
    serial = P._deck_serial()
    if not serial:
        return False
    deadline = time.monotonic() + timeout
    consecutive = 0
    while time.monotonic() < deadline:
        try:
            result = adb.run(
                ["-s", serial, "shell", "getprop sys.usb.config"],
                capture_output=True, text=True, timeout=P._ADB_TIMEOUT,
            )
            consecutive = consecutive + 1 if result.returncode == 0 else 0
        except (subprocess.SubprocessError, OSError):
            consecutive = 0
        if consecutive >= P._TRANSPORT_STABLE_SAMPLES:
            return True
        time.sleep(P._TRANSPORT_RECOVERY_POLL)
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
    Require `P._HID_STABLE_SAMPLES` consecutive HID reads. `none` after ADB is the
    re-enumeration dip (t+3.7s none, t+4.2s hid), not "USB cannot answer".
    """
    mode = P._usb_mode()
    if not require_hid and (mode is None or mode == "hid"):
        return True
    deadline = time.monotonic() + timeout
    consecutive = 0
    while True:
        mode = P._usb_mode()
        consecutive = consecutive + 1 if mode == "hid" else 0
        if consecutive >= P._HID_STABLE_SAMPLES:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(P._HID_RETURN_POLL)


def _hid_stuck_message() -> str:
    return (
        "the stock UI was restarted but the USB gadget is still in ADB "
        "(usb.detect() mode=adb, VID/PID 18d1:d002 not 2207:0019) after "
        f"{P._HID_RETURN_TIMEOUT:.0f}s; the bounce did not return the deck to HID. "
        "Replug or power-cycle the deck, then re-run `ghostdeck stop`"
    )

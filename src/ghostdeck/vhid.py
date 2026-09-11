"""Experimental userspace virtual HID. Not DriverKit; does not touch Studio."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ghostdeck import HID_PID, HID_VID
from ghostdeck import state as gdstate
from ghostdeck import usb

_HOLD = []
_PS_TIMEOUT = 5.0
_DESCRIPTOR = bytes(
    [
        0x06, 0x00, 0xFF,
        0x09, 0x01,
        0xA1, 0x01,
        0x15, 0x00,
        0x26, 0xFF, 0x00,
        0x75, 0x08,
        0x95, 0x40,
        0x09, 0x01,
        0x81, 0x02,
        0x09, 0x01,
        0x91, 0x02,
        0xC0,
    ]
)


# --- keeper identity (finding A-125) ---------------------------------------
# `quit()` used to believe `vhid_pid` on `pid_alive()` alone, so any recycled pid was SIGTERMed and
# then SIGKILLed, and `ghostdeck quit` exited 0 as if it had cleaned up its own keeper. `play.py`
# already carries the fix for the same defect class (A-003); this is the vhid half, deliberately the
# same shape so the two lanes do not drift: the pid is recorded next to its `ps -o lstart=` start
# time, and the record is ours only while the live start time still matches.
#
# A sidecar (`~/.ghostdeck/vhid.pid`, play.py's `{"pid", "lstart"}` format) was chosen over a
# `vhid_lstart` field in state.json: state.json is a shared document that several lanes load and save,
# and a field the writer writes while a loader drops it is invisible (the A-001 lesson). The sidecar
# is one 0600 file this module owns.
#
# The probe is duplicated here rather than imported from play.py because play.py imports this module
# (`from ghostdeck import ... vhid`), so importing back would be circular -- and because the TZ pin
# below is load-bearing. If it ever moves to a shared home, both callers must keep BOTH pins.


def _identity_path() -> Path:
    """0600 sidecar recording which pid is our keeper, resolved at call time."""
    return gdstate.HOME / "vhid.pid"


def _write_identity(payload: dict) -> None:
    """Atomically write the sidecar: mkstemp + fchmod + os.replace, as state.save does."""
    target = _identity_path()
    target.parent.mkdir(mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".vhid.pid.")
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
    """Read a live process's start time, keeping "ps could not answer" distinct from "no such pid".

    Returns ``(lstart, unknown_reason)``:

    * ``(text, None)``  - ps ran; the process is live and this is its start time.
    * ``(None, None)``  - ps ran; there is no such process (dead, or recycled away).
    * ``(None, reason)`` - ps itself could not answer, so identity is undeterminable.

    ``LC_ALL=C`` **and** ``TZ=UTC`` are forced, and the zone is not optional: on this host
    ``ps -o lstart=`` renders in the caller's locale *and* timezone, so pinning the locale alone let a
    record written under one zone compare unequal to a reading under another, which is exactly the
    A-124 regression (it made `stop()` classify a live player as a stranger). ``-ww`` prevents
    truncation so a long command line cannot shorten the string.
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
    """Record the pid and its start time, so a later run can tell it from a recycled pid."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return
    lstart, _reason = _probe_start_time(pid)
    if lstart is None:
        # Nothing trustworthy to record: identity stays undeterminable rather than a guess.
        return
    _write_identity({"pid": pid, "lstart": lstart})


def _keeper_identity(pid):
    """Tri-state identity: ``(True | False | None, reason)``. Same contract as play._player_identity.

    * True - the sidecar records this pid and the live start time still matches.
    * False - identity is determinable and this is not our keeper: the sidecar records a different
      pid, the live start time does not match, or ps reports no such process. Safe to discard.
    * None - identity cannot be determined. Two sub-cases, both deliberately conservative:
        - ps itself could not answer, so nothing can be classified; or
        - ps answered and the process is live, but no identity was ever recorded for it (a state file
          written by an older revision, or a lost sidecar). Signalling would risk killing a stranger,
          and erasing would lose the only handle on a real keeper, so nothing is touched.
      Callers must not treat None as "not ours".
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
        return None, "no recorded identity for this pid"
    if recorded["pid"] != pid:
        return False, ""
    lstart, reason = _probe_start_time(pid)
    if reason is not None:
        return None, reason
    if lstart is None:
        return False, ""  # ps ran and reports no such process
    return lstart == recorded["lstart"], ""


def _recorded_pid(data: dict):
    """(pid, iohid) as persisted, matching what `status()` and `quit()` used to read inline."""
    record = data.get("vhid") if isinstance(data.get("vhid"), dict) else {}
    return record.get("pid", data.get("vhid_pid")), bool(
        record.get("iohid", data.get("vhid_iohid", False))
    )


def _down_record(pid=None, iohid: bool = False, **extra) -> dict:
    record = {
        "pid": pid,
        "experimental": True,
        "iohid": iohid,
        "visible": False,
        "status": "down",
        "release_gate": "blocked",
    }
    record.update(extra)
    return record


def start() -> dict:
    current = status()
    if current["status"] == "up" and gdstate.pid_alive(current.get("pid")):
        return current
    # `status()` only reports up for an identity it can prove, so reaching here with a live pid means
    # that pid is a stranger or unverifiable. It is deliberately left untouched (never signalled), and
    # the user is told rather than left with a silently orphaned process.
    stale = gdstate.load().get("vhid_pid")
    if stale is not None and gdstate.pid_alive(stale):
        identity, reason = _keeper_identity(stale)
        if identity is not True:
            print(
                f"warning: virtual HID pid {stale} is not verifiably ours "
                f"({reason or 'the recorded start time does not match'}); starting a new keeper and "
                "leaving that process alone",
                file=sys.stderr,
            )
    src = str(Path(__file__).resolve().parent.parent)
    env = os.environ.copy()
    previous = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = src if not previous else src + os.pathsep + previous
    proc = subprocess.Popen(
        [sys.executable, "-m", "ghostdeck.vhid"],
        env=env,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    _write_vhid(proc.pid, visible=False, iohid=False, status="up")
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            _write_vhid(None, visible=False, iohid=False, status="down")
            raise RuntimeError("virtual HID process exited")
        record = status()
        if record.get("pid") == proc.pid and record.get("status") == "up":
            if record.get("visible") or time.monotonic() + 0.15 >= deadline:
                return record
        time.sleep(0.05)
    return status()


def status() -> dict:
    from ghostdeck import usb

    data = gdstate.load()
    pid, iohid = _recorded_pid(data)
    identity, reason = _keeper_identity(pid)
    if identity is None:
        # The pid is live but nothing proves it is our keeper (no recorded identity, or ps could not
        # answer). Reporting up would adopt a stranger as our keeper -- the A-125 defect -- and
        # erasing the record would drop the only handle on a process whose fate is unknown, so the
        # record is preserved and the uncertainty is reported instead.
        return _down_record(pid, iohid, unverified=reason)
    if identity is False:
        record = data.get("vhid") if isinstance(data.get("vhid"), dict) else {}
        if pid is not None or record.get("status") == "up":
            # Determinable and not ours (or already dead): the record is stale, so clear it.
            return _write_vhid(None, visible=False, iohid=False, status="down")
        return _down_record()
    visible = bool(usb.virtual_hid_enumerated())
    return {
        "pid": pid,
        "experimental": True,
        "iohid": iohid,
        "visible": visible,
        "status": "up",
        "release_gate": "open" if visible else "blocked",
    }


def is_up() -> bool:
    record = status()
    return record.get("status") == "up" and gdstate.pid_alive(record.get("pid"))


def quit() -> dict:
    """Stop our own keeper. Raises when its fate cannot be determined; nothing is then erased."""
    data = gdstate.load()
    pid, _iohid = _recorded_pid(data)
    identity, reason = _keeper_identity(pid)
    if identity is None:
        raise RuntimeError(
            f"cannot verify virtual HID pid {pid}: {reason}; not signalling and leaving the record "
            "in place"
        )
    if identity:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and gdstate.pid_alive(pid):
            time.sleep(0.05)
        if gdstate.pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and gdstate.pid_alive(pid):
                time.sleep(0.05)
        gdstate.reap(pid)
    # Determinable either way, so the record is no longer meaningful: clear it with the pid.
    return _write_vhid(None, visible=False, iohid=False, status="down")


def serve() -> None:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    device = None
    iohid_mod = None
    try:
        from ghostdeck import iohid as iohid_mod
        device = iohid_mod.create()
    except Exception:
        iohid_mod = None
        device = None
    if device is None:
        device = _try_iohid_user_device()
    if device is not None:
        _HOLD.append(device)
    _write_vhid(os.getpid(), visible=False, iohid=device is not None, status="up")
    while True:
        if device is not None and iohid_mod is not None:
            try:
                iohid_mod.pump(1.0)
            except Exception:
                time.sleep(1.0)
        else:
            time.sleep(1.0)


def _write_vhid(pid, *, visible: bool, iohid: bool = False, status: str) -> dict:
    data = gdstate.load()
    data["vhid_pid"] = pid
    data["vhid_experimental"] = True
    data["vhid_iohid"] = iohid
    data["vhid_visible"] = visible
    data["vhid_vid"] = HID_VID
    data["vhid_pid_usb"] = HID_PID
    data["vhid"] = {
        "pid": pid,
        "experimental": True,
        "iohid": iohid,
        "visible": visible,
        "status": status,
    }
    gdstate.save(data)
    if status == "up" and pid is not None:
        _record_identity(pid)
    else:
        _remove_identity()
    return {
        "pid": pid,
        "experimental": True,
        "iohid": iohid,
        "visible": visible,
        "status": status,
        "release_gate": "open" if visible else "blocked",
    }


def _stop(_signum, _frame) -> None:
    raise SystemExit(0)


def _try_iohid_user_device():
    try:
        import objc
    except ImportError:
        return None
    try:
        from Foundation import NSData, NSDictionary, NSNumber
    except ImportError:
        return None
    try:
        from IOKit.hid import IOHIDUserDeviceCreate
    except ImportError:
        IOHIDUserDeviceCreate = _load_iohid_create(objc)
    if IOHIDUserDeviceCreate is None:
        return None
    try:
        descriptor = NSData.dataWithBytes_length_(_DESCRIPTOR, len(_DESCRIPTOR))
        props = NSDictionary.dictionaryWithDictionary_(
            {
                "VendorID": NSNumber.numberWithUnsignedInt_(HID_VID),
                "ProductID": NSNumber.numberWithUnsignedInt_(HID_PID),
                "Product": "ulanzi",
                "ReportDescriptor": descriptor,
            }
        )
        return IOHIDUserDeviceCreate(None, props)
    except Exception:
        return None


def _load_iohid_create(objc):
    loaded = {}
    try:
        objc.loadBundle(
            "IOKit",
            loaded,
            bundle_path="/System/Library/Frameworks/IOKit.framework",
        )
    except Exception:
        return None
    return loaded.get("IOHIDUserDeviceCreate")


if __name__ == "__main__":
    serve()

"""Experimental userspace virtual HID. Not DriverKit; does not touch Studio."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from ghostdeck import HID_PID, HID_VID
from ghostdeck import state as gdstate
from ghostdeck import usb

_HOLD = []
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


def start() -> dict:
    current = status()
    if current["status"] == "up" and gdstate.pid_alive(current.get("pid")):
        return current
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
    record = data.get("vhid") if isinstance(data.get("vhid"), dict) else {}
    pid = record.get("pid", data.get("vhid_pid"))
    iohid = bool(record.get("iohid", data.get("vhid_iohid", False)))
    if not gdstate.pid_alive(pid):
        if pid is not None or record.get("status") == "up":
            return _write_vhid(None, visible=False, iohid=False, status="down")
        return {
            "pid": None,
            "experimental": True,
            "iohid": False,
            "visible": False,
            "status": "down",
            "release_gate": "blocked",
        }
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
    record = status()
    pid = record.get("pid")
    if gdstate.pid_alive(pid):
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and gdstate.pid_alive(pid):
            time.sleep(0.05)
        if gdstate.pid_alive(pid):
            os.kill(pid, signal.SIGKILL)
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and gdstate.pid_alive(pid):
                time.sleep(0.05)
    gdstate.reap(pid)
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

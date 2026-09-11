"""USB detect and HID 0x00ff ADB switch. Serials come from the bus only."""

from __future__ import annotations

import time

from ghostdeck import ADB_PID, ADB_VID, HID_PID, HID_VID

HID_SWITCH = 0x00FF
HID_REPORT_SIZE = 1025


def detect() -> dict:
    adb_hit = _adb_device()
    if adb_hit is not None:
        return adb_hit
    hid_hit = _hid_device()
    if hid_hit is not None:
        return hid_hit
    return {"serial": None, "vid": None, "pid": None, "mode": "none"}


def virtual_hid_enumerated() -> bool:
    """True only if 2207:0019 is on the bus while the physical deck is ADB."""
    return _adb_device() is not None and _hid_present()


def enable_adb(*, timeout: float = 15.0) -> dict:
    """If the deck is HID iface 0, write the 1025-byte || + 0x00ff report."""
    current = detect()
    if current["mode"] == "adb":
        return current
    info = _hid_iface0(timeout=timeout)
    if info is None:
        raise RuntimeError("D200 HID interface 0 not found")
    packet = bytearray(HID_REPORT_SIZE)
    packet[1:3] = b"||"
    packet[3:5] = HID_SWITCH.to_bytes(2, "big")
    try:
        import hid
    except ImportError as error:
        raise RuntimeError("hidapi is required to switch HID to ADB") from error
    device = hid.device()
    path = info["path"]
    try:
        device.open_path(path)
        written = device.write(packet)
        if written != len(packet):
            raise RuntimeError(f"short HID-to-ADB write: {written}")
    finally:
        device.close()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = detect()
        if found["mode"] == "adb":
            return found
        time.sleep(0.25)
    raise RuntimeError("D200 did not enumerate through ADB")


switch_to_adb = enable_adb
switch_hid_to_adb = enable_adb
send_00ff = enable_adb


def _hid_device() -> dict | None:
    serial = _hid_serial()
    if serial is not None or _hid_present():
        return {
            "serial": serial,
            "vid": HID_VID,
            "pid": HID_PID,
            "mode": "hid",
        }
    dev = _usb_find(HID_VID, HID_PID)
    if dev is None:
        return None
    return {
        "serial": _usb_serial(dev),
        "vid": HID_VID,
        "pid": HID_PID,
        "mode": "hid",
    }


def _adb_device() -> dict | None:
    dev = _usb_find(ADB_VID, ADB_PID)
    if dev is None:
        return None
    return {
        "serial": _usb_serial(dev),
        "vid": ADB_VID,
        "pid": ADB_PID,
        "mode": "adb",
    }


def _hid_present() -> bool:
    try:
        import hid
    except ImportError:
        return False
    try:
        return bool(hid.enumerate(HID_VID, HID_PID))
    except Exception:
        return False


def _hid_serial() -> str | None:
    try:
        import hid
    except ImportError:
        return None
    try:
        entries = hid.enumerate(HID_VID, HID_PID)
    except Exception:
        return None
    for entry in entries:
        serial = entry.get("serial_number") or None
        if isinstance(serial, bytes):
            serial = serial.decode("utf-8", "replace") or None
        if serial:
            return str(serial).strip() or None
    return None


def _hid_iface0(*, timeout: float):
    try:
        import hid
    except ImportError:
        return None
    deadline = time.monotonic() + max(timeout, 0)
    while True:
        try:
            entries = hid.enumerate(HID_VID, HID_PID)
        except Exception:
            entries = []
        for entry in entries:
            if entry.get("interface_number") != 0:
                continue
            path = entry.get("path")
            if path is None:
                continue
            if isinstance(path, str):
                path = path.encode()
            result = dict(entry)
            result["path"] = path
            return result
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.25)


def _usb_find(vid: int, pid: int):
    try:
        import usb.core
    except ImportError:
        return None
    try:
        return usb.core.find(idVendor=vid, idProduct=pid)
    except Exception:
        return None


def _usb_serial(dev) -> str | None:
    try:
        value = dev.serial_number
    except Exception:
        value = None
    if not value:
        try:
            import usb.util

            value = usb.util.get_string(dev, dev.iSerialNumber)
        except Exception:
            value = None
    if not value:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    text = str(value).strip()
    return text or None

"""USB detect and HID 0x00ff ADB switch. Serials come from the bus only."""

from __future__ import annotations

import importlib.util
import time

from ghostdeck import ADB_PID, ADB_VID, HID_PID, HID_VID

HID_SWITCH = 0x00FF
HID_REPORT_SIZE = 1025

HID_INSTALL_HINT = "hidapi is not installed (pip install hidapi)"
USB_INSTALL_HINT = "pyusb is not installed (pip install pyusb)"


def _importable(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def missing_dependency() -> str | None:
    """Name the first unusable optional backend, or None when every backend imports.

    `hidapi` is the primary backend for the D200 control interface; `pyusb` is only
    used to read the descriptor serial when `hidapi` is unavailable.
    """
    if not _importable("hid"):
        return HID_INSTALL_HINT
    if not _importable("usb"):
        return USB_INSTALL_HINT
    return None


class MissingDependency(RuntimeError):
    """An optional backend package is not importable, so no hardware conclusion is possible."""


def _hid_module():
    try:
        import hid
    except ImportError as error:
        raise MissingDependency(HID_INSTALL_HINT) from error
    return hid


def detect() -> dict:
    adb_hit = _adb_device()
    if adb_hit is not None:
        return adb_hit
    hid_hit = _hid_device()
    if hid_hit is not None:
        return hid_hit
    result = {"serial": None, "vid": None, "pid": None, "mode": "none"}
    hint = missing_dependency()
    if hint is not None:
        # A missing backend is not a hardware verdict; the caller must not blame the deck.
        result["dependency"] = hint
    return result


def virtual_hid_enumerated() -> bool:
    """True only if 2207:0019 is on the bus while the physical deck is ADB."""
    return bool(_adb_device() is not None and _hid_present())


def enable_adb(*, timeout: float = 15.0) -> dict:
    """If the deck is HID iface 0, write the 1025-byte || + 0x00ff report."""
    current = detect()
    if current["mode"] == "adb":
        return current
    info = _hid_iface0(timeout=timeout)
    if info is None:
        hint = missing_dependency()
        if hint is not None:
            # Never report a missing package as a missing deck.
            raise MissingDependency(hint)
        raise RuntimeError("D200 HID interface 0 not found")
    packet = bytearray(HID_REPORT_SIZE)
    packet[1:3] = b"||"
    packet[3:5] = HID_SWITCH.to_bytes(2, "big")
    hid = _hid_module()
    device = hid.device()
    path = info["path"]
    written = None
    try:
        device.open_path(path)
        written = device.write(packet)
    finally:
        device.close()
    # ADB-mode enumeration can only be observed through pyusb (`detect` -> `_adb_device` ->
    # `_usb_find`, which returns None without it), so with pyusb missing the poll below can never
    # succeed and its timeout would be reported as "the deck did not switch" -- a hardware verdict
    # for what is really a missing package (A-108). Same rule as the `info is None` branch above:
    # never report a missing package as a missing deck. Checked before the poll so the user is not
    # made to wait out the timeout for the wrong answer.
    hint = missing_dependency()
    if hint is not None:
        raise MissingDependency(hint)
    # The deck detaches from HID as the switch report lands, so hidapi usually reports a
    # short or -1 write for a switch that worked. Only a deck that never appears through
    # ADB is an error, and then the short write is the more specific one to report.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = detect()
        if found["mode"] == "adb":
            return found
        time.sleep(0.25)
    if written is not None and written != len(packet):
        raise RuntimeError(f"short HID-to-ADB write: {written}")
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


def _hid_present() -> bool | None:
    """True/False for the bus, None when the backend itself is unusable."""
    try:
        hid = _hid_module()
    except MissingDependency:
        return None
    try:
        return bool(hid.enumerate(HID_VID, HID_PID))
    except Exception:
        return False


def _hid_serial() -> str | None:
    try:
        hid = _hid_module()
    except MissingDependency:
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
    hid = _hid_module()
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
    if not _importable("usb"):
        return None
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

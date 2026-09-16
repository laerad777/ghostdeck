"""ghostdeck host package.

USB identities live here and nowhere else. They are the values the whole host side matches on --
`usb.detect()`, the shim's `hid_enumerate`, the bridge's device enumeration -- so a second definition
is a second thing to update when a deck revision appears, and the two drift silently. The bridge is
run as a child process with this package on `PYTHONPATH`, so it imports these rather than restating
the literals (it used to).

`GHOSTDECK_HID_VID` / `GHOSTDECK_ADB_VID` (and the `_PID` pair) override them. Reverse-engineering a
deck means the identity is an observation about one unit, not a law: the pins have been confirmed on
the D200 only, and a firmware update or a sibling model can move either half. Without an override, a
host whose deck enumerates under a different id has no path except editing the source. The parse is
strict -- a value that is not a 16-bit hex integer falls back to the built-in rather than raising at
import, because an unreadable environment is not a reason to make every command fail.
"""

from __future__ import annotations

import os

__version__ = "0.1.0"


def _usb_id(name: str, default: int) -> int:
    """`int(name, 16)` when it is a plain 16-bit hex number, else `default`."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw, 16)
    except ValueError:
        return default
    return value if 0 < value <= 0xFFFF else default


HID_VID = _usb_id("GHOSTDECK_HID_VID", 0x2207)
HID_PID = _usb_id("GHOSTDECK_HID_PID", 0x0019)
ADB_VID = _usb_id("GHOSTDECK_ADB_VID", 0x18D1)
ADB_PID = _usb_id("GHOSTDECK_ADB_PID", 0xD002)

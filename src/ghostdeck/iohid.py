"""ctypes IOHIDUserDevice. No pyobjc."""

from __future__ import annotations

import ctypes
from ctypes import POINTER, byref, c_char_p, c_int32, c_long, c_uint32, c_uint8, c_void_p
from ctypes.util import find_library

from ghostdeck import HID_PID, HID_VID

CFAllocatorRef = c_void_p
CFTypeRef = c_void_p
CFIndex = c_long
kCFAllocatorDefault = None
kCFStringEncodingUTF8 = 0x08000100
kCFNumberSInt32Type = 3

REPORT_DESCRIPTOR = bytes(
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
# The single source of the 25-byte report descriptor, handed to IOHIDUserDeviceCreate by both
# virtual-HID bindings: this module's ctypes path and `vhid._try_iohid_user_device`'s pyobjc path
# (which imports this constant). It used to be copied verbatim into both modules with nothing tying
# the copies together, so an edit to one side would have made only the fallback binding wrong
# (A-136). It lives here because `vhid` can import `iohid` and not the reverse: `iohid` must stay
# importable without objc (it is the no-pyobjc path), and `vhid` is the module that picks between
# the two bindings.


def _cf():
    """CoreFoundation, or None when this host does not provide it.

    macOS-only. Returning None rather than raising is what keeps the platform a *value*: `create()`
    is already documented to answer None when it cannot make a device, so a caller on a non-Apple
    host -- `vhid.serve()`'s binding probe, a user running `ghostdeck status` on Linux, the offline
    suite on a CI runner -- gets that documented answer instead of an `OSError` traceback from a
    macOS framework path (A-155, which made the `ubuntu-latest` job red by construction).
    """
    path = find_library("CoreFoundation")
    if not path:
        path = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    try:
        lib = ctypes.cdll.LoadLibrary(path)
    except OSError:
        return None
    lib.CFStringCreateWithCString.restype = c_void_p
    lib.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_uint32]
    lib.CFNumberCreate.restype = c_void_p
    lib.CFNumberCreate.argtypes = [c_void_p, c_int32, c_void_p]
    lib.CFDataCreate.restype = c_void_p
    lib.CFDataCreate.argtypes = [c_void_p, POINTER(c_uint8), CFIndex]
    lib.CFDictionaryCreate.restype = c_void_p
    lib.CFDictionaryCreate.argtypes = [
        c_void_p,
        POINTER(c_void_p),
        POINTER(c_void_p),
        CFIndex,
        c_void_p,
        c_void_p,
    ]
    lib.CFRunLoopGetCurrent.restype = c_void_p
    lib.CFRunLoopGetCurrent.argtypes = []
    lib.CFRunLoopRunInMode.restype = c_int32
    lib.CFRunLoopRunInMode.argtypes = [c_void_p, ctypes.c_double, ctypes.c_ubyte]
    lib.CFRelease.argtypes = [c_void_p]
    return lib


def _iokit():
    """IOKit HID user-device entry points, or None when this host does not provide them.

    `IOHIDUserDeviceCreate` is not a usable export of the IOKit umbrella on this
    macOS: ctypes still produces a FuncPtr, but the call returns NULL. The real
    symbol lives in IOHIDLib.plugin. Even with the right dylib, a non-app process
    (the `python -m ghostdeck.vhid` keeper) still gets NULL — keys come from the
    hidshim Studio copy, not from this CLI path.
    """
    candidates = (
        "/System/Library/Extensions/IOHIDFamily.kext/Contents/PlugIns/IOHIDLib.plugin/Contents/MacOS/IOHIDLib",
        "/System/Library/Frameworks/IOKit.framework/IOKit",
    )
    for path in candidates:
        try:
            lib = ctypes.cdll.LoadLibrary(path)
        except OSError:
            continue
        try:
            lib.IOHIDUserDeviceCreate.restype = c_void_p
            lib.IOHIDUserDeviceCreate.argtypes = [c_void_p, c_void_p]
            lib.IOHIDUserDeviceScheduleWithRunLoop.argtypes = [c_void_p, c_void_p, c_void_p]
        except AttributeError:
            continue
        return lib
    return None


def create(vid: int = HID_VID, pid: int = HID_PID, product: str = "ulanzi") -> c_void_p | None:
    cf = _cf()
    iokit = _iokit()
    if cf is None or iokit is None:
        return None
    key_cb = ctypes.addressof(ctypes.c_char.in_dll(cf, "kCFTypeDictionaryKeyCallBacks"))
    val_cb = ctypes.addressof(ctypes.c_char.in_dll(cf, "kCFTypeDictionaryValueCallBacks"))
    default_mode = c_void_p.in_dll(cf, "kCFRunLoopDefaultMode")

    def cstr(text: str):
        return cf.CFStringCreateWithCString(None, text.encode("utf-8"), kCFStringEncodingUTF8)

    def num(value: int):
        box = c_int32(value)
        return cf.CFNumberCreate(None, kCFNumberSInt32Type, byref(box))

    desc = (c_uint8 * len(REPORT_DESCRIPTOR)).from_buffer_copy(REPORT_DESCRIPTOR)
    data = cf.CFDataCreate(None, desc, len(REPORT_DESCRIPTOR))
    keys = (c_void_p * 5)(
        cstr("VendorID"),
        cstr("ProductID"),
        cstr("Product"),
        cstr("Manufacturer"),
        cstr("ReportDescriptor"),
    )
    values = (c_void_p * 5)(
        num(vid),
        num(pid),
        cstr(product),
        cstr("Zkswe"),
        data,
    )
    props = cf.CFDictionaryCreate(
        None,
        keys,
        values,
        5,
        key_cb,
        val_cb,
    )
    if not props:
        return None
    device = iokit.IOHIDUserDeviceCreate(None, props)
    if not device:
        return None
    loop = cf.CFRunLoopGetCurrent()
    iokit.IOHIDUserDeviceScheduleWithRunLoop(device, loop, default_mode)
    return device


def pump(seconds: float = 0.25) -> None:
    """Run the current thread's run loop briefly, or do nothing where there is no CoreFoundation."""
    cf = _cf()
    if cf is None:
        return
    default_mode = c_void_p.in_dll(cf, "kCFRunLoopDefaultMode")
    cf.CFRunLoopRunInMode(default_mode, ctypes.c_double(seconds), 0)

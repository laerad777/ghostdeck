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


def _cf():
    path = find_library("CoreFoundation")
    if not path:
        path = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    lib = ctypes.cdll.LoadLibrary(path)
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
    lib = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/IOKit.framework/IOKit")
    lib.IOHIDUserDeviceCreate.restype = c_void_p
    lib.IOHIDUserDeviceCreate.argtypes = [c_void_p, c_void_p]
    lib.IOHIDUserDeviceScheduleWithRunLoop.argtypes = [c_void_p, c_void_p, c_void_p]
    return lib


def create(vid: int = HID_VID, pid: int = HID_PID, product: str = "ulanzi") -> c_void_p | None:
    cf = _cf()
    iokit = _iokit()
    key_cb = ctypes.addressof(ctypes.c_char.in_dll(cf, "kCFTypeDictionaryKeyCallBacks"))
    val_cb = ctypes.addressof(ctypes.c_char.in_dll(cf, "kCFTypeDictionaryValueCallBacks"))
    default_mode = c_void_p.in_dll(cf, "kCFRunLoopDefaultMode")

    def cstr(text: str):
        return cf.CFStringCreateWithCString(None, text.encode("utf-8"), kCFStringEncodingUTF8)

    def num(value: int):
        box = c_int32(value)
        return cf.CFNumberCreate(None, kCFNumberSInt32Type, byref(box))

    desc = (c_uint8 * len(_DESCRIPTOR)).from_buffer_copy(_DESCRIPTOR)
    data = cf.CFDataCreate(None, desc, len(_DESCRIPTOR))
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
    cf = _cf()
    default_mode = c_void_p.in_dll(cf, "kCFRunLoopDefaultMode")
    cf.CFRunLoopRunInMode(default_mode, ctypes.c_double(seconds), 0)

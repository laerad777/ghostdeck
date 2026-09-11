"""Local hidshim Studio copy. Official /Applications/Ulanzi Studio.app is never written."""

from __future__ import annotations

import os
import plistlib
import socket
import subprocess
import sys
import time
from pathlib import Path

from ghostdeck import adb, devicebuild, usb

ORIGINAL = Path("/Applications/Ulanzi Studio.app")
COPY = Path.home() / "Applications" / "Ulanzi Studio ADB.app"
SOCKET = Path("/tmp/d200-adb-bridge.sock")
ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / "vendor"
BRIDGE = VENDOR / "d200-local-bridge.py"
HIDSHIM_SRC = ROOT / "reference" / "hidshim.c"
SHIM = COPY / "Contents/Frameworks/libhidapi.0.dylib"
REAL = COPY / "Contents/Frameworks/libhidapi.0.real.dylib"
EXE = COPY / "Contents/MacOS/UlanziDeck"


def copy_exists() -> bool:
    return COPY.is_dir() and SHIM.is_file() and REAL.is_file() and EXE.is_file()


def running() -> bool:
    if not COPY.is_dir() or not EXE.is_file():
        return False
    marker = str(EXE.resolve())
    try:
        listed = subprocess.check_output(["ps", "-axo", "command="], text=True)
    except subprocess.CalledProcessError:
        return False
    return any(marker in line for line in listed.splitlines())


def launch() -> None:
    ensure_copy()
    devicebuild.ensure()
    _ensure_bridge()
    subprocess.run(["/usr/bin/open", str(COPY)], check=True, timeout=15)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if running():
            return
        time.sleep(0.25)
    raise RuntimeError("hidshim Studio copy did not stay running")


def ensure_copy() -> None:
    if copy_exists():
        return
    if not ORIGINAL.is_dir():
        raise RuntimeError(f"install official Studio at {ORIGINAL}")
    if not HIDSHIM_SRC.is_file():
        raise RuntimeError(f"missing hidshim source: {HIDSHIM_SRC}")
    COPY.parent.mkdir(parents=True, exist_ok=True)
    if COPY.exists():
        subprocess.run(["/bin/rm", "-rf", str(COPY)], check=True, timeout=60)
    subprocess.run(["/usr/bin/ditto", str(ORIGINAL), str(COPY)], check=True, timeout=120)
    info_path = COPY / "Contents/Info.plist"
    info = plistlib.loads(info_path.read_bytes())
    info["CFBundleIdentifier"] = "ulanzi.UlanziStudioADB"
    info["CFBundleName"] = "Ulanzi Studio ADB"
    info["CFBundleDisplayName"] = "Ulanzi Studio ADB"
    info_path.write_bytes(plistlib.dumps(info, sort_keys=False))
    frameworks = COPY / "Contents/Frameworks"
    shim = frameworks / "libhidapi.0.dylib"
    real = frameworks / "libhidapi.0.real.dylib"
    if real.exists():
        real.unlink()
    shim.rename(real)
    subprocess.run(
        ["install_name_tool", "-id", "@rpath/libhidapi.0.real.dylib", str(real)],
        check=True,
        timeout=30,
    )
    sdk = subprocess.check_output(
        ["xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True, timeout=30
    ).strip()
    subprocess.run(
        [
            "clang",
            "-arch",
            "arm64",
            "-dynamiclib",
            "-O2",
            "-isysroot",
            sdk,
            str(HIDSHIM_SRC),
            "-o",
            str(shim),
            "-framework",
            "IOKit",
            "-framework",
            "CoreFoundation",
            "-lpthread",
        ],
        check=True,
        timeout=60,
    )
    subprocess.run(
        ["install_name_tool", "-id", "@rpath/libhidapi.0.dylib", str(shim)],
        check=True,
        timeout=30,
    )
    subprocess.run(
        ["codesign", "--force", "--deep", "--sign", "-", str(COPY)],
        check=True,
        timeout=120,
    )


def _socket_live() -> bool:
    if not SOCKET.exists():
        return False
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(0.4)
        client.connect(str(SOCKET))
        return True
    except OSError:
        return False
    finally:
        client.close()


def _ensure_bridge() -> None:
    if _socket_live():
        return
    if SOCKET.exists():
        SOCKET.unlink()
    if not BRIDGE.is_file():
        raise RuntimeError(f"bridge missing: {BRIDGE}")
    serial = ""
    found = usb.detect()
    if found and found.get("mode") == "adb":
        serial = found.get("serial") or ""
    if not serial:
        serial = adb.serial_from_devices() or ""
    if not serial:
        raise RuntimeError("no ADB serial for hidshim bridge")
    log = open("/tmp/d200-local-bridge.log", "ab", buffering=0)
    env = os.environ.copy()
    previous = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(VENDOR) if not previous else str(VENDOR) + os.pathsep + previous
    subprocess.Popen(
        [
            sys.executable,
            "-B",
            "-u",
            str(BRIDGE),
            "--adb",
            adb.require_adb(),
            "--serial",
            serial,
            "--state-file",
            "/tmp/d200-local-bridge.pid",
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env=env,
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if _socket_live():
            return
        time.sleep(0.2)
    raise RuntimeError("hidshim bridge socket did not come up")

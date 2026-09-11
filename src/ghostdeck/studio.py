"""Local hidshim Studio copy. Official /Applications/Ulanzi Studio.app is never written."""

from __future__ import annotations

import os
import plistlib
import shutil
import signal
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

_BUILD_TOOLS = ("ditto", "xcrun", "clang", "install_name_tool", "codesign")

# A deck that has just re-enumerated through ADB answers device commands late, the HID-to-ADB
# switch report itself is flaky, and the bridge exits on the first rejected device command, so
# bridge bring-up is prepared and retried instead of reported from a single attempt.
BRIDGE_WAIT = 15.0
BRIDGE_ATTEMPTS = 3
BRIDGE_READY_TIMEOUT = 25.0
BRIDGE_RETRY_DELAY = 2.0


def _require_build_tools() -> None:
    """Fail with the missing tool named instead of a bare FileNotFoundError mid-copy."""
    for tool in _BUILD_TOOLS:
        if shutil.which(tool) is None:
            raise RuntimeError(f"{tool} not on PATH: install it (xcode-select --install)")


def copy_exists() -> bool:
    return COPY.is_dir() and SHIM.is_file() and REAL.is_file() and EXE.is_file()


def _copy_pids() -> list[int]:
    """PIDs whose live command line is this copy's own executable.

    Identity comes from the running process, so the official Studio.app and a
    recycled pid are never matched. `-ww` disables ps truncation of the argv.
    """
    if not COPY.is_dir() or not EXE.is_file():
        return []
    marker = str(EXE.resolve())
    try:
        listed = subprocess.check_output(["ps", "-axo", "pid=,command="], text=True)
    except subprocess.CalledProcessError:
        return []
    pids = []
    for line in listed.splitlines():
        pid, _, command = line.strip().partition(" ")
        if pid.isdigit() and marker in command:
            pids.append(int(pid))
    return pids


def running() -> bool:
    return bool(_copy_pids())


def _quit_copy(*, timeout: float = 15.0) -> None:
    """Stop only this copy, never the official app. A dead shim needs a fresh Studio."""
    pids = _copy_pids()
    if not pids:
        return
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _copy_pids():
            return
        time.sleep(0.25)
    raise RuntimeError("hidshim Studio copy did not stop")


def launch() -> None:
    ensure_copy()
    devicebuild.ensure()
    if not _socket_live():
        # Studio holds HID interface 0 while it runs, so the HID-to-ADB switch needs
        # the copy stopped first; a restarted bridge also leaves an already running
        # copy holding a dead shim, so it is relaunched either way.
        _quit_copy()
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
    _require_build_tools()
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


def _bus_serial() -> str:
    """The deck's serial as reported by the bus. Never stored, never hard-coded."""
    found = usb.detect()
    if found and found.get("mode") == "adb" and found.get("serial"):
        return str(found["serial"])
    return adb.serial_from_devices() or ""


def _adb_serial() -> str:
    """Bus serial, switching the deck off HID first.

    The deck enumerates as HID or as ADB, never both, so a deck that is still on
    HID is moved over with the same 0x00ff report `play` uses before the bridge
    can open a session.
    """
    serial = _bus_serial()
    if serial:
        return serial
    usb.enable_adb()
    serial = _bus_serial()
    if not serial:
        raise RuntimeError("D200 is not enumerating through ADB: no bridge serial")
    return serial


def _device_ready(serial: str, *, timeout: float) -> bool:
    """Wait until the deck runs an allowlisted shell command again.

    `getprop` is the cheapest command the adb allowlist already permits, and a
    successful one proves the device shell is answering, which is what the
    bridge's own staging needs.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            result = adb.run(
                ["-s", serial, "shell", "getprop sys.usb.config"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except subprocess.SubprocessError:
            result = None
        if result is not None and result.returncode == 0:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def _spawn_bridge(serial: str, log) -> subprocess.Popen:
    env = os.environ.copy()
    previous = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(VENDOR) if not previous else str(VENDOR) + os.pathsep + previous
    return subprocess.Popen(
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


def _stop_owned_bridge(child: subprocess.Popen, *, timeout: float = 5.0) -> None:
    """Reap this attempt's own child only; a foreign bridge is never touched."""
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=timeout)


def _ensure_bridge() -> None:
    if _socket_live():
        return
    if not BRIDGE.is_file():
        raise RuntimeError(f"bridge missing: {BRIDGE}")
    log = open("/tmp/d200-local-bridge.log", "ab", buffering=0)
    reason = "hidshim bridge socket did not come up"
    try:
        for attempt in range(BRIDGE_ATTEMPTS):
            if attempt:
                time.sleep(BRIDGE_RETRY_DELAY)
            try:
                serial = _adb_serial()
            except Exception as error:
                reason = str(error) or type(error).__name__
                continue
            if not _device_ready(serial, timeout=BRIDGE_READY_TIMEOUT):
                reason = "D200 stopped answering device commands after switching to ADB"
                continue
            if SOCKET.exists():
                SOCKET.unlink()
            child = _spawn_bridge(serial, log)
            deadline = time.monotonic() + BRIDGE_WAIT
            while time.monotonic() < deadline:
                if _socket_live():
                    return
                if child.poll() is not None:
                    reason = f"hidshim bridge exited with status {child.returncode}"
                    break
                time.sleep(0.2)
            _stop_owned_bridge(child)
    finally:
        log.close()
    raise RuntimeError(reason)

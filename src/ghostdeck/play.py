from __future__ import annotations

import os
import signal
import shutil
import subprocess
import sys
from pathlib import Path

from ghostdeck import adb, devicebuild, state as gdstate, usb, vhid

VENDOR_PLAY = Path(__file__).resolve().parents[2] / "vendor" / "d200-color-play.py"
VENDOR_DIR = Path(__file__).resolve().parents[2] / "vendor"


def _require_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not on PATH")


def _kill_play() -> None:
    data = gdstate.load()
    pid = data.get("play_pid")
    if isinstance(pid, int) and pid > 0:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    data["play_pid"] = None
    gdstate.save(data)


def start_play(source: str) -> None:
    _require_ffmpeg()
    adb.require_adb()
    gdstate.ensure_dirs()
    devicebuild.ensure()
    found = usb.detect()
    if found is None or found.get("mode") in (None, "none"):
        raise RuntimeError("no D200 on USB")
    if found["mode"] == "hid":
        usb.switch_hid_to_adb()
        found = usb.detect()
    if found is None or found.get("mode") != "adb":
        raise RuntimeError("deck is not in ADB after switch")
    try:
        vhid.start()
    except Exception as error:
        print(f"virtual HID skipped: {error}", file=sys.stderr)
    _kill_play()
    if not VENDOR_PLAY.is_file():
        raise RuntimeError(f"vendor player missing: {VENDOR_PLAY}")
    env = dict(os.environ)
    env["GHOSTDECK_SERIAL"] = found.get("serial") or adb.serial_from_devices() or ""
    env["PYTHONPATH"] = str(VENDOR_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-B",
            "-u",
            str(VENDOR_PLAY),
            source,
            "--fps",
            "source",
            "--quality",
            "12",
            "--crop",
            "auto",
            "--loop",
        ],
        start_new_session=True,
        env=env,
    )
    data = gdstate.load()
    data["play_pid"] = proc.pid
    gdstate.save(data)


def playing() -> bool:
    data = gdstate.load()
    pid = data.get("play_pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def stop() -> None:
    _kill_play()
    serial = adb.serial_from_devices()
    if serial:
        adb.run(["-s", serial, "shell", "setprop ctl.start zkswe"])
        adb.run(["-s", serial, "shell", "rm -f /tmp/ghostdeck-*"])
        adb.run(["-s", serial, "shell", "ls /tmp/ghostdeck"])

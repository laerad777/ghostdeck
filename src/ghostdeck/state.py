"""Persistent host state under ~/.ghostdeck."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from pathlib import Path

HOME = Path.home() / ".ghostdeck"
HOME_DIR = HOME
STATE_PATH = HOME / "state.json"
PLUGIN_DIR = HOME / "plugins"
BIN_DIR = HOME / "bin"
LOCK_NAME = ".state.lock"

# vhid record fields written by vhid._write_vhid() as nested + "vhid_<name>" pairs.
_VHID_FLAGS = (("experimental", True), ("iohid", False), ("visible", False))
_VHID_IDS = ("vhid_vid", "vhid_pid_usb")
# pid_t is a signed 32-bit int on macOS; a larger stored value cannot be signalled.
_PID_MAX = 2**31 - 1


def default_state() -> dict:
    return {
        "play_pid": None,
        "vhid_pid": None,
        "play": {"pid": None},
        "vhid": {
            "pid": None,
            "experimental": True,
            "iohid": False,
            "visible": False,
            "status": "down",
        },
    }


def ensure_dirs() -> None:
    HOME.mkdir(mode=0o700, exist_ok=True)
    PLUGIN_DIR.mkdir(mode=0o700, exist_ok=True)
    BIN_DIR.mkdir(mode=0o700, exist_ok=True)


def load() -> dict:
    ensure_dirs()
    data = default_state()
    if not STATE_PATH.is_file():
        return data
    try:
        raw = json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return data
    if not isinstance(raw, dict):
        return data
    play = raw.get("play") if isinstance(raw.get("play"), dict) else {}
    vhid = raw.get("vhid") if isinstance(raw.get("vhid"), dict) else {}
    play_pid = _as_pid(play.get("pid"))
    if play_pid is None:
        play_pid = _as_pid(raw.get("play_pid"))
    vhid_pid = _as_pid(vhid.get("pid"))
    if vhid_pid is None:
        vhid_pid = _as_pid(raw.get("vhid_pid"))
    status = vhid.get("status", "down")
    data["play"]["pid"] = play_pid
    data["play_pid"] = play_pid
    data["vhid"]["pid"] = vhid_pid
    data["vhid_pid"] = vhid_pid
    for name, default in _VHID_FLAGS:
        value = bool(vhid.get(name, raw.get("vhid_" + name, default)))
        data["vhid"][name] = value
        if "vhid_" + name in raw:
            data["vhid_" + name] = value
    for name in _VHID_IDS:
        value = _as_pid(raw.get(name))
        if value is not None:
            data[name] = value
    data["vhid"]["status"] = status if status in ("up", "down") else "down"
    return data


@contextlib.contextmanager
def locked():
    """Serialize a whole load-mutate-save sequence against other ghostdeck writers."""
    ensure_dirs()
    fd = os.open(HOME / LOCK_NAME, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _write_state(data: dict) -> None:
    """Unique temp, 0600, atomic replace. The caller must hold `locked()`."""
    ensure_dirs()
    text = json.dumps(_normalized(data), indent=2, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(dir=HOME, prefix=".state.json.")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fchmod(handle.fileno(), 0o600)
            os.fsync(handle.fileno())
        os.replace(tmp, STATE_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save(data: dict) -> None:
    with locked():
        _write_state(data)


def update(**sections) -> dict:
    with locked():
        data = load()
        for key, value in sections.items():
            if isinstance(value, dict) and isinstance(data.get(key), dict):
                merged = dict(data[key])
                merged.update(value)
                data[key] = merged
            else:
                data[key] = value
        _write_state(data)
        return load()


def pid_alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, OverflowError):
        return False
    return not reap(pid)


def reap(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        waited, _status = os.waitpid(pid, os.WNOHANG)
    except (OSError, OverflowError):
        return False
    return waited == pid


def _as_pid(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if not 0 < value <= _PID_MAX:
        return None
    return value


def _pick_pid(parent: dict, section: dict, flat_key: str):
    if flat_key in parent:
        return _as_pid(parent.get(flat_key))
    return _as_pid(section.get("pid"))


def _normalized(data: dict) -> dict:
    out = default_state()
    if not isinstance(data, dict):
        return out
    play = data.get("play") if isinstance(data.get("play"), dict) else {}
    vhid = data.get("vhid") if isinstance(data.get("vhid"), dict) else {}
    play_pid = _pick_pid(data, play, "play_pid")
    vhid_pid = _pick_pid(data, vhid, "vhid_pid")
    out["play"]["pid"] = play_pid
    out["play_pid"] = play_pid
    out["vhid"]["pid"] = vhid_pid
    out["vhid_pid"] = vhid_pid
    for name, default in _VHID_FLAGS:
        value = bool(vhid.get(name, data.get("vhid_" + name, default)))
        out["vhid"][name] = value
        if "vhid_" + name in data:
            out["vhid_" + name] = value
    for name in _VHID_IDS:
        value = _as_pid(data.get(name))
        if value is not None:
            out[name] = value
    status = vhid.get("status", "down")
    if vhid_pid and status not in ("up", "down"):
        status = "up"
    out["vhid"]["status"] = status if status in ("up", "down") else "down"
    return out

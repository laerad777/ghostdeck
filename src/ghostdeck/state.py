"""Persistent host state under ~/.ghostdeck."""

from __future__ import annotations

import json
import os
from pathlib import Path

HOME = Path.home() / ".ghostdeck"
HOME_DIR = HOME
STATE_PATH = HOME / "state.json"
PLUGIN_DIR = HOME / "plugins"
BIN_DIR = HOME / "bin"


def default_state() -> dict:
    return {
        "play_pid": None,
        "vhid_pid": None,
        "play": {"pid": None},
        "vhid": {
            "pid": None,
            "experimental": True,
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
    data["vhid"]["experimental"] = bool(vhid.get("experimental", True))
    data["vhid"]["visible"] = bool(vhid.get("visible", False))
    data["vhid"]["status"] = status if status in ("up", "down") else "down"
    return data


def save(data: dict) -> None:
    ensure_dirs()
    payload = _normalized(data)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    tmp = HOME / ".state.json.tmp"
    tmp.write_text(text)
    os.chmod(tmp, 0o600)
    tmp.replace(STATE_PATH)


def update(**sections) -> dict:
    data = load()
    for key, value in sections.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            merged = dict(data[key])
            merged.update(value)
            data[key] = merged
        else:
            data[key] = value
    save(data)
    return load()


def pid_alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return not reap(pid)


def reap(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        waited, _status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return False
    except OSError:
        return False
    return waited == pid


def _as_pid(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value <= 0:
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
    out["vhid"]["experimental"] = bool(vhid.get("experimental", True))
    out["vhid"]["visible"] = bool(vhid.get("visible", False))
    status = vhid.get("status", "down")
    if vhid_pid and status not in ("up", "down"):
        status = "up"
    out["vhid"]["status"] = status if status in ("up", "down") else "down"
    return out

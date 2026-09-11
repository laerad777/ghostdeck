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


def _state_root() -> Path:
    """The directory the state tree hangs off: `STATE_PATH`'s parent.

    Everything this module creates -- the tree `ensure_dirs()` makes, the lock file, the atomic temp
    file -- is derived here, from the one path whose redirection fully isolates the module. It used
    to come from the frozen `HOME`/`PLUGIN_DIR`/`BIN_DIR` siblings instead, which are read by other
    modules (`devicebuild`, `play`, `vhid`) and are therefore patchable independently: a caller that
    redirected the state file but not those siblings still made `ensure_dirs()` create, and `locked()`
    lock, the *operator's* real `~/.ghostdeck` (C-148/C-154: 736 mkdir calls into the real tree in a
    single three-file test run, with the suite reporting green).

    It is also what `os.replace()` needs: a temp file on a different device from its target cannot be
    renamed over it, so the staging directory has to be the target's own.
    """
    return STATE_PATH.parent


def ensure_dirs() -> None:
    """Create the state directories, naming a path that is unusable instead of a bare errno.

    `parents=True` because `~` itself may not exist (a fresh temp HOME, a container), and the
    explicit check because `exist_ok=True` still raises FileExistsError when the path exists as a
    *file* -- a bare errno out of a read-only `ghostdeck status` (A-127).
    """
    root = _state_root()
    for directory in (root, root / "plugins", root / "bin"):
        # Checked before mkdir: `exist_ok=True` raises FileExistsError for a path that exists as a
        # *file*, so the explanatory message below would never be reached.
        if directory.exists() and not directory.is_dir():
            raise RuntimeError(f"{directory} exists and is not a directory; remove it")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)


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
    fd = os.open(_state_root() / LOCK_NAME, os.O_CREAT | os.O_RDWR, 0o600)
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
    fd, tmp = tempfile.mkstemp(dir=_state_root(), prefix=".state.json.")
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


# The vhid fields that exist in BOTH shapes -- the nested `vhid` record and a flat `vhid_<name>`
# alias -- paired with the flat spelling each one mirrors.
_VHID_ALIASES = (("pid", "vhid_pid"),) + tuple((name, "vhid_" + name) for name, _ in _VHID_FLAGS)


def _apply_vhid_intent(data: dict, sections: dict) -> None:
    """Write both spellings of every vhid field the caller asked to change (A-107).

    `load()` materialises both shapes from the nested record, so after any load both keys are
    present in `data` and `_normalized()` -- which prefers the flat key for the pid but the nested
    one for the flags -- cannot tell which side the caller just wrote. The caller's intent is
    therefore read from `sections` (the argument, not the loaded state) and both spellings are
    written from it, so the two can never disagree. A nested section wins for the fields it names;
    a flat alias still wins for fields the section did not mention.
    """
    record = data.get("vhid") if isinstance(data.get("vhid"), dict) else None
    if record is None:
        return
    nested = sections.get("vhid") if isinstance(sections.get("vhid"), dict) else {}
    for name, flat in _VHID_ALIASES:
        if name in nested:
            value = nested[name]
        elif flat in sections:
            value = sections[flat]
        else:
            continue
        record[name] = value
        data[flat] = value


def update(**sections) -> dict:
    with locked():
        data = load()
        for key, value in sections.items():
            if isinstance(value, dict) and isinstance(data.get(key), dict):
                data[key] = {**data[key], **value}
            else:
                data[key] = value
        _apply_vhid_intent(data, sections)
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

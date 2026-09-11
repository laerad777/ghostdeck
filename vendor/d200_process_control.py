"""Slim host ownership helpers. No lab receipts, no machine paths, no serials."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import secrets
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

HOST_STATE = Path("/tmp/d200-color-host.json")
# pid_t is a signed 32-bit int on macOS; a larger stored value cannot be signalled
# and raises out of os.kill. Mirrors src/ghostdeck/state.py's bound.
PID_MAX = 2 ** 31 - 1
# One bridge at a time, host-side. The device-side proxy.lock lives inside each
# bridge's own per-session directory, so it can never see a sibling session.
ADMISSION_LOCK_NAME = "device-admission.lock"
# Identity oracle shared with src/ghostdeck/play.py, which records the same pair.
_PS_TIMEOUT = 5
# The published record's owner identity, beside the record: {"pid", "lstart"}.
# It is a *separate* file because the published record's JSON schema is fixed.
OWNER_SUFFIX = ".owner"


class DeviceAdmissionError(RuntimeError):
    """Another live bridge already owns the deck, so this one must not start."""


def emit_diagnostic(stream, receipt):
    try:
        encoded = json.dumps(receipt, separators=(",", ":"), allow_nan=False) + "\n"
        stream.write(encoded)
        return True
    except Exception:
        return False


class StopEndpoint:
    def __init__(self, kind, stop):
        self.kind = kind
        self.stop = stop
        self.token = secrets.token_hex(32)
        self.directory = Path(tempfile.mkdtemp(prefix="d200-stop-"))
        self.path = self.directory / "control.sock"
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.closed = threading.Event()
        self.thread = None
        try:
            self.listener.bind(str(self.path))
            os.chmod(self.path, 0o600)
            self.listener.listen(4)
            self.listener.settimeout(0.2)
            self.thread = threading.Thread(target=self._serve, daemon=True)
            self.thread.start()
        except BaseException:
            self.listener.close()
            shutil.rmtree(self.directory)
            raise

    def state(self):
        return {"kind": self.kind, "socket": str(self.path), "token": self.token}

    def _serve(self):
        while not self.closed.is_set():
            try:
                client, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self.closed.is_set():
                    return
                raise
            with client:
                try:
                    request = _receive(client, time.monotonic() + 1)
                    token = request.get("token")
                    accepted = (
                        request.get("op") == "stop"
                        and request.get("kind") == self.kind
                        and isinstance(token, str)
                        and len(token) == 64
                        and secrets.compare_digest(token, self.token)
                    )
                    client.sendall(json.dumps({"accepted": accepted}).encode() + b"\n")
                except (OSError, ValueError):
                    continue
                if accepted:
                    self.stop()
                    return

    def close(self):
        self.closed.set()
        self.listener.close()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
        self.path.unlink(missing_ok=True)
        try:
            self.directory.rmdir()
        except OSError:
            pass


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("stop message timed out")
    return remaining


def _receive(client, deadline):
    data = bytearray()
    while not data.endswith(b"\n"):
        client.settimeout(_remaining(deadline))
        block = client.recv(1024)
        _remaining(deadline)
        if not block:
            raise ValueError("stop endpoint closed before response")
        data.extend(block)
        if len(data) > 4096:
            raise ValueError("stop message exceeds limit")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("invalid stop message")
    _remaining(deadline)
    return value


def _pid_alive(pid):
    """True only for a positive, signallable pid that still exists.

    An out-of-range pid (or any non-int) is not live, never an error: this runs
    inside the advisory publication path, which must not raise into the send loop.
    """
    if type(pid) is not int or not 0 < pid <= PID_MAX:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, OverflowError, ValueError):
        return False
    return True


def _process_start_time(pid):
    """Return ``(lstart, unknown_reason)`` for `pid`, the FIX-1-T8 identity shape.

    * ``(text, None)`` -- ps ran; the process is live and this is its start time.
    * ``(None, None)`` -- ps ran; there is no such process (dead, or recycled away).
    * ``(None, reason)`` -- ps itself could not answer, so identity is undeterminable.

    ``LC_ALL=C`` keeps a recorded string from diverging from a later reading by
    locale and ``-ww`` prevents truncation, exactly as ``src/ghostdeck/play.py``
    proves ownership. Both identity users in this module -- the device-admission
    lock holder and the published record's owner -- go through this one oracle, so
    the two lanes agree on how identity is proven. An out-of-range pid cannot be
    asked about, so it is reported as "no such process" rather than handed to ps.
    """
    if type(pid) is not int or not 0 < pid <= PID_MAX:
        return None, None
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-ww", "-p", str(pid)],
            capture_output=True,
            text=True,
            env=dict(os.environ, LC_ALL="C"),
            timeout=_PS_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, f"ps timed out after {_PS_TIMEOUT}s"
    except OSError as error:
        return None, f"ps could not be run ({error})"
    text = " ".join((result.stdout or "").split())
    if text:
        return text, None
    if result.returncode == 1:
        return None, None
    return None, f"ps exited {result.returncode}"


def admission_lock_path():
    """The stable lock file under the state root, resolved at call time (so a test
    HOME or an operator's HOME is honoured rather than frozen at import)."""
    return Path.home() / ".ghostdeck" / ADMISSION_LOCK_NAME


def _lock_owner(descriptor):
    """The recorded lock holder, best effort; diagnostics only, never raises."""
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        recorded = json.loads(os.read(descriptor, 4096).decode("utf-8", "replace"))
    except (OSError, ValueError):
        return None
    return recorded if isinstance(recorded, dict) else None


def _describe_lock_owner(descriptor):
    """Who to name in the rejection: the recorded holder, or that it is unrecorded."""
    pid = (_lock_owner(descriptor) or {}).get("pid")
    return f"pid {pid}" if _pid_alive(pid) else "an unrecorded process"


def _write_lock_owner(descriptor):
    """Record the holder for diagnostics. Exclusion is the kernel's flock, not this text."""
    lstart, _reason = _process_start_time(os.getpid())
    payload = json.dumps({"pid": os.getpid(), "lstart": lstart}).encode() + b"\n"
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.ftruncate(descriptor, 0)
        os.write(descriptor, payload)
    except OSError:
        pass


def _read_state(path):
    """The published record, or None when it is absent, unreadable or junk."""
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _state_kind(path):
    """Classify the state path without following a planted symlink."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return "absent"
    return "regular" if stat.S_ISREG(info.st_mode) else "foreign"


def _write_private_file(path, text):
    """Unique temp in the destination directory, 0600, atomic replace."""
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".")
    temporary = Path(temporary)
    try:
        with os.fdopen(descriptor, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(text)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _owner_path(path):
    return path.with_name(path.name + OWNER_SUFFIX)


def _read_owner(path):
    """The recorded ``{"pid", "lstart"}`` owner, or None when it is unusable."""
    recorded = _read_state(_owner_path(path))
    if recorded is None:
        return None
    pid, lstart = recorded.get("pid"), recorded.get("lstart")
    if type(pid) is not int or not 0 < pid <= PID_MAX:
        return None
    if not isinstance(lstart, str) or not lstart.strip():
        return None
    return {"pid": pid, "lstart": " ".join(lstart.split())}


def _record_owner(path, updated):
    """Bind the published pid to a start time, so a recycled pid cannot pass for it.

    Only the process that *is* the published pid can record its own start time; a
    publisher writing a record about some other process leaves the identity alone.
    A failure here is not an error: the identity is advisory, and losing it only
    makes a later foreign publication more cautious.
    """
    pid = updated.get("pid")
    if pid != os.getpid():
        return
    recorded = _read_owner(path)
    if recorded is not None and recorded["pid"] == pid:
        return  # already bound; a live process's start time cannot change
    lstart, _reason = _process_start_time(pid)
    if lstart is None:
        return  # nothing trustworthy to record, so identity stays unprovable
    try:
        _write_private_file(_owner_path(path), json.dumps({"pid": pid, "lstart": lstart}) + "\n")
    except OSError:
        pass


def _owner_is_live_elsewhere(current, path):
    """True when the record is provably held by another process that still runs.

    Liveness alone is not identity: a recycled pid answers ``os.kill(pid, 0)`` just
    as the previous owner did. The recorded start time is what distinguishes them.
    Everything that cannot be proven -- no recorded identity for the pid, a ps that
    cannot answer -- counts as owned, because the alternative is clobbering a live
    owner's record, and a skipped publication costs diagnostics only.
    """
    pid = current.get("pid")
    if not _pid_alive(pid):
        return False
    recorded = _read_owner(path)
    if recorded is None or recorded["pid"] != pid:
        return True
    live, reason = _process_start_time(pid)
    if reason is not None:
        return True
    if live is None:
        return False  # ps ran and reports no such process
    return live == recorded["lstart"]


def publish_video_state(state, *, claim=False, state_path=HOST_STATE):
    """Publish the advisory record, or skip the publication.

    A non-claim publication runs inside the media send loop (the player's
    ``on_progress``), so it never raises: an unwritable state root, a destination
    that cannot be taken over, and an owner that cannot be proven gone all skip the
    publication and let the send continue. The published JSON schema, the phase
    names and the parameters are unchanged; a skipped publication leaves the file
    exactly as it was and returns the payload that was not written, so the caller's
    own state is never replaced by another process's record.

    ``claim=True`` is the deliberate startup takeover: it still raises, so a broken
    state root or a foreign destination is reported at startup rather than silently.
    """
    path = Path(state_path)
    updated = json.loads(json.dumps(state))
    path.parent.mkdir(parents=True, exist_ok=True)
    if claim and updated.get("phase") != "active":
        raise RuntimeError("playback claim must be active")
    kind = _state_kind(path)
    # Anything that is not our regular file (symlink, fifo, directory) is never
    # read through or written through; drop it and publish a fresh file.
    if kind == "foreign":
        try:
            path.unlink()
        except OSError as error:
            if claim:
                raise RuntimeError("state path is not an owned regular file") from error
            return updated
        kind = "absent"
    if not claim:
        current = _read_state(path) if kind == "regular" else None
        if current is not None and current.get("pid") != updated.get("pid"):
            if _owner_is_live_elsewhere(current, path):
                return updated
    try:
        _write_private_file(path, json.dumps(updated) + "\n")
    except OSError:
        if claim:
            raise
        return updated
    _record_owner(path, updated)
    return updated


def video_bridge_request(request, timeout, socket_path="/tmp/d200-adb-bridge.sock"):
    from d200_video_stream import capability_bytes, session_bytes, uint, validate_status
    import math

    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("video control timeout must be finite and positive")
    fields = {"schemaVersion", "op", "session", "epoch", "capability"}
    if not isinstance(request, dict) or request.get("op") not in ("videoStatus", "videoCancel"):
        raise ValueError("unsupported video control request")
    operation = request["op"]
    if operation == "videoCancel":
        fields.add("reason")
    if (
        set(request) != fields
        or type(request["schemaVersion"]) is not int
        or request["schemaVersion"] != 1
        or type(request["session"]) is not str
        or type(request["capability"]) is not str
    ):
        raise ValueError("invalid video control fields")
    session_bytes(request["session"])
    capability_bytes(request["capability"])
    uint(request["epoch"], 32, "epoch", 1, 1)
    payload = json.dumps(request, separators=(",", ":")).encode() + b"\n"
    endpoint = Path(socket_path).lstat()
    if not stat.S_ISSOCK(endpoint.st_mode):
        raise RuntimeError("untrusted video bridge socket")
    deadline = time.monotonic() + timeout
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(_remaining(deadline))
        client.connect(os.fspath(socket_path))
        client.sendall(payload)
        response = _receive(client, deadline)
    if "status" in response:
        validate_status(response["status"])
    return response


@contextmanager
def managed_device_admission(*, state_path=HOST_STATE, lock_path=None):
    """Admit exactly one bridge at a time to the deck, host-side.

    The guard is an ``fcntl.flock`` on a stable 0600 file under the state root
    (``~/.ghostdeck/device-admission.lock``) rather than an ``O_EXCL`` marker: the
    kernel drops the lock when its holder dies, so a crashed or SIGKILLed bridge
    cannot leave the deck permanently un-admittable, and a lock file left behind by
    a dead pid is reclaimed by the next bridge instead of refusing admission. The
    recorded pid and start time are diagnostics; exclusion is the flock itself.

    Hold it for the whole session, not only for startup: two bridges that serialize
    just their startup still both stage to, and drive, one deck. `state_path` is
    accepted for call compatibility and is not the lock location -- the lock always
    lives under the state root, never beside the published host record.
    """
    target = Path(lock_path) if lock_path is not None else admission_lock_path()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            raise DeviceAdmissionError(
                "another bridge already owns the device "
                f"({_describe_lock_owner(descriptor)})"
            ) from error
        _write_lock_owner(descriptor)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)

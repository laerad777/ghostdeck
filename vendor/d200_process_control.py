"""Slim host ownership helpers. No lab receipts, no machine paths, no serials."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

HOST_STATE = Path("/tmp/d200-color-host.json")


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
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def publish_video_state(state, *, claim=False, state_path=HOST_STATE):
    path = Path(state_path)
    updated = json.loads(json.dumps(state))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    if claim and updated.get("phase") != "active":
        raise RuntimeError("playback claim must be active")
    if not claim:
        try:
            current = json.loads(path.read_text())
        except FileNotFoundError:
            current = None
        if current is not None and current.get("pid") != updated.get("pid"):
            if _pid_alive(current.get("pid")):
                raise RuntimeError("playback publication ownership lost")
    temporary.write_text(json.dumps(updated) + "\n")
    temporary.replace(path)
    return updated


def video_bridge_request(request, timeout, socket_path="/tmp/d200-adb-bridge.sock"):
    from d200_video_stream import capability_bytes, session_bytes, uint, validate_status
    import math
    import stat

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
def managed_device_admission(*, state_path=HOST_STATE):
    yield

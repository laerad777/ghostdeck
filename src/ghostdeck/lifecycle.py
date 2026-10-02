"""Cooperative startup ownership, independent of temporary PID records.

A child inherits a pipe lease and the command's flock. EOF cancels startup, including
when a parent is killed; only an explicit commit permits a long-lived child to detach.
The inherited lock vetoes another mutating command until restoration actually exits.
Never SIGKILL a device owner or infer ownership from a process census.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import select
import signal
import subprocess
import threading
import tempfile
import time
from pathlib import Path

from ghostdeck import state

CLEANUP_WAIT = 60.0
COMMANDS = frozenset(("play", "stop", "studio", "bridge", "reconnect"))
_active = None


class Cancelled(BaseException):
    pass


def _close(fd):
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass


def _pipes():
    first = os.pipe()
    try:
        return first, os.pipe()
    except BaseException:
        for fd in first:
            _close(fd)
        raise


def _read_fd(name):
    value = os.environ.pop(name, None)
    return int(value) if value is not None else None


def child_lease(cancel):
    """Arm after signal handlers, before device effects. Called only by owned children.

    The lease/lock descriptors are close-on-exec here, so ffmpeg/adb descendants
    cannot keep the parent's lifetime pipe or the restoration veto alive by accident.
    """
    lease = _read_fd("GHOSTDECK_CHILD_LEASE")
    if lease is None:
        return
    gate = _read_fd("GHOSTDECK_CHILD_GATE")
    ack = _read_fd("GHOSTDECK_CHILD_ACK")
    decision = _read_fd("GHOSTDECK_CHILD_DECISION")
    for fd in (lease, gate, ack, decision):
        if fd is not None:
            os.set_inheritable(fd, False)

    def watch():
        try:
            os.read(lease, 1)  # EOF: every child observes the same atomic decision.
            committed = os.pread(decision, 1, 0) == b"C"
            if committed:
                _close(gate)
                try:
                    os.write(ack, b"C")
                except BrokenPipeError:
                    pass  # Parent died after committing; this child is intentionally detached.
            else:
                # Retain gate until process exit, including the whole finally/restore path.
                cancel()
        finally:
            _close(lease)
            _close(ack)
            _close(decision)
    if select.select([lease], [], [], 0)[0]:
        watch()  # Parent already gone: cancel before the child performs device effects.
    else:
        threading.Thread(target=watch, name="ghostdeck-startup-lease", daemon=True).start()


class Command:
    def __init__(self, *, cleanup_wait=CLEANUP_WAIT):
        self.cleanup_wait = cleanup_wait
        self.children = []
        self.gate = None
        self.cancelled = False
        self.approved = threading.Event()
        self.finished = threading.Event()
        self.parent = None
        self.ready = None
        self.handlers = {}
        self.decision = None
        self.committed = False

    def interrupt(self, *_):
        if self.committed:
            return
        self.cancelled = True
        # Never asynchronously raise through Popen registration or restoration.
        # Startup checks bounded stage boundaries; stop is allowed to finish safely.

    def check(self):
        if self.cancelled:
            raise Cancelled("command cancelled")

    def __enter__(self):
        global _active
        state.ensure_dirs()
        self.gate = os.open(state.STATE_PATH.parent / ".command.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self.gate, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _close(self.gate)
            self.gate = None
            raise RuntimeError("command or startup cleanup is still pending; wait before retrying") from None
        try:
            self.decision = tempfile.TemporaryFile()
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                self.handlers[sig] = signal.signal(sig, self.interrupt)
            _active = self
            self.parent = _read_fd("GHOSTDECK_COMMAND_LEASE")
            self.ready = _read_fd("GHOSTDECK_COMMAND_READY")
            for fd in (self.parent, self.ready):
                if fd is not None:
                    os.set_inheritable(fd, False)
            if self.parent is not None:
                # Observe an already-dead GUI before dispatching any device work.
                if select.select([self.parent], [], [], 0)[0]:
                    if os.read(self.parent, 1) != b"C":
                        self.cancelled = True
                    else:
                        self.approved.set()
                def watch():
                    try:
                        value = os.read(self.parent, 1)
                        if value == b"C":
                            self.approved.set()
                        elif not self.finished.is_set():
                            os.kill(os.getpid(), signal.SIGTERM)
                    except OSError:
                        if not self.finished.is_set():
                            os.kill(os.getpid(), signal.SIGTERM)
                threading.Thread(target=watch, name="ghostdeck-command-lease", daemon=True).start()
        except BaseException:
            self.finished.set()
            _active = None
            for sig, handler in self.handlers.items():
                signal.signal(sig, handler)
            for fd in (self.parent, self.ready, self.gate):
                _close(fd)
            if self.decision is not None:
                self.decision.close()
            raise
        return self

    def spawn(self, argv, **kwargs):
        """Register atomically with respect to cooperative signals; EOF covers SIGKILL."""
        self.check()
        (lease, writer), (ack, answer) = _pipes()
        env = dict(kwargs.pop("env", os.environ))
        env.update(GHOSTDECK_CHILD_LEASE=str(lease), GHOSTDECK_CHILD_GATE=str(self.gate),
                   GHOSTDECK_CHILD_ACK=str(answer), GHOSTDECK_CHILD_DECISION=str(self.decision.fileno()))
        # Vendor scripts are also runnable standalone; only managed children import us.
        src = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
        try:
            proc = subprocess.Popen(argv, env=env, pass_fds=(lease, self.gate, answer, self.decision.fileno()), **kwargs)
            self.children.append([proc, writer, ack])
        except BaseException:
            _close(writer)
            _close(ack)
            raise
        finally:
            _close(lease)
            _close(answer)
        self.check()
        return proc

    def commit(self):
        self.check()
        if self.parent is not None:
            # The GUI arbitrates timeout/cancel versus success before detaching children.
            os.write(self.ready, b"R")
            while not self.approved.wait(0.05):
                self.check()
        self.check()
        # One shared atomic decision, not per-child commits. A parent killed midway
        # through closing pipes cannot detach a player while cancelling its bridge.
        blocked = signal.pthread_sigmask(signal.SIG_BLOCK, self.handlers)
        try:
            self.check()
            os.pwrite(self.decision.fileno(), b"C", 0)
            self.committed = True
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
        for child in self.children:
            writer, child[1] = child[1], None
            _close(writer)
        for proc, writer, ack in self.children:
            if proc.poll() is not None:
                continue
            if not select.select([ack], [], [], 5.0)[0] or os.read(ack, 1) != b"C":
                raise RuntimeError("startup handoff was committed but is still unconfirmed; wait before retrying")

    def __exit__(self, kind, error, tb):
        global _active
        pending = False
        try:
            if kind is None:
                try:
                    self.commit()
                except BaseException:
                    self.cancelled = not self.committed
                    raise
            else:
                self.cancelled = True
        finally:
            # Closing leases is the cancellation request. No force kill and no foreign PID.
            for proc, writer, ack in self.children:
                _close(writer)
                _close(ack)
            if self.cancelled:
                deadline = time.monotonic() + self.cleanup_wait
                for proc, _, _ in reversed(self.children):
                    try:
                        proc.wait(timeout=max(0, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired:
                        pending = True
            self.finished.set()
            _close(self.ready)
            # A blocked lease-reader is daemonized; close is safe once it has consumed C/EOF.
            _close(self.parent)
            _active = None
            for sig, handler in self.handlers.items():
                signal.signal(sig, handler)
            # Close, never LOCK_UN: children share this open-file description and retain it.
            _close(self.gate)
            if self.decision is not None:
                self.decision.close()
            if pending:
                raise RuntimeError("startup cleanup is still pending; restoration continues without SIGKILL; wait before retrying") from None
        return False


def spawn(argv, **kwargs):
    return _active.spawn(argv, **kwargs) if _active is not None else subprocess.Popen(argv, **kwargs)


def managed():
    return _active is not None


def check():
    if _active is not None:
        _active.check()


def command(name):
    return Command() if name in COMMANDS else contextlib.nullcontext()


def run_command(argv, *, env, timeout, cancel=None):
    """GUI-side lease owner. A timeout requests unwind; it never kills the CLI.

    Returns (returncode, stdout, stderr). 75 means an owned command/restoration
    is still running; the inherited gate prevents another mutation even after GUI exit.
    """
    (parent, writer), (reader, ready) = _pipes()
    child_env = dict(env, GHOSTDECK_COMMAND_LEASE=str(parent), GHOSTDECK_COMMAND_READY=str(ready))
    proc = None
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=child_env,
                                pass_fds=(parent, ready), start_new_session=True)
    finally:
        _close(parent)
        _close(ready)
        if proc is None:
            _close(writer)
            _close(reader)
    deadline = time.monotonic() + timeout
    reason = ""
    committed = False
    try:
        while True:
            if not reason and not committed:
                if cancel is not None and cancel.is_set():
                    reason = "cancelled"
                elif time.monotonic() >= deadline:
                    reason = "timed out"
                if reason:
                    _close(writer)
                    writer = None
                    deadline = time.monotonic() + CLEANUP_WAIT + 2
                elif select.select([reader], [], [], 0)[0]:
                    if os.read(reader, 1) == b"R":
                        os.write(writer, b"C")
                        committed = True
                        # Handoff is bounded too, but never force-kill restoration.
                        deadline = time.monotonic() + CLEANUP_WAIT + 2
            try:
                out, err = proc.communicate(timeout=0.05)
                if reason:
                    err = reason + ("\n" + err.rstrip() if err else "")
                code = 75 if "still pending" in (err or "") or "still unconfirmed" in (err or "") else proc.returncode or (1 if reason else 0)
                return code, out or "", err or ""
            except subprocess.TimeoutExpired:
                if (reason or committed) and time.monotonic() >= deadline:
                    # Keep draining/reaping precisely this child after the bounded GUI wait.
                    threading.Thread(target=proc.communicate, name="ghostdeck-cleanup-reaper", daemon=True).start()
                    return 75, "", (reason or "command handoff") + "; startup cleanup is still pending; wait before retrying"
    finally:
        _close(writer)
        _close(reader)

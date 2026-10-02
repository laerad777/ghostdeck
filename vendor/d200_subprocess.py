"""Source helpers supervised in exact, private process groups.

The group leader remains alive until the player terminates the whole group, even if a
probe exits before its helpers. EOF also terminates helpers if the player itself dies.
No bridge, player or device-restoration owner is ever in these disposable groups.
"""
import os
import json
from pathlib import Path
import select
import signal
import subprocess
import sys
import threading
import time


def _guard(status, lease, argv):
    os.set_inheritable(status, False)
    os.set_inheritable(lease, False)
    # A caught handler resets to default across exec in the actual source tool.
    signal.signal(signal.SIGTERM, lambda *_: None)
    def watch():
        os.read(lease, 1)
        os.killpg(os.getpid(), signal.SIGTERM)
        time.sleep(.1)
        os.killpg(os.getpid(), signal.SIGKILL)
    threading.Thread(target=watch, daemon=True).start()
    try:
        try:
            result = {"code": subprocess.Popen(argv).wait()}
        except OSError as error:
            result = {"errno": error.errno, "message": error.strerror, "filename": error.filename}
        except BaseException as error:
            result = {"error": f"source supervisor failed: {type(error).__name__}"}
        try:
            os.write(status, json.dumps(result).encode() + b"\n")
        except OSError:
            pass  # Parent death still needs the non-exiting guard's group cleanup.
        finally:
            os.close(status)
    finally:
        # Never exit while the lease watcher might still be in its TERM grace.
        while True:
            time.sleep(3600)



class SourceProcesses:
    def __init__(self):
        self.lock = threading.RLock()
        self.children = {}
        self.cancelled = threading.Event()
        self.spawning = 0

    def run(self, argv, *, check=True, capture_output=False, text=False, timeout=90):
        with self.lock:
            if self.cancelled.is_set():
                raise InterruptedError("source probes cancelled")
            status, answer = os.pipe()
            try:
                lease, writer = os.pipe()
            except BaseException:
                os.close(status)
                os.close(answer)
                raise
            self.spawning += 1
            try:
                child = subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve()), str(answer), str(lease), *argv],
                    stdout=subprocess.PIPE if capture_output else None,
                    stderr=subprocess.PIPE if capture_output else None, text=text,
                    start_new_session=True, pass_fds=(answer, lease),
                )
                child_lock = threading.Lock()
                self.children[child] = (child_lock, writer)
            except BaseException:
                os.close(writer)
                os.close(status)
                raise
            finally:
                os.close(answer)
                os.close(lease)
                self.spawning -= 1
        deadline = time.monotonic() + timeout
        try:
            while True:
                if self.cancelled.is_set():
                    raise InterruptedError("source probes cancelled")
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(argv, timeout)
                if select.select([status], [], [], 0)[0]:
                    raw = os.read(status, 4096)
                    if not raw:
                        raise RuntimeError("source supervisor exited without a result")
                    report = json.loads(raw)
                    break
                # Drain pipe output while waiting, but the guard deliberately cannot exit yet.
                try:
                    with child_lock:
                        child.communicate(timeout=.05)
                    raise RuntimeError("source supervisor exited before its result")
                except subprocess.TimeoutExpired:
                    pass
            self.stop(child)
            with child_lock:
                out, err = child.communicate(timeout=2)
            if "errno" in report:
                raise OSError(report["errno"], report["message"], report["filename"])
            if "error" in report:
                raise RuntimeError(report["error"])
            result = subprocess.CompletedProcess(argv, report["code"], out, err)
            if check:
                result.check_returncode()
            return result
        finally:
            self.stop(child)
            os.close(status)
            with self.lock:
                self.children.pop(child, None)

    def stop(self, child):
        with self.lock:
            entry = self.children.get(child)
        if entry is None:
            return
        lock, _ = entry
        with lock:
            with self.lock:
                entry = self.children.get(child)
                if entry is None:
                    return
                lock, writer = entry
                self.children[child] = (lock, None)
            if writer is not None:
                # EOF triggers the guard's private-group TERM→KILL and holds its PGID
                # until then. No PID census, reaped-PID signal, or concurrent wait race.
                os.close(writer)
            child.wait(timeout=3)

    def close(self):
        self.cancelled.set()
        with self.lock:
            children = tuple(self.children)
        errors = []
        for child in children:
            try:
                self.stop(child)
            except (OSError, subprocess.TimeoutExpired) as error:
                errors.append(error)
        if errors:
            raise RuntimeError("source subprocess cleanup is unproven") from errors[0]


if __name__ == "__main__":
    _guard(int(sys.argv[1]), int(sys.argv[2]), sys.argv[3:])

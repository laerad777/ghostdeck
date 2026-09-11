"""Regression proof for FIX-5-T12 -- bridge bring-up instrumentation and revive cost.

Measured on the attached deck (2026-09-11), `studio._ensure_bridge()` took 22-24s
from an already-ADB deck against 5.5s from HID, while the bridge was respawned and
its own log recorded nine `device proxy socket did not become ready` TimeoutErrors
out of `_start`, 56 `transport_revive`, and 60 dropped streams. Playback worked once
it converged, so this is a slow-and-noisy bring-up, not a broken one.

Two things are pinned here, both device-free:

1. **A start that never becomes ready is one greppable line, not a traceback.**
   `_start` raising `TimeoutError` used to escape `main()` as an unhandled exception
   whose last frame named `_start` and said nothing about why, so the nine failures
   in that log could not be told apart. Now each attempt emits a
   `bridgeStartupAttempt` record carrying the attempt ordinal, the staging seconds,
   the readiness seconds and the last device command issued.

2. **A revive does not re-push ~77 KB when the deck provably still holds it.**
   Every dropped stream used to re-run the whole of `_stage()`. The check is
   fail-closed: only a clean, parseable, exactly-matching size report skips the
   pushes, and a mismatch/absence/garbage re-stages.

No device, no real `adb` (every run passes a fake one), no `/tmp/d200-*`, and HOME
is redirected so the admission lock is a scratch file too.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor"
BRIDGE_PATH = VENDOR / "d200-local-bridge.py"

sys.path.insert(0, str(VENDOR))

spec = importlib.util.spec_from_file_location("d200_local_bridge_startup", BRIDGE_PATH)
bridge = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bridge
spec.loader.exec_module(bridge)

# Stand-in artifact sizes. Deliberately not derived from anything the code computes,
# so a match below is a real match rather than a restatement of the code's own numbers.
PROXY_BYTES = b"p" * 96
PRELOAD_BYTES = b"l" * 48
BUILD_ARTIFACTS = ("d200-zkgui-proxy", "d200-color-agent", "libd200-zkgui-preload.so")
PUSH_MARKER = "push"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """No test here may touch the operator's real state root or admission lock."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture()
def artifacts(tmp_path):
    proxy_binary = tmp_path / "d200-zkgui-proxy"
    preload_library = tmp_path / "libd200-zkgui-preload.so"
    proxy_binary.write_bytes(PROXY_BYTES)
    preload_library.write_bytes(PRELOAD_BYTES)
    (tmp_path / "d200-color-agent").write_bytes(b"a" * 16)
    return proxy_binary, preload_library


class RecordingProxy(bridge.DeviceProxy):
    """DeviceProxy with adb replaced by a recorded command list."""

    def __init__(self, *arguments, size_report=None, size_error=None, **keywords):
        super().__init__(*arguments, **keywords)
        self.commands = []
        self.size_report = size_report
        self.size_error = size_error

    def _run(self, *arguments, timeout=15):
        self.commands.append((arguments, timeout))
        if arguments and arguments[0] == "shell" and "wc -c" in str(arguments[1]):
            if self.size_error is not None:
                raise self.size_error
            return subprocess.CompletedProcess([], 0, stdout=self.size_report)
        return subprocess.CompletedProcess([], 0, stdout=b"")

    def pushes(self):
        return [arguments for arguments, _timeout in self.commands
                if arguments and arguments[0] == PUSH_MARKER]


def make_proxy(artifacts, **keywords):
    proxy_binary, preload_library = artifacts
    return RecordingProxy("unused-adb", "unused", proxy_binary, preload_library, **keywords)


def full_report(proxy, *, proxy_bytes=None, preload_bytes=None):
    """The deck's answer for an intact session directory, as the shell prints it."""
    proxy_bytes = proxy.proxy_binary.stat().st_size if proxy_bytes is None else proxy_bytes
    preload_bytes = (proxy.preload_library.stat().st_size if preload_bytes is None
                     else preload_bytes)
    return (f"proxy|{proxy_bytes}\npreload.so|{preload_bytes}\n").encode()


# --- behaviour 2: the revive does not re-push what is already there ----------------


def test_a_revive_reuses_the_staged_artifacts_and_does_not_repush(artifacts):
    """56 revives × (rm -rf + mkdir + 2 pushes + chmod) was the observed churn."""
    proxy = make_proxy(artifacts)
    proxy.size_report = full_report(proxy)

    restaged = proxy._ensure_staged_for_revive()

    assert restaged is False
    assert proxy.pushes() == [], (
        "an intact session directory must not be re-pushed on every dropped stream"
    )
    # The modes are the part the size report does not prove, so they are re-applied.
    assert [arguments[0] for arguments, _ in proxy.commands].count("shell") >= 2
    modes = [arguments[1] for arguments, _ in proxy.commands
             if arguments[0] == "shell" and "chmod" in str(arguments[1])]
    assert modes and f"chmod 700 {proxy.remote_dir}" in modes[0]


@pytest.mark.parametrize("shape", ["size-mismatch", "missing-entry", "empty", "garbage",
                                   "command-error"])
def test_a_revive_restages_whenever_the_check_cannot_prove_intact(artifacts, shape):
    """Fail closed: every uncertainty must produce a real stage, never a silent skip."""
    proxy = make_proxy(artifacts)
    if shape == "size-mismatch":
        proxy.size_report = full_report(proxy, proxy_bytes=len(PROXY_BYTES) - 1)
    elif shape == "missing-entry":
        proxy.size_report = f"proxy|{len(PROXY_BYTES)}\n".encode()
    elif shape == "empty":
        proxy.size_report = b""
    elif shape == "garbage":
        proxy.size_report = b"proxy|not-a-number\npreload.so|48\n"
    else:
        proxy.size_error = bridge.DeviceCommandError(1)

    restaged = proxy._ensure_staged_for_revive()

    assert restaged is True, f"{shape} must re-stage"
    pushed = [arguments[2] for arguments in proxy.pushes()]
    assert f"{proxy.remote_dir}/proxy" in pushed, f"{shape}: the proxy was not re-pushed"


def test_the_reuse_check_reads_the_deck_and_never_a_stale_local_number(artifacts):
    """The decision must come from the deck's own report, not from the local files."""
    proxy = make_proxy(artifacts)
    proxy.size_report = full_report(proxy)

    assert proxy._staged_entries_present() is True
    proxy.size_report = full_report(proxy, preload_bytes=len(PRELOAD_BYTES) + 1)
    assert proxy._staged_entries_present() is False

    # And the query names only the two staged entries, via the vocabulary the
    # sibling listing already relies on (no stat(1) on the deck).
    query = proxy._staged_entries_query()
    assert "wc -c" in query and "ls -A" not in query
    assert bridge.SESSION_DIR_PROXY in query and bridge.SESSION_DIR_PRELOAD in query


# --- behaviour 1: a start that never becomes ready is one line ---------------------


def test_a_start_that_never_becomes_ready_emits_one_diagnostic_and_raises(
        artifacts, tmp_path, monkeypatch, capfd):
    """Drives the real `_start` against a real (fake) adb; the forwarded port is dead.

    The adb stub is a real script rather than a `_run` override, so the diagnostic's
    `lastDeviceCommand` is produced by the shipped `_run` and not by the test.
    """
    monkeypatch.setattr(bridge, "PROXY_READINESS_SECONDS", 0.3)
    adb = tmp_path / "adb"
    adb.write_text(
        "#!/bin/sh\n"
        "for a in \"$@\"; do [ \"$a\" = forward ] && { echo 1; exit 0; }; done\n"
        "exit 0\n"
    )
    adb.chmod(0o755)
    proxy_binary, preload_library = artifacts
    proxy = bridge.DeviceProxy(str(adb), "unused", proxy_binary, preload_library)

    with pytest.raises(bridge.StartupError) as raised:
        proxy._start()

    assert "did not become ready" in str(raised.value)
    assert isinstance(raised.value, RuntimeError), "callers catching RuntimeError keep working"

    records = [json.loads(line) for line in capfd.readouterr().err.splitlines()
               if line.startswith("{") and "bridgeStartupAttempt" in line]
    assert len(records) == 1, f"exactly one record per start attempt, got {records}"
    record = records[0]
    assert set(record) == {"event", "pid", "attempt", "clock", "stageSeconds",
                           "readinessSeconds", "outcome", "failure", "lastDeviceCommand"}
    assert record["event"] == "bridgeStartupAttempt"
    assert record["attempt"] == 1
    assert record["outcome"] == "failed"
    assert record["failure"] == "TimeoutError"
    assert isinstance(record["stageSeconds"], float)
    assert record["readinessSeconds"] >= 0.3
    # The attribution half: which command was in flight when it gave up.
    assert record["lastDeviceCommand"]["verb"] == "forward"
    assert record["lastDeviceCommand"]["outcome"] == "ok"


def test_the_attempt_ordinal_counts_up_and_a_successful_stage_reports_zero_readiness(
        artifacts, tmp_path, monkeypatch, capfd):
    """A stage failure is still one record, with a null readiness half."""
    proxy = make_proxy(artifacts)
    attempts = [proxy._begin_startup_attempt(), proxy._begin_startup_attempt()]
    assert attempts == [1, 2]

    started = bridge.time.monotonic()
    proxy._report_startup(2, started, None, bridge.StagingError("no"))
    record = json.loads([line for line in capfd.readouterr().err.splitlines()
                         if "bridgeStartupAttempt" in line][0])
    assert record["attempt"] == 2
    assert record["readinessSeconds"] is None
    assert record["failure"] == "StagingError"


def test_the_diagnostic_never_carries_device_supplied_text(artifacts):
    """`self.error` is set from a device frame; it must not reach the user-facing line."""
    device_text = "peer-supplied text that must not be forwarded"
    for error in (TimeoutError("x"), TimeoutError("x"), bridge.ProtocolError(device_text),
                  RuntimeError(device_text), EOFError(device_text)):
        reason = bridge.DeviceProxy._startup_failure_reason(error)
        assert device_text not in reason
        assert "\n" not in reason


# --- end to end: the real `main()` against a proxy that never becomes ready --------


@pytest.fixture()
def deckless_bridge(tmp_path):
    """A copy of the real bridge with a fake adb whose forwarded port never answers."""
    target = tmp_path / "deckless"
    target.mkdir()
    for name in ("d200-local-bridge.py", "d200_process_control.py", "d200_video_stream.py"):
        shutil.copy(VENDOR / name, target / name)
    for name in BUILD_ARTIFACTS:
        (target / name).write_bytes(b"x" * 32)
    adb = target / "adb"
    # Accepts every staged command, allocates a forwarding port, and never serves it,
    # so the proxy-readiness wait is guaranteed to expire.
    adb.write_text("#!/bin/sh\nfor a in \"$@\"; do [ \"$a\" = forward ] && { echo 1; exit 0; }; done\nexit 0\n")
    adb.chmod(0o755)
    return target


@pytest.fixture()
def short_scratch():
    """A short absolute path: an AF_UNIX endpoint lives in 104 bytes, and a
    `tmp_path` under `/private/var/folders/...` is longer than that -- which makes
    `socket_listener_live` answer "cannot be proven dead" and correctly refuses."""
    directory = Path(tempfile.mkdtemp(prefix="vendorbridge-startup-", dir="/tmp"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_a_proxy_that_never_becomes_ready_exits_with_one_line_and_no_traceback(
        deckless_bridge, short_scratch, isolated_home):
    """The nine TimeoutErrors in the real-deck log were tracebacks; this is the regression.

    The readiness bound is shortened through the module constant so the test does not
    pay the shipped 15s; the shipped value is asserted separately below.
    """
    runner = short_scratch / "runner.py"
    runner.write_text(
        "import importlib.util, sys\n"
        f"sys.path.insert(0, {str(deckless_bridge)!r})\n"
        f"spec = importlib.util.spec_from_file_location('b', {str(deckless_bridge / 'd200-local-bridge.py')!r})\n"
        "bridge = importlib.util.module_from_spec(spec)\n"
        "sys.modules['b'] = bridge\n"
        "spec.loader.exec_module(bridge)\n"
        "bridge.PROXY_READINESS_SECONDS = 1.0\n"
        "sys.argv = ['d200-local-bridge.py', '--socket', sys.argv[1], '--serial', 'unused',\n"
        "            '--adb', sys.argv[2]]\n"
        "bridge.main()\n",
        encoding="utf-8",
    )
    socket_path = short_scratch / "bridge.sock"
    result = subprocess.run(
        [sys.executable, str(runner), str(socket_path), str(deckless_bridge / "adb")],
        capture_output=True, text=True, timeout=90,
        env=dict(os.environ, HOME=str(isolated_home)),
    )

    assert result.returncode == 1, result.stderr
    assert "bridge_startup_failed error=" in result.stderr
    assert "did not become ready" in result.stderr
    assert "Traceback" not in result.stderr, "the H4 discipline must cover this path too"
    assert "line 674" not in result.stderr
    # The attribution half: one machine-readable record per attempt, greppable.
    records = [json.loads(line) for line in result.stderr.splitlines()
               if line.startswith("{") and "bridgeStartupAttempt" in line]
    assert records and records[0]["outcome"] == "failed"
    assert records[0]["failure"] == "TimeoutError"
    assert not socket_path.exists(), "a bridge that never became ready must not bind its socket"


def test_the_shipped_readiness_bound_is_the_documented_fifteen_seconds():
    """The bound is a contract with the host's own wait; changing it is a decision, not a tweak."""
    assert bridge.PROXY_READINESS_SECONDS == 15.0

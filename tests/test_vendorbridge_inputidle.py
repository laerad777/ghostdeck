"""Regression proof for B-113: a blocking read must not be killed by the host budget.

The macOS shim enforces one host-side transport budget per request
(`reference/hidshim.c` `D200_RPC_BUDGET_MS`, 15 s) and passes the peer's own
`timeoutMs` through unchanged. `hid_read()` in its default blocking mode sends
`timeoutMs: -1`, and the bridge's `input()` used to park on that until a report
arrived -- so on a *healthy but idle* deck the shim's budget expired first and the
caller saw `rc=-1 errno=ETIMEDOUT` every 15 s, with the bridge left holding a
parked handler thread per expired read. The fix serves the blocking wait in
bounded windows (`INPUT_IDLE_TICK_SECONDS`) and answers the same empty report an
expired positive timeout already returned.

Everything here is device-free: a `DeviceProxy` that was never started (its queues
are empty, which is exactly the idle case), and for the wire test a real
`BridgeServer` on a scratch socket under pytest's `tmp_path`. No adb, no device,
no `/tmp/d200-*` path, and HOME is redirected so the admission lock is scratch too.

The compiled proof of the shim side is `/tmp/f5-t12/idletick` (fixed peer:
`rc=0 elapsed_ms=504`; pre-fix peer: `rc=-1 errno=60 elapsed_ms=15001` with one
peer thread parked forever); this file pins the bridge-side behaviour it mirrors.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import shutil
import socket
import sys
import tempfile
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor"
BRIDGE_PATH = VENDOR / "d200-local-bridge.py"
SHIM_PATH = ROOT / "reference" / "hidshim.c"

sys.path.insert(0, str(VENDOR))

spec = importlib.util.spec_from_file_location("d200_local_bridge_inputidle", BRIDGE_PATH)
bridge = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bridge
spec.loader.exec_module(bridge)

TICK = bridge.INPUT_IDLE_TICK_SECONDS
# Generous over the tick: the assertion is "bounded, and by the tick", not a
# scheduler measurement.
JOIN = TICK * 6


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def make_proxy(tmp_path):
    proxy = bridge.DeviceProxy(
        str(tmp_path / "no-such-adb"), "unused",
        tmp_path / "d200-zkgui-proxy", tmp_path / "libd200-zkgui-preload.so",
    )
    return proxy


@pytest.fixture()
def short_scratch():
    """An AF_UNIX endpoint lives in 104 bytes, so the wire test needs a short path."""
    directory = Path(tempfile.mkdtemp(prefix="vendorbridge-inputidle-", dir="/tmp"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def call_in_thread(function):
    """Run `function` in a daemon thread and report (returned, value, elapsed)."""
    result = {}
    started = time.monotonic()

    def run():
        try:
            result["value"] = function()
        except BaseException as error:  # reported, not swallowed
            result["error"] = error

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(JOIN)
    return {
        "finished": not worker.is_alive(),
        "value": result.get("value"),
        "error": result.get("error"),
        "elapsed": time.monotonic() - started,
    }


def test_a_blocking_read_is_answered_within_the_idle_window(tmp_path):
    """B-113: `input(-1)` must answer, not park until the caller's budget expires."""
    proxy = make_proxy(tmp_path)

    outcome = call_in_thread(lambda: proxy.input(0, -1, lambda: False))

    assert outcome["finished"], (
        "a blocking input() parked past the idle window and its caller's budget")
    assert outcome["error"] is None
    assert outcome["value"] == b"", "the window answers the empty report"
    assert outcome["elapsed"] < JOIN, outcome["elapsed"]


def test_a_queued_report_is_returned_immediately(tmp_path):
    """The window must not add latency to a real report."""
    proxy = make_proxy(tmp_path)
    proxy.inputs[0].append(b"\x01report")

    started = time.monotonic()
    report = proxy.input(0, -1, lambda: False)

    assert report == b"\x01report"
    assert time.monotonic() - started < TICK, "a pending report must not wait for the window"


def test_an_expired_positive_timeout_still_answers_the_empty_report(tmp_path):
    """The pre-existing contract the blocking case now mirrors."""
    proxy = make_proxy(tmp_path)

    started = time.monotonic()
    report = proxy.input(0, 10, lambda: False)

    assert report == b""
    assert time.monotonic() - started < TICK, "a 10 ms timeout must not become a window"


def test_the_window_does_not_swallow_an_error_or_a_close(tmp_path):
    """The error/closing checks still precede the window, so teardown stays prompt."""
    for attribute, value, message in (("error", "transport died", "transport died"),
                                      ("closed", True, "device proxy closed")):
        proxy = make_proxy(tmp_path)
        setattr(proxy, attribute, value)
        started = time.monotonic()
        with pytest.raises(RuntimeError) as raised:
            proxy.input(0, -1, lambda: False)
        assert message in str(raised.value)
        assert time.monotonic() - started < TICK


def test_cancellation_still_raises_promptly(tmp_path):
    proxy = make_proxy(tmp_path)
    cancelled = threading.Event()
    raised = []

    def wait():
        try:
            proxy.input(0, -1, cancelled.is_set)
        except RuntimeError as error:  # the documented close signal, not a test failure
            raised.append(str(error))

    thread = threading.Thread(target=wait, daemon=True)
    thread.start()
    time.sleep(TICK / 5)
    cancelled.set()
    thread.join(JOIN)

    assert not thread.is_alive(), "a closed handle must end a blocking read"
    assert raised == ["virtual HID handle closed"]


def test_the_window_is_well_inside_the_shim_host_budget():
    """The pairing that makes the fix work, pinned across the two files.

    A window at or above the shim's `D200_RPC_BUDGET_MS` would reintroduce B-113;
    the margin here is what keeps a healthy idle deck from looking like a timeout.
    """
    budget = int(re.search(
        r"^#define D200_RPC_BUDGET_MS (\d+)$", SHIM_PATH.read_text(), re.MULTILINE).group(1))

    assert TICK * 1000 * 4 <= budget, (
        f"an idle window of {TICK * 1000:.0f} ms is too close to the shim's {budget} ms budget")


def test_the_wire_answer_for_a_blocking_read_is_the_empty_report(tmp_path, short_scratch):
    """End to end through the real handler, with no device involved.

    `open` is served by the bridge's own handle registry, and `input` reads the
    (empty) queue, so the whole request/response pair is real. The client sends
    the exact JSON the shim's `rpc()` builds for a blocking read, and asserts the
    exact reply its `parse_reply()` accepts: an empty `report` string.
    """
    socket_path = short_scratch / "bridge.sock"
    proxy = make_proxy(tmp_path)
    state = bridge.BridgeState(proxy)
    server = bridge.BridgeServer(socket_path, state)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                              daemon=True)
    worker.start()

    def request(payload):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(JOIN)
            client.connect(str(socket_path))
            client.sendall(json.dumps(payload, separators=(",", ":")).encode() + b"\n")
            return json.loads(client.makefile("rb").readline())

    try:
        opened = request({"schemaVersion": 1, "op": "open", "handle": 7, "interface": 0,
                          "timeoutMs": -1, "report": ""})
        assert opened["accepted"] is True
        capability = opened["capability"]

        started = time.monotonic()
        answered = request({"schemaVersion": 1, "op": "input", "handle": 7,
                            "interface": 0, "timeoutMs": -1, "report": "",
                            "capability": capability})
        elapsed = time.monotonic() - started
    finally:
        server.shutdown()
        server.server_close()

    assert answered == {"schemaVersion": 1, "accepted": True, "report": ""}
    assert elapsed < JOIN, "the blocking request must be answered inside the window"
    assert elapsed > TICK / 2, "and not before the window elapses"

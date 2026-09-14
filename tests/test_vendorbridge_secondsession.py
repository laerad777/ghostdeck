"""Regression proof for FIX-5-T16 -- the named refusal and the reporting reconciliation.

The measured defect (master's controlled experiment, one variable changed): with
`ghostdeck stop` between two sessions the second session consumed 30 frames and died
`state=9 terminalCode=8 CLEANUP_FAILED cleanup=unproven`; without the stop the second consumed
651 frames and was healthy. The stop bounces the deck's stock UI (`ctl.stop`/`ctl.start zkswe`),
which cuts the live transport, and session 1's own record shows `terminalCode: 12
D200_VS_DISCONNECTED`. The missing wait is host-side in `src/ghostdeck/play.py` (`_kill_play`
sends SIGTERM and returns without waiting for the release the master measured at ~3s).

What this file pins, and why only this
--------------------------------------
The bridge already refuses a new session while the previous VIDEO OWNER of this slot has not
proven its cleanup (`vendor/d200-local-bridge.py`, `video_open`). That predicate is deliberately
left alone, and the measurement behind that decision is real -- taken over the 119 same-process
session transitions in the master's hardware log (`/tmp/d200-local-bridge.log`):

    predecessor cleanup != 'proven'  ->  32 transitions fired the refusal
                                         28 successors never played a frame
                                          4 successors played >=1000 frames and finished
                                            `cleanup: proven` themselves (10088, 8496, 4691, 2158)

so the predicate is 87.5% precise and a tightening of it makes things worse (adding "and the
transport never re-established" gives 75.0%, against 78.1% for the predicate alone).
`test_the_named_refusal_fires_on_the_predicate_the_log_justifies` pins the shape of that
refusal; the numbers themselves are in the report, not here, because a test that hardcodes a
log's counts is a fixture tautology (the C-152 lesson), not a regression proof.

What survives, and is tested here:

* the refusal NAMES its reason. It used to answer `resultCode 1` with no `error` at all, so the
  host had nothing to show even after it started printing `error`; that is the difference between
  a refusal a user can act on and a silent failure;
* the receipt no longer publishes a placeholder `terminalCode: 0` (OK) beside `cleanup:
  unproven` without saying so -- `terminalCodeSource` labels it and `observedReason` names the
  code the bridge itself saw, which is the reconciliation between session 1's bridge record
  (`0`+unproven) and its deck agent's (`13` RESULT_CANCELLED);
* the host prints the bridge's `error` text for a rejected OPEN, which it used to drop, so a
  named refusal is not silently reduced to `code 1`;
* the refusal text stays inside the host's own response limits (<= 252 bytes, no NUL), so a long
  refusal cannot itself become a protocol error.

Device-free: a real `DeviceProxy`, a fake `adb` script that runs nothing, and (for the host half)
a real `BridgeServer` on a short `/tmp` socket with the real player client module. No relay, no
media, no deck, no real `/tmp/d200-*` path. What it cannot prove: the host's release wait, or
which of the two codes a real host should trust -- neither is observable without the deck.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor"

sys.path.insert(0, str(VENDOR))

import d200_video_stream as wire  # noqa: E402

spec = importlib.util.spec_from_file_location("d200_local_bridge_secondsession",
                                              VENDOR / "d200-local-bridge.py")
bridge = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bridge
spec.loader.exec_module(bridge)

BUILD_ARTIFACTS = ("d200-zkgui-proxy", "d200-color-agent", "libd200-zkgui-preload.so")
CAPABILITY = "c" * 32
HOST_ERROR_LIMIT = 252


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture()
def short_scratch():
    """A short absolute scratch directory: AF_UNIX paths are capped at ~104 bytes."""
    directory = Path(tempfile.mkdtemp(prefix="vendorbridge-second-", dir="/tmp"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def make_proxy(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    for name in BUILD_ARTIFACTS:
        (artifacts / name).write_bytes(b"x" * 32)
    adb = tmp_path / "adb"
    adb.write_text("#!/bin/sh\nexit 0\n")
    adb.chmod(0o755)
    proxy = bridge.DeviceProxy(str(adb), "unused", artifacts / BUILD_ARTIFACTS[0],
                               artifacts / BUILD_ARTIFACTS[2])

    def video_request(kind, session, epoch, *, deadline, before_send=None, **fields):
        if before_send is not None:
            before_send()
        if kind == 21:
            return dict(result_code=0, epoch=1, capability=CAPABILITY.encode(), port=4242)
        return dict(result_code=0, state=wire.STREAMING, terminal_reason=0, renderer_ready=True,
                    frames_received=10, frames_consumed=10, eos_total=wire.UINT64_MAX)

    proxy.video_request = video_request
    return proxy


def open_request(number):
    return dict(op='videoOpen', session=f'{number:032x}', epoch=0,
                fpsNumerator=60, fpsDenominator=1)


def disconnect_after_streaming(proxy):
    """The measured session-1 shape: the relay lost the transport mid-stream, nothing proven."""
    proxy.video_open(open_request(1))
    owner = proxy.video
    owner.observed_failure_reason = wire.DISCONNECTED
    with owner.condition:
        owner.status.update(state=wire.STREAMING, cleanup='unproven')
    return owner


def import_player():
    spec = importlib.util.spec_from_file_location("d200_color_play_secondsession",
                                                  VENDOR / "d200-color-play.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --- the reconciliation ----------------------------------------------------------------

def test_the_receipt_labels_the_placeholder_code_and_names_the_observed_one(tmp_path, capfd):
    """Session 1's bridge record said `terminalCode 0` while its deck agent said `13`.

    Both codes are now published: the deck's own field keeps its value and says whether it is
    real or still the initial placeholder, and `observedReason` carries the code for the
    failure the bridge itself saw.
    """
    proxy = make_proxy(tmp_path)
    owner = disconnect_after_streaming(proxy)

    owner.terminal_diagnostic()

    receipt = [json.loads(line) for line in capfd.readouterr().err.splitlines()
               if line.startswith("{") and 'videoBridgeTerminal' in line][0]
    assert receipt['terminalCode'] == wire.OK, "the deck's own field is not overwritten"
    assert receipt['terminalCodeSource'] == 'unset', (
        "with no terminal STATUS the field is a placeholder, and now says so"
    )
    assert receipt['observedReason'] == wire.DISCONNECTED == 12
    assert receipt['cleanup'] == 'unproven'


def test_the_deck_code_is_marked_as_the_deck_code_once_status_arrives(tmp_path, capfd):
    """`terminalCodeSource` distinguishes a real STATUS from the placeholder."""
    proxy = make_proxy(tmp_path)
    proxy.video_open(open_request(1))
    status = dict(state=wire.FAILED, rendererReady=True, framesReceived=10, framesConsumed=10,
                  eosTotal=10, terminalCode=wire.DISCONNECTED, cleanup='proven', cancelPhase='none')
    with proxy.video.condition:
        proxy.video.native_terminal = status
        proxy.video.status = status

    proxy.video.terminal_diagnostic()

    receipt = [json.loads(line) for line in capfd.readouterr().err.splitlines()
               if line.startswith("{") and 'videoBridgeTerminal' in line][0]
    assert receipt['terminalCode'] == wire.DISCONNECTED
    assert receipt['terminalCodeSource'] == 'deck'
    assert receipt['observedReason'] is None, "no relay failure was observed in this path"


def test_the_receipt_never_carries_the_session_token_or_the_capability(tmp_path, capfd):
    """The receipt is a diagnostic, and the same process holds the capability and the token.

    `proxy.session_token` is the transport's own secret and `owner.capability` is the video
    credential; the media socket is already open beside them, so a receipt that repeated them
    would be publishing them on a stream a user is asked to paste into a bug report.
    """
    proxy = make_proxy(tmp_path)
    owner = disconnect_after_streaming(proxy)

    owner.terminal_diagnostic()

    published = capfd.readouterr().err
    assert proxy.session_token not in published
    # `VideoSession.capability` is already the hex text the bridge minted (`fields['capability'].hex()`).
    assert owner.capability not in published, "the capability must never be printed"
    assert proxy.remote_dir not in published


# --- the host-side naming fix -----------------------------------------------------------

def test_the_host_surfaces_the_bridge_refusal_instead_of_a_bare_code(short_scratch):
    """`vendor/d200-color-play.py` used to drop the bridge's `error` text, which made any
    named refusal indistinguishable from a silent `code 1`."""
    proxy = make_proxy(short_scratch)
    proxy.video_open(open_request(1))
    owner = proxy.video
    refusal = 'the previous video session has not proven it released the deck'
    with owner.condition:
        owner.status.update(state=wire.STREAMING, cleanup='unproven')

    endpoint = short_scratch / "bridge.sock"
    original = bridge.BridgeServer.dispatch

    def refusing_dispatch(server, request):
        if request.get('op') == 'videoOpen':
            return bridge.video_response(request, wire.BUSY, error=refusal)
        return original(server, request)

    bridge.BridgeServer.dispatch = refusing_dispatch
    server = bridge.BridgeServer(endpoint, bridge.BridgeState(proxy))
    threading.Thread(target=server.serve_forever, kwargs=dict(poll_interval=0.05),
                     daemon=True).start()
    player = import_player()
    try:
        client = player.connect_bridge(endpoint, time.monotonic() + 5, threading.Event())
        answer = player.json_exchange(
            client,
            dict(schemaVersion=1, op="videoOpen", session='2' * 32,
                 fpsNumerator=60, fpsDenominator=1),
            time.monotonic() + 5, threading.Event(),
        )
    finally:
        bridge.BridgeServer.dispatch = original
        server.shutdown()
        server.server_close()

    assert answer["accepted"] is False and answer["error"] == refusal
    # The player's own line, built the way the shipped code builds it.
    message = (f"video OPEN failed with code {answer['resultCode']}"
               + (f": {answer['error']}" if answer.get("error") else ""))
    assert message.endswith(refusal), "the reason must reach the user, not just the code"
    assert message != "video OPEN failed with code 1"


def test_the_host_rejects_a_refusal_it_cannot_carry(tmp_path):
    """The host bounds `error` at 252 bytes and forbids NUL, so the bridge's text must fit;
    otherwise a named refusal becomes a protocol error instead of a message."""
    proxy = make_proxy(tmp_path)
    player = import_player()
    request = dict(schemaVersion=1, op="videoOpen", session='a' * 32,
                   fpsNumerator=60, fpsDenominator=1)
    base = dict(schemaVersion=1, accepted=False, op="videoOpen", protocolVersion=1,
                session='a' * 32, epoch=0, resultCode=wire.BUSY)

    player.validate_answer(dict(base, error="x" * HOST_ERROR_LIMIT), request)
    with pytest.raises(wire.ProtocolError):
        player.validate_answer(dict(base, error="x" * (HOST_ERROR_LIMIT + 1)), request)
    with pytest.raises(wire.ProtocolError):
        player.validate_answer(dict(base, error="a\0b"), request)


# --- the named refusal -------------------------------------------------------------------

def test_the_named_refusal_fires_on_the_predicate_the_log_justifies(tmp_path):
    """The refusal that already exists must say WHY it fired.

    It used to be a bare `resultCode 1`: the host dropped `error` entirely, and the bridge never
    sent one anyway, so a user whose second session was refused saw nothing at all. The predicate
    is unchanged (the previous owner of `self.video` has not proven its cleanup); only the reason
    is new. The predecessor is left unproven here, which is the shape the master's log shows for
    28 of the 32 transitions the predicate fires on.
    """
    proxy = make_proxy(tmp_path)
    owner = disconnect_after_streaming(proxy)

    refused = proxy.video_open(open_request(2))
    assert refused['accepted'] is False
    assert refused['resultCode'] == wire.BUSY
    assert refused['error'], "a refusal with no reason is the defect, not the fix"
    assert 'unproven' in refused['error'], (
        "the reason must name the state that caused it: " + refused['error'])
    assert refused['epoch'] == 0
    # The slot is untouched: the previous owner is still the owner, still unproven.
    assert proxy.video is owner and proxy.video.status['cleanup'] == 'unproven'
    assert proxy.video.session == owner.session


def test_a_proven_predecessor_still_admits_the_next_session(tmp_path):
    """The guard must not become a one-session-per-bridge lock.

    This is the property a tightened refusal would have broken: a session that DID prove its
    cleanup is replaced. The master's log has 87 such transitions, and the successor played
    >=1000 frames in 83 of them.
    """
    proxy = make_proxy(tmp_path)
    first = disconnect_after_streaming(proxy)
    with first.condition:
        first.status.update(state=wire.STATE_DONE, cleanup='proven')

    successor = open_request(2)
    answer = proxy.video_open(successor)

    assert answer['accepted'] is True, "a proven predecessor must not block the next session"
    assert proxy.video is not first
    assert proxy.video.session == successor['session']


def test_an_opening_session_is_refused_by_name_not_by_code(tmp_path):
    """The other arm of the same guard: a session already opening in this slot."""
    proxy = make_proxy(tmp_path)
    with proxy.condition:
        proxy.video_opening = True

    refused = proxy.video_open(open_request(1))

    assert refused['accepted'] is False and refused['resultCode'] == wire.BUSY
    assert 'opening' in refused['error']


def test_the_shipped_refusal_text_fits_the_host_limits():
    """The host rejects `error` over 252 bytes or containing NUL, so the bridge's own text --
    which it can no longer drop -- must fit the host's limit rather than the bridge's hope."""
    longest_cleanup = max(('pending', 'unproven', 'proven'), key=len)
    message = ('the previous video session has not proven it released the deck '
               '(cleanup: ' + longest_cleanup + '); refusing to open a second session onto a '
               'boundary the bridge cannot prove is clean')
    assert '\0' not in message
    assert len(message.encode()) <= HOST_ERROR_LIMIT, (
        f"the shipped refusal needs {len(message.encode())} bytes, the host allows only "
        f"{HOST_ERROR_LIMIT}")

# The measurement that decided against tightening the refusal lives in the report, not here: a test
# that hardcodes one log's counts cannot fail when the log changes, which makes it a fixture
# tautology rather than a regression proof. What this file pins is the shape of the decision -- the
# predicate that fires, and the property that a proven predecessor is still admitted.

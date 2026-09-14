"""`ghostdeck play` must not report a successful start for a player that is not running (A-103).

Everything here is device-free: `devicebuild.ensure`, `vhid.start`, `usb.detect` and
`adb.require_adb` are stubbed, `VENDOR_PLAY` is redirected to a temp script, and HOME points at
`tmp_path`, so nothing is compiled, copied, spawned against a real deck, or written to the
operator's `~/.ghostdeck`.

T19 adds the other half of a start's preconditions: the player cannot stream without the hidshim
bridge, so `play` refuses without one. Those tests redirect the bridge endpoint (`SOCKET`,
`BRIDGE_STATE`) into `tmp_path` and poison `studio._socket_state`; none of them bind a socket, so
they cannot see or disturb a real bridge or a real deck.
"""

from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))


@pytest.fixture
def deck_home(tmp_path, monkeypatch):
    """A stubbed deck path plus a temp HOME, so start_play can reach `Popen` and nothing else.

    T19 added one more precondition to reaching `Popen`: the hidshim bridge must be serving
    `SOCKET`. It is stubbed here like every other precondition (`devicebuild.ensure`, `vhid.start`,
    `usb.detect`, `adb.require_adb`) - liveness and ownership are two lambdas, so no test in this file
    opens a socket, and none can see or disturb a real bridge. A test that wants the refusal
    overrides `_socket_state` with `_ENDPOINT_DEAD`.

    `_require_tools` is stubbed for the same reason (C-157): it is the one precondition that reads
    the ambient PATH, so without this stub the two tests that reach `Popen` pass only on a host that
    happens to have `ffmpeg` installed. The tool hints themselves are covered in
    `test_hostcli_tools.py`.
    """
    from ghostdeck import devicebuild, play, state, studio, usb

    home = tmp_path / "home"
    (home / ".ghostdeck").mkdir(parents=True)
    monkeypatch.setattr(state, "HOME", home / ".ghostdeck")
    monkeypatch.setattr(state, "STATE_PATH", home / ".ghostdeck" / "state.json")
    monkeypatch.setattr(devicebuild, "ensure", lambda: None)
    monkeypatch.setattr(play, "_require_tools", lambda source: None)
    monkeypatch.setattr(play.vhid, "start", lambda: None)
    monkeypatch.setattr(play.adb, "require_adb", lambda: None)
    monkeypatch.setattr(usb, "detect", lambda: {"serial": "FAKESERIAL", "mode": "adb"})
    monkeypatch.setattr(studio, "SOCKET", tmp_path / "bridge.sock")
    monkeypatch.setattr(studio, "BRIDGE_STATE", tmp_path / "bridge.pid")
    monkeypatch.setattr(studio, "_socket_state", lambda: (studio._ENDPOINT_LIVE, ""))
    monkeypatch.setattr(studio, "_bridge_owner_live", lambda: True)
    return home


def _player(tmp_path: Path, body: str) -> Path:
    """A stand-in vendor player. start_play launches it with `sys.executable`, so it is real Python."""
    path = tmp_path / "fakeplayer.py"
    path.write_text(body, encoding="utf-8")
    return path


def _source(tmp_path: Path) -> Path:
    path = tmp_path / "whatever.mov"
    path.write_bytes(b"not really a movie")
    return path


def test_start_play_raises_when_the_player_dies_on_startup(tmp_path, monkeypatch, deck_home):
    """The reported defect: `play` exited 0 with a dead player and a stale `play_pid`.

    Before the fix `start_play` returned None, `state.json` kept the dead pid, and `cli.main`
    returned 0 - the user was told playback had started while nothing was streaming.
    """
    from ghostdeck import play, state

    monkeypatch.setattr(play, "VENDOR_PLAY", _player(tmp_path, "import sys\nprint('boom', file=sys.stderr)\nraise SystemExit(3)\n"))

    with pytest.raises(RuntimeError) as excinfo:
        play.start_play(str(_source(tmp_path)))

    assert "status 3" in str(excinfo.value), excinfo.value
    assert "nothing is playing" in str(excinfo.value), excinfo.value
    # The dead player must not be recorded as a running session.
    assert state.load()["play_pid"] is None
    assert not (deck_home / ".ghostdeck" / "play.pid").exists()
    assert play.playing() is False


def test_start_play_records_a_player_that_stays_up(tmp_path, monkeypatch, deck_home):
    """The other direction: a player that survives the grace window is still recorded as before.

    A-104 canary: `state.save` is poisoned for the whole call, so a revert to the old
    `load()`-then-`save()` record path fails here immediately rather than only under a race.
    """
    from ghostdeck import play, state

    def forbidden(data):
        raise AssertionError(
            "start_play wrote state with a whole-dict save(); that read is outside the lock (A-104)"
        )

    monkeypatch.setattr(play.gdstate, "save", forbidden)
    monkeypatch.setattr(
        play, "VENDOR_PLAY", _player(tmp_path, "import time\ntime.sleep(60)\n")
    )

    play.start_play(str(_source(tmp_path)))

    data = state.load()
    assert isinstance(data["play_pid"], int) and data["play_pid"] > 0
    # Both spellings are written by the one locked update, not by a stale whole-dict write.
    assert data["play"]["pid"] == data["play_pid"]
    assert (deck_home / ".ghostdeck" / "play.pid").is_file()
    assert play.playing() is True

    # Leave nothing running: this is the player `start_play` just launched.
    import os
    import signal

    os.kill(data["play_pid"], signal.SIGTERM)


def test_start_play_rejects_an_unusable_source_before_any_device_work(tmp_path, monkeypatch, deck_home):
    """A-103's second half: SOURCE was passed straight to the child argv with no validation.

    The check must be ordered before the compile/detect steps, and it must not touch the device path
    or even create `~/.ghostdeck` - a typo should cost nothing and name the input the user typed.
    `deck_home` is required, not decorative: without it `state.load()` below would read the
    operator's real `~/.ghostdeck/state.json` and this test would be asserting on live state.
    """
    from ghostdeck import play, state

    reached: list[str] = []
    monkeypatch.setattr(play, "_require_tools", lambda source: reached.append("tools"))
    monkeypatch.setattr(play.adb, "require_adb", lambda: reached.append("adb"))
    monkeypatch.setattr(play.devicebuild, "ensure", lambda: reached.append("devicebuild"))
    monkeypatch.setattr(play.usb, "detect", lambda: reached.append("detect") or {"mode": "adb"})
    monkeypatch.setattr(play.gdstate, "ensure_dirs", lambda: reached.append("ensure_dirs"))

    with pytest.raises(RuntimeError) as excinfo:
        play.start_play(str(tmp_path / "typo.mov"))

    assert "not a file and is not a URL" in str(excinfo.value), excinfo.value
    assert "typo.mov" in str(excinfo.value), excinfo.value
    # Everything before the check ran exactly once; nothing past it ran at all.
    assert reached == ["tools", "adb"], reached
    assert state.load()["play_pid"] is None


def test_validate_source_accepts_urls_and_rejects_missing_paths(tmp_path):
    """The source check itself: a URL is not a file, and a missing path is neither."""
    from ghostdeck import play

    play._validate_source("https://example.com/clip.mp4")  # must not raise
    existing = tmp_path / "real.mov"
    existing.write_bytes(b"x")
    play._validate_source(str(existing))  # must not raise

    with pytest.raises(RuntimeError) as excinfo:
        play._validate_source(str(tmp_path / "absent.mov"))
    assert "not a file and is not a URL" in str(excinfo.value)


def test_start_play_refuses_without_the_bridge_and_spawns_nothing(tmp_path, monkeypatch, deck_home):
    """T19: with no bridge, `play` must refuse with the command that starts one, and do nothing else.

    The live report was a raw `ConnectionRefusedError: [Errno 61] Connection refused` out of the
    player, because the player's first device-side act is `connect_bridge(BRIDGE_SOCKET)` and
    `play` never checked. The refusal has to be the same one line whether the user typed the command
    or called this function, and it must come before anything that costs something: no state dir, no
    device-binary build, no USB probe, no virtual HID, no player. `_ENDPOINT_DEAD` covers both "no
    socket file" and "socket file nobody listens on", so it is exercised here with the absent path.
    """
    from ghostdeck import play, studio

    # The fixture presents a live bridge of ours; this is the report's case instead: nothing is
    # listening. (`_ENDPOINT_DEAD` is also what a socket file nobody listens on returns.)
    monkeypatch.setattr(studio, "_socket_state", lambda: (studio._ENDPOINT_DEAD, "endpoint is absent"))

    def forbidden(*args, **kwargs):
        raise AssertionError("start_play ran a step it must reach only with a live bridge")

    spawned: list[list[str]] = []
    monkeypatch.setattr(play, "_require_tools", lambda source: None)
    monkeypatch.setattr(play.gdstate, "ensure_dirs", forbidden)
    monkeypatch.setattr(play.devicebuild, "ensure", forbidden)
    monkeypatch.setattr(play.usb, "detect", forbidden)
    monkeypatch.setattr(play.vhid, "start", forbidden)
    monkeypatch.setattr(play.gdstate, "update", forbidden)
    monkeypatch.setattr(
        play,
        "subprocess",
        types.SimpleNamespace(
            Popen=lambda *args, **kwargs: spawned.append(args[0]),
            TimeoutExpired=subprocess.TimeoutExpired,
        ),
    )

    with pytest.raises(RuntimeError) as excinfo:
        play.start_play(str(_source(tmp_path)))

    message = str(excinfo.value)
    assert "\n" not in message, message
    assert "hidshim bridge is not running" in message, message
    assert "ghostdeck studio" in message, message
    assert spawned == [], spawned
    # `ensure_dirs` is poisoned above, so a state file here would mean the refusal ran late.
    assert not (deck_home / ".ghostdeck" / "state.json").exists()


def test_require_bridge_accepts_only_a_live_bridge_of_ours(tmp_path, monkeypatch):
    """Ownership, not liveness (A-133): only our own live bridge may satisfy the check.

    A listener that no live bridge of ours owns would leave the player talking to a stranger, and an
    endpoint that cannot be classified must not be reported as either. Both refusals are one line
    that names the socket and what to do, never a bare `socket.error`.
    """
    from ghostdeck import studio

    monkeypatch.setattr(studio, "SOCKET", tmp_path / "bridge.sock")
    monkeypatch.setattr(studio, "_socket_state", lambda: (studio._ENDPOINT_LIVE, ""))
    monkeypatch.setattr(studio, "_bridge_owner_live", lambda: True)
    studio.require_bridge()  # our own live bridge: the only case that may proceed

    monkeypatch.setattr(studio, "_bridge_owner_live", lambda: False)
    with pytest.raises(RuntimeError) as excinfo:
        studio.require_bridge()
    owner_message = str(excinfo.value)
    assert "\n" not in owner_message, owner_message
    assert f"no live {studio.BRIDGE.name} of ours owns it" in owner_message, owner_message
    assert "ghostdeck studio" not in owner_message, owner_message  # nothing to start here

    monkeypatch.setattr(
        studio,
        "_socket_state",
        lambda: (studio._ENDPOINT_UNDETERMINABLE, "EMFILE: too many open files"),
    )
    with pytest.raises(RuntimeError) as excinfo:
        studio.require_bridge()
    assert "leaving the endpoint alone" in str(excinfo.value), excinfo.value


def test_stop_detect_and_status_stay_usable_without_the_bridge(tmp_path, monkeypatch, deck_home):
    """T19: only `play` and `studio` depend on the bridge, and that must stay true.

    `stop` is the recovery command and `detect`/`status` are the reporting commands, so they are
    exactly what a user runs while the bridge is down. Poisoning the liveness probe, the ownership
    record and the requirement itself makes a future `require_bridge()` call in one of these paths
    fail here, instead of taking the recovery command away from the user who needs it.
    """
    from ghostdeck import cli, play, studio, usb

    def forbidden(*args, **kwargs):
        raise AssertionError("a recovery or reporting command consulted the bridge")

    monkeypatch.setattr(studio, "require_bridge", forbidden)
    monkeypatch.setattr(studio, "_socket_state", forbidden)
    monkeypatch.setattr(studio, "_bridge_owner_live", forbidden)
    # Host-side facts, not the bridge: stubbed so the assertions below cannot depend on whether this
    # machine happens to have a built copy in `~/Applications`.
    monkeypatch.setattr(studio, "running", lambda: False)
    monkeypatch.setattr(studio, "copy_exists", lambda: False)
    monkeypatch.setattr(
        usb,
        "detect",
        lambda: {"serial": "FAKESERIAL", "vid": 0x18D1, "pid": 0xD002, "mode": "adb"},
    )
    monkeypatch.setattr(play, "deck_transport", lambda **kwargs: (None, "", []))

    assert cli._detect() == 0
    assert cli._status() == 0

    # `stop`: the player record is empty under the temp HOME, and the only device calls it can make
    # are stubbed to success, so a bridge consult is the only thing left that could fail this.
    monkeypatch.setattr(play, "deck_transport", lambda **kwargs: ("FAKESERIAL", "device", []))
    monkeypatch.setattr(play, "_adb_mutate", lambda argv: None)
    monkeypatch.setattr(
        play.adb,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args, returncode=1, stdout="", stderr=""
        ),
    )
    play.stop()

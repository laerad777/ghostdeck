"""`ghostdeck play` must not report a successful start for a player that is not running (A-103).

Everything here is device-free: `devicebuild.ensure`, `vhid.start`, `usb.detect` and
`adb.require_adb` are stubbed, `VENDOR_PLAY` is redirected to a temp script, and HOME points at
`tmp_path`, so nothing is compiled, copied, spawned against a real deck, or written to the
operator's `~/.ghostdeck`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))


@pytest.fixture
def deck_home(tmp_path, monkeypatch):
    """A stubbed deck path plus a temp HOME, so start_play can reach `Popen` and nothing else."""
    from ghostdeck import devicebuild, play, state, usb

    home = tmp_path / "home"
    (home / ".ghostdeck").mkdir(parents=True)
    monkeypatch.setattr(state, "HOME", home / ".ghostdeck")
    monkeypatch.setattr(state, "STATE_PATH", home / ".ghostdeck" / "state.json")
    monkeypatch.setattr(devicebuild, "ensure", lambda: None)
    monkeypatch.setattr(play.vhid, "start", lambda: None)
    monkeypatch.setattr(play.adb, "require_adb", lambda: None)
    monkeypatch.setattr(usb, "detect", lambda: {"serial": "FAKESERIAL", "mode": "adb"})
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

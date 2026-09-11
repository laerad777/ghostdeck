"""A-102: a missing optional backend must be named, never reported as a missing deck.

The distinguishing evidence is the pair (message, exit code): before this, `detect` printed
"no device" and exited 1 for both "no deck attached" and "no backend can see any device", so a
missing Python package was indistinguishable from absent hardware.

Device-free: `usb.detect` is replaced in-process, so no USB access happens at all, and HOME is a
temp dir for the one test that lets the CLI reach the state layer.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

HINT = "hidapi is not installed (pip install hidapi)"
# `detect`/`status`/`play` return this for an unusable environment. Not 1, which means "no deck".
ENV_EXIT = 2

NO_BACKEND = {"serial": None, "vid": None, "pid": None, "mode": "none", "dependency": HINT}
NO_DECK = {"serial": None, "vid": None, "pid": None, "mode": "none"}
REAL_DECK = {"serial": "ABC123XYZ", "vid": 0x2207, "pid": 0x0019, "mode": "hid"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A temp HOME, so nothing here can read or write the operator's `~/.ghostdeck`."""
    from ghostdeck import state

    monkeypatch.setattr(state, "HOME", tmp_path / ".ghostdeck")
    monkeypatch.setattr(state, "STATE_PATH", tmp_path / ".ghostdeck" / "state.json")
    return tmp_path


def _run(monkeypatch, detect_result: dict, *args: str) -> int:
    """`cli.main(argv)` with `usb.detect` replaced, returning the exit code it would exit with."""
    from ghostdeck import cli, usb

    monkeypatch.setattr(usb, "detect", lambda: dict(detect_result))
    return cli.main(list(args or ("detect",)))


def test_detect_names_the_missing_backend_instead_of_the_deck(monkeypatch, capsys):
    code = _run(monkeypatch, NO_BACKEND, "detect")
    captured = capsys.readouterr()
    assert HINT in captured.err, captured
    assert "no device" not in captured.err + captured.out, captured
    assert code == ENV_EXIT, (code, captured)


def test_detect_still_reports_no_deck_when_every_backend_works(monkeypatch, capsys):
    """The distinction this task exists for: a working environment with no deck is unchanged."""
    code = _run(monkeypatch, NO_DECK, "detect")
    captured = capsys.readouterr()
    assert "no device" in captured.err, captured
    assert HINT not in captured.err
    assert code == 1, (code, captured)


def test_detect_reports_a_real_deck_unchanged(monkeypatch, capsys):
    code = _run(monkeypatch, REAL_DECK, "detect")
    captured = capsys.readouterr()
    assert code == 0, (code, captured)
    assert "mode=hid" in captured.out, captured
    assert captured.err == ""


def test_status_does_not_present_a_hardware_conclusion_without_a_backend(monkeypatch, capsys, home):
    """`usb=none` is a device verdict; with no usable backend there is no verdict to report."""
    code = _run(monkeypatch, NO_BACKEND, "status")
    captured = capsys.readouterr()
    assert HINT in captured.err, captured
    assert "usb=none" not in captured.out, captured
    assert "usb=unknown" in captured.out, captured
    assert code == ENV_EXIT, (code, captured)
    # The host-side diagnostics stay available even when the USB layer cannot be probed.
    assert "release_gate=" in captured.out, captured


def test_status_still_exits_zero_with_a_working_environment(monkeypatch, capsys, home):
    code = _run(monkeypatch, NO_DECK, "status")
    captured = capsys.readouterr()
    assert code == 0, (code, captured)
    assert "usb=none" in captured.out, captured
    assert HINT not in captured.err


def _stub_play_until_detect(monkeypatch):
    """Let `start_play` reach the `usb.detect()` verdict without touching a device or a file."""
    from ghostdeck import devicebuild, play

    monkeypatch.setattr(play, "_require_tools", lambda source: None)
    monkeypatch.setattr(play.adb, "require_adb", lambda: None)
    monkeypatch.setattr(play, "_validate_source", lambda source: None)
    monkeypatch.setattr(play.gdstate, "ensure_dirs", lambda: None)
    monkeypatch.setattr(devicebuild, "ensure", lambda: None)


def test_start_play_blames_the_backend_not_the_device(monkeypatch):
    from ghostdeck import play, usb

    _stub_play_until_detect(monkeypatch)
    monkeypatch.setattr(usb, "detect", lambda: dict(NO_BACKEND))

    with pytest.raises(usb.MissingDependency) as excinfo:
        play.start_play("/tmp/whatever.mov")
    assert HINT in str(excinfo.value)
    assert "no D200 on USB" not in str(excinfo.value)


def test_start_play_still_reports_a_missing_deck_when_backends_work(monkeypatch):
    from ghostdeck import play, usb

    _stub_play_until_detect(monkeypatch)
    monkeypatch.setattr(usb, "detect", lambda: dict(NO_DECK))

    with pytest.raises(RuntimeError) as excinfo:
        play.start_play("/tmp/whatever.mov")
    assert "no D200 on USB" in str(excinfo.value)


def test_main_maps_a_missing_backend_to_the_environment_exit_code(monkeypatch, capsys):
    """The mapping is in `main`, so every command reports the environment consistently."""
    from ghostdeck import cli, usb

    def boom(*args, **kwargs):
        raise usb.MissingDependency(HINT)

    monkeypatch.setattr(cli.vhid, "quit", boom)
    code = cli.main(["quit"])
    captured = capsys.readouterr()
    assert code == ENV_EXIT, (code, captured)
    assert HINT in captured.err, captured
    assert "Traceback" not in captured.err + captured.out

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
# T16: a deck that is attached but whose adb transport cannot run a command. Its own code, so a
# script can tell "replug the deck" apart from "install hidapi" (2) and "no deck attached" (1).
OFFLINE_EXIT = 3

NO_BACKEND = {"serial": None, "vid": None, "pid": None, "mode": "none", "dependency": HINT}
NO_DECK = {"serial": None, "vid": None, "pid": None, "mode": "none"}
REAL_DECK = {"serial": "ABC123XYZ", "vid": 0x2207, "pid": 0x0019, "mode": "hid"}
# The wedged deck's USB verdict, from the real host: enumerated as ADB, adbd not answering.
WEDGED_DECK = {"serial": "ABC123XYZ", "vid": 0x18D1, "pid": 0xD002, "mode": "adb"}


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
    assert "shim=" in captured.out, captured
    assert "playing=" in captured.out, captured


def test_status_still_exits_zero_with_a_working_environment(monkeypatch, capsys, home):
    code = _run(monkeypatch, NO_DECK, "status")
    captured = capsys.readouterr()
    assert code == 0, (code, captured)
    assert "usb=none" in captured.out, captured
    assert HINT not in captured.err


def _stub_play_until_detect(monkeypatch):
    """Let `start_play` reach the `usb.detect()` verdict without touching a device or a file."""
    from ghostdeck import devicebuild, play, studio

    monkeypatch.setattr(play, "_require_tools", lambda source: None)
    monkeypatch.setattr(play.adb, "require_adb", lambda: None)
    monkeypatch.setattr(play, "_validate_source", lambda source: None)
    monkeypatch.setattr(play.gdstate, "ensure_dirs", lambda: None)
    monkeypatch.setattr(devicebuild, "ensure", lambda: None)
    # T19: `start_play` also refuses while the hidshim bridge is down, and the contract here is to
    # reach the `usb.detect()` verdict, so the bridge is presented as live. Two lambdas, not a
    # socket: liveness and ownership are stubbed, so no test can see or bind a real bridge.
    monkeypatch.setattr(studio, "_socket_state", lambda: (studio._ENDPOINT_LIVE, ""))
    monkeypatch.setattr(studio, "_bridge_owner_live", lambda: True)


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

    monkeypatch.setattr(cli.playmod, "stop", boom)
    code = cli.main(["stop"])
    captured = capsys.readouterr()
    assert code == ENV_EXIT, (code, captured)
    assert HINT in captured.err, captured
    assert "Traceback" not in captured.err + captured.out


# --- T16: `detect`/`status` must not report an unusable deck as a healthy one -------------


def _stub_transport(monkeypatch, serial, state, blocked=()):
    """Replace `play.deck_transport`, returning the recorded keyword arguments.

    `detect`/`status` consult the transport only for an ADB-mode verdict, so any test here that
    stubs `usb.detect` to ADB MUST stub this too: otherwise the CLI would run the operator's real
    `adb`, and this host has a real deck attached.
    """
    from ghostdeck import play

    calls = []

    def spy(**kwargs):
        calls.append(kwargs)
        return serial, state, list(blocked)

    monkeypatch.setattr(play, "deck_transport", spy)
    return calls


def test_detect_does_not_report_a_wedged_deck_as_healthy(monkeypatch, capsys):
    """T16: `detect` exited 0 with `mode=adb` for a deck that could not run a single command."""
    calls = _stub_transport(monkeypatch, "ABC123XYZ", "offline")
    code = _run(monkeypatch, WEDGED_DECK, "detect")
    captured = capsys.readouterr()
    assert code == OFFLINE_EXIT, (code, captured)
    assert "mode=adb (offline)" in captured.err, captured
    assert "power-cycle" in captured.err and "replug" in captured.err, captured
    assert "no device" not in captured.err + captured.out, captured
    # A diagnostic must not reset the adb server it is reporting on (A-134).
    assert calls == [{"restart": False}], calls


def test_detect_still_reports_a_usable_deck(monkeypatch, capsys):
    """The distinction must not turn a healthy ADB deck into an error."""
    _stub_transport(monkeypatch, "ABC123XYZ", "device")
    code = _run(monkeypatch, WEDGED_DECK, "detect")
    captured = capsys.readouterr()
    assert code == 0, (code, captured)
    assert "mode=adb" in captured.out, captured
    assert "offline" not in captured.out + captured.err, captured
    assert captured.err == "", captured


def test_detect_reports_an_offline_device_it_cannot_attribute(monkeypatch, capsys):
    """No USB verdict (the venv has neither hidapi nor pyusb), but `adb` lists a wedged device.

    The deck cannot be proven, so the state is reported without claiming which device it is, and
    without falling back to `no device` - that fallback is what told a user to check a cable.
    """
    _stub_transport(monkeypatch, None, "", blocked=[("SAMPLE0000000001", "offline")])
    code = _run(monkeypatch, WEDGED_DECK, "detect")
    captured = capsys.readouterr()
    assert code == OFFLINE_EXIT, (code, captured)
    assert "mode=adb (offline)" in captured.err, captured
    assert "SAMPLE0000000001" in captured.err, captured
    assert "not identified as the D200" in captured.err, captured
    assert "power-cycle" in captured.err, captured


def test_status_annotates_an_offline_transport(monkeypatch, capsys, home):
    """T16: `usb=adb` alone read as healthy. The mode is real, so it is kept and annotated."""
    _stub_transport(monkeypatch, "ABC123XYZ", "offline")
    code = _run(monkeypatch, WEDGED_DECK, "status")
    captured = capsys.readouterr()
    assert code == OFFLINE_EXIT, (code, captured)
    assert "usb=adb (offline)" in captured.out, captured
    assert "shim=" in captured.out, captured
    assert "power-cycle" in captured.err, captured


def test_status_still_exits_zero_for_a_usable_deck(monkeypatch, capsys, home):
    _stub_transport(monkeypatch, "ABC123XYZ", "device")
    code = _run(monkeypatch, WEDGED_DECK, "status")
    captured = capsys.readouterr()
    assert code == 0, (code, captured)
    assert "usb=adb" in captured.out, captured
    assert "offline" not in captured.out + captured.err, captured
    assert captured.err == "", captured


# --- in-lane hardening: a non-zero exit must never be silent ------------------------------
#
# From a real-deck observation that could not be attributed: cycle 2 of the master's hardware pass
# reported `stop` exiting 1 with EMPTY output. Two mechanisms produce exactly that signature, and
# both are closed here. Neither had a proven reachable instance - every `raise` in the stop path
# carries a message, and nothing in the dispatch path exits the process - so these are
# diagnosability fixes: they make an unattributable failure name itself instead of being a blank.


def test_a_message_less_failure_still_names_its_type(monkeypatch, capsys, home):
    """`print(error)` renders an empty message as a blank line: rc=1, output strips to ""."""
    from ghostdeck import cli, play

    for error in (OSError(), RuntimeError(), ValueError()):
        monkeypatch.setattr(play, "stop", lambda e=error: (_ for _ in ()).throw(e))
        code = cli.main(["stop"])
        captured = capsys.readouterr()
        assert code == 1, (code, captured)
        assert captured.err.strip(), f"{type(error).__name__} exited 1 with no output at all"
        assert type(error).__name__ in captured.err, captured


def test_a_messageful_failure_is_printed_unchanged(monkeypatch, capsys, home):
    """The fallback must not decorate a real message: existing output stays byte-identical."""
    from ghostdeck import cli, play

    monkeypatch.setattr(
        play, "stop", lambda: (_ for _ in ()).throw(RuntimeError("deck is on fire"))
    )
    code = cli.main(["stop"])
    captured = capsys.readouterr()
    assert code == 1, (code, captured)
    assert captured.err == "deck is on fire\n", captured


def test_a_command_that_exits_the_process_is_reported_not_silent(monkeypatch, capsys, home):
    """`except Exception` cannot see `SystemExit`, so it ended the interpreter with no output."""
    from ghostdeck import cli, play

    monkeypatch.setattr(play, "stop", lambda: sys.exit(1))
    code = cli.main(["stop"])
    captured = capsys.readouterr()
    assert code == 1, (code, captured)  # the code is preserved, not swallowed
    assert captured.err.strip(), "a command exited the process and said nothing"
    assert "ghostdeck bug" in captured.err, captured


def test_an_exit_with_no_code_still_returns_zero(monkeypatch, capsys, home):
    """`sys.exit()` (code None) means 0, and the guard must not turn it into a failure."""
    from ghostdeck import cli, play

    monkeypatch.setattr(play, "stop", lambda: sys.exit())
    code = cli.main(["stop"])
    assert code == 0, (code, capsys.readouterr())


def test_argparse_usage_errors_are_not_swallowed_by_the_exit_guard(capsys):
    """The guard sits INSIDE the dispatch try, so argparse's own exits are untouched.

    `parse_args` runs above the try on purpose: `--help` and a usage error must still exit 0 and 2
    with argparse's own output, not be reported as a ghostdeck bug.
    """
    import pytest as _pytest

    from ghostdeck import cli

    with _pytest.raises(SystemExit) as help_exit:
        cli.main(["--help"])
    assert help_exit.value.code == 0

    with _pytest.raises(SystemExit) as usage_exit:
        cli.main(["definitely-not-a-command"])
    assert usage_exit.value.code == 2
    assert "ghostdeck bug" not in capsys.readouterr().err

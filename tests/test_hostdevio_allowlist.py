"""Offline adb allowlist and `devices -l` parsing tests. No device is contacted, `adb_bin` is never
reached, and nothing is shelled out: `adb.run` is replaced in-process (A-135, and the devices-parse
gap a hardware pass exposed)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from ghostdeck import adb

DENY = [
    ["-s", "S", "shell", "/tmp/ghostdeck-x", ";", "reboot"],
    ["-s", "S", "shell", "/tmp/ghostdeck-x", "&&", "reboot"],
    ["-s", "S", "shell", "/tmp/ghostdeck-x", "|", "reboot"],
    ["-s", "S", "shell", "/tmp/ghostdeck-x", "$(reboot)"],
    ["-s", "S", "shell", "/tmp/ghostdeck-x", ">", "/data/x"],
    ["-s", "S", "shell", "/tmp/ghostdeck-x", "`reboot`"],
    ["-s", "S", "shell", "/tmp/ghostdeck-x", "&", "reboot"],
    # The literal must not hide inside a single argv element either.
    ["-s", "S", "shell", "/tmp/ghostdeck-x; reboot"],
    ["-s", "S", "shell", "/tmp/ghostdeck-x&&reboot"],
    ["-s", "S", "shell", "/tmp/ghostdeck-x|reboot"],
    ["-s", "S", "shell", "/tmp/ghostdeck-x;reboot"],
    # Whitespace normalisation must not turn one argv element into two safe tokens.
    ["-s", "S", "shell", "/tmp/ghostdeck-x\nreboot"],
    ["-s", "S", "shell", "/tmp/ghostdeck-x\treboot"],
    ["-s", "S", "shell", "/tmp/ghostdeck-x\rreboot"],
    ["-s", "S", "shell", "/tmp/ghostdeck-x", "\n", "reboot"],
    # A-135: the whitespace codepoints `_UNSAFE` does not name. `str.split()` is Unicode-aware, so
    # these were silently rewritten into an allowlisted command while adb forwarded the raw bytes.
    ["-s", "S", "shell", "/tmp/ghostdeck-x\u00a0reboot"],
    ["-s", "S", "shell", "/tmp/ghostdeck-x\u2028reboot"],
    ["-s", "S", "shell", "/tmp/ghostdeck-x\u3000reboot"],
    ["-s", "S", "shell", "/tmp/ghostdeck-x\x0breboot"],
    ["-s", "S", "shell", "/tmp/ghostdeck-x\x0creboot"],
    ["-s", "S", "shell", "/tmp/ghostdeck-x\x1creboot"],
    ["-s", "S", "shell", "getprop\u00a0sys.usb.config"],
    # Normalisation that merely *adds* whitespace is the same defect: the allowlist would judge a
    # different string than the one forwarded.
    ["-s", "S", "shell", "rm -f  /tmp/ghostdeck-*"],
    ["-s", "S", "shell", " rm -f /tmp/ghostdeck-*"],
    # Quoting, substitution and redirection inside a token.
    ["-s", "S", "shell", "/tmp/ghostdeck-x", '"reboot"'],
    ["-s", "S", "shell", "/tmp/ghostdeck-x", "'reboot'"],
    ["-s", "S", "shell", "/tmp/ghostdeck-x", "${IFS}reboot"],
    ["-s", "S", "shell", "/tmp/ghostdeck-x", "reboot>x"],
    ["-s", "S", "shell", "/tmp/ghostdeck-x", "reboot|x"],
    # Any absolute path outside the anchor.
    ["-s", "S", "shell", "/tmp/ghostdeck-x", "/data/local/tmp/evil"],
    ["-s", "S", "shell", "/tmp/ghostdeck-x", "/system/bin/reboot"],
    ["-s", "S", "shell", "/tmp/ghostdeck-x", "../.."],
    ["-s", "S", "shell", "/tmp/ghostdeck-x", "/tmp/ghostdeck-y/../../data"],
    # The free-form branch must stay anchored on the first token.
    ["-s", "S", "shell", "reboot"],
    ["-s", "S", "shell", "sh -c reboot"],
    ["-s", "S", "shell", "/data/tmp/ghostdeck-x", "ls"],
    # The composite-gadget prohibition holds across the whole argv.
    ["-s", "S", "shell", "echo hid,adb"],
    ["-s", "S", "shell", "/tmp/ghostdeck-x", "hid", ",adb"],
    ["-s", "S", "shell", "/tmp/ghostdeck-x", "setprop", "sys.usb.config", "hid,adb"],
    ["-s", "S", "setprop", "sys.usb.config", "hid,adb"],
    # Structural rejections.
    ["-s", "S", "install", "x.apk"],
    ["-s", "S", "remount"],
    ["-s", "S", "push", "a", "/data/x"],
    ["-s", "S", "push", "a"],
    ["-s", "bad serial!", "get-state"],
    ["-s", "S", "shell", ""],
    ["shell", "reboot"],
    ["-s", "S"],
    [],
]

ALLOW = [
    ["-s", "S", "shell", "setprop ctl.start zkswe"],
    ["-s", "S", "shell", "setprop ctl.stop zkswe"],
    ["-s", "S", "shell", "rm -f /tmp/ghostdeck-*"],
    ["-s", "S", "shell", "ls /tmp/ghostdeck"],
    ["-s", "S", "shell", "ls /tmp/ghostdeck*"],
    ["-s", "S", "shell", "chmod +x /tmp/ghostdeck-agent"],
    ["-s", "S", "shell", "chmod 755 /tmp/ghostdeck-agent"],
    ["-s", "S", "shell", "getprop sys.usb.config"],
    ["-s", "S", "shell", "cat /sys/class/zkswe_usb/zkswe0/functions"],
    ["-s", "S", "shell", "cat /sys/class/zkswe_usb/zkswe0/state"],
    ["-s", "S", "get-state"],
    ["devices", "-l"],
]


@pytest.mark.parametrize("argv", DENY, ids=lambda a: " ".join(a) or "empty")
def test_denied_argv(argv):
    assert adb.allowed(argv) is False
    with pytest.raises(adb.AdbDenied):
        adb.validate(argv)


@pytest.mark.parametrize("argv", ALLOW, ids=lambda a: " ".join(a))
def test_allowed_argv(argv):
    assert adb.allowed(argv) is True
    assert adb.validate(argv) is None


@pytest.mark.parametrize("part", [";", "&&", "|", "&", "$(reboot)", "`reboot`", ">", "<", "\n"])
def test_every_metacharacter_is_rejected_after_the_anchor(part):
    assert adb.allowed(["-s", "S", "shell", "/tmp/ghostdeck-x", part, "reboot"]) is False


@pytest.mark.parametrize("part", [";", "&&", "|", "&", "\n", "\t", "\\"])
def test_metacharacters_are_rejected_inside_a_single_token(part):
    assert adb.allowed(["-s", "S", "shell", f"/tmp/ghostdeck-x{part}reboot"]) is False


def test_public_surface_is_unchanged():
    assert adb.check is adb.validate
    assert adb.allowlisted is adb.allowed
    assert adb.require_adb is adb.adb_bin
    assert adb.execute is adb.run
    assert issubclass(adb.AdbDenied, ValueError)


def test_denied_argv_never_reaches_adb(monkeypatch):
    calls = []
    monkeypatch.setattr(adb.subprocess, "run", lambda *a, **k: calls.append(a))
    for argv in DENY:
        with pytest.raises(adb.AdbDenied):
            adb.run(argv)
    assert calls == []


# --- `devices -l` parsing against REAL output, not a fabricated one -------------------------
# A hardware pass found that `ghostdeck stop` could never work on the real deck: the product/model
# fields it matched on are fields THIS DECK NEVER EMITS, and the suite was green because the
# fixtures invented those fields. `serial_from_devices()` is the shared parser in this lane, and it
# had no test here at all, so the same mistake was available to it. These tests reproduce the SHAPE
# the deck really prints (see the constant below), so the parser is pinned against reality rather
# than against a fixture that agrees with it by construction.

# The shape the attached deck actually prints in `adb devices -l`: NO product/model/device
# fields, only `usb:` and `transport_id:`. The serial here is SYNTHETIC on purpose -- the
# real one must never be committed (README "will not do": hard-code a device serial), and
# test_public_tree_has_no_lab_identity enforces that.
REAL_DECK_LINE = "D200SAMPLE0000001      device usb:18092032X transport_id:4"


def _devices_stdout(*lines: str) -> str:
    return "List of devices attached\n" + "".join(f"{line}\n" for line in lines)


def _stub_devices(monkeypatch, stdout: str):
    """Answer `adb devices -l` in-process: no adb binary, no subprocess, no device."""

    def fake_run(argv, **kwargs):
        assert list(argv) == ["devices", "-l"], argv
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    monkeypatch.setattr(adb, "run", fake_run)


def test_serial_from_devices_reads_the_real_decks_verbatim_line(monkeypatch):
    """The real deck emits NO product/model field; the serial alone must be enough.

    The LINE SHAPE is what the deck prints; the serial inside it is synthetic (see the constant).
    """
    _stub_devices(monkeypatch, _devices_stdout(REAL_DECK_LINE))
    assert "product:" not in REAL_DECK_LINE and "model:" not in REAL_DECK_LINE
    assert adb.serial_from_devices() == "D200SAMPLE0000001"


def test_serial_from_devices_accepts_both_real_separators(monkeypatch):
    """`adb devices -l` separates the serial from the state with spaces or a tab.

    The tab form is what adb actually writes and the space form is how it appears when pasted;
    matching only one of them would read as "no device attached" on the other.
    """
    for separator in ("\t", "      "):
        _stub_devices(monkeypatch, _devices_stdout(f"D200SAMPLE0000001{separator}device usb:18092032X"))
        assert adb.serial_from_devices() == "D200SAMPLE0000001", repr(separator)


def test_serial_from_devices_ignores_devices_that_are_not_usable(monkeypatch):
    """Only a `device` state is usable; offline/unauthorized must not be returned as a target."""
    _stub_devices(
        monkeypatch,
        _devices_stdout(
            "OFFLINE123\toffline usb:1-1",
            "UNAUTH123\tunauthorized usb:1-2",
            "NOPERM123\tno permissions (user in plugdev group) usb:1-3",
        ),
    )
    assert adb.serial_from_devices() is None


def test_serial_from_devices_returns_none_for_an_empty_listing(monkeypatch):
    _stub_devices(monkeypatch, _devices_stdout())
    assert adb.serial_from_devices() is None


def test_serial_from_devices_returns_the_first_usable_device_by_position(monkeypatch):
    """Documents the positional behaviour that made `stop` target a phone (A-137).

    This is pinned as the CURRENT contract, not as a desirable one: the caller that must not target
    the wrong device is `play._deck_serial()`, which filters by the deck's own fields. Recording it
    here means a future change to either side has to be deliberate.
    """
    _stub_devices(
        monkeypatch,
        _devices_stdout(
            "PHONE123\tdevice product:shiba model:Pixel_8 device:shiba transport_id:1",
            REAL_DECK_LINE,
        ),
    )
    # Positional: the phone wins, which is exactly why a deck-identifying filter is required above it.
    assert adb.serial_from_devices() == "PHONE123"


def test_serial_from_devices_skips_a_serial_it_cannot_pass_on(monkeypatch):
    """A first token the allowlist charset rejects is skipped, so a real device behind it is found.

    `????????????` is a real `adb devices -l` artefact: a device whose serial could not be read.
    `_SERIAL` rejects it, and the loop must continue rather than return it or give up. (My first
    version of this test invented a token like `bad`, which `_SERIAL` happens to ACCEPT -- an
    unrealistic fixture proving nothing, the same mistake that let a broken `stop` look green.)
    """
    placeholder = "????????????"
    assert adb._SERIAL.fullmatch(placeholder) is None
    _stub_devices(monkeypatch, _devices_stdout(f"{placeholder}\tdevice usb:1-1", REAL_DECK_LINE))
    assert adb.serial_from_devices() == "D200SAMPLE0000001"


def test_serial_from_devices_only_ever_returns_an_allowlistable_serial(monkeypatch):
    """Property: whatever comes back must be passable to `adb -s`, or the caller cannot use it."""
    for line in (
        "????????????\tdevice usb:1-1",
        "\x00weird\tdevice usb:1-2",
        REAL_DECK_LINE,
        "emulator-5554\tdevice product:x model:y",
    ):
        _stub_devices(monkeypatch, _devices_stdout(line))
        serial = adb.serial_from_devices()
        assert serial is None or adb._SERIAL.fullmatch(serial), (line, serial)
        if serial is not None:
            assert adb.allowed(["-s", serial, "get-state"]), serial


# --- A-135: the allowlist must judge exactly the bytes adb forwards --------------------------

UNICODE_WHITESPACE = [
    "\u00a0",  # no-break space
    "\u1680",
    "\u2000",
    "\u2028",  # line separator
    "\u2029",
    "\u202f",
    "\u205f",
    "\u3000",  # ideographic space
    "\x0b",  # vertical tab
    "\x0c",  # form feed
    "\x1c",  # file/group/record/unit separators
    "\x1d",
    "\x1e",
    "\x1f",
]


@pytest.mark.parametrize("space", UNICODE_WHITESPACE, ids=lambda c: f"U+{ord(c):04X}")
def test_no_unicode_whitespace_smuggles_a_second_token(space):
    """A-135: 14 whitespace codepoints are not named by `_UNSAFE` but *are* split on by
    `str.split()`, so the allowlist used to approve `getprop\xa0sys.usb.config` (judged as the
    allowlisted `getprop sys.usb.config`) while adb forwarded the U+00A0 form."""
    assert adb.allowed(["-s", "S", "shell", f"/tmp/ghostdeck-x{space}reboot"]) is False
    assert adb.allowed(["-s", "S", "shell", f"getprop{space}sys.usb.config"]) is False
    assert adb.allowed(["-s", "S", "shell", f"rm -f{space}{space}/tmp/ghostdeck-*"]) is False


def test_the_allowlist_judges_the_string_that_is_forwarded():
    """The invariant behind A-135, stated directly: normalising must be a no-op for anything the
    allowlist approves, so a permitted argv can never differ from the bytes adb receives."""
    for argv in ALLOW:
        parts = argv[3:]
        if argv[:3] != ["-s", "S", "shell"] or not parts:
            continue
        forwarded = " ".join(parts)
        assert forwarded == " ".join(forwarded.split()), argv


def test_multi_token_single_parts_stay_allowed():
    """The precise rule is on the raw join, not on per-part token count.

    A per-part `len(part.split()) != 1` rule closes A-135 too, but it would also reject the
    multi-token single parts this product really sends -- `rm -f /tmp/ghostdeck-*`,
    `ls /tmp/ghostdeck*`, `getprop sys.usb.config` -- so it must not be the fix.
    """
    for command in (
        "rm -f /tmp/ghostdeck-*",
        "ls /tmp/ghostdeck*",
        "getprop sys.usb.config",
        "chmod 700 /tmp/ghostdeck-x",
        "killall /tmp/ghostdeck-x",
        "setprop ctl.start zkswe",
    ):
        assert len(command.split()) > 1, command
        assert adb.allowed(["-s", "S", "shell", command]) is True, command

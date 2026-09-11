"""Offline adb allowlist tests. No device is ever contacted; `adb_bin` is never reached."""

from __future__ import annotations

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

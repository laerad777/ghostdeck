"""PATH adb wrapper with a default-deny allowlist."""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Sequence

_SERIAL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_TMP = re.compile(r"^/tmp/ghostdeck-[A-Za-z0-9._+-]+$")
_LS = re.compile(r"^ls /tmp/ghostdeck\*?[A-Za-z0-9._+-]*$")
_CHMOD = re.compile(
    r"^chmod (?:[0-7]{3,4}|\+x|a\+x|u\+x|ug\+x|ugo\+x) /tmp/ghostdeck-[A-Za-z0-9._+-]+$"
)
_KILL = re.compile(
    r"^(?:killall(?: -9)?|kill(?: -[0-9]+| -TERM| -KILL| -INT)?) /tmp/ghostdeck-[A-Za-z0-9._+-]+$"
)
_RM = re.compile(r"^rm -f /tmp/ghostdeck(?:-\*|[A-Za-z0-9._+-]*)$")
# Deny-by-default charset for tokens of the free-form shell branch.
_TOKEN = re.compile(r"^[A-Za-z0-9._+:=/-]+$")
# Rejected on the raw argv, before whitespace normalisation, so a token cannot
# smuggle a second device-side command through a newline or tab.
_UNSAFE = re.compile(r"[;&|<>$`\"'(){}\\\n\r\t]")
_EXACT_SHELL = frozenset(
    {
        "getprop sys.usb.config",
        "cat /sys/class/zkswe_usb/zkswe0/functions",
        "cat /sys/class/zkswe_usb/zkswe0/state",
        "setprop ctl.stop zkswe",
        "setprop ctl.start zkswe",
    }
)


class AdbDenied(ValueError):
    """Raised when argv is outside the allowlist."""


def adb_bin() -> str:
    path = shutil.which("adb")
    if not path:
        raise FileNotFoundError("adb is not on PATH")
    return path


require_adb = adb_bin


def validate(argv: Sequence[str]) -> None:
    args = [str(part) for part in argv]
    blob = "".join(args)
    if "hid,adb" in blob.replace(" ", ""):
        raise AdbDenied("composite gadget hid+adb is not allowed")
    if args == ["devices", "-l"]:
        return
    if len(args) >= 3 and args[0] == "-s":
        serial = args[1]
        if not _SERIAL.fullmatch(serial):
            raise AdbDenied("invalid serial")
        rest = args[2:]
        if rest == ["get-state"]:
            return
        if rest and rest[0] == "push":
            if len(rest) != 3:
                raise AdbDenied("push requires local and remote paths")
            if not _TMP.fullmatch(rest[2]):
                raise AdbDenied("push remote must be /tmp/ghostdeck-*")
            return
        if rest and rest[0] == "shell":
            parts = [str(part) for part in rest[1:]]
            for part in parts:
                if _UNSAFE.search(part):
                    raise AdbDenied("shell argument contains a shell metacharacter")
            if "hid,adb" in "".join(parts):
                raise AdbDenied("composite gadget hid+adb is not allowed")
            command = " ".join(parts).strip()
            command = " ".join(command.split())
            # The allowlist must judge exactly the bytes adb will forward. `str.split()` is
            # Unicode-aware, so re-tokenising silently rewrites 14 whitespace codepoints that
            # `_UNSAFE` does not name (U+00A0, U+2028, U+3000, \x0b, \x0c, \x1c-\x1f, ...):
            # `getprop\xa0sys.usb.config` matched the allowlisted `getprop sys.usb.config` while
            # the device shell received the U+00A0 form instead (A-135). Comparing the raw join
            # against the normalised command closes every such codepoint at once, and cannot
            # drift from Python's own definition of whitespace the way a charset can.
            #
            # A per-part `len(part.split()) != 1` rule would close it too, but it would also
            # reject the legitimate multi-token single parts this host really sends
            # (`rm -f /tmp/ghostdeck-*`, `ls /tmp/ghostdeck*`, `getprop sys.usb.config`).
            if " ".join(parts) != command:
                raise AdbDenied("shell argument contains whitespace")
            if not command:
                raise AdbDenied("empty shell command")
            if not _shell_allowed(command):
                raise AdbDenied("shell command is not allowed")
            return
    raise AdbDenied("adb argv is not allowed")


check = validate


def allowed(argv: Sequence[str]) -> bool:
    try:
        validate(argv)
    except AdbDenied:
        return False
    return True


allowlisted = allowed


def run(argv: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
    validate(argv)
    kwargs.setdefault("check", False)
    return subprocess.run([adb_bin(), *[str(part) for part in argv]], **kwargs)


execute = run

# --- host adb *server* management (finding H3) ------------------------------
# `kill-server`/`start-server` act on the host daemon, never on the deck, so they are
# deliberately not entries in `validate()`: that allowlist gates everything that reaches
# the deck, and a device entry is the wrong home for a host operation. This one named
# call is the entire server-management surface — there is no general "run any adb argv".
_SERVER_ARGV = (("kill-server",), ("start-server",))
_SERVER_TIMEOUT = 30.0


def restart_server(*, timeout: float = _SERVER_TIMEOUT) -> None:
    """Restart the host adb server, for a deck the server has not picked up (H3).

    A deck that switched HID -> ADB can be visible to USB while `adb devices` is still empty;
    no amount of waiting fixes that server. Raises RuntimeError naming the failing command so
    the caller can report what was tried rather than a bare status code.
    """
    binary = adb_bin()
    for argv in _SERVER_ARGV:
        try:
            result = subprocess.run(
                [binary, *argv], capture_output=True, text=True, timeout=timeout, check=False
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError(f"'adb {argv[0]}' could not be run: {error}") from error
        if result.returncode != 0:
            lines = (result.stderr or result.stdout or "").strip().splitlines()
            tail = f": {lines[-1]}" if lines else ""
            raise RuntimeError(f"'adb {argv[0]}' failed with status {result.returncode}{tail}")


def serial_from_devices() -> str | None:
    result = run(["devices", "-l"], capture_output=True, text=True, timeout=30)
    for line in (result.stdout or "").splitlines():
        if line.startswith("List"):
            continue
        if "\tdevice" in line or " device " in line:
            serial = line.split()[0]
            if _SERIAL.fullmatch(serial):
                return serial
    return None


def _shell_allowed(command: str) -> bool:
    if command in _EXACT_SHELL:
        return True
    if (
        _LS.fullmatch(command)
        or _CHMOD.fullmatch(command)
        or _KILL.fullmatch(command)
        or _RM.fullmatch(command)
    ):
        return True
    tokens = command.split()
    if not tokens or not _TMP.fullmatch(tokens[0]):
        return False
    for token in tokens:
        if not _TOKEN.fullmatch(token):
            return False
        if ".." in token:
            return False
        if token.startswith("/") and not _TMP.fullmatch(token):
            return False
    return True

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

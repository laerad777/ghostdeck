"""Device-free CLI and allowlist tests. No hardware."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
PACKAGE = SRC / "ghostdeck"
MARKER = "02C47" + "A"


def _env():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    return env


def _run(*args):
    return subprocess.run(
        [sys.executable, "-m", "ghostdeck.cli", *args],
        cwd=str(ROOT),
        env=_env(),
        capture_output=True,
        text=True,
    )


def test_cli_help_parses():
    result = _run("--help")
    assert result.returncode == 0, result.stderr
    out = result.stdout + result.stderr
    for name in ("play", "stop", "quit", "status", "detect", "studio", "build"):
        assert name in out
    script = subprocess.run(
        [sys.executable, str(PACKAGE / "cli.py"), "-h"],
        cwd=str(ROOT),
        env=_env(),
        capture_output=True,
        text=True,
    )
    assert script.returncode == 0, script.stderr
    for name in ("play", "stop", "quit", "status", "detect", "studio", "build"):
        assert name in script.stdout + script.stderr


def test_detect_source_has_no_hardcoded_serial():
    for path in sorted(PACKAGE.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert MARKER not in text, path.name
        ast.parse(text)


def test_public_tree_has_no_lab_identity():
    skip = {".pyc", ".png"}
    home = "/Users/" + "mose"
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix in skip:
            continue
        if "__pycache__" in path.parts or ".pytest_cache" in path.parts:
            continue
        if path.name == "test_ghostdeck_offline.py":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        assert MARKER not in text, path
        assert home not in text, path


def test_allowlist_rejects_illegal_shell():
    sys.path.insert(0, str(SRC))
    from ghostdeck import adb

    assert adb.allowlisted(["devices", "-l"]) is True
    assert adb.allowlisted(["-s", "X", "get-state"]) is True
    assert adb.allowlisted(["-s", "X", "shell", "reboot"]) is False
    assert adb.allowlisted(["-s", "X", "install", "x.apk"]) is False
    assert adb.allowlisted(["-s", "X", "remount"]) is False
    assert adb.allowlisted(["-s", "X", "push", "a", "/data/x"]) is False
    assert adb.allowlisted(["-s", "X", "shell", "echo hid,adb"]) is False
    assert adb.allowlisted(["-s", "X", "shell", "rm -f /tmp/ghostdeck-*"]) is True
    raised = False
    try:
        adb.run(["shell", "reboot"])
    except Exception:
        raised = True
    assert raised


def test_status_and_detect_without_device_do_not_crash():
    detect = _run("detect")
    assert detect.returncode in (0, 1)
    status = _run("status")
    assert status.returncode in (0, 1)
    combined = detect.stdout + detect.stderr + status.stdout + status.stderr
    assert "Traceback" not in combined

def test_readmes_describe_hidshim_copy():
    en = (ROOT / "README.md").read_text(encoding="utf-8")
    ko = (ROOT / "README.ko.md").read_text(encoding="utf-8")
    en_l = en.lower()
    assert "hidshim" in en_l
    assert "ulanzi studio adb.app" in en_l
    assert "/applications/ulanzi studio.app" in en_l
    assert "never written" in en_l or "not written" in en_l or "never" in en_l
    assert MARKER not in en
    assert "hidshim" in ko.lower()
    assert "2207:0019" in ko
    assert MARKER not in ko


def test_status_stdout_release_gate_offline_not_visible():
    status = _run("status")
    assert status.returncode in (0, 1)
    out = status.stdout
    assert "release_gate=" in out
    assert "visible=yes" not in out
    assert "shim=" in out
    assert "copy=" in out
def test_play_starts_vhid_after_physical_adb():
    text = (PACKAGE / "play.py").read_text(encoding="utf-8")
    switch = text.find("switch_hid_to_adb")
    vhid_start = text.find("vhid.start()")
    assert switch != -1 and vhid_start != -1
    assert switch < vhid_start
    assert "virtual HID skipped" in text
def test_iohid_create_does_not_raise():
    sys.path.insert(0, str(SRC))
    from ghostdeck import iohid
    device = iohid.create()
    assert device is None or device
def test_studio_does_not_hardcode_serial_or_write_official_app():
    sys.path.insert(0, str(SRC))
    from ghostdeck import studio
    text = (PACKAGE / "studio.py").read_text(encoding="utf-8")
    assert MARKER not in text
    assert "Ulanzi Studio ADB.app" in text
    assert str(studio.ORIGINAL) == "/Applications/Ulanzi Studio.app"
    assert "HIDSHIM_SRC" in text
    assert "ensure_copy" in text
    assert (ROOT / "device" / "d200-zkgui-proxy.c").is_file()
    assert (ROOT / "device" / "d200-color-agent.c").is_file()

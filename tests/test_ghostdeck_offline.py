"""Device-free CLI and allowlist tests. No hardware."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
PACKAGE = SRC / "ghostdeck"
MARKER = "02C47" + "A"

# A-102 made an unusable Python environment distinguishable from an absent deck, so the exit code of
# `detect`/`status` is no longer a fixed value. These tests assert the REASON the command reported
# and let that reason entail the code. A widened `returncode in (0, 1, 2)` would accept every outcome
# and hide exactly the regression class this operation has been removing.
BACKEND_HINT = "is not installed (pip install"


def _backend_hint():
    """The dependency hint THIS interpreter should produce, asked of `usb` directly.

    Deriving the expectation from the environment rather than from the CLI's own output is what keeps
    these assertions non-vacuous. A test that only checked "the exit code matches the text" would still
    pass if the CLI stopped mentioning the missing backend altogether and fell back to blaming the
    deck - which is precisely the A-102 regression. This probe is independent of the code under test.
    """
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    from ghostdeck import usb

    return usb.missing_dependency()


def _assert_detect_outcome(result) -> None:
    """Assert `detect` reported the right reason for THIS environment, and the code that follows."""
    hint = _backend_hint()
    text = result.stdout + result.stderr
    assert "Traceback" not in text, text
    if hint:
        # A-102: the Python environment is unusable. `detect` must name the package, must NOT present
        # a hardware verdict, and must not share "no deck"'s exit code.
        assert hint in result.stderr, (hint, result.stderr)
        assert result.returncode == 2, (result.returncode, text)
        assert "no device" not in text, text
        return
    # Every backend is usable here, so the deck is either found or genuinely absent.
    if result.returncode == 0:
        assert "mode=" in result.stdout, result.stdout
    else:
        assert result.returncode == 1, (result.returncode, text)
        assert "no device" in result.stderr, (result.returncode, text)


def _assert_status_outcome(result) -> None:
    """Assert `status` printed its line and did not turn an environment failure into a verdict."""
    hint = _backend_hint()
    text = result.stdout + result.stderr
    assert "Traceback" not in text, text
    assert "release_gate=" in result.stdout, result.stdout
    if hint:
        assert hint in result.stderr, (hint, result.stderr)
        assert result.returncode == 2, (result.returncode, text)
        assert "usb=unknown" in result.stdout, result.stdout
        assert "usb=none" not in result.stdout, result.stdout
        return
    assert result.returncode == 0, (result.returncode, text)


def _env(home):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    env["HOME"] = str(home)
    return env


def _run(*args):
    # Never let a CLI invocation read or write the operator's real ~/.ghostdeck.
    with tempfile.TemporaryDirectory(prefix="ghostdeck-home-") as home:
        return subprocess.run(
            [sys.executable, "-m", "ghostdeck.cli", *args],
            cwd=str(ROOT),
            env=_env(home),
            capture_output=True,
            text=True,
        )


def _run_script(path, *args):
    with tempfile.TemporaryDirectory(prefix="ghostdeck-home-") as home:
        return subprocess.run(
            [sys.executable, str(path), *args],
            cwd=str(ROOT),
            env=_env(home),
            capture_output=True,
            text=True,
        )


def test_cli_help_parses():
    result = _run("--help")
    assert result.returncode == 0, result.stderr
    out = result.stdout + result.stderr
    for name in ("play", "stop", "quit", "status", "detect", "studio", "build"):
        assert name in out
    script = _run_script(PACKAGE / "cli.py", "-h")
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
    # `build`/`dist` are setuptools output: they hold stale COPIES of the tracked package
    # sources, so scanning them adds no signal and only noise (the same reason `.egg-info`
    # is skipped). They are gitignored; a leak in real source is still caught.
    skip_dirs = {".git", ".gjc", ".venv", "__pycache__", ".pytest_cache", "build", "dist"}
    home = "/Users/" + "mose"
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix in skip:
            continue
        if any(part in skip_dirs or part.endswith(".egg-info") for part in path.parts):
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
    status = _run("status")
    combined = detect.stdout + detect.stderr + status.stdout + status.stderr
    assert "Traceback" not in combined
    # The property that motivated this test is "no crash", and its specific form is "a reason was
    # reported and the exit code follows from that reason" - not a fixed code.
    _assert_detect_outcome(detect)
    _assert_status_outcome(status)
    assert detect.stdout or detect.stderr, "`detect` said nothing at all"

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
    # This test is about the CONTENT of the status line, which is printed whatever the USB verdict is.
    # The exit code is asserted against the reason rather than pinned, so the test does not depend on
    # whether an optional backend happens to be installed in the interpreter running the suite.
    _assert_status_outcome(status)
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

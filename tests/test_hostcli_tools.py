"""Host-side preflight tests: a missing external tool must produce one friendly line, not a traceback.

C-021 (host half) + A-015. Every CLI run uses a temp HOME and a stubbed PATH; no device, no Studio.app,
no `~/.ghostdeck` access.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

# A PATH with the OS tools but none of the media/build tools these modules need.
BASE_PATH = "/usr/bin:/bin"


def _stub(bin_dir: Path, name: str) -> None:
    path = bin_dir / name
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)


def _cli(tmp_path: Path, *args: str, tools: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
    bin_dir = tmp_path / "bin"
    home = tmp_path / "home"
    bin_dir.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    for tool in tools:
        _stub(bin_dir, tool)
    env = dict(os.environ)
    env.update(HOME=str(home), PYTHONPATH=str(SRC), PATH=str(bin_dir))
    return subprocess.run(
        [sys.executable, "-m", "ghostdeck.cli", *args],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
    )


def _only_line(result: subprocess.CompletedProcess) -> str:
    assert result.returncode == 1, (result.returncode, result.stdout, result.stderr)
    assert "Traceback" not in result.stdout + result.stderr
    assert "FileNotFoundError" not in result.stdout + result.stderr
    lines = [line for line in result.stderr.splitlines() if line.strip()]
    assert len(lines) == 1, lines
    return lines[0]


def _stub_build_tools(monkeypatch, present: set[str]) -> None:
    """Pin the build-tool lookup so the test states exactly which tools exist (C-157).

    `studio._require_build_tools()` asks `shutil.which`, so stubbing that instead of the PATH makes
    the answer independent of the host. The previous version read the ambient PATH: it passed on a
    Mac with the Xcode tools installed and failed under `PATH=/bin`, the same class of defect as a
    test that inherits the operator's real $HOME.
    """
    from ghostdeck import studio

    monkeypatch.setattr(
        studio.shutil, "which", lambda name: f"/usr/bin/{name}" if name in present else None
    )


def test_play_preflight_names_missing_ffmpeg(tmp_path):
    line = _only_line(_cli(tmp_path, "play", "/tmp/whatever.mov"))
    assert line.startswith("ffmpeg not on PATH")
    assert "brew install ffmpeg" in line


def test_play_preflight_names_missing_ffprobe(tmp_path):
    """ffmpeg alone is not enough: the vendor player hard-requires ffprobe."""
    line = _only_line(_cli(tmp_path, "play", "/tmp/whatever.mov", tools=("ffmpeg",)))
    assert line.startswith("ffprobe not on PATH")
    assert "ffmpeg" in line  # names the fix


def test_play_preflight_requires_ytdlp_only_for_url_sources(tmp_path):
    url = _only_line(
        _cli(tmp_path, "play", "https://example.com/clip.mp4", tools=("ffmpeg", "ffprobe"))
    )
    assert url.startswith("yt-dlp not on PATH")
    assert "brew install yt-dlp" in url

    file_result = _cli(tmp_path, "play", "/tmp/whatever.mov", tools=("ffmpeg", "ffprobe"))
    assert "yt-dlp" not in file_result.stderr


def test_play_preflight_runs_before_device_and_adb_work(tmp_path):
    """The tool check must not be masked by, or mask, the later device errors."""
    missing = _cli(tmp_path, "play", "/tmp/whatever.mov")
    assert "adb is not on PATH" not in missing.stderr

    present = _cli(tmp_path, "play", "/tmp/whatever.mov", tools=("ffmpeg", "ffprobe"))
    assert "adb is not on PATH" in present.stderr  # tools satisfied, so adb is reached next
    assert "not on PATH" in present.stderr


def test_studio_preflight_names_missing_build_tool(tmp_path, monkeypatch):
    from ghostdeck import studio

    monkeypatch.setattr(studio, "COPY", tmp_path / "copy-does-not-exist.app")
    monkeypatch.setattr(studio, "ORIGINAL", tmp_path / "original-does-not-exist.app")
    _stub_build_tools(monkeypatch, set(studio._BUILD_TOOLS) - {"clang"})
    with pytest.raises(RuntimeError) as excinfo:
        studio.ensure_copy()
    message = str(excinfo.value)
    assert message.startswith("clang not on PATH")
    assert "xcode-select --install" in message
    assert "\n" not in message


def test_studio_preflight_names_the_first_missing_build_tool_in_order(tmp_path, monkeypatch):
    """The tool named is the first MISSING one in `_BUILD_TOOLS` order, not merely a missing one.

    The test above has exactly one tool absent, so it cannot see an ordering change. Here two are
    absent and the earlier one must be named: a reorder that put `clang` ahead of `ditto` would name
    `clang` and fail here, rather than being caught by accident elsewhere.
    """
    from ghostdeck import studio

    monkeypatch.setattr(studio, "COPY", tmp_path / "copy-does-not-exist.app")
    monkeypatch.setattr(studio, "ORIGINAL", tmp_path / "original-does-not-exist.app")
    present = set(studio._BUILD_TOOLS) - {"ditto", "clang"}
    _stub_build_tools(monkeypatch, present)
    missing = [tool for tool in studio._BUILD_TOOLS if tool not in present]
    assert missing == ["ditto", "clang"], f"the fixture no longer isolates order: {missing}"
    with pytest.raises(RuntimeError) as excinfo:
        studio.ensure_copy()
    assert str(excinfo.value) == "ditto not on PATH: install it (xcode-select --install)"


def test_studio_preflight_passes_with_all_tools_present(tmp_path, monkeypatch):
    """With the whole toolchain present the preflight passes, so the next failure is the missing app."""
    from ghostdeck import studio

    monkeypatch.setattr(studio, "COPY", tmp_path / "copy-does-not-exist.app")
    monkeypatch.setattr(studio, "ORIGINAL", tmp_path / "original-does-not-exist.app")
    _stub_build_tools(monkeypatch, set(studio._BUILD_TOOLS))
    with pytest.raises(RuntimeError) as excinfo:
        studio.ensure_copy()
    message = str(excinfo.value)
    assert message.startswith("install official Studio at ")
    assert "not on PATH" not in message


def test_studio_preflight_skipped_when_copy_already_usable(tmp_path, monkeypatch):
    """No build happens, so a missing toolchain must not block a working copy.

    Every path `copy_exists()` consults is redirected, not just `COPY`. `SHIM`, `REAL` and `EXE`
    are derived from `COPY` at import time, so patching only `COPY` left `copy_exists()` reading
    the operator's real `~/Applications` copy: this test then passed on a host where the master had
    already built one, and failed under an isolated HOME. Measured at HEAD with a temp HOME and the
    toolchain stubbed out -> `RuntimeError: ditto not on PATH`, from the preflight this test exists
    to prove is skipped. The `copy_exists()` assertion below keeps the redirect honest, so an
    incomplete one fails here instead of silently reading the real $HOME.
    """
    from ghostdeck import studio

    copy = tmp_path / "Ulanzi Studio ADB.app"
    shim = copy / "Contents/Frameworks/libhidapi.0.dylib"
    real = copy / "Contents/Frameworks/libhidapi.0.real.dylib"
    exe = copy / "Contents/MacOS/UlanziDeck"
    for target in (shim, real, exe):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
    monkeypatch.setattr(studio, "COPY", copy)
    monkeypatch.setattr(studio, "SHIM", shim)
    monkeypatch.setattr(studio, "REAL", real)
    monkeypatch.setattr(studio, "EXE", exe)
    monkeypatch.setattr(studio, "ORIGINAL", tmp_path / "original-does-not-exist.app")
    # A fresh shim must also be given, or the staleness check would send this through the build
    # path this test exists to prove is skipped -- which on a host whose `hidshim.c` is newer than
    # the redirected empty file is exactly what happens.
    monkeypatch.setattr(studio, "HIDSHIM_SRC", tmp_path / "absent-hidshim.c")
    _stub_build_tools(monkeypatch, set())

    assert studio.copy_exists() is True, "the redirect is incomplete; this would read the real $HOME"
    assert studio._shim_is_stale() is False, "no source to be stale against"
    studio.ensure_copy()  # returns early; must not raise


def test_a_shim_older_than_its_source_is_rebuilt(tmp_path, monkeypatch):
    """The copy was reused on `copy_exists()` alone, so it served a 5-day-old shim (A-166 class).

    Measured on this host: `hidshim.c` was edited twice on 2026-09-14 while the copy's
    `libhidapi.0.dylib` stayed the 2026-09-09 build. The rebuilt image is where the
    bridge-unreachable diagnostic lives, so the stale one could not say why Studio never attached
    -- it simply did nothing and left no record. The staleness verdict is what forces the rebuild,
    so it is asserted directly rather than through the compiler.
    """
    from ghostdeck import studio

    shim = tmp_path / "libhidapi.0.dylib"
    shim.write_text("", encoding="utf-8")
    source = tmp_path / "hidshim.c"
    source.write_text("int main(void) { return 0; }", encoding="utf-8")
    monkeypatch.setattr(studio, "SHIM", shim)
    monkeypatch.setattr(studio, "HIDSHIM_SRC", source)

    # Old shim, newer source: the case that went unnoticed.
    stamp = time.time()
    os.utime(shim, (stamp - 5 * 86400, stamp - 5 * 86400))
    os.utime(source, (stamp, stamp))
    assert studio._shim_is_stale() is True

    # Rebuilt: the source is now older than the image, so the copy is reused again.
    os.utime(shim, (stamp + 10, stamp + 10))
    assert studio._shim_is_stale() is False

    # A source that cannot be stat()ed must not read as "fresh": producing the artifact is the safe
    # answer, and it keeps the failure at the compiler instead of silently here.
    monkeypatch.setattr(studio, "HIDSHIM_SRC", tmp_path / "absent-hidshim.c")
    assert studio._shim_is_stale() is False


def test_ensure_copy_rebuilds_a_stale_shim_instead_of_reusing_it(tmp_path, monkeypatch):
    """The verdict has to reach `ensure_copy()`, or nothing is actually rebuilt.

    Asserting `_shim_is_stale()` alone would pass with the old `if copy_exists(): return` still in
    place. This drives `ensure_copy()` down the build path and proves it got there: the toolchain is
    stubbed empty, so a real build attempt fails with the preflight message rather than returning
    silently. Verified by mutation -- restoring `copy_exists()` alone as the precondition makes this
    test fail with no exception raised.
    """
    from ghostdeck import studio

    copy = tmp_path / "Ulanzi Studio ADB.app"
    shim = copy / "Contents/Frameworks/libhidapi.0.dylib"
    real = copy / "Contents/Frameworks/libhidapi.0.real.dylib"
    exe = copy / "Contents/MacOS/UlanziDeck"
    for target in (shim, real, exe):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
    source = tmp_path / "hidshim.c"
    source.write_text("int main(void) { return 0; }", encoding="utf-8")
    monkeypatch.setattr(studio, "COPY", copy)
    monkeypatch.setattr(studio, "SHIM", shim)
    monkeypatch.setattr(studio, "REAL", real)
    monkeypatch.setattr(studio, "EXE", exe)
    monkeypatch.setattr(studio, "HIDSHIM_SRC", source)
    monkeypatch.setattr(studio, "ORIGINAL", tmp_path / "original-does-not-exist.app")
    _stub_build_tools(monkeypatch, set())

    stamp = time.time()
    os.utime(shim, (stamp - 5 * 86400, stamp - 5 * 86400))
    os.utime(source, (stamp, stamp))

    assert studio.copy_exists() is True, "the reuse precondition is otherwise satisfied"
    with pytest.raises(RuntimeError) as excinfo:
        studio.ensure_copy()
    assert "not on PATH" in str(excinfo.value), "the stale copy was reused, so nothing rebuilt"

"""Host-side preflight tests: a missing external tool must produce one friendly line, not a traceback.

C-021 (host half) + A-015. Every CLI run uses a temp HOME and a stubbed PATH; no device, no Studio.app,
no `~/.ghostdeck` access.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
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
    real_which = shutil.which
    monkeypatch.setattr(
        studio.shutil, "which", lambda name: None if name == "clang" else real_which(name)
    )
    with pytest.raises(RuntimeError) as excinfo:
        studio.ensure_copy()
    message = str(excinfo.value)
    assert message.startswith("clang not on PATH")
    assert "xcode-select --install" in message
    assert "\n" not in message


def test_studio_preflight_passes_with_all_tools_present(tmp_path, monkeypatch):
    from ghostdeck import studio

    monkeypatch.setattr(studio, "COPY", tmp_path / "copy-does-not-exist.app")
    monkeypatch.setattr(studio, "ORIGINAL", tmp_path / "original-does-not-exist.app")
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
    monkeypatch.setattr(studio.shutil, "which", lambda name: None)

    assert studio.copy_exists() is True, "the redirect is incomplete; this would read the real $HOME"
    studio.ensure_copy()  # returns early; must not raise

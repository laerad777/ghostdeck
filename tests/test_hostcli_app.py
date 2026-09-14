"""The GUI is a remote for the CLI. These tests never open a window or a deck."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ghostdeck.app import CommandResult, DeckRemote, shim_is_up


def test_shim_is_up_reads_the_status_line():
    assert shim_is_up("usb=adb shim=up copy=yes playing=no")
    assert not shim_is_up("usb=adb shim=down copy=yes playing=no")
    assert not shim_is_up("usb=none")
    assert not shim_is_up("usb=adb copy=yes playing=no")


def test_play_starts_studio_when_the_shim_is_down():
    calls: list[list[str]] = []

    def run(argv):
        calls.append(list(argv))
        if argv == ["status"]:
            return CommandResult(argv, 0, "usb=adb shim=down copy=yes playing=no\n", "")
        return CommandResult(argv, 0, "", "")

    results = DeckRemote(run).play("/tmp/clip.mp4")
    assert calls == [["status"], ["studio"], ["play", "/tmp/clip.mp4"]]
    assert [item.argv for item in results] == calls
    assert all(item.code == 0 for item in results)


def test_play_skips_studio_when_the_shim_is_already_up():
    calls: list[list[str]] = []

    def run(argv):
        calls.append(list(argv))
        if argv == ["status"]:
            return CommandResult(argv, 0, "usb=hid shim=up copy=yes playing=no\n", "")
        return CommandResult(argv, 0, "", "")

    DeckRemote(run).play("/tmp/clip.mp4")
    assert calls == [["status"], ["play", "/tmp/clip.mp4"]]
    assert ["studio"] not in calls


def test_play_does_not_call_play_if_studio_fails():
    calls: list[list[str]] = []

    def run(argv):
        calls.append(list(argv))
        if argv == ["status"]:
            return CommandResult(argv, 0, "usb=adb shim=down copy=no playing=no\n", "")
        if argv == ["studio"]:
            return CommandResult(argv, 1, "", "official Studio.app is missing")
        raise AssertionError(f"unexpected {argv}")

    results = DeckRemote(run).play("/tmp/clip.mp4")
    assert calls == [["status"], ["studio"]]
    assert results[-1].code == 1
    assert "Studio.app" in results[-1].detail


def test_play_refuses_an_empty_path_without_touching_the_cli():
    def run(argv):
        raise AssertionError(f"cli ran {argv}")

    results = DeckRemote(run).play("  ")
    assert results[0].code == 2
    assert "파일" in results[0].stderr

def test_play_passes_a_url_through_to_the_cli():
    calls: list[list[str]] = []

    def run(argv):
        calls.append(list(argv))
        if argv == ["status"]:
            return CommandResult(argv, 0, "usb=adb shim=up copy=yes playing=no\n", "")
        return CommandResult(argv, 0, "", "")

    DeckRemote(run).play("https://youtu.be/dQw4w9WgXcQ")
    assert calls == [["status"], ["play", "https://youtu.be/dQw4w9WgXcQ"]]


def test_stop_is_only_stop():
    calls: list[list[str]] = []

    def run(argv):
        calls.append(list(argv))
        return CommandResult(argv, 0, "", "")

    result = DeckRemote(run).stop()
    assert calls == [["stop"]]
    assert result.argv == ["stop"]

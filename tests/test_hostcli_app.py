"""The GUI is a remote for the CLI. These tests never open a window or a deck."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ghostdeck.app import (
    CommandResult,
    DeckRemote,
    ensure_store_id,
    is_google_login_host,
    read_pasteboard,
    resolve_source,
    shim_is_up,
    youtube_watch_url,
)


def test_shim_is_up_reads_the_status_line():
    assert shim_is_up("usb=adb shim=up copy=yes playing=no")
    assert not shim_is_up("usb=adb shim=down copy=yes playing=no")
    assert not shim_is_up("usb=none")
    assert not shim_is_up("usb=adb copy=yes playing=no")


def test_read_pasteboard_uses_macos_pbpaste_not_tk():
    class Result:
        stdout = "  https://youtu.be/dQw4w9WgXcQ  \n"

    def run(argv, **_kwargs):
        assert argv == ["/usr/bin/pbpaste"]
        return Result()

    assert read_pasteboard(run=run) == "https://youtu.be/dQw4w9WgXcQ"


def test_read_pasteboard_is_empty_when_pbpaste_is_missing():
    def run(argv, **_kwargs):
        raise OSError("no pbpaste")

    assert read_pasteboard(run=run) == ""


def test_youtube_watch_url_keeps_only_the_video_id():
    watch = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert youtube_watch_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=12s") == watch
    assert youtube_watch_url("https://youtu.be/dQw4w9WgXcQ") == watch
    assert youtube_watch_url("https://m.youtube.com/watch?v=dQw4w9WgXcQ") == watch
    assert youtube_watch_url("https://www.youtube.com/shorts/dQw4w9WgXcQ") == watch
    assert youtube_watch_url("https://www.youtube.com/embed/dQw4w9WgXcQ") == watch
    assert youtube_watch_url("https://www.youtube.com/results?search_query=x") == ""
    assert youtube_watch_url("https://www.youtube.com/") == ""


def test_google_login_host_is_desktop_accounts_not_youtube():
    assert is_google_login_host("accounts.google.com")
    assert is_google_login_host("accounts.google.co.kr")
    assert is_google_login_host("accounts.youtube.com")
    assert not is_google_login_host("m.youtube.com")
    assert not is_google_login_host("www.youtube.com")


def test_ensure_store_id_reuses_the_same_uuid(tmp_path):
    path = tmp_path / "webkit-store-id"
    first = ensure_store_id(path, lambda: "11111111-1111-1111-1111-111111111111")
    second = ensure_store_id(path, lambda: "22222222-2222-2222-2222-222222222222")
    assert first == "11111111-1111-1111-1111-111111111111"
    assert second == first


def test_empty_field_plays_a_copied_url():
    assert resolve_source("", "https://youtu.be/dQw4w9WgXcQ") == "https://youtu.be/dQw4w9WgXcQ"
    assert resolve_source("/tmp/clip.mp4", "https://youtu.be/x") == "/tmp/clip.mp4"
    assert resolve_source("", "not a source") == ""


def test_play_uses_pasteboard_when_the_field_is_empty():
    calls: list[list[str]] = []

    def run(argv):
        calls.append(list(argv))
        if argv == ["status"]:
            return CommandResult(argv, 0, "usb=adb shim=up copy=yes playing=no\n", "")
        return CommandResult(argv, 0, "", "")

    DeckRemote(run).play("  ", pasteboard="https://youtu.be/dQw4w9WgXcQ")
    assert calls == [["status"], ["play", "https://youtu.be/dQw4w9WgXcQ"]]


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
    assert "유튜브" in results[0].stderr

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

"""The GUI is a remote for the CLI. These tests never open a window or a deck."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ghostdeck.app import (
    CommandResult,
    DeckRemote,
    bridge_down,
    dropped_play_source,
    ensure_store_id,
    is_google_login_host,
    media_path_candidate,
    page_follow_action,
    parse_status_fields,
    play_request,
    play_should_loop,
    status_text,
    read_pasteboard,
    resolve_source,
    run_cli,
    shim_is_up,
    youtube_watch_url,
    playable_source,
    should_start_play,
    play_offset,
    parse_watch_payload,
)


def test_run_cli_is_a_child_process(monkeypatch):
    """hid.enumerate on the GUI poll thread SIGTRAPs the window (macOS 27 PAC)."""
    import subprocess as sp
    from ghostdeck import app, cli

    called: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        called.append(list(cmd))
        return sp.CompletedProcess(cmd, 0, "usb=hid shim=up copy=yes playing=no\n", "")

    monkeypatch.setattr(app.subprocess, "run", fake_run)

    def forbidden(*_a, **_k):
        raise AssertionError("run_cli called cli.main in-process")

    monkeypatch.setattr(cli, "main", forbidden)
    result = run_cli(["status"])
    assert called, "run_cli did not spawn"
    assert called[0][:3] == [sys.executable, "-m", "ghostdeck"]
    assert called[0][3:] == ["status"]
    assert result.code == 0
    assert "usb=hid" in result.stdout


def test_shim_is_up_reads_the_status_line():
    assert shim_is_up("usb=adb shim=up copy=yes playing=no")
    assert not shim_is_up("usb=adb shim=down copy=yes playing=no")
    assert not shim_is_up("usb=none")
    assert not shim_is_up("usb=adb copy=yes playing=no")


def test_the_window_shows_a_readable_state_instead_of_the_raw_status_line():
    """The window is what someone watches a video through, so `usb=adb shim=up …` is not the text."""
    assert status_text("usb=adb shim=up copy=yes playing=yes") == "덱 ADB · 재생 중"
    assert status_text("usb=adb shim=up copy=yes playing=no") == "덱 ADB · 멈춤"
    assert status_text("usb=hid shim=down copy=yes playing=no") == "덱 HID · 멈춤 · 브리지 꺼짐"
    assert status_text("usb=none shim=down copy=no playing=no") == "덱 없음 · 멈춤 · 브리지 꺼짐"


def test_a_bridge_that_is_up_is_not_worth_saying_in_the_window():
    """`브리지 꺼짐` is the state where 재생 has something to do; the healthy case stays quiet."""
    assert "브리지" not in status_text("usb=adb shim=up copy=yes playing=no")


def test_an_annotated_transport_mode_is_still_read_as_its_mode():
    """`status` appends `(offline)` to a wedged transport; the window must not lose the mode."""
    assert status_text("usb=adb (offline) shim=up copy=yes playing=no") == "덱 ADB · 멈춤"
    assert status_text("usb=unknown shim=down copy=yes playing=no") == "덱 알 수 없음 · 멈춤 · 브리지 꺼짐"


def test_unknown_status_text_never_renders_as_a_wrong_state():
    """Empty or unrecognised output must say so, not silently claim the deck is idle."""
    assert status_text("") == "상태를 읽지 못했습니다"
    assert status_text("something else entirely") == "상태를 읽지 못했습니다"


def test_status_fields_ignore_keys_this_window_does_not_know():
    """The CLI owns the line; a field added later must not be rendered as one of these four."""
    parsed = parse_status_fields("usb=adb shim=up copy=yes playing=no future=whatever")
    assert parsed == {"usb": "adb", "shim": "up", "copy": "yes", "playing": "no"}
    # Only the summary line counts: anything printed before it is not part of the state.
    assert parse_status_fields("warning: something\nusb=adb shim=up copy=yes playing=no")["usb"] == "adb"


def test_bridge_down_matches_the_refusal_play_actually_raises():
    """The phrase is shared with `studio`, so a reword cannot silently stop recovery working."""
    from ghostdeck import studio

    assert bridge_down(f"{studio.BRIDGE_DOWN} (no listener accepted the connection), and ...")
    assert not bridge_down("no D200 on USB")
    assert not bridge_down("")
    assert not bridge_down(studio.BRIDGE_DOWN[:-1] + "!")


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


def test_playable_source_keeps_youtube_as_a_watch_url():
    watch = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert playable_source("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=1") == watch
    assert playable_source(watch, "https://rr.googlevideo.com/videoplayback") == watch
    assert playable_source("https://www.youtube.com/") == ""
    assert playable_source("https://www.youtube.com/results?search_query=x") == ""


def test_playable_source_uses_a_direct_media_file():
    page = "https://example.com/watch"
    media = "https://cdn.example.com/clip.mp4"
    assert playable_source(page, media) == media


def test_should_start_play_ignores_the_same_youtube_video():
    watch = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert should_start_play(watch, watch, "https://rr.googlevideo.com/videoplayback") == ""
    assert should_start_play("", watch) == watch


def test_play_offset_and_watch_payload():
    assert play_offset(-3) == 0.0
    assert play_offset("12.5") == 12.5
    assert parse_watch_payload('{"url":"https://www.youtube.com/watch?v=x","t":9.25}') == (
        "https://www.youtube.com/watch?v=x",
        9.25,
    )


def test_gui_play_passes_start_and_does_not_loop():
    calls: list[list[str]] = []

    def run(argv):
        calls.append(list(argv))
        if argv[:1] == ["status"]:
            return CommandResult(argv, 0, "usb=adb shim=up copy=yes playing=no\n", "")
        return CommandResult(argv, 0, "", "")

    DeckRemote(run).play("https://www.youtube.com/watch?v=dQw4w9WgXcQ", start=15.2, loop=False)
    assert calls[-1] == [
        "play",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "--start",
        "15.200",
        "--no-loop",
    ]


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


def test_play_recovers_when_the_copy_is_up_but_its_bridge_is_gone():
    """The reported stuck state: `shim=up` skipped `studio`, and `play` refused every time.

    `studio` starts the copy and the bridge, but the copy outlives the bridge, so `shim=up` alone
    does not mean `play` will work. The first `play` is expected to refuse; the window must then
    start the bridge and retry rather than reporting the same failure until a human runs `studio`.
    """
    from ghostdeck import studio

    calls: list[list[str]] = []
    refused = CommandResult(["play", "/tmp/clip.mp4"], 1, "", studio.BRIDGE_DOWN + " (none)")
    play_calls = 0

    def run(argv):
        nonlocal play_calls
        calls.append(list(argv))
        if argv[0] == "status":
            return CommandResult(argv, 0, "usb=adb shim=up copy=yes playing=no\n", "")
        if argv[0] == "studio":
            return CommandResult(argv, 0, "", "")
        play_calls += 1
        return refused if play_calls == 1 else CommandResult(argv, 0, "", "")

    results = DeckRemote(run).play("/tmp/clip.mp4")
    assert calls == [
        ["status"],
        ["play", "/tmp/clip.mp4"],
        ["studio"],
        ["play", "/tmp/clip.mp4"],
    ]
    assert results[-1].code == 0, "the retry after starting the bridge must be reported"


def test_play_does_not_retry_a_failure_studio_cannot_fix():
    """A refusal that is not about the bridge must not spend a `studio` bring-up on it."""
    calls: list[list[str]] = []

    def run(argv):
        calls.append(list(argv))
        if argv[0] == "status":
            return CommandResult(argv, 0, "usb=adb shim=up copy=yes playing=no\n", "")
        return CommandResult(argv, 1, "", "no D200 on USB")

    results = DeckRemote(run).play("/tmp/clip.mp4")
    assert calls == [["status"], ["play", "/tmp/clip.mp4"]]
    assert results[-1].code == 1


def test_play_does_not_retry_when_starting_the_bridge_fails():
    """One retry at most: a `studio` that failed is reported, not looped on."""
    from ghostdeck import studio

    calls: list[list[str]] = []

    def run(argv):
        calls.append(list(argv))
        if argv[0] == "status":
            return CommandResult(argv, 0, "usb=adb shim=up copy=yes playing=no\n", "")
        if argv[0] == "studio":
            return CommandResult(argv, 1, "", "official Studio.app is missing")
        return CommandResult(argv, 1, "", studio.BRIDGE_DOWN + " (none)")

    results = DeckRemote(run).play("/tmp/clip.mp4")
    assert calls == [["status"], ["play", "/tmp/clip.mp4"], ["studio"]]
    assert results[-1].code == 1


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


def test_media_path_candidate_accepts_local_files_and_file_urls():
    assert media_path_candidate("/tmp/clip.mp4") == "/tmp/clip.mp4"
    assert media_path_candidate('"/tmp/clip.mkv"') == "/tmp/clip.mkv"
    assert media_path_candidate("file:///tmp/clip.webm") == "/tmp/clip.webm"
    assert media_path_candidate("https://youtu.be/x") == ""
    assert media_path_candidate("/tmp/notes.txt") == ""


def test_play_should_loop_only_for_local_files():
    assert play_should_loop("/tmp/clip.mp4") is True
    assert play_should_loop("https://www.youtube.com/watch?v=dQw4w9WgXcQ") is False


def test_dropped_play_source_picks_the_first_media_file():
    assert dropped_play_source(["/tmp/readme.txt", "/tmp/clip.mp4"]) == "/tmp/clip.mp4"
    assert dropped_play_source(["/tmp/readme.txt"]) == ""


def test_play_request_prefers_a_file_in_the_field_over_the_page():
    watch = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    source, start = play_request("/tmp/clip.mp4", watch, start=9)
    assert source == "/tmp/clip.mp4"
    assert start == 0.0


def test_play_request_uses_the_page_video_when_the_field_is_the_site():
    watch = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    source, start = play_request("https://www.youtube.com/", watch, start=4.5)
    assert source == watch
    assert start == 4.5


def test_play_request_does_not_treat_the_youtube_homepage_as_a_video():
    assert play_request("https://www.youtube.com/", "https://www.youtube.com/") == ("", 0.0)


def test_play_request_falls_back_to_a_copied_file():
    assert play_request("", "https://www.youtube.com/", pasteboard="/tmp/clip.mp4") == (
        "/tmp/clip.mp4",
        0.0,
    )


def test_leaving_a_youtube_video_stops_the_deck():
    watch = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert page_follow_action(watch, "https://www.youtube.com/") == ("", "stop")
    assert page_follow_action(watch, watch) == (watch, "")


def test_a_local_file_is_not_stopped_by_sitting_on_youtube():
    """The window stays on youtube.com while a dropped file plays; that is not 'left the video'."""
    path = "/tmp/clip.mp4"
    assert page_follow_action(path, "https://www.youtube.com/") == (path, "")
    watch = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert page_follow_action(path, watch) == (watch, watch)

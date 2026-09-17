"""The GUI is a remote for the CLI. These tests never open a window or a deck."""

from __future__ import annotations

import sys
import json
import time
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
    playlist_add,
    playlist_extend,
    youtube_playlist_page,
    youtube_playlist_entries,
    playlist_advance,
    queue_play_source,
    playlist_next,
    playlist_prev,
    repeat_label,
    player_prefs_load,
    player_prefs_save,
    playlist_label,
    playlist_load,
    playlist_remove,
    playlist_save,
    playlist_should_loop,
    deck_now_playing,
    deck_session_active,
    deck_has_picture,
    deck_playhead,
    format_clock,
    source_duration,
    deck_duration,
    parse_duration,
    playlist_normalize,
    should_retry_pending,
    playlist_entry,
    playlist_source,
    playlist_move,
    playlist_title,
    playlist_subtitle,
    source_identity,
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


def test_playlist_label_is_a_name_not_a_path():
    assert playlist_label("/tmp/clips/iris.mp4") == "iris"
    assert playlist_label("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "YouTube · dQw4w9WgXcQ"
    assert playlist_label({"source": "https://www.youtube.com/watch?v=x",
                           "title": "Never Gonna Give You Up",
                           "channel": "Rick Astley"}) == "Rick Astley · Never Gonna Give You Up"
    assert playlist_label("") == ""


def test_playlist_add_skips_a_source_already_queued():
    first = playlist_add([], "/tmp/a.mp4")
    assert [playlist_source(item) for item in first] == ["/tmp/a.mp4"]
    assert playlist_add(first, "/tmp/a.mp4") == first
    later = playlist_add(playlist_add(first, "/tmp/b.mp4"), "/tmp/a.mp4")
    assert [playlist_source(item) for item in later] == ["/tmp/a.mp4", "/tmp/b.mp4"]
    watch = "https://www.youtube.com/watch?v=x"
    queued = playlist_add([], watch)
    assert playlist_add(queued, watch + "&list=PLabc") == queued
    assert [playlist_source(item) for item in playlist_normalize([watch, watch, "/tmp/b.mp4"])] == [
        watch, "/tmp/b.mp4",
    ]
    assert [playlist_source(item) for item in playlist_add(first, "/tmp/b.mp4")] == [
        "/tmp/a.mp4", "/tmp/b.mp4",
    ]


def test_playlist_advance_plays_through_then_stops():
    items = playlist_add(playlist_add([], "/tmp/a.mp4"), "/tmp/b.mp4")
    assert playlist_advance(items, "/tmp/a.mp4") == "/tmp/b.mp4"
    assert playlist_advance(items, "/tmp/b.mp4") == ""
    assert playlist_advance(items, "") == "/tmp/a.mp4"
    assert [playlist_source(item) for item in playlist_remove(items, 0)] == ["/tmp/b.mp4"]


def test_a_queue_does_not_loop_a_file():
    assert playlist_should_loop("/tmp/a.mp4", ["/tmp/a.mp4"]) is False
    assert playlist_should_loop("/tmp/a.mp4", ["/tmp/a.mp4"], repeat="one") is True
    assert playlist_should_loop("/tmp/a.mp4", ["/tmp/a.mp4"], repeat="all") is True
    assert playlist_should_loop("/tmp/a.mp4", ["/tmp/a.mp4", "/tmp/b.mp4"], repeat="all") is False
    assert playlist_should_loop("https://www.youtube.com/watch?v=x", []) is False


def test_playlist_roundtrip(tmp_path):
    path = tmp_path / "playlist.json"
    playlist_save(path, ["/tmp/a.mp4", "https://www.youtube.com/watch?v=x"])
    assert [playlist_source(item) for item in playlist_load(path)] == [
        "/tmp/a.mp4", "https://www.youtube.com/watch?v=x",
    ]
    assert playlist_load(tmp_path / "missing.json") == []


def test_deck_now_playing_reads_the_player_receipt(tmp_path):
    path = tmp_path / "host.json"
    path.write_text('{"source":"/tmp/iris.mp4","phase":"active"}\n', encoding="utf-8")
    assert deck_now_playing(path) == "/tmp/iris.mp4"
    path.write_text('{"source":"/tmp/iris.mp4","phase":"terminal"}\n', encoding="utf-8")
    assert deck_now_playing(path) == ""
    assert deck_now_playing(tmp_path / "gone.json") == ""


def test_source_identity_uses_oembed_title_and_channel():
    class Resp:
        def read(self):
            return b'{"title":"Never Gonna Give You Up","author_name":"Rick Astley"}'

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    def fetch(_req, timeout=5):
        return Resp()

    title, channel = source_identity(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ", fetch=fetch,
    )
    assert (title, channel) == ("Never Gonna Give You Up", "Rick Astley")


def test_file_identity_falls_back_to_the_stem():
    def probe(*_a, **_k):
        raise OSError("no ffprobe")

    title, channel = source_identity("/tmp/clips/iris.mp4", probe=probe)
    assert (title, channel) == ("iris", "")


def test_playlist_title_and_channel_are_separate_lines():
    item = {"source": "https://www.youtube.com/watch?v=x",
            "title": "Never Gonna Give You Up", "channel": "Rick Astley"}
    assert playlist_title(item) == "Never Gonna Give You Up"
    assert playlist_subtitle(item) == "Rick Astley"
    assert playlist_title("/tmp/clips/iris.mp4") == "iris"


def test_playlist_move_reorders_rows():
    items = playlist_add(playlist_add([], "/tmp/a.mp4"), "/tmp/b.mp4")
    items = playlist_add(items, "/tmp/c.mp4")
    moved = playlist_move(items, 0, 2)
    assert [playlist_source(item) for item in moved] == ["/tmp/b.mp4", "/tmp/c.mp4", "/tmp/a.mp4"]
    assert playlist_move(items, 9, 0) == items


def test_deck_session_active_reads_phase(tmp_path):
    path = tmp_path / "host.json"
    path.write_text('{"phase":"active","source":"/tmp/a.mp4"}\n', encoding="utf-8")
    assert deck_session_active(path) is True
    path.write_text('{"phase":"terminal"}\n', encoding="utf-8")
    assert deck_session_active(path) is False
    assert deck_session_active(tmp_path / "gone.json") is False


def test_youtube_overlay_queues_without_playing():
    text = (ROOT / "src" / "ghostdeck" / "app.py").read_text(encoding="utf-8")
    assert "ghostdeck-queue" in text
    assert "post('queue')" in text
    assert 'kind == "queue"' in text


def test_youtube_playlist_page_is_not_a_single_watch():
    page = "https://www.youtube.com/playlist?list=PLabcdefghijk"
    assert youtube_playlist_page(page) == page
    assert youtube_playlist_page("https://www.youtube.com/watch?v=x&list=PLabcdefghijk") == ""
    assert youtube_playlist_page("https://www.youtube.com/playlist?list=RDmix") == ""


def test_youtube_playlist_entries_use_flat_yt_dlp():
    payload = {
        "entries": [
            {"id": "aaa", "title": "One", "uploader": "Ch"},
            {"id": "bbb", "title": "Two", "channel": "Ch"},
        ]
    }

    def run(argv, **_k):
        assert argv[:3] == ["yt-dlp", "--flat-playlist", "--no-warnings"]
        class Result:
            stdout = json.dumps(payload)
        return Result()

    items = youtube_playlist_entries("https://www.youtube.com/playlist?list=PLabc", run=run)
    assert [playlist_source(item) for item in items] == [
        "https://www.youtube.com/watch?v=aaa",
        "https://www.youtube.com/watch?v=bbb",
    ]
    assert items[0]["title"] == "One"


def test_playlist_extend_skips_sources_already_queued():
    first = playlist_add([], "https://www.youtube.com/watch?v=aaa")
    extra = [
        playlist_entry("https://www.youtube.com/watch?v=aaa", "One", "Ch"),
        playlist_entry("https://www.youtube.com/watch?v=bbb", "Two", "Ch"),
    ]
    out = playlist_extend(first, extra)
    assert [playlist_source(item) for item in out] == [
        "https://www.youtube.com/watch?v=aaa",
        "https://www.youtube.com/watch?v=bbb",
    ]


def test_opening_a_playlist_page_does_not_stop_the_deck():
    watch = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    page = "https://www.youtube.com/playlist?list=PLabc"
    assert page_follow_action(watch, page) == (watch, "")


def test_format_clock_is_compact():
    assert format_clock(0) == "0:00"
    assert format_clock(65) == "1:05"
    assert format_clock(3661) == "1:01:01"
    assert format_clock(-3) == "0:00"


def test_deck_playhead_adds_elapsed_since_first_frame(tmp_path):
    path = tmp_path / "host.json"
    path.write_text(
        json.dumps(
            {
                "phase": "active",
                "source": "/tmp/a.mp4",
                "start": 12,
                "playbackRate": 1,
                "diagnostics": {
                    "startedMonotonicNs": 1_000_000_000,
                    "hostElapsedNs": 8_000_000_000,
                    "milestones": {
                        "firstConsumedReceipt": {"monotonicNs": 3_000_000_000},
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    source, pos, active = deck_playhead(path)
    assert source == "/tmp/a.mp4"
    assert active is True
    assert abs(pos - 18.0) < 0.01
def test_deck_playhead_keeps_moving_after_the_last_host_publish(tmp_path):
    path = tmp_path / "host.json"
    payload = {
        "phase": "active",
        "source": "/tmp/a.mp4",
        "start": 12,
        "playbackRate": 1,
        "playheadAt": time.time() - 2.0,
        "diagnostics": {
            "startedMonotonicNs": 1_000_000_000,
            "hostElapsedNs": 8_000_000_000,
            "milestones": {"firstConsumedReceipt": {"monotonicNs": 3_000_000_000}},
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    _source, pos, active = deck_playhead(path)
    assert active is True
    assert abs(pos - 20.0) < 0.3
def test_deck_playhead_stays_at_start_until_the_first_frame(tmp_path):
    path = tmp_path / "host.json"
    path.write_text(
        json.dumps(
            {
                "phase": "active",
                "source": "/tmp/a.mp4",
                "start": 0,
                "playheadAt": time.time() - 0.5,
                "diagnostics": {"startedMonotonicNs": 1_000_000_000, "hostElapsedNs": 3_000_000_000},
            }
        ),
        encoding="utf-8",
    )
    source, pos, active = deck_playhead(path)
    assert source == "/tmp/a.mp4"
    assert active is True
    assert pos == 0.0
    assert deck_has_picture(path) is False


def test_deck_has_picture_needs_a_sent_frame(tmp_path):
    path = tmp_path / "host.json"
    path.write_text(
        json.dumps({"phase": "active", "playheadAt": time.time(), "diagnostics": {"framesSent": 12}}),
        encoding="utf-8",
    )
    assert deck_has_picture(path) is True
    path.write_text('{"phase":"active","diagnostics":{"framesSent":0}}\n', encoding="utf-8")
    assert deck_has_picture(path) is False


def test_deck_has_picture_ignores_a_stale_host_receipt(tmp_path):
    path = tmp_path / "host.json"
    path.write_text(
        json.dumps(
            {
                "phase": "active",
                "playheadAt": time.time() - 10,
                "diagnostics": {"framesSent": 231},
            }
        ),
        encoding="utf-8",
    )
    assert deck_has_picture(path) is False


def test_abandon_host_session_marks_a_leftover_active_receipt(tmp_path):
    from ghostdeck.play import abandon_host_session
    path = tmp_path / "host.json"
    path.write_text('{"phase":"active","source":"/tmp/a.mp4","playheadAt":1}\n', encoding="utf-8")
    abandon_host_session(path)
    assert json.loads(path.read_text())["phase"] == "terminal"


def test_source_duration_reads_ffprobe():
    def probe(argv, **_k):
        assert argv[0] == "ffprobe"
        class Result:
            stdout = "183.4\n"
        return Result()

    assert source_duration("/tmp/clip.mp4", probe=probe) == 183.4
    assert source_duration("", probe=probe) == 0.0
def test_should_retry_pending_allows_a_seek_on_the_same_source():
    watch = "https://www.youtube.com/watch?v=x"
    assert should_retry_pending(watch, 40.0, watch, 10.0) is True
    assert should_retry_pending(watch, 10.0, watch, 10.0) is False
    assert should_retry_pending("/tmp/b.mp4", 0.0, watch, 10.0) is True
    assert should_retry_pending("", 40.0, watch, 10.0) is False


def test_youtube_duration_uses_yt_dlp_metadata():
    def probe(argv, **_k):
        assert argv[0] == "yt-dlp"
        assert "-O" in argv
        class Result:
            stdout = "146.601\n"
        return Result()

    assert source_duration("https://www.youtube.com/watch?v=x", probe=probe) == 146.601


def test_deck_duration_reads_the_player_receipt(tmp_path):
    path = tmp_path / "host.json"
    path.write_text('{"source":"/tmp/a.mp4","duration":183.4,"phase":"active"}\n', encoding="utf-8")
    assert deck_duration(path) == 183.4
    assert parse_duration("NA") == 0.0
    assert parse_duration(-1) == 0.0


def test_playlist_next_respects_repeat_and_shuffle():
    items = playlist_add(playlist_add([], "/tmp/a.mp4"), "/tmp/b.mp4")
    items = playlist_add(items, "/tmp/c.mp4")
    assert playlist_next(items, "/tmp/b.mp4") == "/tmp/c.mp4"
    assert playlist_next(items, "/tmp/c.mp4") == ""
    assert playlist_next(items, "/tmp/c.mp4", repeat="all") == "/tmp/a.mp4"
    assert playlist_next(items, "/tmp/b.mp4", repeat="one") == "/tmp/b.mp4"
    class Rng:
        def choice(self, pool):
            assert "/tmp/b.mp4" not in pool
            return pool[0]
    assert playlist_next(items, "/tmp/b.mp4", shuffle=True, rng=Rng()) == "/tmp/a.mp4"
    assert playlist_prev(items, "/tmp/b.mp4") == "/tmp/a.mp4"
    assert playlist_prev(items, "/tmp/a.mp4", repeat="all") == "/tmp/c.mp4"
    assert repeat_label("all") == "전체"
    assert repeat_label("one") == "한곡"
    assert repeat_label("off") == "반복"


def test_player_prefs_roundtrip(tmp_path):
    path = tmp_path / "player.json"
    player_prefs_save(path, {"repeat": "one", "shuffle": True})
    assert player_prefs_load(path) == {"repeat": "one", "shuffle": True}
    assert player_prefs_load(tmp_path / "gone.json") == {"repeat": "off", "shuffle": False}
def test_queue_play_source_prefers_selection_then_now_then_head():
    items = playlist_add(playlist_add([], "/tmp/a.mp4"), "/tmp/b.mp4")
    assert queue_play_source(items, 1) == "/tmp/b.mp4"
    assert queue_play_source(items, -1, now="/tmp/a.mp4") == "/tmp/a.mp4"
    assert queue_play_source(items, -1, seen="/tmp/b.mp4") == "/tmp/b.mp4"
    assert queue_play_source(items, -1) == "/tmp/a.mp4"
    assert queue_play_source([], -1) == ""

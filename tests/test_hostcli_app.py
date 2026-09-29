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
    play_fit,
    play_crop_choice,
    fit_label,
    crop_label,
    play_crop_argv,
    playlist_click_row,
    playlist_playing,
    Layout,
    LAYOUT_DEFAULT,
    LAYOUT_MIN,
    QUEUE_ROW_H,
    clamp_seek,
    fetch_thumbnail,
    now_state_label,
    playlist_meta,
    seek_step,
    thumbnail_cache_path,
    thumbnail_url,
    volume_step,
    youtube_video_id,
    play_failure_note,
    play_success_note,
    should_auto_next,
    opening_session,
    seek_note,
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
    deck_crop,
    request_live_seek,
    request_live_volume,
    request_live_overlay,
    clamp_volume,
    audio_gain,
    wrap_playhead,
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
    failure_note,
    message_head,
    playlist_index,
    recovery_action,
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
    assert status_text("usb=adb shim=up copy=yes playing=yes") == "덱 ADB · 키 연결됨 · 재생 중"
    assert status_text("usb=adb shim=up copy=yes playing=yes", has_picture=False) == "덱 ADB · 키 연결됨 · 여는 중"
    assert status_text("usb=adb shim=up copy=yes playing=no") == "덱 ADB · 키 연결됨 · 멈춤"
    assert status_text("usb=hid shim=down copy=yes playing=no") == "덱 HID · 키 없음 · 멈춤"
    assert status_text("usb=none shim=down copy=no playing=no") == "덱 없음 · 키 없음 · 멈춤 · 연결을 누르십시오"


def test_a_bridge_that_is_up_is_not_worth_saying_in_the_window():
    """`브리지 꺼짐` is the state where 재생 has something to do; the healthy case stays quiet."""
    assert "브리지" not in status_text("usb=adb shim=up copy=yes playing=no")


def test_a_wedged_transport_is_shown_as_the_physical_fix():
    """`usb=adb (offline)` used to split on the space and render as a healthy `덱 ADB`.

    `status` now prints the wedged state as its own `transport=` field, and the window shows the
    only remedy that works (replug) instead of keys and play state that cannot matter.
    """
    line = "usb=adb transport=offline shim=up copy=yes playing=no"
    assert parse_status_fields(line)["transport"] == "offline"
    text = status_text(line)
    assert "offline" in text and "뽑았다" in text, text
    assert "키 연결됨" not in text
    assert recovery_action(line) == ""
    assert status_text("usb=unknown shim=down copy=yes playing=no") == "덱 알 수 없음 · 키 없음 · 멈춤"


def test_status_line_from_the_cli_round_trips_through_the_window(monkeypatch, capsys):
    """The CLI writes the line and the window reads it; drive both, not a hand-written string."""
    from ghostdeck import cli, play, studio, usb

    monkeypatch.setattr(usb, "detect", lambda: {"serial": "S", "vid": 1, "pid": 2, "mode": "adb"})
    monkeypatch.setattr(play, "deck_transport", lambda **_k: ("S", "offline", []))
    monkeypatch.setattr(studio, "running", lambda: False)
    monkeypatch.setattr(studio, "copy_exists", lambda: True)
    monkeypatch.setattr(play, "playing", lambda: False)
    cli._status()
    out = capsys.readouterr().out
    assert parse_status_fields(out) == {
        "usb": "adb", "transport": "offline", "shim": "down", "copy": "yes", "playing": "no",
    }
    assert "뽑았다" in status_text(out)


def test_recovery_action_names_the_one_button_that_helps():
    assert recovery_action("usb=none shim=down copy=no playing=no") == "reconnect"
    assert recovery_action("usb=adb shim=down copy=yes playing=no") == "studio"
    assert recovery_action("usb=hid shim=down copy=yes playing=no") == "studio"
    assert recovery_action("usb=adb shim=up copy=yes playing=no") == ""
    # No official app: `studio` cannot help, and shim=down is simply how that host runs.
    assert recovery_action("usb=adb shim=down copy=no playing=no", has_studio=False) == ""
    assert recovery_action("usb=unknown shim=down copy=no playing=no") == ""
    assert recovery_action("") == ""


def test_a_host_without_studio_says_why_there_are_no_keys():
    line = "usb=adb shim=down copy=no playing=no"
    assert status_text(line, has_studio=False) == "덱 ADB · Studio 없음 · 멈춤"
    assert status_text(line) == "덱 ADB · 키 없음 · 멈춤"


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
def test_a_resume_skips_the_status_probe():
    calls: list[list[str]] = []

    def run(argv):
        calls.append(list(argv))
        return CommandResult(argv, 0, "", "")

    DeckRemote(run).play("/tmp/clip.mp4", start=12.0, crop="none")
    assert calls == [["play", "/tmp/clip.mp4", "--start", "12.000", "--crop", "none"]]
def test_request_live_seek_writes_the_player_file(tmp_path):
    path = tmp_path / "seek"
    assert request_live_seek(41.25, path=path) is True
    assert path.read_text(encoding="utf-8").strip() == "41.250"


def test_request_live_volume_writes_the_player_file(tmp_path):
    path = tmp_path / "volume"
    assert request_live_volume(0.25, path=path) is True
    assert path.read_text(encoding="utf-8").strip() == "0.2500"


def test_request_live_overlay_writes_the_player_file(tmp_path):
    path = tmp_path / "overlay"
    assert request_live_overlay(0.4, path=path) is True
    assert path.read_text(encoding="utf-8").strip() == "0.4000"


def test_request_live_overlay_pushes_alpha_to_the_deck(monkeypatch, tmp_path):
    seen: list[float] = []
    monkeypatch.setattr("ghostdeck.app._push_studio_alpha", lambda gain: seen.append(gain))
    path = tmp_path / "overlay"
    assert request_live_overlay(0.25, path=path) is True
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not seen:
        time.sleep(0.01)
    assert seen == [0.25]

def test_audio_gain_mutes_without_losing_the_slider():
    assert clamp_volume(1.4) == 1.0
    assert clamp_volume(-0.2) == 0.0
    assert clamp_volume("nope") == 1.0
    assert audio_gain(0.4, False) == 0.4
    assert audio_gain(0.4, True) == 0.0


def test_gui_play_passes_volume():
    calls: list[list[str]] = []

    def run(argv):
        calls.append(list(argv))
        if argv[:1] == ["status"]:
            return CommandResult(argv, 0, "usb=adb shim=up copy=yes playing=no\n", "")
        return CommandResult(argv, 0, "", "")

    DeckRemote(run).play("/tmp/clip.mp4", volume=0.25)
    assert calls[-1] == ["play", "/tmp/clip.mp4", "--volume", "0.2500"]


def test_deck_crop_reads_a_letterbox_rect(tmp_path):
    path = tmp_path / "host.json"
    path.write_text('{"crop":"1920:804:0:138","phase":"active"}\n', encoding="utf-8")
    assert deck_crop(path) == "1920:804:0:138"
    path.write_text('{"crop":"auto"}\n', encoding="utf-8")
    assert deck_crop(path) == ""


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
            return CommandResult(argv, 1, "", "hidshim Studio copy did not stay running")
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
            return CommandResult(argv, 1, "", "hidshim Studio copy did not stay running")
        raise AssertionError(f"unexpected {argv}")

    results = DeckRemote(run).play("/tmp/clip.mp4")
    assert calls == [["status"], ["studio"]]
    assert results[-1].code == 1
    assert "did not stay running" in results[-1].detail


def test_play_falls_back_to_the_bridge_when_studio_is_not_installed():
    """No official app: `studio` can never work, but the bridge alone still plays video."""
    from ghostdeck import studio

    calls: list[list[str]] = []

    def run(argv):
        calls.append(list(argv))
        if argv == ["status"]:
            return CommandResult(argv, 0, "usb=adb shim=down copy=no playing=no\n", "")
        if argv == ["studio"]:
            return CommandResult(argv, 1, "", f"{studio.STUDIO_MISSING} /Applications/Ulanzi Studio.app\n")
        return CommandResult(argv, 0, "", "")

    results = DeckRemote(run).play("/tmp/clip.mp4")
    assert calls == [["status"], ["studio"], ["bridge"], ["play", "/tmp/clip.mp4"]]
    assert results[-1].code == 0


def test_bridge_fallback_failure_is_reported_without_playing():
    from ghostdeck import studio

    calls: list[list[str]] = []

    def run(argv):
        calls.append(list(argv))
        if argv == ["status"]:
            return CommandResult(argv, 0, "usb=adb shim=down copy=no playing=no\n", "")
        if argv == ["studio"]:
            return CommandResult(argv, 1, "", f"{studio.STUDIO_MISSING} /Applications/Ulanzi Studio.app\n")
        if argv == ["bridge"]:
            return CommandResult(argv, 1, "", "hidshim bridge socket did not come up\n")
        raise AssertionError(f"unexpected {argv}")

    results = DeckRemote(run).play("/tmp/clip.mp4")
    assert calls == [["status"], ["studio"], ["bridge"]]
    assert results[-1].argv == ["bridge"] and results[-1].code == 1


def test_a_bridge_refusal_on_a_studio_less_host_retries_through_the_bridge():
    from ghostdeck import studio

    calls: list[list[str]] = []
    plays = 0

    def run(argv):
        nonlocal plays
        calls.append(list(argv))
        if argv[0] == "status":
            return CommandResult(argv, 0, "usb=adb shim=up copy=yes playing=no\n", "")
        if argv[0] == "studio":
            return CommandResult(argv, 1, "", f"{studio.STUDIO_MISSING} /x\n")
        if argv[0] == "bridge":
            return CommandResult(argv, 0, "", "")
        plays += 1
        return CommandResult(argv, 1, "", studio.BRIDGE_DOWN + " (none)") if plays == 1 else CommandResult(argv, 0, "", "")

    results = DeckRemote(run).play("/tmp/clip.mp4")
    assert calls == [
        ["status"], ["play", "/tmp/clip.mp4"], ["studio"], ["bridge"], ["play", "/tmp/clip.mp4"],
    ]
    assert results[-1].code == 0


def test_detail_keeps_the_reason_of_a_multi_line_error():
    """The agent-missing error ends in an indented URL; the headline is the reason, not the URL."""
    stderr = (
        "d200-color-agent is not built.\n"
        "  Download the ARMv7 Linux build:\n"
        "    https://example.invalid/d200-color-agent\n"
    )
    result = CommandResult(["play"], 1, "", stderr)
    assert result.detail == "d200-color-agent is not built."
    assert "에이전트" in play_failure_note(result.detail)
    # A warning printed before the final one-line error does not win.
    assert message_head("warning: something\nno D200 on USB\n") == "no D200 on USB"
    assert message_head("") == ""
    assert CommandResult(["x"], 3, "", "").detail == "exit 3"


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


def test_reconnect_runs_the_cli():
    calls: list[list[str]] = []

    def run(argv):
        calls.append(list(argv))
        return CommandResult(argv, 0, "", "")

    result = DeckRemote(run).reconnect()
    assert calls == [["reconnect"]]
    assert result.argv == ["reconnect"]


def test_studio_and_bridge_run_the_cli():
    calls: list[list[str]] = []

    def run(argv):
        calls.append(list(argv))
        return CommandResult(argv, 0, "", "")

    remote = DeckRemote(run)
    assert remote.studio().argv == ["studio"]
    assert remote.bridge().argv == ["bridge"]
    assert calls == [["studio"], ["bridge"]]


def test_gui_play_passes_fit():
    calls: list[list[str]] = []

    def run(argv):
        calls.append(list(argv))
        if argv[:1] == ["status"]:
            return CommandResult(argv, 0, "usb=adb shim=up copy=yes playing=no\n", "")
        return CommandResult(argv, 0, "", "")

    DeckRemote(run).play("/tmp/clip.mp4", fit="cover")
    assert calls[-1] == ["play", "/tmp/clip.mp4", "--fit", "cover"]


def test_play_fit_and_crop_choice_clamp():
    assert play_fit("pad") == "pad"
    assert play_fit("nope") == "auto"
    assert play_crop_choice("none") == "none"
    assert play_crop_choice("1920:804:0:138") == "auto"


def test_reconnect_restarts_adb_quits_the_copy_and_launches_studio(monkeypatch):
    from ghostdeck import studio, usb

    seen: list[str] = []
    monkeypatch.setattr(studio.adb, "restart_server", lambda: seen.append("adb"))
    monkeypatch.setattr(usb, "detect", lambda: {"serial": "X", "vid": 1, "pid": 2, "mode": "hid"})
    monkeypatch.setattr(studio, "running", lambda: True)
    monkeypatch.setattr(studio, "_quit_copy", lambda: seen.append("quit"))
    monkeypatch.setattr(studio, "_stop_our_bridge", lambda: seen.append("bridge"))
    monkeypatch.setattr(studio, "launch", lambda: seen.append("launch"))
    studio.reconnect(wait=0)
    assert seen == ["adb", "quit", "bridge", "launch"]


def test_reconnect_refuses_when_the_deck_is_missing(monkeypatch):
    from ghostdeck import studio, usb

    monkeypatch.setattr(studio.adb, "restart_server", lambda: None)
    monkeypatch.setattr(usb, "detect", lambda: {"serial": None, "vid": None, "pid": None, "mode": "none"})
    raised = None
    try:
        studio.reconnect(wait=0)
    except RuntimeError as error:
        raised = error
    assert raised is not None
    assert "USB" in str(raised)


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


def test_browsing_does_not_start_or_stop_the_deck():
    watch = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert page_follow_action(watch, "https://www.youtube.com/") == (watch, "")
    assert page_follow_action(watch, watch) == (watch, "")
    path = "/tmp/clip.mp4"
    assert page_follow_action(path, "https://www.youtube.com/") == (path, "")
    assert page_follow_action(path, watch) == (path, "")


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
    assert "function silence(v)" in text
    assert "post('play')" not in text
    assert 'kind == "play" or kind == "nav"' in text


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
def test_wrap_playhead_loops_back_to_the_start():
    assert wrap_playhead(18.0, 8.0, True) == 2.0
    assert wrap_playhead(18.0, 8.0, False) == 8.0
    assert wrap_playhead(-1, 8.0, True) == 0.0


def test_deck_playhead_wraps_when_the_source_loops(tmp_path):
    path = tmp_path / "host.json"
    path.write_text(
        json.dumps(
            {
                "phase": "active",
                "source": "/tmp/a.mp4",
                "start": 0,
                "loop": True,
                "duration": 8,
                "playbackRate": 1,
                "diagnostics": {
                    "startedMonotonicNs": 1_000_000_000,
                    "hostElapsedNs": 12_000_000_000,
                    "milestones": {
                        "firstConsumedReceipt": {"monotonicNs": 3_000_000_000},
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    _source, pos, active = deck_playhead(path)
    assert active is True
    assert abs(pos - 2.0) < 0.01
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
    assert player_prefs_load(path) == {
        "repeat": "one",
        "shuffle": True,
        "volume": 1.0,
        "muted": False,
        "overlay": 1.0,
        "fit": "auto",
        "crop": "auto",
    }
    player_prefs_save(path, {"repeat": "off", "shuffle": False, "volume": 0.3, "muted": True})
    assert player_prefs_load(path) == {
        "repeat": "off",
        "shuffle": False,
        "volume": 0.3,
        "muted": True,
        "overlay": 1.0,
        "fit": "auto",
        "crop": "auto",
    }
    assert player_prefs_load(tmp_path / "gone.json") == {
        "repeat": "off",
        "shuffle": False,
        "volume": 1.0,
        "muted": False,
        "overlay": 1.0,
        "fit": "auto",
        "crop": "auto",
    }
def test_the_stopped_track_is_the_row_play_resumes():
    """■ on track 3 then ▶ played track 0: the table forced row 0 selected, and selection wins.

    The selection now follows the deck (`playlist_index`) and the table allows it to be empty.
    """
    items = playlist_normalize([playlist_entry(f"/tmp/t{i}.mp4") for i in range(4)])
    stopped = "/tmp/t3.mp4"
    row = playlist_index(items, stopped)
    assert row == 3
    assert queue_play_source(items, row, "", stopped) == stopped
    assert queue_play_source(items, -1, "", stopped) == stopped
    watch = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert playlist_index(playlist_add([], watch), "https://youtu.be/dQw4w9WgXcQ") == 0
    assert playlist_index(items, "/tmp/other.mp4") == -1
    assert playlist_index(items, "") == -1
    text = Path(__file__).resolve().parents[1].joinpath("src/ghostdeck/app.py").read_text(encoding="utf-8")
    assert "setAllowsEmptySelection_(False)" not in text


def test_esc_is_not_a_stop_shortcut_and_the_app_has_a_menu_bar():
    """Esc on ■ stopped the deck while editing the URL; without a main menu ⌘Q/⌘V had no route."""
    text = Path(__file__).resolve().parents[1].joinpath("src/ghostdeck/app.py").read_text(encoding="utf-8")
    assert 'setKeyEquivalent_("\\x1b")' not in text
    assert "NSApp.setMainMenu_(bar)" in text
    for action in ('"paste:"', '"copy:"', '"selectAll:"', '"terminate:"', '"stop:", "."'):
        assert action in text, action


def test_queue_play_source_prefers_selection_then_now_then_head():
    items = playlist_add(playlist_add([], "/tmp/a.mp4"), "/tmp/b.mp4")
    assert queue_play_source(items, 1) == "/tmp/b.mp4"
    assert queue_play_source(items, -1, now="/tmp/a.mp4") == "/tmp/a.mp4"
    assert queue_play_source(items, -1, seen="/tmp/b.mp4") == "/tmp/b.mp4"
    assert queue_play_source(items, -1) == "/tmp/a.mp4"
    assert queue_play_source([], -1) == ""

def test_status_text_one_product():
    line = status_text("usb=adb shim=up copy=yes playing=no")
    assert "키 연결됨" in line
    assert "공식" not in line
    assert "Studio" not in line
    assert status_text("usb=adb shim=down copy=yes playing=no") == "덱 ADB · 키 없음 · 멈춤"


def test_play_success_note_waits_for_picture():
    assert "재생 중" not in play_success_note(False)
    assert "첫 프레임" in play_success_note(False)
    assert play_success_note(True).startswith("덱에서 재생 중입니다")


def test_open_failure_is_korean():
    note = play_failure_note("RuntimeError: video OPEN failed with code 1")
    assert note == "덱이 아직 이전 영상을 안 놓았습니다."
    assert "RuntimeError" not in note
    assert play_failure_note("player exited with status 1 before it started; nothing is playing") == (
        "재생을 시작하지 못했습니다."
    )
    assert play_failure_note("") == "재생을 시작하지 못했습니다."


def test_the_refusals_a_user_actually_hits_are_korean_and_actionable():
    """Driven from the CLI's own messages, so a reworded refusal shows up here as English."""
    from ghostdeck import studio

    cases = {
        "no D200 on USB": "연결",
        "timed out": "연결",
        f"{studio.STUDIO_MISSING} {studio.ORIGINAL}": "브리지",
        f"{studio.BRIDGE_DOWN} (absent), and the player reaches the deck through it": "스튜디오",
        "ffmpeg not on PATH: install it (brew install ffmpeg)": "brew install ffmpeg",
        "yt-dlp not on PATH: install it (brew install yt-dlp)": "brew install yt-dlp",
        "hidapi is not installed (pip install hidapi)": "pip install",
        "the deck (S) is attached in ADB mode but its adb transport is offline, so it": "뽑았다",
        "D200 is not enumerating through ADB: no bridge serial": "연결",
        "hidshim bridge socket did not come up": "연결",
        f"a listener holds {studio.SOCKET} but no live {studio.BRIDGE.name} of ours owns it": "브리지 소켓",
        "d200-color-agent is not built.": "README",
    }
    for raw, hint in cases.items():
        note = failure_note(raw)
        assert note != raw, f"left in English: {raw}"
        assert hint in note, (raw, note)
        assert failure_note(f"RuntimeError: {raw}") == note
    # A reason the table does not know is shown rather than hidden behind a generic line.
    assert failure_note("something new") == "something new"
    assert failure_note("") == "실패했습니다."


def test_a_bridge_that_lost_the_deck_recovers_through_studio(monkeypatch):
    """The observed failure: our own bridge held the socket with the deck back in HID.

    `require_bridge` names it as the bridge being down, so the window's existing recovery (`studio`
    then retry) runs instead of a dead end, and the note says the bridge lost the deck.
    """
    from ghostdeck import studio

    monkeypatch.setattr(studio, "_socket_state", lambda: (studio._ENDPOINT_LIVE, ""))
    monkeypatch.setattr(studio, "_bridge_owner_live", lambda: True)
    monkeypatch.setattr(studio, "_bridge_lost_deck", lambda: True)
    try:
        studio.require_bridge()
    except RuntimeError as error:
        refusal = str(error)
    else:
        raise AssertionError("a bridge that lost the deck was accepted")
    assert bridge_down(refusal), refusal
    assert "놓쳤습니다" in play_failure_note(refusal)

    calls: list[list[str]] = []
    plays = 0

    def run(argv):
        nonlocal plays
        calls.append(list(argv))
        if argv[0] == "status":
            return CommandResult(argv, 0, "usb=hid shim=up copy=yes playing=no\n", "")
        if argv[0] == "studio":
            return CommandResult(argv, 0, "", "")
        plays += 1
        return CommandResult(argv, 1, "", refusal + "\n") if plays == 1 else CommandResult(argv, 0, "", "")

    results = DeckRemote(run).play("/tmp/clip.mp4")
    assert [call[0] for call in calls] == ["status", "play", "studio", "play"]
    assert results[-1].code == 0


def test_real_cli_refusals_map_to_korean(monkeypatch, tmp_path):
    """Raise the actual RuntimeErrors, not copies of their text."""
    from ghostdeck import play, studio

    monkeypatch.setattr(studio, "ORIGINAL", tmp_path / "no-studio.app")
    monkeypatch.setattr(studio, "COPY", tmp_path / "no-copy.app")
    monkeypatch.setattr(studio, "_require_build_tools", lambda: None)
    try:
        studio.ensure_copy()
    except RuntimeError as error:
        assert "브리지" in failure_note(str(error))
    else:
        raise AssertionError("ensure_copy did not refuse a missing Studio")
    monkeypatch.setattr(play.shutil, "which", lambda _tool: None)
    try:
        play._require_tools("https://youtu.be/x")
    except RuntimeError as error:
        assert "brew install ffmpeg" in failure_note(str(error))
    else:
        raise AssertionError("_require_tools did not refuse")


def test_auto_next_skips_live_session():
    assert should_auto_next(
        playing=True, session_active=False, was_playing=True, saw_picture=True,
        user_stopped=False, busy=False,
    ) is False
    assert should_auto_next(
        playing=False, session_active=True, was_playing=True, saw_picture=True,
        user_stopped=False, busy=False,
    ) is True
    assert should_auto_next(
        playing=False, session_active=True, was_playing=False, saw_picture=False,
        user_stopped=False, busy=False,
    ) is False
    assert should_auto_next(
        playing=False, session_active=False, was_playing=True, saw_picture=True,
        user_stopped=False, busy=False,
    ) is True
    assert opening_session(playing=True, has_picture=False, session_active=True) is True
    assert opening_session(playing=False, has_picture=True, session_active=False) is False


def test_fit_crop_labels_name_every_mode_distinctly():
    """Each press cycles the mode; the label must say which mode is now selected."""
    fits = [fit_label(mode) for mode in ("auto", "pad", "cover")]
    assert len(set(fits)) == 3 and all(label.startswith("화면") for label in fits)
    crops = [crop_label(mode) for mode in ("auto", "none")]
    assert len(set(crops)) == 2 and all(label.startswith("여백") for label in crops)
    assert fit_label("nope") == fit_label("auto")
    assert seek_note(65, True) == "1:05부터 다시 재생합니다."
    assert seek_note(65, False) == "1:05부터 다시 재생합니다."

def test_layout_puts_the_card_on_top_and_gives_spare_width_to_the_browser():
    """Toolbar, then a full-width Now card, then browser | fixed queue column. Nothing overlaps."""
    for size in (LAYOUT_DEFAULT, LAYOUT_MIN, (1440, 1000)):
        lay = Layout(*size)
        cx, cy, cw, ch = lay.card
        wx, wy, ww, wh = lay.web
        qx, qy, qw, qh = lay.queue
        # The card spans the window under the toolbar.
        assert (cx, cx + cw) == (lay.pad, size[0] - lay.pad)
        assert cy + ch + lay.gap == lay.toolbar_y
        # Browser and queue share one band under the card, with the gap between them.
        assert wy == qy == lay.footer_h and wh == qh == cy - lay.gap - lay.footer_h
        assert wx == lay.pad and wx + ww + lay.gap == qx and qx + qw == size[0] - lay.pad
        # The queue column never changes width; the browser takes the rest.
        assert qw == lay.side_w
    grow = Layout(1440, 1000).web[2] - Layout(*LAYOUT_DEFAULT).web[2]
    assert grow == 1440 - LAYOUT_DEFAULT[0], "all extra width goes to the page"
    # The minimum still leaves the mobile page its 320pt and the card room for its controls.
    assert Layout(*LAYOUT_MIN).web[2] >= 320
    assert LAYOUT_MIN[0] >= 756
    assert Layout(*LAYOUT_MIN).body_h > QUEUE_ROW_H * 4


def test_player_keys_seek_and_set_volume_within_bounds():
    assert seek_step("right") == 5 and seek_step("left") == -5
    assert seek_step("right", shift=True) == 30 and seek_step("left", shift=True) == -30
    assert seek_step("l") == 10 and seek_step("j") == -10 and seek_step("l", shift=True) == 10
    assert seek_step("x") == 0 and seek_step("up") == 0
    assert clamp_seek(100, 5, 600) == 105
    assert clamp_seek(3, -10, 600) == 0
    assert clamp_seek(598, 30, 600) == 599, "a seek past the end lands on the last second"
    assert clamp_seek(10, 5, 0) == 15, "unknown length does not clamp the top"
    assert volume_step(0.5, "up") == 0.6 and volume_step(0.5, "down") == 0.4
    assert volume_step(0.95, "up") == 1.0 and volume_step(0.05, "down") == 0.0
    assert volume_step(0.5, "left") == 0.5


def test_now_state_label_says_what_the_deck_is_doing():
    assert now_state_label(True, True, False).endswith("덱에서 재생 중")
    assert now_state_label(False, True, True) == "여는 중…"
    assert now_state_label(False, True, False) == "멈춤"
    assert now_state_label(False, False, False) == "대기 중"


def test_queue_rows_carry_length_and_a_still():
    watch = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    item = playlist_entry(watch, "Never Gonna Give You Up", "Rick Astley", 213)
    assert item["duration"] == 213
    assert playlist_meta(item) == "Rick Astley · 3:33"
    assert playlist_meta(playlist_entry(watch, "t")) == ""
    assert playlist_meta(playlist_entry("/tmp/a.mp4")) == "로컬 파일"
    assert playlist_meta(playlist_entry("/tmp/a.mp4", "a", "", 7260)) == "2:01:00"
    assert "duration" not in playlist_entry(watch, "t", "c", 0)
    assert "duration" not in playlist_entry(watch, "t", "c", "NA")
    # A length learned later is merged in, never overwriting a known one, and survives a save.
    merged = playlist_add([playlist_entry(watch, "t")], watch, duration=99)
    assert merged[0]["duration"] == 99
    assert playlist_add(merged, watch, duration=5)[0]["duration"] == 99
    assert playlist_normalize([{"source": watch, "duration": "12.5"}])[0]["duration"] == 12.5
    assert youtube_video_id(watch) == "dQw4w9WgXcQ"
    assert youtube_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert youtube_video_id("/tmp/a.mp4") == ""
    assert thumbnail_url(watch) == "https://i.ytimg.com/vi/dQw4w9WgXcQ/mqdefault.jpg"
    assert thumbnail_url("/tmp/a.mp4") == ""


def test_playlist_durations_survive_save_and_load(tmp_path):
    path = tmp_path / "playlist.json"
    watch = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    playlist_save(path, [playlist_entry(watch, "t", "c", 213), playlist_entry("/tmp/a.mp4")])
    loaded = playlist_load(path)
    assert loaded[0]["duration"] == 213
    assert "duration" not in loaded[1]


def test_thumbnails_are_fetched_once_and_cached(tmp_path):
    watch = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    hits = []

    class Resp:
        def __init__(self, data):
            self.data = data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, _n=-1):
            return self.data

    def fetch(req, timeout=0):
        hits.append(req.full_url)
        return Resp(b"\xff\xd8jpeg\xff\xd9")

    first = fetch_thumbnail(watch, root=tmp_path, fetch=fetch)
    again = fetch_thumbnail(watch, root=tmp_path, fetch=fetch)
    assert first == again == thumbnail_cache_path(watch, tmp_path)
    assert first.read_bytes() == b"\xff\xd8jpeg\xff\xd9"
    assert hits == ["https://i.ytimg.com/vi/dQw4w9WgXcQ/mqdefault.jpg"], "cached after the first fetch"
    # The same video under another URL form shares the cache entry.
    assert thumbnail_cache_path("https://youtu.be/dQw4w9WgXcQ", tmp_path) == first
    # A failed fetch leaves nothing behind, so the next call can retry.
    other = "https://www.youtube.com/watch?v=aaaaaaaaaaa"

    def broken(req, timeout=0):
        raise OSError("offline")

    assert fetch_thumbnail(other, root=tmp_path, fetch=broken) is None
    assert not thumbnail_cache_path(other, tmp_path).exists()
    assert list(tmp_path.glob("*.part")) == []
    # A missing local file has no still.
    assert fetch_thumbnail(str(tmp_path / "missing.mp4"), root=tmp_path) is None


def test_local_file_still_is_one_ffmpeg_frame(tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")
    seen = []

    def probe(argv, **_kwargs):
        seen.append(argv)
        if argv[0] == "ffprobe":
            return type("R", (), {"stdout": "100.0\n", "returncode": 0})()
        Path(argv[-1]).write_bytes(b"\xff\xd8frame")
        return type("R", (), {"stdout": "", "returncode": 0})()

    path = fetch_thumbnail(str(clip), root=tmp_path / "thumbs", probe=probe)
    assert path is not None and path.read_bytes() == b"\xff\xd8frame"
    grab = seen[-1]
    assert grab[0] == "ffmpeg" and grab[grab.index("-ss") + 1] == "10.00", "10% in, past any black intro"
    assert "-frames:v" in grab


def test_the_window_is_the_card_layout_not_the_phone_console():
    text = Path(__file__).resolve().parents[1].joinpath("src/ghostdeck/app.py").read_text(encoding="utf-8")
    assert "L = Layout(*LAYOUT_DEFAULT)" in text
    assert "setContentMinSize_(LAYOUT_MIN)" in text
    assert "PHONE_W = 392" not in text, "the fixed phone column is gone: the browser resizes"
    assert "QUEUE_CHROME" not in text
    assert "tableView_viewForTableColumn_row_" in text
    assert "tableView_willDisplayCell_forTableColumn_row_" not in text
    assert "addLocalMonitorForEventsMatchingMask_handler_" in text
    assert "play.fill" in text
    assert "TOOL_Y" in text

def test_playlist_click_row_prefers_clicked():
    assert playlist_click_row(2, -1, 3) == 2
    assert playlist_click_row(-1, 1, 3) == 1
    assert playlist_click_row(-1, -1, 3) == -1
    assert playlist_click_row(9, 0, 3) == 0


def test_youtube_auto_crop_skips_http_detect():
    watch = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert play_crop_argv(watch, "auto") == "none"
    assert play_crop_argv("/tmp/clip.mp4", "auto") == "auto"
    assert play_crop_argv(watch, "none") == "none"

def test_playlist_row_is_channel_title_without_play_glyph():
    watch = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    item = playlist_entry(watch, "IRIS OUT", "Kenshi Yonezu")
    assert playlist_label(item) == "Kenshi Yonezu · IRIS OUT"
    assert playlist_playing(item, watch)
    assert playlist_playing(item, "", watch)
    assert not playlist_playing(item, "https://www.youtube.com/watch?v=other")
    text = Path(__file__).resolve().parents[1].joinpath("src/ghostdeck/app.py").read_text(encoding="utf-8")
    assert '"▶ " + title' not in text
    assert 'initWithIdentifier_("track")' in text


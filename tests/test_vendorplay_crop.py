"""Letterbox detection must see compressed bars, not only pure-black ones."""

from __future__ import annotations

import importlib.util
import sys
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLAY = ROOT / "vendor" / "d200-color-play.py"


def _play():
    sys.path.insert(0, str(ROOT / "vendor"))
    spec = importlib.util.spec_from_file_location("d200_color_play", PLAY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cropdetect_limit_sees_compressed_letterbox():
    """limit=8 kept the iris 1080p bars; 24 finds 1920:804:0:138."""
    text = PLAY.read_text(encoding="utf-8")
    assert "cropdetect=24:2:0" in text
    assert "cropdetect=8:2:0" not in text


def test_youtube_stream_prefers_avc():
    text = PLAY.read_text(encoding="utf-8")
    assert "vcodec^=avc" in text


def test_usable_letterbox_keeps_a_widescreen_bar_crop():
    play = _play()
    assert play._usable_letterbox_crop(1920, 1080, 1920, 804, 0, 138) == "1920:804:0:138"


def test_usable_letterbox_rejects_a_full_frame_as_no_crop():
    play = _play()
    assert play._usable_letterbox_crop(1920, 1080, 1920, 1080, 0, 0) == "none"


def test_letterbox_then_cover_fills_the_native_plane():
    play = _play()
    args = type(
        "Args",
        (),
        {"image_resolution": "native", "playback_rate": 1.0, "interpolate": False},
    )()
    graph = play.build_video_filters(args, Fraction(30), "1920:804:0:138")
    assert "crop=1920:804:0:138" in graph
    assert "force_original_aspect_ratio=increase" in graph
    assert "crop=960:540" in graph
    assert "pad=" not in graph


def test_auto_without_detected_crop_still_covers_the_plane():
    play = _play()
    args = type("Args", (), {"image_resolution": "native", "playback_rate": 1.0, "interpolate": False})()
    graph = play.build_video_filters(args, Fraction(30), "none")
    assert "force_original_aspect_ratio=increase" in graph
    assert "pad=" not in graph


def test_auto_crop_runs_on_http_streams():
    text = PLAY.read_text(encoding="utf-8")
    assert 'args.crop == "auto" and args.input.startswith' not in text
    assert "detect_crop(source)" in text


def test_host_speaker_argv_is_our_audiotoolbox_ffmpeg():
    from ghostdeck.playident import is_host_speaker_argv
    assert is_host_speaker_argv(["ffmpeg", "-f", "audiotoolbox", "dummy"]) is True
    assert is_host_speaker_argv(["/opt/homebrew/bin/ffmpeg", "-re", "-i", "a", "-f", "audiotoolbox", "dummy"]) is True
    assert is_host_speaker_argv(["ffmpeg", "-i", "in.mp4", "out.mp4"]) is False
    assert is_host_speaker_argv(["python", "-u", "d200-color-play.py"]) is False


def test_unproven_open_is_retried_once():
    """A YouTube session that dies messy leaves cleanup unproven; the next OPEN was refused.

    Measured from the GUI: first watch consumed ~1080 frames then DISCONNECTED, the next
    play (and a local clip after it) both died with resultCode 1 and 0 frames. The player
    waits the same 5s settle as stop, then retries OPEN once.
    """
    play = _play()
    text = PLAY.read_text(encoding="utf-8")
    assert "OPEN_RETRY_WAIT" in text
    assert play.should_retry_unproven_open(
        {"accepted": False, "error": "the previous video session has not proven it released the deck (cleanup: unproven)"}
    )
    assert not play.should_retry_unproven_open({"accepted": True, "error": ""})
    assert not play.should_retry_unproven_open({"accepted": False, "error": "another video session is already opening"})


def test_probe_duration_uses_yt_dlp_for_http():
    play = _play()
    calls = []

    def fake_run(*arguments, **_k):
        calls.append(list(arguments))
        class Result:
            stdout = "146.6\n"
        return Result()

    play.run = fake_run
    assert play.probe_duration("https://www.youtube.com/watch?v=x") == 146.6
    assert calls[0][0] == "yt-dlp"


def test_take_seek_request_reads_and_clears_the_file(tmp_path):
    play = _play()
    path = tmp_path / "seek"
    path.write_text("33.5\n", encoding="utf-8")
    assert play.take_seek_request(path) == 33.5
    assert not path.exists()
    assert play.take_seek_request(path) is None
def test_host_audio_is_in_the_same_ffmpeg():
    play = _play()
    args = type("Args", (), {"loop": False, "start": 12.5, "duration": 0, "quality": 12})()
    command = play.build_encoder_command(
        args, "https://v.example/video", "https://v.example/audio", "fps=30,scale=960:540",
    )
    assert command.count("-i") == 2
    assert "audiotoolbox" in command
    assert "-an" not in command
    assert "-re" in command
    assert "12.5" in command
    silent = play.build_encoder_command(args, "/tmp/clip.mp4", None, "fps=30,scale=960:540")
    assert "-an" in silent
    assert "audiotoolbox" not in silent

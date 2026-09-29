"""Letterbox detection must see compressed bars, not only pure-black ones."""

from __future__ import annotations

import importlib.util
import sys
from fractions import Fraction
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLAY = ROOT / "vendor" / "d200-color-play.py"
# One test imports the host package; without this it passed only when another file had added it first.
sys.path.insert(0, str(ROOT / "src"))


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


def test_take_volume_request_reads_and_clears_the_file(tmp_path):
    play = _play()
    path = tmp_path / "volume"
    path.write_text("0.25\n", encoding="utf-8")
    assert play.take_volume_request(path) == 0.25
    assert not path.exists()
    assert play.take_volume_request(path) is None
    overlay = tmp_path / "overlay"
    overlay.write_text("0.4000\n", encoding="utf-8")
    assert play.take_overlay_request(overlay) == 0.4
    assert not overlay.exists()
    assert play.clamp_volume(2) == 1.0
    assert play.clamp_volume(-1) == 0.0
def test_host_audio_is_a_separate_realtime_ffmpeg():
    play = _play()
    args = type("Args", (), {"loop": False, "start": 12.5, "duration": 0, "quality": 12})()
    command = play.build_encoder_command(
        args, "https://v.example/video", "https://v.example/audio", "fps=30,scale=960:540",
    )
    assert command.count("-i") == 1
    assert "-re" in command
    assert "-an" in command
    assert "audiotoolbox" not in command
    audio_cmd = play.build_audio_command(args, "https://v.example/audio")
    assert "-re" in audio_cmd
    assert "audiotoolbox" in audio_cmd
    assert "image2pipe" not in audio_cmd
    joined = " ".join(audio_cmd)
    assert "async=1" not in joined
    assert "aresample=48000" in joined
    assert "asetnsamples=n=8192" in joined
    assert "volume@vol=" in joined
    quiet = type("Args", (), {"loop": False, "start": 0, "duration": 0, "quality": 12, "volume": 0.25})()
    quiet_cmd = play.build_audio_command(quiet, "/tmp/clip.mp4")
    assert "volume@vol=0.2500" in " ".join(quiet_cmd)
    silent = play.build_encoder_command(args, "/tmp/clip.mp4", None, "fps=30,scale=960:540")
    assert "-an" in silent
    assert "audiotoolbox" not in silent
    assert "-re" in silent

class _Speaker:
    """A stand-in speaker: `stdin` records what the player writes, `poll()` says if it is alive."""

    def __init__(self, alive=True, stdin=True, broken=False):
        import io

        self.alive = alive
        self.broken = broken
        self.stdin = io.BytesIO() if stdin else None

    def poll(self):
        return None if self.alive else 0

    def written(self):
        return self.stdin.getvalue().decode()


def test_an_http_source_that_stops_early_is_resumed_not_finished():
    """ffmpeg exits 0 when it loses the origin, so a long stream's drop looked like its end."""
    play = _play()
    url = "https://rr1---sn-x.googlevideo.com/videoplayback?expire=1"
    assert play.ended_early(url, 7200.0, 3600.0) is True, "stopped at 1h of a 2h video"
    assert play.ended_early(url, 7200.0, 7199.0) is False, "a clean end lands within a frame or two"
    assert play.ended_early(url, 7200.0, 7200.0 - play.EARLY_END_MARGIN) is False
    assert play.ended_early(url, 0.0, 30.0) is False, "unknown duration is never early"
    assert play.ended_early(url, None, 30.0) is False
    assert play.ended_early(url, float("nan"), 30.0) is False
    assert play.ended_early("/tmp/clip.mp4", 7200.0, 30.0) is False, "files do not lose an origin"


def test_produce_resumes_on_an_early_clean_exit():
    """The encoder exited 0 before the end: `produce` raises SourceEndedEarly instead of EOS/loop."""
    import queue
    import threading

    play = _play()
    stream = play.VideoStream.__new__(play.VideoStream)
    stream.cancel = threading.Event()
    stream.state = type("S", (), {"received": 0, "consumed": 0})()
    stream.progress_deadline = float("inf")
    stream.client = None

    class Pump:
        def get(self, timeout):
            raise queue.Empty

    class Done:
        returncode = 0

        def poll(self):
            return 0

    play.take_seek_request = lambda: None
    play.take_volume_request = lambda: None
    play.take_overlay_request = lambda: None
    try:
        stream.produce(Pump(), Done(), loop=True, early_end=lambda: True)
    except play.SourceEndedEarly:
        pass
    else:
        raise AssertionError("an early clean exit was treated as the end")
    try:
        stream.produce(Pump(), Done(), loop=True, early_end=lambda: False)
    except play.SeekRequested as seek:
        assert seek.start == 0.0, "a real end with loop on still restarts from 0"
    else:
        raise AssertionError("loop did not restart")


def test_http_inputs_ride_out_a_long_origin_outage():
    """`-reconnect_delay_max 2` gave up on a 20s outage and decoded 18s of a 60s stream (exit 0)."""
    play = _play()
    args = type("Args", (), {"loop": False, "start": 0, "duration": 0, "quality": 12, "volume": 1.0})()
    for command in (play.build_audio_command(args, "https://v.example/a"),
                    play.build_encoder_command(args, "https://v.example/v", None, "fps=30")):
        joined = " ".join(command)
        assert "-reconnect_delay_max 30" in joined, joined
        assert "-reconnect_delay_total_max 120" in joined, joined
        assert "-reconnect_delay_max 2 " not in joined + " "
    local = " ".join(play.build_audio_command(args, "/tmp/clip.mp4"))
    assert "reconnect" not in local


def test_live_volume_changes_the_running_speaker_in_place():
    """A slider step was a speaker kill + respawn: an audible gap per step, many per drag.

    The speaker takes ffmpeg's interactive command on stdin instead, so the running AudioQueue keeps
    playing. Respawn is kept only as the fallback when there is no live speaker to talk to.
    """
    play = _play()
    speaker = _Speaker()
    assert play.apply_encoder_volume(speaker, 0.25) is True
    assert speaker.written() == "cvolume@vol -1 volume 0.2500\n"
    assert play.CURRENT_VOLUME == 0.25
    assert play.apply_encoder_volume(speaker, 7) is True  # clamped, still in place
    assert speaker.written().endswith("cvolume@vol -1 volume 1.0000\n")

    assert play.apply_encoder_volume(None, 0.5) is False
    assert play.CURRENT_VOLUME == 0.5, "the fallback respawn must still see the new gain"
    assert play.apply_encoder_volume(_Speaker(alive=False), 0.5) is False
    assert play.apply_encoder_volume(_Speaker(stdin=False), 0.5) is False
    closed = _Speaker()
    closed.stdin.close()
    assert play.apply_encoder_volume(closed, 0.5) is False

    # The graph the command targets: `volume@vol` has to be the label in the speaker's own filter.
    args = type("Args", (), {"loop": False, "start": 0, "duration": 0, "quality": 12, "volume": 0.0})()
    silent = play.build_audio_command(args, "/tmp/clip.mp4")
    assert "volume@vol=0.0000" in " ".join(silent)
    assert "-nostdin" not in silent, "-nostdin would switch off the command channel"
    assert play.speaker_playhead(10, 1_000_000_000, 3_000_000_000, 1.0) == 12.0
    assert play.speaker_playhead(0, 5, 1, 1.0) == 0.0
    assert play.spawn_speaker(None) is None


def test_speaker_stdin_is_a_pipe_and_stderr_is_not_left_unread(monkeypatch):
    """stdin carries the volume command; an undrained stderr PIPE would block ffmpeg once full."""
    play = _play()
    seen = {}

    def fake_popen(argv, **kwargs):
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(play.subprocess, "Popen", fake_popen)
    play.spawn_speaker(["ffmpeg", "-f", "audiotoolbox", "dummy"])
    assert seen["stdin"] is play.subprocess.PIPE
    assert seen["stderr"] is play.subprocess.DEVNULL


def test_the_volume_command_really_changes_ffmpegs_gain():
    """Run the exact command string against the speaker's own filter graph and measure the level."""
    import shutil
    import subprocess
    import time

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")
    play = _play()
    args = type("Args", (), {"loop": False, "start": 0, "duration": 0, "quality": 12, "volume": 1.0})()
    graph = play.audio_filter(args) + ",astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level"
    speaker = subprocess.Popen(
        ["ffmpeg", "-v", "info", "-re", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=4",
         "-filter:a", graph, "-f", "null", "-"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    time.sleep(1.5)
    assert play.apply_encoder_volume(speaker, 0.25) is True
    _, err = speaker.communicate(timeout=20)
    levels = [float(line.rsplit("=", 1)[1]) for line in err.decode().splitlines() if "RMS_level=" in line]
    assert len(levels) > 4, err.decode()[-500:]
    # 0.25 of the amplitude is -12 dB; the level must drop by about that while the process keeps running.
    assert levels[-1] < levels[0] - 10, levels

def test_frame_pump_does_not_skip_queued_jpegs():
    play = _play()
    text = PLAY.read_text(encoding="utf-8")
    assert "Keep only the newest frames" not in text
    assert "max_frames=8" in text
    play.probe_source_fps = lambda _source: Fraction(60, 1)
    assert play.parse_fps("source", "clip") == Fraction(30, 1)
    play.probe_source_fps = lambda _source: Fraction(24, 1)
    assert play.parse_fps("source", "clip") == Fraction(24, 1)
def test_http_loop_restarts_instead_of_stream_loop():
    play = _play()
    args = type("Args", (), {"loop": True, "start": 0, "duration": 0, "quality": 12})()
    command = play.build_encoder_command(
        args, "https://v.example/video", "https://v.example/audio", "fps=30,scale=960:540",
    )
    assert "-stream_loop" not in command
    assert "audiotoolbox" not in command
    assert "-an" in command
    audio_cmd = play.build_audio_command(args, "https://v.example/audio")
    assert "audiotoolbox" in audio_cmd
    local = play.build_encoder_command(args, "/tmp/clip.mp4", "/tmp/clip.mp4", "fps=30,scale=960:540")
    assert "-stream_loop" not in local
    assert local.count("-i") == 1
    assert "-an" in local

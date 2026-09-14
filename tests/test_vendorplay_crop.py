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

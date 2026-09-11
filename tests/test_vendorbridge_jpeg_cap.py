"""Regression proof that the host JPEG framing cap equals the deck's own cap.

`device/d200_video_stream.h` defines `D200_VS_MAX_JPEG` as 1048576; the agent
advertises that value in its READY window and rejects larger frames in
`d200_vs_validate_payload`. The Python framer default must not be looser, or a
frame in the gap is accepted here and killed on the device. Reads only; no
device, no adb, no socket.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vendor"))

import d200_jpeg
import d200_video_stream


def test_framer_default_matches_the_vendor_wire_cap():
    assert d200_jpeg.MAX_JPEG_FRAME_BYTES == d200_video_stream.MAX_JPEG


def test_framer_default_is_the_deck_value():
    assert d200_jpeg.MAX_JPEG_FRAME_BYTES == 1048576


def test_framer_constructs_with_the_cap_it_advertises():
    framer = d200_jpeg.JpegFramer()
    assert framer.max_frame_bytes == d200_video_stream.MAX_JPEG


def test_jpeg_module_does_not_restate_the_number():
    """The cap must arrive by import, so the two sides cannot drift again."""
    source = (ROOT / "vendor" / "d200_jpeg.py").read_text(encoding="utf-8")
    assert "MAX_JPEG_FRAME_BYTES = MAX_JPEG" in source
    assert not re.search(r"MAX_JPEG_FRAME_BYTES\s*=\s*\d", source)


def test_device_header_cap_matches_the_vendor_cap():
    header = (ROOT / "device" / "d200_video_stream.h").read_text(encoding="utf-8")
    match = re.search(r"#define\s+D200_VS_MAX_JPEG\s+(\d+)", header)
    assert match, "D200_VS_MAX_JPEG must stay defined in device/d200_video_stream.h"
    assert int(match.group(1)) == d200_video_stream.MAX_JPEG


def test_ready_window_advertises_the_same_cap():
    agent = (ROOT / "device" / "d200-color-agent.c").read_text(encoding="utf-8")
    assert "D200_VS_MAX_JPEG" in agent

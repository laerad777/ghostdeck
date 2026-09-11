"""Frame-fitting regression proof for FIX-5-T8 / C-108 and FIX-5-T9.

History, because both tasks touch the same helper and a test that pins only one
branch is how the C-108 bug survived:

* **T8 / C-108.** `align_jpeg_payload` padded every frame up to a
  `FRAME_JPEG_CHUNK` (12232-byte) multiple, and the FRAME record's `total` field
  carries that *padded* length. `d200_vs_validate_payload` case 3 requires
  `total <= D200_VS_MAX_JPEG`, so inputs of 1039721..1048576 padded to 1051952 --
  3376 bytes over the cap -- and the host sent a record the deck was guaranteed to
  reject. T8 stopped that, but by *refusing* those frames.
* **T9.** Refusing them left a dead band: with chunk 12232 the largest reachable
  padded total is `floor(1048576 / 12232) * 12232 = 1039720`, so 8856 bytes of
  valid JPEG range became hard errors. Padding is not a protocol requirement: the
  device completes a frame by accumulated offset
  (`d200_vs_state_accept`, kind 3: `s->partial_offset += h->payload_length - 16;
  if (s->partial_offset == total)`), the reader's bound `n - 16 <= total - offset`
  explicitly permits a short final fragment, and the comment above the chunk size
  records a stall caused by records that were TOO BIG. A shorter final record
  cannot re-introduce that. So the dead band is now sent unpadded.

Both branches are pinned below: padded when the padded total fits, an unpadded
short final fragment in the dead band, and refusal for a frame the deck genuinely
cannot accept. For every frame whose padded length fits, the returned bytes are
byte-identical to the padded-only revision -- that is what makes T9 a strict
superset rather than a behaviour change for frames that already played.

Imports `vendor/d200-color-play.py` by path (importing it performs no device I/O);
reads and writes nothing outside `tempfile`. No device, no adb, no ffmpeg, no
Studio, and never the real `/tmp/d200-adb-bridge.sock` or `/tmp/d200-color-host.json`.
Nothing here is hardware validation -- see the module note in the T9 report.
"""

from __future__ import annotations

import importlib.util
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor"
sys.path.insert(0, str(VENDOR))

import d200_video_stream as wire  # noqa: E402
from d200_jpeg import JpegFramer, JpegFramingError  # noqa: E402

FRAME_HEADER = 16  # '>QII' index/total/offset prefix inside a FRAME payload


def _load_player():
    spec = importlib.util.spec_from_file_location("d200_color_play_padding", VENDOR / "d200-color-play.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


player = _load_player()
CHUNK = player.FRAME_JPEG_CHUNK

# The largest input whose padded length still fits: 85 * 12232 <= 1048576 < 86 * 12232.
FITS = (wire.MAX_JPEG // CHUNK) * CHUNK
# (i) padded, and padded byte-for-byte as before.
PADDED_INPUTS = (1, CHUNK - 1, CHUNK + 1, 1036345, FITS - 1)
# A padding-free input: an exact multiple was never padded, so it must not become padded.
ALIGNED_INPUTS = (0, CHUNK, FITS)
# (ii) the dead band: unpadded, sent, short final fragment.
# The band is (FITS, MAX_JPEG]: FITS+1 .. 1048576. Its upper bound is the cap itself.
DEAD_BAND_INPUTS = (FITS + 1, 1040000, wire.MAX_JPEG - 1, wire.MAX_JPEG)
# (iii) genuinely over the cap, unpadded: refused.
OVER_CAP_INPUTS = (wire.MAX_JPEG + 1, wire.MAX_JPEG + CHUNK, 2 * wire.MAX_JPEG)


def _expected_padding(size):
    remainder = size % CHUNK
    return 0 if not remainder else CHUNK - remainder


def test_the_chunking_constants_are_consistent():
    assert CHUNK == 12288 - wire.HEADER_SIZE - FRAME_HEADER
    assert FITS == 1039720 and wire.MAX_JPEG == 1048576
    assert FITS + CHUNK > wire.MAX_JPEG, "the dead band this task is about must still exist"


def test_the_framer_budget_comes_from_the_wire_module():
    source = (VENDOR / "d200-color-play.py").read_text(encoding="utf-8")
    assert "MAX_JPEG_BYTES = wire.MAX_JPEG" in source
    assert "1024 * 1024" not in source, "the cap must not be restated a third time"
    assert player.MAX_JPEG_BYTES == wire.MAX_JPEG


# (i) padded when the padded total fits -------------------------------------------------


@pytest.mark.parametrize("size", PADDED_INPUTS)
def test_a_frame_that_fits_is_padded_exactly_as_before(size):
    expected = _expected_padding(size)
    assert expected and size + expected <= wire.MAX_JPEG, "these inputs must be in the padded branch"
    padded = player.align_jpeg_payload(b"x" * size)
    assert len(padded) == size + expected
    assert len(padded) % CHUNK == 0
    assert padded[:size] == b"x" * size
    assert padded[size:] == b"\x00" * expected, "the fill must stay zero bytes"


@pytest.mark.parametrize("size", ALIGNED_INPUTS)
def test_a_frame_that_is_already_aligned_is_returned_untouched(size):
    """Never padded before, so it must not start being padded: padding cannot add bytes here."""
    frame = b"x" * size
    assert player.align_jpeg_payload(frame) is frame


# (ii) the dead band: sent unpadded, short final fragment --------------------------------


@pytest.mark.parametrize("size", DEAD_BAND_INPUTS)
def test_the_dead_band_is_sent_unpadded_and_stays_within_the_cap(size):
    """T9's whole point: these used to raise, and the deck accepts them unpadded."""
    frame = b"x" * size
    returned = player.align_jpeg_payload(frame)
    assert returned is frame, "the dead band must be passed through, not copied"
    assert len(returned) == size <= wire.MAX_JPEG
    assert size % CHUNK != 0, "a dead-band input is not a whole number of fragments"


@pytest.mark.parametrize("size", DEAD_BAND_INPUTS)
def test_a_dead_band_frame_fragments_into_a_short_final_record(size):
    """The records `produce()` would emit: full fragments, then one partial final one."""
    frame = player.align_jpeg_payload(b"\xff\xd8" + b"y" * (size - 2))
    fragments = [frame[offset:offset + CHUNK] for offset in range(0, len(frame), CHUNK)]
    assert len(fragments) == -(-len(frame) // CHUNK)
    assert [len(fragment) for fragment in fragments[:-1]] == [CHUNK] * (len(fragments) - 1)
    assert 0 < len(fragments[-1]) < CHUNK, "the final fragment must be short, not full"
    assert sum(len(fragment) for fragment in fragments) == len(frame)


@pytest.mark.parametrize("size", DEAD_BAND_INPUTS)
def test_every_dead_band_record_passes_the_decks_own_bounds(size):
    """Check each FRAME record against the reader's stated bound, not a re-derived one.

    `d200_vs_validate_payload` case 3:
        total && total <= D200_VS_MAX_JPEG && offset < total && n - 16 <= total - offset
    where `n` is the payload length including the 16-byte index/total/offset prefix.
    """
    session = "ab" * 16
    capability = "cd" * 32
    frame = player.align_jpeg_payload(b"\xff\xd8" + b"y" * (size - 2))
    state = _attached_state(session, capability)
    for offset in range(0, len(frame), CHUNK):
        payload = struct.pack(">QII", 0, len(frame), offset) + frame[offset:offset + CHUNK]
        total, record_offset, n = len(frame), offset, len(payload)
        assert total and total <= wire.MAX_JPEG
        assert record_offset < total
        assert n - FRAME_HEADER <= total - record_offset
        record = wire.decode_record(wire.encode_record(wire.FRAME, session, 1, state.sequences["producer"], payload),
                                   direction=wire.PRODUCER)
        state.accept(record, wire.PRODUCER)
    assert state.received == 1
    assert state.partial_total == 0


@pytest.mark.parametrize("size", DEAD_BAND_INPUTS)
def test_the_device_accumulator_completes_on_the_short_final_fragment(size):
    """A faithful port of `d200_vs_state_accept`'s kind-3 arithmetic, in Python.

    This is a port of the cited device code, not a run of the device:
        s->partial_total = total; s->partial_offset += h->payload_length - 16;
        if (s->partial_offset == total) { ++s->received; ... }
    It demonstrates that a short final fragment completes the accumulated total,
    which is the property the unpadded branch depends on -- and that padding was
    never what made completion work.
    """
    frame = player.align_jpeg_payload(b"x" * size)
    partial_total = partial_offset = 0
    received = 0
    for offset in range(0, len(frame), CHUNK):
        fragment = frame[offset:offset + CHUNK]
        payload_length = FRAME_HEADER + len(fragment)
        assert offset == partial_offset, "the device requires offset == partial_offset"
        partial_total = len(frame)
        partial_offset += payload_length - FRAME_HEADER
        if partial_offset == partial_total:
            received += 1
            partial_total = partial_offset = 0
    assert received == 1, "the frame must complete exactly once"
    assert partial_total == partial_offset == 0, "no half-finished frame may be left behind"


# (iii) genuinely over the cap: refused -------------------------------------------------


@pytest.mark.parametrize("size", OVER_CAP_INPUTS)
def test_an_unpadded_frame_over_the_cap_is_refused_with_the_whole_size_named(size):
    """`total` is at least the frame length, so this is the one the deck cannot accept."""
    with pytest.raises(JpegFramingError) as refused:
        player.align_jpeg_payload(b"x" * size)
    message = str(refused.value)
    assert str(size) in message, "the message must describe the unpadded frame size"
    assert str(wire.MAX_JPEG) in message
    assert "deck cap" in message
    assert "pads to" not in message, "the old padded projection must not be reported any more"
    assert isinstance(refused.value, ValueError)


@pytest.mark.parametrize("size", PADDED_INPUTS + ALIGNED_INPUTS + DEAD_BAND_INPUTS)
def test_no_acceptable_size_can_produce_an_over_cap_total(size):
    """The property as the deck states it: `total <= D200_VS_MAX_JPEG`, for every branch."""
    total = len(player.align_jpeg_payload(b"x" * size))
    assert total <= wire.MAX_JPEG
    assert total >= size, "no branch may drop bytes from the frame"


# the C-108 evidence, kept so the padded failure cannot come back unnoticed --------------


@pytest.mark.parametrize("size", DEAD_BAND_INPUTS)
def test_the_padded_total_for_a_dead_band_frame_is_still_a_record_the_deck_rejects(size):
    """Padded, these frames were over the cap -- that is why they cannot simply be padded."""
    padded_total = size + _expected_padding(size)
    assert padded_total > wire.MAX_JPEG
    payload = struct.pack(">QII", 0, padded_total, 0) + b"x" * CHUNK
    with pytest.raises(wire.ProtocolError, match="invalid JPEG fragment bounds"):
        wire.validate_record_payload(wire.FRAME, payload, wire.PRODUCER)


def test_the_padded_total_for_the_last_padding_free_frame_still_fits():
    """The boundary T8 established, unchanged: 1036345 pads to 1039720, inside the cap."""
    assert 1036345 + _expected_padding(1036345) == FITS <= wire.MAX_JPEG
    assert FITS + _expected_padding(FITS + 1) > wire.MAX_JPEG


def _attached_state(session, capability):
    """A producer-side StreamState after ATTACH and READY, ready to take FRAME records."""
    state = wire.StreamState(session, 30, 1, capability=capability)
    state.accept(wire.Record(wire.ATTACH, bytes.fromhex(session), 1, 0, bytes.fromhex(capability)),
                 wire.PRODUCER)
    state.accept(wire.Record(wire.READY, bytes.fromhex(session), 1, 0,
                             struct.pack(">6I", 30, 1, wire.WINDOW_FRAMES, wire.MAX_JPEG,
                                         wire.WINDOW_BYTES, wire.MAX_PAYLOAD)), wire.CONSUMER)
    return state


# reachability: which frame lengths can actually reach align_jpeg_payload -----------------

_MAX_SEGMENT_PAYLOAD = 65533  # a two-byte segment length holds at most this much payload


def build_jpeg(total):
    """A structurally valid JPEG of exactly `total` bytes: SOI, segments, SOS, EOI."""
    head, tail = bytearray(b"\xff\xd8"), b"\xff\xda\x00\x02\xff\xd9"
    remaining = total - len(head) - len(tail)
    while remaining - 4 > 0:
        chunk = min(_MAX_SEGMENT_PAYLOAD, remaining - 4)
        head += b"\xff\xe0" + (chunk + 2).to_bytes(2, "big") + b"z" * chunk
        remaining -= 4 + chunk
    assert remaining == 0, remaining
    return bytes(head) + tail


def test_the_framer_delivers_the_whole_dead_band_and_nothing_above_the_cap():
    """Pin the reachable range, so the send branch is known to be reachable.

    `produce()` builds `JpegFramer(max_frame_bytes=MAX_JPEG_BYTES)` and
    `MAX_JPEG_BYTES == wire.MAX_JPEG`, so the dead band (FITS, MAX_JPEG] is exactly
    the reachable range and its top is inclusive. Above the cap the framer refuses
    first, which is why the helper's own over-cap guard is defence in depth rather
    than a live path.
    """
    assert player.MAX_JPEG_BYTES == wire.MAX_JPEG
    for total in (FITS, FITS + 1, wire.MAX_JPEG - 1, wire.MAX_JPEG):
        framer = JpegFramer(max_frame_bytes=player.MAX_JPEG_BYTES)
        assert [len(frame) for frame in framer.feed(build_jpeg(total))] == [total]
    for total in (wire.MAX_JPEG + 1, 86 * CHUNK):
        framer = JpegFramer(max_frame_bytes=player.MAX_JPEG_BYTES)
        with pytest.raises(JpegFramingError):
            list(framer.feed(build_jpeg(total)))


def test_the_top_of_the_dead_band_is_handled_end_to_end():
    """A real MAX_JPEG-byte JPEG: framer, helper and wire validator agree on it."""

    session, capability = "ab" * 16, "cd" * 32
    source = build_jpeg(wire.MAX_JPEG)
    framed = next(iter(JpegFramer(max_frame_bytes=player.MAX_JPEG_BYTES).feed(source)))
    assert len(framed) == wire.MAX_JPEG
    returned = player.align_jpeg_payload(framed)
    assert returned is framed, "an unpadded cap-sized frame must not be copied"
    state = _attached_state(session, capability)
    for offset in range(0, len(returned), CHUNK):
        fragment = returned[offset:offset + CHUNK]
        payload = struct.pack(">QII", 0, len(returned), offset) + fragment
        state.accept(wire.decode_record(
            wire.encode_record(wire.FRAME, session, 1, state.sequences["producer"], payload),
            direction=wire.PRODUCER), wire.PRODUCER)
    assert state.received == 1, "a cap-sized frame must still complete as exactly one frame"

"""Device-free host harness for the D2JF/D2PX wire codecs in `device/d200_video_stream.h`.

The header is pure portable C with no I/O and no platform dependencies, so it can be compiled and
executed on this host instead of only cross-compiled for the deck. That matters because the bridge,
the on-device agent and `vendor/d200_video_stream.py` all depend on these exact byte layouts: a
silent regression here breaks the deck with nothing in Python failing.

The dispatcher is `tasks/FIX-4-T1.md`. This file compiles a C driver with the host compiler, runs
it, and asserts on what it prints. It never needs a deck, `adb`, `ffmpeg`, or a device node.

Three things are deliberately not hand-copied:

* the kind lists the driver sweeps come from the `D200_VS_*` `#define`s, and
* the expected case names are derived here by parsing `d200_vs_record_length`'s and
  `d200_vs_control_length`'s `case` labels out of the header, so adding a wire kind to the header
  fails this test until the driver covers it, and
* the one-byte-short capacity cases allocate a destination of *exactly* the short size, so a
  buffer overrun is an AddressSanitizer error rather than a silent pass.

The ARM cross toolchain is used for `-fsyntax-only` proof only; it cannot link against a macOS
`libturbojpeg.dylib`, so linking the agent is never attempted here.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HEADER = ROOT / "device" / "d200_video_stream.h"

CC_FLAGS = ("-std=c11", "-O1", "-Wall", "-Wextra", "-Werror")
SANITIZE_FLAGS = ("-fsanitize=address,undefined", "-fno-omit-frame-pointer")

DRIVER = r"""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "d200_video_stream.h"

static unsigned cases;
static unsigned failures;

static void check(const char *name, int ok) {
    ++cases;
    if (ok) {
        printf("CASE %s PASS\n", name);
    } else {
        ++failures;
        printf("CASE %s FAIL\n", name);
    }
}

static const char session_text[] = "0123456789abcdef";
static uint8_t session_bytes[16];
static uint8_t capability[32];

static d200_vs_header make_header(uint8_t kind, uint32_t length, uint64_t sequence,
                                  const uint8_t *session) {
    d200_vs_header h;
    memset(&h, 0, sizeof(h));
    h.kind = kind;
    h.payload_length = length;
    h.epoch = 1;
    h.sequence = sequence;
    memcpy(h.session, session, 16);
    return h;
}

/* A legal payload for record `kind`, sized to exactly its `d200_vs_record_length` length. The
 * chosen values are the smallest ones the validator and the session state machine both accept:
 * FRAME index 0 / total 1 / offset 0, CONSUMED 0, EOS 1, DONE 1==1 with a zero credit, CANCEL and
 * CANCELLED reason D200_VS_SOURCE_FAILURE, ERROR code D200_VS_PROTOCOL plus "x". */
static uint32_t fill_record(uint8_t kind, uint8_t *p, size_t capacity) {
    uint32_t length = 0;
    memset(p, 0, capacity);
    switch (kind) {
    case D200_VS_ATTACH: length = 32; break;
    case D200_VS_READY:
        d200_vs_put_u32(p, 30);
        d200_vs_put_u32(p + 4, 1);
        d200_vs_put_u32(p + 8, D200_VS_WINDOW_FRAMES);
        d200_vs_put_u32(p + 12, D200_VS_MAX_JPEG);
        d200_vs_put_u32(p + 16, D200_VS_WINDOW_BYTES);
        d200_vs_put_u32(p + 20, D200_VS_MAX_PAYLOAD);
        length = 24;
        break;
    case D200_VS_FRAME:
        d200_vs_put_u64(p, 0);
        d200_vs_put_u32(p + 8, 1);
        d200_vs_put_u32(p + 12, 0);
        p[16] = 0x2a;
        length = 17;
        break;
    case D200_VS_CONSUMED: d200_vs_put_u64(p, 0); length = 8; break;
    case D200_VS_EOS: d200_vs_put_u64(p, 1); length = 8; break;
    case D200_VS_DONE:
        d200_vs_put_u64(p, 1);
        d200_vs_put_u64(p + 8, 1);
        length = 20;
        break;
    case D200_VS_CANCEL:
    case D200_VS_CANCELLED: d200_vs_put_u32(p, D200_VS_SOURCE_FAILURE); length = 4; break;
    case D200_VS_ERROR:
        d200_vs_put_u32(p, D200_VS_PROTOCOL);
        p[4] = 'x';
        length = 5;
        break;
    default: length = 0; break;
    }
    return length;
}

/* A legal payload for control `kind`, exactly `d200_vs_control_length(kind)` bytes. */
static uint32_t fill_control(uint8_t kind, uint8_t *p, size_t capacity) {
    uint32_t length = d200_vs_control_length(kind);
    if (!length || length > capacity) return 0;
    memset(p, 0, capacity);
    switch (kind) {
    case D200_VS_VIDEO_OPEN_REQUEST:
        d200_vs_put_u32(p + 24, 0);
        p[28] = 1;
        d200_vs_put_u32(p + 32, 30);
        d200_vs_put_u32(p + 36, 1);
        break;
    case D200_VS_VIDEO_CANCEL_REQUEST:
        d200_vs_put_u32(p + 24, 1);
        d200_vs_put_u32(p + 60, D200_VS_SOURCE_FAILURE);
        break;
    case D200_VS_VIDEO_STATUS_REQUEST:
        d200_vs_put_u32(p + 24, 1);
        break;
    case D200_VS_VIDEO_OPEN_RESULT:
        d200_vs_put_u32(p + 28, 1);
        d200_vs_put_u32(p + 32, 0);
        p[36] = 1;
        p[37] = 0;
        d200_vs_put_u16(p + 38, 4);
        break;
    case D200_VS_VIDEO_CANCEL_RESULT:
        d200_vs_put_u32(p + 28, 1);
        d200_vs_put_u32(p + 32, D200_VS_OK);
        d200_vs_put_u32(p + 36, D200_VS_STATE_DONE);
        d200_vs_put_u32(p + 40, 0);
        break;
    case D200_VS_VIDEO_STATUS_RESULT:
        d200_vs_put_u32(p + 28, 1);
        d200_vs_put_u32(p + 32, D200_VS_OK);
        d200_vs_put_u32(p + 36, D200_VS_STATE_READY);
        d200_vs_put_u64(p + 40, 5);
        d200_vs_put_u64(p + 48, 2);
        d200_vs_put_u64(p + 56, UINT64_MAX);
        d200_vs_put_u32(p + 68, 0);
        break;
    default: return 0;
    }
    return length;
}

static unsigned record_direction(uint8_t kind) {
    switch (kind) {
    case D200_VS_READY:
    case D200_VS_CONSUMED:
    case D200_VS_DONE:
    case D200_VS_CANCELLED:
        return D200_VS_CONSUMER;
    default:
        return D200_VS_PRODUCER;
    }
}

/* ---------------------------------------------------------------- header codec */

static void test_header_codec(void) {
    uint8_t wire[D200_VS_HEADER_SIZE];
    uint8_t short_wire[D200_VS_HEADER_SIZE];
    d200_vs_header out = make_header(D200_VS_ATTACH, 32, 7, session_bytes);
    d200_vs_header back;

    check("header_round_trip",
          d200_vs_encode_header(wire, sizeof(wire), &out) == 1 &&
          d200_vs_decode_header(wire, sizeof(wire), &back) == 1 &&
          back.kind == out.kind && back.payload_length == out.payload_length &&
          back.epoch == 1 && back.sequence == out.sequence &&
          !memcmp(back.session, session_bytes, 16) &&
          !memcmp(wire, "D2JF", 4) && wire[4] == D200_VS_VERSION);

    memcpy(short_wire, wire, sizeof(short_wire));
    check("header_decode_rejects_short_buffer",
          d200_vs_decode_header(wire, D200_VS_HEADER_SIZE - 1, &back) == 0 &&
          d200_vs_decode_header(wire, 0, &back) == 0);

    memcpy(short_wire, wire, sizeof(short_wire));
    short_wire[0] = 'X';
    check("header_decode_rejects_bad_magic",
          d200_vs_decode_header(short_wire, sizeof(short_wire), &back) == 0);

    memcpy(short_wire, wire, sizeof(short_wire));
    short_wire[4] = D200_VS_VERSION + 1;
    check("header_decode_rejects_bad_version",
          d200_vs_decode_header(short_wire, sizeof(short_wire), &back) == 0);

    memcpy(short_wire, wire, sizeof(short_wire));
    d200_vs_put_u32(short_wire + 28, 2);
    check("header_decode_rejects_wrong_epoch",
          d200_vs_decode_header(short_wire, sizeof(short_wire), &back) == 0);

    memcpy(short_wire, wire, sizeof(short_wire));
    d200_vs_put_u32(short_wire + 8, 31);
    check("header_decode_rejects_impossible_record_length",
          d200_vs_decode_header(short_wire, sizeof(short_wire), &back) == 0);

    check("header_encode_rejects_null_and_bad_kind",
          d200_vs_encode_header(NULL, sizeof(wire), &out) == 0 &&
          d200_vs_encode_header(wire, sizeof(wire), NULL) == 0 &&
          d200_vs_encode_header(wire, sizeof(wire),
                                &(d200_vs_header){0, {0}, 0, 1, 0}) == 0);
}

/* ---------------------------------------------------------------- record codec */

static void test_record_codec(const uint8_t *kinds, unsigned count) {
    uint8_t payload[512];
    uint8_t wire[sizeof(payload) + D200_VS_HEADER_SIZE];
    unsigned i;

    for (i = 0; i < count; ++i) {
        uint8_t kind = kinds[i];
        uint32_t length = fill_record(kind, payload, sizeof(payload));
        unsigned direction = record_direction(kind);
        d200_vs_header out, back;
        char name[64];

        snprintf(name, sizeof(name), "record_round_trip_kind_%u", (unsigned)kind);
        out = make_header(kind, length, 3, session_bytes);
        memset(&back, 0, sizeof(back));
        check(name,
              length != 0 &&
              d200_vs_encode_record(wire, sizeof(wire), &out, payload) == 1 &&
              d200_vs_decode_record(wire, D200_VS_HEADER_SIZE + length, direction, &back) == 1 &&
              back.kind == kind && back.payload_length == length &&
              back.sequence == 3 && !memcmp(back.session, session_bytes, 16) &&
              !memcmp(wire + D200_VS_HEADER_SIZE, payload, length));

        snprintf(name, sizeof(name), "record_decode_rejects_truncated_kind_%u", (unsigned)kind);
        check(name,
              d200_vs_decode_record(wire, D200_VS_HEADER_SIZE + length - 1, direction, &back) == 0);

        snprintf(name, sizeof(name), "record_direction_gate_kind_%u", (unsigned)kind);
        {
            /* ERROR (kind 9) is legal in both directions by design: the agent
             * (d200-color-agent.c) and the proxy (d200-zkgui-proxy.c) each emit it as their
             * terminal record. Every other kind is gated to exactly one direction, so the
             * mirrored direction must be refused. Asserting the derived expectation keeps this
             * honest instead of hardcoding "must reject" for a kind that must not. */
            unsigned other = 3 - direction;
            int other_legal = d200_vs_validate_payload(kind, payload, length, other);
            int decoded = d200_vs_decode_record(wire, D200_VS_HEADER_SIZE + length,
                                                other, &back);
            check(name, other_legal ? decoded == 1 : decoded == 0);
        }
    }
}

static void test_record_negative(void) {
    uint8_t payload[512];
    uint8_t wire[sizeof(payload) + D200_VS_HEADER_SIZE];
    d200_vs_header out, back;
    uint32_t length = fill_record(D200_VS_FRAME, payload, sizeof(payload));

    /* The fragment length is gated by D200_VS_MAX_PAYLOAD; the assembled image size by
     * D200_VS_MAX_JPEG. Both caps must be inclusive-exact and reject one byte over. */
    check("record_length_accepts_caps_and_rejects_one_over",
          d200_vs_record_length(D200_VS_FRAME, D200_VS_MAX_PAYLOAD) == 1 &&
          d200_vs_record_length(D200_VS_FRAME, D200_VS_MAX_PAYLOAD + 1) == 0 &&
          d200_vs_record_length(D200_VS_FRAME, D200_VS_MAX_PAYLOAD - 1) == 1 &&
          d200_vs_record_length(D200_VS_FRAME, 16) == 0 &&
          d200_vs_record_length(0, 4) == 0);

    check("validate_payload_rejects_oversize_fragment",
          d200_vs_validate_payload(D200_VS_FRAME, payload, D200_VS_MAX_PAYLOAD + 1,
                                   D200_VS_PRODUCER) == 0);

    /* total == D200_VS_MAX_JPEG is the largest legal image; +1 is not. */
    d200_vs_put_u64(payload, 0);
    d200_vs_put_u32(payload + 8, D200_VS_MAX_JPEG + 1);
    d200_vs_put_u32(payload + 12, 0);
    check("validate_payload_rejects_oversize_total",
          d200_vs_validate_payload(D200_VS_FRAME, payload, length, D200_VS_PRODUCER) == 0 &&
          d200_vs_validate_payload(D200_VS_FRAME, payload, 1u + 16u, D200_VS_PRODUCER) == 0);

    /* Re-fill: the total above is still MAX_JPEG+1, which would make a legal payload look
     * invalid and turn this into a test of the wrong thing. */
    length = fill_record(D200_VS_FRAME, payload, sizeof(payload));
    check("validate_payload_rejects_null_and_overlong_direction",
          d200_vs_validate_payload(D200_VS_FRAME, NULL, length, D200_VS_PRODUCER) == 0 &&
          d200_vs_validate_payload(D200_VS_FRAME, payload, length, 3) == 0 &&
          d200_vs_validate_payload(D200_VS_FRAME, payload, length, D200_VS_PRODUCER) == 1 &&
          d200_vs_validate_payload(D200_VS_FRAME, payload, length, 0) == 1);

    /* Encode destinations that are exactly one byte too small. The buffers are heap-allocated at
     * exactly the short size so an overrun is an AddressSanitizer failure, not a silent pass. */
    length = fill_record(D200_VS_ATTACH, payload, sizeof(payload));
    out = make_header(D200_VS_ATTACH, length, 0, session_bytes);
    {
        uint8_t *short_buffer = malloc(D200_VS_HEADER_SIZE - 1);
        check("encode_header_rejects_one_byte_short_destination",
              short_buffer != NULL &&
              d200_vs_encode_header(short_buffer, D200_VS_HEADER_SIZE - 1, &out) == 0);
        free(short_buffer);
    }
    {
        uint8_t *short_buffer = malloc(D200_VS_HEADER_SIZE + length - 1);
        check("encode_record_rejects_one_byte_short_destination",
              short_buffer != NULL &&
              d200_vs_encode_record(short_buffer, D200_VS_HEADER_SIZE + length - 1,
                                    &out, payload) == 0);
        free(short_buffer);
    }
    /* A zero-length payload must not turn a byte-short destination into a legal one. */
    {
        uint8_t *short_buffer = malloc(D200_VS_HEADER_SIZE - 1);
        d200_vs_header empty = make_header(D200_VS_ATTACH, length, 0, session_bytes);
        check("encode_record_checks_header_capacity_not_only_payload",
              short_buffer != NULL &&
              d200_vs_encode_record(short_buffer, D200_VS_HEADER_SIZE - 1, &empty, payload) == 0);
        free(short_buffer);
    }

    /* A complete record with a mutated header must not decode. */
    d200_vs_encode_record(wire, sizeof(wire), &out, payload);
    wire[0] = 'X';
    check("record_decode_rejects_bad_magic",
          d200_vs_decode_record(wire, D200_VS_HEADER_SIZE + length, D200_VS_PRODUCER, &back) == 0);
}

/* --------------------------------------------------------------- control codec */

static void test_control_codec(const uint8_t *kinds, unsigned count) {
    uint8_t payload[512];
    uint8_t wire[sizeof(payload) + D200_VS_CONTROL_HEADER_SIZE];
    unsigned i;

    for (i = 0; i < count; ++i) {
        uint8_t kind = kinds[i];
        uint32_t length = fill_control(kind, payload, sizeof(payload));
        uint8_t back_kind = 0;
        uint32_t back_sequence = 0;
        char name[64];

        snprintf(name, sizeof(name), "control_round_trip_kind_%u", (unsigned)kind);
        check(name,
              length != 0 && length == d200_vs_control_length(kind) &&
              d200_vs_encode_control(wire, sizeof(wire), kind, 9, payload, length) == 1 &&
              d200_vs_decode_control(wire, D200_VS_CONTROL_HEADER_SIZE + length,
                                     &back_kind, &back_sequence) == 1 &&
              back_kind == kind && back_sequence == 9 &&
              !memcmp(wire, "D2PX", 4) && wire[4] == D200_VS_VERSION &&
              d200_vs_get_u32(wire + 8) == length);

        snprintf(name, sizeof(name), "control_decode_rejects_short_buffer_kind_%u", (unsigned)kind);
        check(name,
              d200_vs_decode_control(wire, D200_VS_CONTROL_HEADER_SIZE + length - 1,
                                     &back_kind, &back_sequence) == 0);

        snprintf(name, sizeof(name), "control_validate_rejects_wrong_length_kind_%u", (unsigned)kind);
        check(name,
              d200_vs_validate_control_payload(kind, payload, length + 1) == 0 &&
              d200_vs_validate_control_payload(kind, payload, length - 1) == 0);

        snprintf(name, sizeof(name), "control_encode_rejects_one_byte_short_kind_%u", (unsigned)kind);
        {
            uint8_t *short_buffer = malloc(D200_VS_CONTROL_HEADER_SIZE + length - 1);
            check(name,
                  short_buffer != NULL &&
                  d200_vs_encode_control(short_buffer, D200_VS_CONTROL_HEADER_SIZE + length - 1,
                                         kind, 9, payload, length) == 0);
            free(short_buffer);
        }
    }

    check("control_length_rejects_unknown_kind",
          d200_vs_control_length(0) == 0 && d200_vs_control_length(20) == 0 &&
          d200_vs_control_length(27) == 0 &&
          d200_vs_validate_control_payload(0, payload, 0) == 0 &&
          d200_vs_validate_control_payload(D200_VS_VIDEO_OPEN_REQUEST, NULL, 40) == 0);

    /* A wrong-length payload must be rejected independently of its content, and a result record
     * carrying a non-zero result must have its tail zeroed. */
    {
        uint32_t length = fill_control(D200_VS_VIDEO_OPEN_RESULT, payload, sizeof(payload));
        uint8_t back_kind = 0;
        uint32_t back_sequence = 0;
        check("control_validate_accepts_complete_result_record",
              length == 72 && d200_vs_validate_control_payload(D200_VS_VIDEO_OPEN_RESULT,
                                                                payload, length) == 1);
        d200_vs_put_u32(payload + 28, 0);
        check("control_validate_rejects_success_result_with_zeroed_flag",
              d200_vs_validate_control_payload(D200_VS_VIDEO_OPEN_RESULT, payload, length) == 0);
        d200_vs_put_u32(payload + 28, 1);
        /* An error result must carry 0 in the version/epoch field and a fully zeroed tail from
         * p+36 on; see the header's `k == 22 && result ? 0u : 1u` and its mirror in
         * vendor/d200_video_stream.py. Zeroing only the flag is not enough. */
        d200_vs_put_u32(payload + 32, D200_VS_START_FAILED);
        d200_vs_put_u32(payload + 28, 0);
        memset(payload + 36, 0, length - 36);
        check("control_validate_accepts_zeroed_error_result",
              d200_vs_validate_control_payload(D200_VS_VIDEO_OPEN_RESULT, payload, length) == 1);
        payload[71] = 1;
        check("control_validate_rejects_nonzero_error_result_tail",
              d200_vs_validate_control_payload(D200_VS_VIDEO_OPEN_RESULT, payload, length) == 0);
        payload[71] = 0;
        d200_vs_put_u32(payload + 28, 1);
        check("control_validate_rejects_error_result_carrying_the_success_flag",
              d200_vs_validate_control_payload(D200_VS_VIDEO_OPEN_RESULT, payload, length) == 0);
        memset(payload, 0, length);
        d200_vs_put_u32(payload + 24, 1);
        d200_vs_put_u32(payload + 32, D200_VS_OK);
        d200_vs_put_u32(payload + 36, D200_VS_IDLE);
        d200_vs_put_u32(payload + 40, D200_VS_SOURCE_FAILURE);
        check("control_validate_rejects_cancel_result_without_terminal_state",
              d200_vs_validate_control_payload(D200_VS_VIDEO_CANCEL_RESULT, payload, 44) == 0);
        wire[0] = 'X';
        check("control_decode_rejects_bad_magic",
              d200_vs_decode_control(wire, D200_VS_CONTROL_HEADER_SIZE + 40,
                                     &back_kind, &back_sequence) == 0);
    }
}

/* ------------------------------------------------------------ session lifecycle */

static void test_state_lifecycle(void) {
    d200_vs_state s;
    uint8_t payload[512];
    uint8_t other_session[16];
    d200_vs_header h;
    uint32_t length;
    unsigned i;

    for (i = 0; i < sizeof(capability); ++i) capability[i] = (uint8_t)(0x40 + i);
    memset(other_session, 0x5a, sizeof(other_session));

    check("state_init_accepts_valid_parameters_and_starts_unattached",
          d200_vs_state_init(&s, session_bytes, 30, 1, capability) == 1 &&
          s.attached == 0 && s.ready == 0 && !s.terminal);

    length = fill_record(D200_VS_ATTACH, payload, sizeof(payload));
    memcpy(payload, capability, 32);
    h = make_header(D200_VS_ATTACH, length, s.sequence[0], session_bytes);
    check("state_accept_attach",
          d200_vs_state_accept(&s, &h, payload, D200_VS_PRODUCER) == 1 && s.attached == 1);

    length = fill_record(D200_VS_READY, payload, sizeof(payload));
    h = make_header(D200_VS_READY, length, s.sequence[1], session_bytes);
    check("state_accept_ready",
          d200_vs_state_accept(&s, &h, payload, D200_VS_CONSUMER) == 1 && s.ready == 1);

    length = fill_record(D200_VS_FRAME, payload, sizeof(payload));
    h = make_header(D200_VS_FRAME, length, s.sequence[0], session_bytes);
    check("state_accept_frame",
          d200_vs_state_accept(&s, &h, payload, D200_VS_PRODUCER) == 1 &&
          s.received == 1 && s.consumed == 0);

    length = fill_record(D200_VS_CONSUMED, payload, sizeof(payload));
    h = make_header(D200_VS_CONSUMED, length, s.sequence[1], session_bytes);
    check("state_accept_consumed",
          d200_vs_state_accept(&s, &h, payload, D200_VS_CONSUMER) == 1 &&
          s.consumed == 1 && s.received == 1);

    length = fill_record(D200_VS_EOS, payload, sizeof(payload));
    h = make_header(D200_VS_EOS, length, s.sequence[0], session_bytes);
    check("state_accept_eos",
          d200_vs_state_accept(&s, &h, payload, D200_VS_PRODUCER) == 1 &&
          s.has_eos == 1 && s.eos == s.received);

    length = fill_record(D200_VS_DONE, payload, sizeof(payload));
    h = make_header(D200_VS_DONE, length, s.sequence[1], session_bytes);
    check("state_accept_done_is_terminal",
          d200_vs_state_accept(&s, &h, payload, D200_VS_CONSUMER) == 1 && s.terminal == 1);
    check("state_finish_after_terminal_returns_1", d200_vs_state_finish(&s) == 1);

    /* Out-of-order and mismatched headers must be refused by a fresh session. */
    {
        d200_vs_state t;
        d200_vs_state_init(&t, session_bytes, 30, 1, capability);
        length = fill_record(D200_VS_ATTACH, payload, sizeof(payload));
        memcpy(payload, capability, 32);
        h = make_header(D200_VS_ATTACH, length, 7, session_bytes);
        check("state_accept_rejects_out_of_order_sequence",
              d200_vs_state_accept(&t, &h, payload, D200_VS_PRODUCER) == 0 && t.terminal == 1);
    }
    {
        d200_vs_state t;
        d200_vs_state_init(&t, session_bytes, 30, 1, capability);
        length = fill_record(D200_VS_ATTACH, payload, sizeof(payload));
        memcpy(payload, capability, 32);
        h = make_header(D200_VS_ATTACH, length, 0, other_session);
        check("state_accept_rejects_wrong_session",
              d200_vs_state_accept(&t, &h, payload, D200_VS_PRODUCER) == 0);
    }
    {
        d200_vs_state t;
        d200_vs_state_init(&t, session_bytes, 30, 1, capability);
        length = fill_record(D200_VS_ATTACH, payload, sizeof(payload));
        memset(payload, 0x11, 32);
        h = make_header(D200_VS_ATTACH, length, 0, session_bytes);
        check("state_accept_rejects_attach_with_wrong_capability",
              d200_vs_state_accept(&t, &h, payload, D200_VS_PRODUCER) == 0 && t.attached == 0);
    }
    {
        d200_vs_state t;
        d200_vs_state_init(&t, session_bytes, 30, 1, capability);
        length = fill_record(D200_VS_ATTACH, payload, sizeof(payload));
        memcpy(payload, capability, 32);
        h = make_header(D200_VS_ATTACH, length, 0, session_bytes);
        check("state_accept_rejects_direction_zero",
              d200_vs_state_accept(&t, &h, payload, 0) == 0);
    }
    {
        d200_vs_state t;
        d200_vs_state_init(&t, session_bytes, 30, 1, capability);
        length = fill_record(D200_VS_ATTACH, payload, sizeof(payload));
        memcpy(payload, capability, 32);
        h = make_header(D200_VS_ATTACH, length, 0, session_bytes);
        check("state_accept_rejects_direction_three",
              d200_vs_state_accept(&t, &h, payload, 3) == 0);
    }
    {
        d200_vs_state t;
        d200_vs_state_init(&t, session_bytes, 30, 1, NULL);
        length = fill_record(D200_VS_ATTACH, payload, sizeof(payload));
        h = make_header(D200_VS_ATTACH, length, 0, session_bytes);
        check("state_accept_rejects_second_attach",
              d200_vs_state_accept(&t, &h, payload, D200_VS_PRODUCER) == 0);
    }
    {
        d200_vs_state t;
        d200_vs_state_init(&t, session_bytes, 30, 1, capability);
        length = fill_record(D200_VS_READY, payload, sizeof(payload));
        h = make_header(D200_VS_READY, length, t.sequence[1], session_bytes);
        check("state_accept_rejects_ready_before_attach",
              d200_vs_state_accept(&t, &h, payload, D200_VS_CONSUMER) == 0);
    }
    {
        d200_vs_state t;
        d200_vs_state_init(&t, session_bytes, 30, 1, NULL);
        length = fill_record(D200_VS_FRAME, payload, sizeof(payload));
        h = make_header(D200_VS_FRAME, length, t.sequence[0], session_bytes);
        check("state_accept_rejects_frame_before_ready",
              d200_vs_state_accept(&t, &h, payload, D200_VS_PRODUCER) == 0);
    }
    {
        d200_vs_state t;
        d200_vs_state_init(&t, session_bytes, 30, 1, NULL);
        t.ready = 1;
        length = fill_record(D200_VS_CONSUMED, payload, sizeof(payload));
        h = make_header(D200_VS_CONSUMED, length, t.sequence[1], session_bytes);
        check("state_accept_rejects_consumed_without_a_frame",
              d200_vs_state_accept(&t, &h, payload, D200_VS_CONSUMER) == 0);
    }
    {
        d200_vs_state t;
        d200_vs_state_init(&t, session_bytes, 30, 1, NULL);
        t.ready = 1;
        length = fill_record(D200_VS_READY, payload, sizeof(payload));
        h = make_header(D200_VS_READY, length, t.sequence[1], session_bytes);
        check("state_accept_rejects_duplicate_ready",
              d200_vs_state_accept(&t, &h, payload, D200_VS_CONSUMER) == 0);
    }
    {
        d200_vs_state t;
        d200_vs_state_init(&t, session_bytes, 30, 1, NULL);
        length = fill_record(D200_VS_ERROR, payload, sizeof(payload));
        h = make_header(D200_VS_ERROR, length, t.sequence[0], session_bytes);
        check("state_accept_error_is_terminal",
              d200_vs_state_accept(&t, &h, payload, D200_VS_PRODUCER) == 1 && t.terminal == 1);
    }

    check("state_init_rejects_zero_fps_and_nulls",
          d200_vs_state_init(&s, session_bytes, 30, 0, NULL) == 0 &&
          d200_vs_state_init(&s, session_bytes, 0, 1, NULL) == 0 &&
          d200_vs_state_init(NULL, session_bytes, 30, 1, NULL) == 0 &&
          d200_vs_state_init(&s, NULL, 30, 1, NULL) == 0);

    /* Fresh session: `s` is still terminal from the DONE case above, and cancel on a terminal
     * state is a documented failure. */
    check("state_init_restarts_the_session",
          /* A NULL capability means "already attached": the header sets
           * s->attached = (capability == NULL), skipping the ATTACH handshake. */
          d200_vs_state_init(&s, session_bytes, 30, 1, NULL) == 1 && !s.terminal &&
          s.attached == 1 && !s.ready && s.received == 0 && s.consumed == 0);
    check("state_cancel_accepts_known_reason_and_records_it",
          d200_vs_state_cancel(&s, D200_VS_SOURCE_FAILURE) == 1 &&
          s.cancel_reason == D200_VS_SOURCE_FAILURE && !s.terminal);
    check("state_cancel_rejects_different_second_reason",
          d200_vs_state_cancel(&s, D200_VS_RESULT_CANCELLED) == 0 && s.terminal == 1);
    {
        d200_vs_state t;
        d200_vs_state_init(&t, session_bytes, 30, 1, NULL);
        check("state_cancel_rejects_unknown_reason",
              d200_vs_state_cancel(&t, 7) == 0 && t.terminal == 1);
    }
    {
        d200_vs_state t;
        d200_vs_state_init(&t, session_bytes, 30, 1, NULL);
        check("state_finish_sets_terminal_and_returns_0",
              d200_vs_state_finish(&t) == 0 && t.terminal == 1);
        check("state_finish_is_idempotent", d200_vs_state_finish(&t) == 1);
    }
    check("state_api_rejects_null_state",
          d200_vs_state_accept(NULL, NULL, NULL, D200_VS_PRODUCER) == 0 &&
          d200_vs_state_cancel(NULL, D200_VS_SOURCE_FAILURE) == 0 &&
          d200_vs_state_finish(NULL) == 0);
}

int main(void) {
    static const uint8_t record_kinds[] = {
        D200_VS_ATTACH, D200_VS_READY, D200_VS_FRAME, D200_VS_CONSUMED, D200_VS_EOS,
        D200_VS_DONE, D200_VS_CANCEL, D200_VS_CANCELLED, D200_VS_ERROR
    };
    static const uint8_t control_kinds[] = {
        D200_VS_VIDEO_OPEN_REQUEST, D200_VS_VIDEO_OPEN_RESULT,
        D200_VS_VIDEO_CANCEL_REQUEST, D200_VS_VIDEO_CANCEL_RESULT,
        D200_VS_VIDEO_STATUS_REQUEST, D200_VS_VIDEO_STATUS_RESULT
    };

    memcpy(session_bytes, session_text, sizeof(session_bytes));

    test_header_codec();
    test_record_codec(record_kinds, (unsigned)(sizeof(record_kinds) / sizeof(record_kinds[0])));
    test_record_negative();
    test_control_codec(control_kinds, (unsigned)(sizeof(control_kinds) / sizeof(control_kinds[0])));
    test_state_lifecycle();

    printf("TOTAL %u\n", cases);
    printf("FAILED %u\n", failures);
    return failures ? 1 : 0;
}
"""


# --------------------------------------------------------------------------- header parsing


def _section(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


def _case_labels(header_text: str, signature: str) -> set[int]:
    """Every `case N:` label inside the function starting at `signature`.

    This is how the expected kind lists stay tied to the header: a new wire kind added to either
    dispatch function changes this set, and the test then demands a driver case for it.
    """
    body = _section(header_text, signature, "static inline int ")
    return {int(match.group(1)) for match in re.finditer(r"case\s+(\d+)\s*:", body)}


def expected_case_names() -> set[str]:
    header_text = HEADER.read_text(encoding="utf-8")
    record_kinds = _case_labels(header_text, "d200_vs_record_length(")
    control_kinds = _case_labels(header_text, "d200_vs_control_length(")
    names = {"header_round_trip", "state_accept_attach", "state_accept_frame"}
    names |= {f"record_round_trip_kind_{kind}" for kind in record_kinds}
    names |= {f"control_round_trip_kind_{kind}" for kind in control_kinds}
    return names


RECORD_KINDS = _case_labels(HEADER.read_text(encoding="utf-8"), "d200_vs_record_length(")
CONTROL_KINDS = _case_labels(HEADER.read_text(encoding="utf-8"), "d200_vs_control_length(")


# --------------------------------------------------------------------------- coverage reporting


class _CoverageReport:
    """Prints the driver's per-case lines in the terminal summary, so a green run still shows them."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def pytest_terminal_summary(self, terminalreporter) -> None:  # pragma: no cover - pytest hook
        for line in self.lines:
            terminalreporter.write_line(line)


_coverage = _CoverageReport()


@pytest.fixture(scope="module", autouse=True)
def _publish_coverage(request):
    # Registered for the session and deliberately never unregistered: a module-scoped teardown runs
    # before pytest's terminal summary, so unregistering here would silently drop the coverage
    # report. The pluginmanager is per session, so nothing leaks into another run.
    request.config.pluginmanager.register(_coverage, "devicecolor-codec-coverage")
    yield


# --------------------------------------------------------------------------- the harness


def _host_cc() -> str | None:
    """The host C compiler, or None when this box has none."""
    return shutil.which("cc") or shutil.which("clang")


def _compile(cc: str, source: Path, binary: Path, extra: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
    return subprocess.run(
        [cc, *CC_FLAGS, *extra, f"-I{HEADER.parent}", "-o", str(binary), str(source)],
        capture_output=True,
        text=True,
    )


def _run_driver(tmp_path: Path, label: str, extra: tuple[str, ...]) -> tuple[dict[str, str], str]:
    """Compile the driver with `extra` flags, run it, and return (case -> PASS/FAIL, raw output)."""
    cc = _host_cc()
    assert cc is not None
    source = tmp_path / f"driver_{label}.c"
    binary = tmp_path / f"driver_{label}"
    source.write_text(DRIVER, encoding="utf-8")

    compiled = _compile(cc, source, binary, extra)
    assert compiled.returncode == 0, f"[{label}] compiler output:\n{compiled.stdout}{compiled.stderr}"

    ran = subprocess.run([str(binary)], capture_output=True, text=True)
    output = ran.stdout + ran.stderr
    assert "AddressSanitizer" not in output, f"[{label}] sanitizer report:\n{output}"
    assert "runtime error:" not in output, f"[{label}] sanitizer report:\n{output}"
    assert "LeakSanitizer" not in output, f"[{label}] sanitizer report:\n{output}"

    statuses = {
        match.group(1): match.group(2)
        for match in re.finditer(r"^CASE (\S+) (PASS|FAIL)$", ran.stdout, re.M)
    }
    assert ran.returncode == 0, f"[{label}] driver exited {ran.returncode}:\n{output}"
    return statuses, output


def test_codec_suite_compiles_and_runs_device_free(tmp_path):
    """Compile `device/d200_video_stream.h` with the host compiler and sweep every entry point."""
    if _host_cc() is None:
        pytest.skip("no host C compiler (cc/clang) available")

    statuses, output = _run_driver(tmp_path, "plain", ())
    _coverage.lines = [f"devicecolor-codec[{line}]" for line in output.splitlines()]

    assert statuses, f"the driver printed no CASE lines:\n{output}"
    failed = sorted(name for name, status in statuses.items() if status != "PASS")
    assert not failed, f"driver cases failed: {failed}\n{output}"

    missing = sorted(expected_case_names() - statuses.keys())
    assert not missing, f"driver never exercised: {missing}\n{output}"

    # The kind sweep must cover every kind the header defines, not a convenient subset.
    for kind in sorted(RECORD_KINDS):
        assert f"record_round_trip_kind_{kind}" in statuses
    for kind in sorted(CONTROL_KINDS):
        assert f"control_round_trip_kind_{kind}" in statuses

    totals = dict(re.findall(r"^(TOTAL|FAILED) (\d+)$", output, re.M))
    assert totals.get("FAILED") == "0", output
    assert int(totals.get("TOTAL", "0")) >= len(expected_case_names())
    assert int(totals.get("TOTAL", "0")) >= 60, f"coverage regressed to {totals.get('TOTAL')} cases"


def test_codec_suite_is_clean_under_asan_and_ubsan(tmp_path):
    """The same driver under `-fsanitize=address,undefined`, including exact-size short buffers."""
    cc = _host_cc()
    if cc is None:
        pytest.skip("no host C compiler (cc/clang) available")

    probe_source = tmp_path / "sanitizer_probe.c"
    probe_binary = tmp_path / "sanitizer_probe"
    probe_source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
    probe = _compile(cc, probe_source, probe_binary, SANITIZE_FLAGS)
    if probe.returncode != 0:
        _coverage.lines.append(
            "devicecolor-codec[sanitizer] UNAVAILABLE: "
            + (probe.stdout + probe.stderr).strip().splitlines()[0]
        )
        pytest.skip(f"no address/undefined sanitizer on this host: {probe.stderr.strip()}")

    statuses, output = _run_driver(tmp_path, "sanitized", SANITIZE_FLAGS)
    failed = sorted(name for name, status in statuses.items() if status != "PASS")
    assert not failed, f"driver cases failed under sanitizers: {failed}\n{output}"
    assert statuses.keys() >= expected_case_names(), output
    _coverage.lines.append(f"devicecolor-codec[sanitizer] clean, {len(statuses)} cases")

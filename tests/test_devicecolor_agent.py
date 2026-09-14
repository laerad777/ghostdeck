"""Device-free host harness for the production core of `device/d200-color-agent.c`.

Why this exists
---------------
`device/d200-color-agent.c` runs on the deck and is the thing that consumes both wire headers this
lane owns. Until this module existed it was proven only by `-fsyntax-only`: no test on any host ever
*executed* its record intake, its pacing/credit state machine, its terminal-record selection, or the
JSON diagnostics the host side reads. That is the same failure shape the master found on hardware,
where 465 green tests certified a `stop` that could not work because nothing exercised the real
path.

The agent declares its own extraction seams in-source:

    /* UNBIND_REQUEST_BEGIN: extracted separately with injected open/ioctl/close. */
    /* DIVP_OPEN_BEGIN: extracted with injected loader and vendor callbacks. */
    /* STREAM_CORE_BEGIN: production core extracted by the host-only fixture. */

**No fixture for any of the three existed in the tree.** This module is the `STREAM_CORE` one. It
slices the production text out of the agent (read-only) and compiles it into a host driver together
with an injected `present` callback and a real socketpair, so the code under test is the shipped
code and not a copy.

Nothing is hand-copied that can be derived:

* the case set is parsed from the driver's own `check("...")` sites, so a deleted case fails instead
  of quietly shrinking coverage;
* every `static` function the core defines must be named by the driver, or be an entry in the
  explicit "reached transitively" allowlist — which itself fails if it goes stale;
* the wire constants the driver reports at runtime are compared against `device/d200_video_stream.h`;
* the diagnostic JSON is parsed and checked key-for-key, because the host side reads `framesConsumed`
  and friends out of this exact record.

Two things this does NOT do, stated plainly: the real decoder callback (`tj3*`, `/dev/divp`) is still
untouched, and `present_planes`' buffer-write path cannot be replayed on a 64-bit host because it
round-trips the vendor buffer address through a 32-bit field. Only its rejection paths run here.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "device" / "d200-color-agent.c"
WIRE_HEADER = ROOT / "device" / "d200_video_stream.h"
REAL_HOME = Path(os.path.expanduser("~")).resolve()

CC_FLAGS = ("-std=c11", "-O1", "-Wall", "-Wextra", "-Werror")
SANITIZE_FLAGS = ("-fsanitize=address,undefined", "-fno-omit-frame-pointer")

# The extraction seams the agent declares for itself. This module owns STREAM_CORE; the other two
# are still without a fixture, and asserting they are still declared keeps that gap on the record.
SEAM_MARKERS = {
    "UNBIND_REQUEST": ("/* UNBIND_REQUEST_BEGIN", "/* UNBIND_REQUEST_END */"),
    "DIVP_OPEN": ("/* DIVP_OPEN_BEGIN", "/* DIVP_OPEN_END */"),
    "STREAM_CORE": ("/* STREAM_CORE_BEGIN", "/* STREAM_CORE_END */"),
}
CORE_START = "#define WIDTH 540"
CORE_END = "/* STREAM_CORE_END */"

# Tokens that would mean the slice had escaped the device-free core and pulled in vendor code.
FORBIDDEN_IN_CORE = ("turbojpeg", "tj3", "divp_context display", "struct presenter")

# Core functions the driver reaches only through another entry point. The list is asserted against
# the core, so it cannot rot into a place where a new function hides.
REACHED_TRANSITIVELY = frozenset(
    {
        "color_emit",        # called by color_ready, color_step and color_terminal
        "color_optional_ns",  # called by color_startup_json and color_summary
        "put16",
        "get16",
        "get32",             # used by present_planes
    }
)

# Every diagnostic key the terminal record carries. The host side reads `framesConsumed` out of this
# exact object, so a rename on either side has to fail here.
SUMMARY_KEYS = frozenset(
    {
        "event", "session", "epoch", "clock", "readyNs", "firstSubmissionNs", "framesReceived",
        "framesConsumed", "successfulSubmissions", "jpegBytesReceived", "queueHighwaterFrames",
        "lateSubmissions", "presentationAttempts", "presentationTotalNs", "presentationMaxNs",
        "cleanupDurationNs", "terminalCode", "countersSaturated", "pixelProof", "startup",
    }
)
STARTUP_KEYS = frozenset(
    {"outcome", "stage", "domain", "vendorReturn", "vendorReturnU32", "errno", "observedNs"}
)

# The contracts this harness proves. Deleting one from the driver makes this test fail rather than
# quietly shrinking coverage.
CONTRACT_CASES = frozenset(
    {
        "a_queued_credit_blocks_the_next_presentation",
        "clamp_saturates_out_of_range_values",
        "cleanup_clears_every_stage_flag",
        "cleanup_failure_is_sticky",
        "cleanup_failure_overrides_the_terminal_reason",
        "cleanup_releases_every_stage_in_order",
        "cleanup_reports_a_failing_stage",
        "counter_saturation_is_reported",
        "divp_open_names_the_failing_loader_stage",
        "failure_notice_does_not_replace_a_pending_record",
        "failure_notice_queues_an_error_record",
        "failure_notice_shuts_down_a_cancelled_stream",
        "pending_presentation_does_not_move_the_deadline",
        "pending_presentation_grants_no_credit",
        "poll_timeout_is_immediate_when_overdue",
        "poll_timeout_is_short_while_idle",
        "poll_timeout_tracks_the_remaining_slice",
        "present_planes_rejects_a_buffer_with_the_wrong_format",
        "present_planes_rejects_a_buffer_without_a_destination",
        "present_planes_reports_a_missing_buffer",
        "range_tables_follow_the_documented_limits",
        "receive_accepts_a_complete_frame_record",
        "receive_copies_a_fragment_at_its_declared_offset",
        "receive_rejects_a_malformed_header",
        "receive_rejects_a_third_frame_while_the_window_is_full",
        "receive_rejects_eos_without_a_frame",
        "receive_reports_disconnect_on_peer_close",
        "receive_resets_the_accumulator_after_a_record",
        "receive_returns_the_cancel_reason",
        "receive_returns_the_error_code",
        "receive_returns_zero_when_nothing_is_available",
        "receive_waits_for_the_declared_payload",
        "receive_waits_for_the_whole_header",
        "signal_selects_the_cancelled_reason",
        "signal_selects_the_source_failure_reason",
        "startup_failure_emits_exactly_one_record",
        "startup_failure_is_silent_without_a_stage",
        "startup_json_reports_a_loader_stage",
        "startup_json_reports_a_ready_outcome",
        "startup_json_reports_the_vendor_return",
        "startup_vendor_records_the_raw_return_code",
        "step_emits_consumed_with_the_consumed_index",
        "step_holds_the_next_credit_before_the_deadline",
        "step_presents_without_emitting_credit",
        "step_reports_a_presentation_failure",
        "step_waits_while_the_presenter_is_pending",
        "summary_carries_the_documented_keys",
        "summary_reports_null_for_unobserved_milestones",
        "terminal_emits_cancelled_for_a_cancel_reason",
        "terminal_emits_done_after_eos_and_consumption",
        "terminal_emits_error_for_a_failure_reason",
        "terminal_refuses_while_a_credit_is_owed",
        "terminal_refuses_while_pacing_with_a_settled_credit",
        "terminal_refuses_while_a_record_is_pending",
        "terminal_refuses_without_an_eos",
        "the_next_frame_is_presented_once_the_credit_is_flushed",
        "timeout_fires_after_the_progress_budget",
        "timeout_is_quiet_while_progress_is_recent",
        "unbind_ioctl_fails_closed_without_the_device_node",
        "write_summary_preserves_an_existing_nonblocking_mode",
        "write_summary_reports_a_short_write",
        "write_summary_restores_the_descriptor_flags",
    }
)

# Deliberate bugs each sanitizer must report, so one that links but does not instrument cannot
# pass as "clean". Both use observable stores: a store whose value is never read is deleted at -O1
# and would silently defeat the check.
SANITIZER_CONTROLS = (
    (
        "address",
        "AddressSanitizer: heap-buffer-overflow",
        r"""
#include <stdlib.h>
#include <stdio.h>
int main(void) {
    unsigned char *p = malloc(4);
    volatile unsigned sum = 0;
    unsigned i;
    for (i = 0; i < 64; ++i) { p[i] = (unsigned char)i; sum += p[i]; }
    printf("sum=%u\n", sum);
    free(p);
    return 0;
}
""",
    ),
    (
        "undefined",
        "runtime error: signed integer overflow",
        r"""
#include <stdio.h>
int main(void) {
    volatile int x = 2147483647;
    int y = x + 1;
    printf("y=%d\n", y);
    return 0;
}
""",
    ),
)

PREAMBLE = '''#define _DARWIN_C_SOURCE 1
#define _POSIX_C_SOURCE 200809L
#include <dlfcn.h>
#include <fcntl.h>
#include <errno.h>
#include <signal.h>
#include <poll.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#ifndef MSG_NOSIGNAL
#define MSG_NOSIGNAL 0
#endif
#include "d200_video_stream.h"
'''

DRIVER = r'''/* Host driver for the extracted production core of device/d200-color-agent.c.
 * Assembled as: includes -> core (agent lines 20..STREAM_CORE_END) -> this file.
 *
 * NOTE on 64-bit hosts: `present_planes` round-trips the vendor buffer address through a 32-bit
 * field of the MI info block, which is correct for the 32-bit armv7 target but cannot be replayed
 * on a 64-bit host. Only its rejection paths are driven here; the write path needs the deck's
 * 32-bit address space. */

/* ------------------------------------------------------------------ harness */

static unsigned cases;
static unsigned failures;
static volatile unsigned observable_sink;

static void observe(const uint8_t *p, size_t n) {
    size_t i;
    unsigned sum = 0;
    for (i = 0; i < n; ++i) sum = sum * 31u + p[i];
    observable_sink = sum;
}

static void check(const char *name, int ok) {
    ++cases;
    if (ok) {
        printf("CASE %s PASS\n", name);
    } else {
        ++failures;
        printf("CASE %s FAIL\n", name);
    }
}

static void emit_json(const char *label, const char *json) {
    printf("JSON %s %s", label, json);
    if (!json[0] || json[strlen(json) - 1] != '\n') printf("\n");
}

static const uint8_t SESSION[16] = {
    0x00, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77,
    0x88, 0x99, 0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xff
};

/* ------------------------------------------------------------------ presenter script */

static unsigned present_calls;
static int present_pending_left;
static int present_failure;
static const uint8_t *present_jpeg_seen;
static size_t present_size_seen;

static int present_cb(void *context, const uint8_t *jpeg, size_t size) {
    (void)context;
    ++present_calls;
    present_jpeg_seen = jpeg;
    present_size_seen = size;
    if (present_pending_left > 0) { --present_pending_left; return 1; }
    if (present_failure) return -7;
    return 0;
}

static void present_reset(void) {
    present_calls = 0;
    present_pending_left = 0;
    present_failure = 0;
    present_jpeg_seen = NULL;
    present_size_seen = 0;
}

/* ------------------------------------------------------------------ stream fixture */

struct fixture {
    struct color_stream *s;
    int fd[2];
    uint64_t producer_sequence;
};

static void fixture_start(struct fixture *f, uint32_t fps_n, uint32_t fps_d) {
    memset(f, 0, sizeof(*f));
    f->fd[0] = f->fd[1] = -1;
    if (socketpair(AF_UNIX, SOCK_STREAM, 0, f->fd)) f->fd[0] = f->fd[1] = -1;
    f->s = calloc(1, sizeof(*f->s));
    if (!f->s) return;
    f->s->slots[0] = calloc(1, D200_VS_MAX_JPEG);
    f->s->slots[1] = calloc(1, D200_VS_MAX_JPEG);
    present_reset();
    d200_vs_state_init(&f->s->wire, SESSION, fps_n, fps_d, NULL);
    if (color_ready(f->s, SESSION, fps_n, fps_d)) return;
    while (f->s->out_size) {
        if (color_flush(f->s, f->fd[0])) break;
    }
}

static void fixture_stop(struct fixture *f) {
    if (f->fd[0] >= 0) close(f->fd[0]);
    if (f->fd[1] >= 0) close(f->fd[1]);
    if (f->s) {
        free(f->s->slots[0]);
        free(f->s->slots[1]);
        free(f->s);
    }
    f->s = NULL;
}

static int send_record(struct fixture *f, uint8_t kind, const uint8_t *p, size_t n) {
    uint8_t buffer[D200_VS_HEADER_SIZE + 256];
    d200_vs_header h = {0};
    h.kind = kind;
    h.payload_length = (uint32_t)n;
    h.epoch = 1;
    h.sequence = f->producer_sequence++;
    memcpy(h.session, SESSION, 16);
    if (!d200_vs_encode_record(buffer, sizeof(buffer), &h, p)) return -1;
    if (send(f->fd[1], buffer, D200_VS_HEADER_SIZE + n, 0) != (ssize_t)(D200_VS_HEADER_SIZE + n))
        return -1;
    return 0;
}

static int send_frame(struct fixture *f, uint64_t index, uint32_t total, uint32_t offset,
                      const uint8_t *fragment, size_t n) {
    uint8_t payload[16 + 256];
    d200_vs_put_u64(payload, index);
    d200_vs_put_u32(payload + 8, total);
    d200_vs_put_u32(payload + 12, offset);
    memcpy(payload + 16, fragment, n);
    return send_record(f, D200_VS_FRAME, payload, 16 + n);
}

static d200_vs_header output_header(struct color_stream *s) {
    d200_vs_header h = {0};
    (void)d200_vs_decode_header(s->output, D200_VS_HEADER_SIZE, &h);
    return h;
}

static void drain_output(struct color_stream *s, int fd) {
    while (s->out_size) {
        if (color_flush(s, fd)) break;
    }
}

/* `color_receive` consumes one bounded pass per call: the 40-byte header first, then the declared
 * payload. Drive one whole record (or return the first error) the way the agent's poll loop does. */
static int receive_record(struct fixture *f) {
    int rc;
    do {
        rc = color_receive(f->s, f->fd[0]);
        if (rc) return rc;
    } while (f->s->have != 0 || f->s->need != D200_VS_HEADER_SIZE);
    return 0;
}

/* ------------------------------------------------------------------ A: wire intake */

static void test_receive(void) {
    struct fixture f;
    uint8_t fragment[4] = {0xde, 0xad, 0xbe, 0xef};
    uint8_t tail[2] = {0x11, 0x22};
    uint8_t payload[64];

    fixture_start(&f, 30, 1);
    close(f.fd[1]);
    f.fd[1] = -1;
    check("receive_reports_disconnect_on_peer_close",
          color_receive(f.s, f.fd[0]) == D200_VS_DISCONNECTED);
    fixture_stop(&f);

    fixture_start(&f, 30, 1);
    check("receive_returns_zero_when_nothing_is_available",
          color_receive(f.s, f.fd[0]) == 0 && f.s->have == 0);
    fixture_stop(&f);

    /* half a header */
    fixture_start(&f, 30, 1);
    {
        uint8_t header[D200_VS_HEADER_SIZE];
        d200_vs_header h = {0};
        h.kind = D200_VS_FRAME;
        h.payload_length = 20;
        h.epoch = 1;
        h.sequence = f.producer_sequence;
        memcpy(h.session, SESSION, 16);
        check("receive_waits_for_the_whole_header",
              d200_vs_encode_header(header, sizeof(header), &h) == 1 &&
              send(f.fd[1], header, 20, 0) == 20 &&
              color_receive(f.s, f.fd[0]) == 0 && f.s->have == 20 && f.s->need == 40);
    }
    fixture_stop(&f);

    /* header complete, payload short */
    fixture_start(&f, 30, 1);
    {
        uint8_t record[D200_VS_HEADER_SIZE + 20];
        uint8_t frame_payload[20];
        d200_vs_header h = {0};
        d200_vs_put_u64(frame_payload, 0);
        d200_vs_put_u32(frame_payload + 8, 4);
        d200_vs_put_u32(frame_payload + 12, 0);
        memcpy(frame_payload + 16, fragment, 4);
        h.kind = D200_VS_FRAME;
        h.payload_length = sizeof(frame_payload);
        h.epoch = 1;
        h.sequence = f.producer_sequence++;
        memcpy(h.session, SESSION, 16);
        (void)d200_vs_encode_record(record, sizeof(record), &h, frame_payload);
        (void)send(f.fd[1], record, D200_VS_HEADER_SIZE + 2, 0);
        check("receive_waits_for_the_declared_payload",
              color_receive(f.s, f.fd[0]) == 0 && f.s->have == D200_VS_HEADER_SIZE &&
              f.s->need == D200_VS_HEADER_SIZE + 20 &&
              color_receive(f.s, f.fd[0]) == 0 && f.s->have == D200_VS_HEADER_SIZE + 2 &&
              f.s->need == D200_VS_HEADER_SIZE + 20);
    }
    fixture_stop(&f);

    fixture_start(&f, 30, 1);
    (void)send_frame(&f, 0, 4, 0, fragment, 4);
    check("receive_accepts_a_complete_frame_record",
          receive_record(&f) == 0 && f.s->wire.received == 1 &&
          f.s->wire.consumed == 0 && f.s->sizes[0] == 4 &&
          !memcmp(f.s->slots[0], fragment, 4) && f.s->jpeg_bytes == 4 &&
          f.s->queue_highwater == 1);
    fixture_stop(&f);

    fixture_start(&f, 30, 1);
    (void)send_frame(&f, 0, 4, 0, fragment, 2);
    (void)receive_record(&f);
    (void)send_frame(&f, 0, 4, 2, tail, 2);
    check("receive_copies_a_fragment_at_its_declared_offset",
          receive_record(&f) == 0 && f.s->wire.received == 1 &&
          f.s->wire.partial_total == 0 && f.s->wire.partial_offset == 0 &&
          !memcmp(f.s->slots[0], fragment, 2) && !memcmp(f.s->slots[0] + 2, tail, 2));
    fixture_stop(&f);

    fixture_start(&f, 30, 1);
    d200_vs_put_u32(payload, D200_VS_RESULT_CANCELLED);
    (void)send_record(&f, D200_VS_CANCEL, payload, 4);
    check("receive_returns_the_cancel_reason",
          receive_record(&f) == (int)D200_VS_RESULT_CANCELLED);
    fixture_stop(&f);

    fixture_start(&f, 30, 1);
    d200_vs_put_u32(payload, D200_VS_TIMEOUT);
    payload[4] = 'E';
    (void)send_record(&f, D200_VS_ERROR, payload, 5);
    check("receive_returns_the_error_code", receive_record(&f) == (int)D200_VS_TIMEOUT);
    fixture_stop(&f);

    fixture_start(&f, 30, 1);
    d200_vs_put_u64(payload, 0);
    (void)send_record(&f, D200_VS_EOS, payload, 8);
    check("receive_rejects_eos_without_a_frame",
          receive_record(&f) == (int)D200_VS_EMPTY_SOURCE);
    fixture_stop(&f);

    fixture_start(&f, 30, 1);
    memset(payload, 0, D200_VS_HEADER_SIZE);
    (void)send(f.fd[1], payload, D200_VS_HEADER_SIZE, 0);
    check("receive_rejects_a_malformed_header",
          color_receive(f.s, f.fd[0]) == (int)D200_VS_PROTOCOL);
    fixture_stop(&f);

    fixture_start(&f, 30, 1);
    (void)send_frame(&f, 0, 4, 0, fragment, 4);
    (void)receive_record(&f);
    check("receive_resets_the_accumulator_after_a_record",
          f.s->have == 0 && f.s->need == D200_VS_HEADER_SIZE && f.s->record_started == 0);
    fixture_stop(&f);

    fixture_start(&f, 30, 1);
    (void)send_frame(&f, 0, 4, 0, fragment, 4);
    (void)send_frame(&f, 1, 4, 0, fragment, 4);
    (void)receive_record(&f);
    (void)receive_record(&f);
    (void)send_frame(&f, 2, 4, 0, fragment, 4);
    check("receive_rejects_a_third_frame_while_the_window_is_full",
          receive_record(&f) == (int)D200_VS_PROTOCOL && f.s->wire.received == 2);
    fixture_stop(&f);

    observe(fragment, sizeof(fragment));
    observe(tail, sizeof(tail));
}

/* ------------------------------------------------------------------ B: pacing and credit */

static void test_pacing(void) {
    struct fixture f;
    uint8_t fragment[4] = {1, 2, 3, 4};
    uint64_t deadline_before;

    /* a pending decode is not a presentation attempt and grants no credit */
    fixture_start(&f, 30, 1);
    (void)send_frame(&f, 0, 4, 0, fragment, 4);
    (void)receive_record(&f);
    present_pending_left = 3;
    deadline_before = f.s->deadline;
    check("step_waits_while_the_presenter_is_pending",
          color_step(f.s, present_cb, NULL) == 0 && f.s->out_size == 0 &&
          f.s->presentation_attempts == 0 && f.s->submissions == 0 && f.s->pacing == 0);
    check("pending_presentation_grants_no_credit",
          f.s->wire.consumed == 0 && f.s->deadline == deadline_before &&
          f.s->presentation_ns == 0 && f.s->submissions == 0);
    fixture_stop(&f);

    /* two frames in flight, so the credit and the following presentation are both observable */
    fixture_start(&f, 30, 1);
    (void)send_frame(&f, 0, 4, 0, fragment, 4);
    (void)send_frame(&f, 1, 4, 0, fragment, 4);
    (void)receive_record(&f);
    (void)receive_record(&f);
    present_reset();
    check("step_presents_without_emitting_credit",
          color_step(f.s, present_cb, NULL) == 0 && f.s->out_size == 0 && f.s->pacing == 1 &&
          f.s->submissions == 1 && f.s->presentation_attempts == 1 && present_calls == 1 &&
          f.s->submission_observed == 1 && f.s->first_submission_ns != 0 &&
          present_jpeg_seen == f.s->slots[0] && present_size_seen == 4 && f.s->deadline != 0);
    check("step_holds_the_next_credit_before_the_deadline",
          color_step(f.s, present_cb, NULL) == 0 && f.s->out_size == 0 && present_calls == 1);
    f.s->deadline = 0;
    {
        d200_vs_header h;
        int rc = color_step(f.s, present_cb, NULL);
        h = output_header(f.s);
        check("step_emits_consumed_with_the_consumed_index",
              rc == 0 && h.kind == D200_VS_CONSUMED && h.payload_length == 8 &&
              d200_vs_get_u64(f.s->output + D200_VS_HEADER_SIZE) == 0 &&
              f.s->wire.consumed == 1 && f.s->pacing == 0 && present_calls == 1 &&
              f.s->out_size == D200_VS_HEADER_SIZE + 8);
    }
    /* the queued credit blocks the next presentation until the socket has taken it */
    check("a_queued_credit_blocks_the_next_presentation",
          color_step(f.s, present_cb, NULL) == 0 && present_calls == 1);
    drain_output(f.s, f.fd[0]);
    check("the_next_frame_is_presented_once_the_credit_is_flushed",
          color_step(f.s, present_cb, NULL) == 0 && present_calls == 2 &&
          present_jpeg_seen == f.s->slots[1] && f.s->submissions == 2);
    fixture_stop(&f);

    /* a failing presenter reports PRESENTATION */
    fixture_start(&f, 30, 1);
    (void)send_frame(&f, 0, 4, 0, fragment, 4);
    (void)receive_record(&f);
    present_reset();
    present_failure = 1;
    check("step_reports_a_presentation_failure",
          color_step(f.s, present_cb, NULL) == (int)D200_VS_PRESENTATION);
    fixture_stop(&f);

    /* pending work never moves the deadline */
    fixture_start(&f, 30, 1);
    (void)send_frame(&f, 0, 4, 0, fragment, 4);
    (void)receive_record(&f);
    present_reset();
    present_pending_left = 100;
    (void)color_step(f.s, present_cb, NULL);
    deadline_before = f.s->deadline;
    (void)color_step(f.s, present_cb, NULL);
    (void)color_step(f.s, present_cb, NULL);
    check("pending_presentation_does_not_move_the_deadline",
          f.s->deadline == deadline_before && f.s->pacing == 0 && f.s->submissions == 0 &&
          present_calls == 3);
    fixture_stop(&f);
}

/* ------------------------------------------------------------------ C: terminal record */

static void test_terminal(void) {
    struct fixture f;
    uint8_t fragment[4] = {9, 9, 9, 9};
    uint8_t payload[64];

    /* refuses while a record is queued */
    fixture_start(&f, 30, 1);
    (void)color_ready(f.s, SESSION, 30, 1);
    check("terminal_refuses_while_a_record_is_pending",
          f.s->out_size != 0 && color_terminal(f.s, 0, 0) == -1);
    fixture_stop(&f);

    /* refuses without EOS */
    fixture_start(&f, 30, 1);
    check("terminal_refuses_without_an_eos", color_terminal(f.s, 0, 0) == -1);
    fixture_stop(&f);

    /* one frame and EOS: the credit is still owed until the CONSUMED record goes out */
    fixture_start(&f, 30, 1);
    (void)send_frame(&f, 0, 4, 0, fragment, 4);
    (void)receive_record(&f);
    d200_vs_put_u64(payload, 1);
    (void)send_record(&f, D200_VS_EOS, payload, 8);
    (void)receive_record(&f);
    present_reset();
    (void)color_step(f.s, present_cb, NULL);
    check("terminal_refuses_while_a_credit_is_owed",
          f.s->wire.has_eos && f.s->wire.eos == 1 && f.s->wire.consumed == 0 &&
          f.s->pacing == 1 && color_terminal(f.s, 0, 0) == -1);
    /* once the credit is emitted and flushed the stream is settled, so DONE is available */
    f.s->deadline = 0;
    (void)color_step(f.s, present_cb, NULL);
    drain_output(f.s, f.fd[0]);
    {
        d200_vs_header h;
        int rc = color_terminal(f.s, 0, 0);
        h = output_header(f.s);
        check("terminal_emits_done_after_eos_and_consumption",
              f.s->wire.eos == f.s->wire.consumed && f.s->wire.eos == 1 &&
              rc == 0 && h.kind == D200_VS_DONE && h.payload_length == 20 &&
              d200_vs_get_u64(f.s->output + 40) == 1 &&
              d200_vs_get_u64(f.s->output + 48) == 1);
    }
    fixture_stop(&f);

    /* A settled credit with pacing still armed. The wire cannot reach this state: the credit that
     * settles `eos == consumed` is the same emit that clears pacing, and a presentation only arms
     * pacing while `consumed < received`. So this pins the defensive guard in `color_terminal`
     * explicitly rather than pretending to reproduce a deck state. */
    fixture_start(&f, 30, 1);
    (void)send_frame(&f, 0, 4, 0, fragment, 4);
    (void)receive_record(&f);
    d200_vs_put_u64(payload, 1);
    (void)send_record(&f, D200_VS_EOS, payload, 8);
    (void)receive_record(&f);
    present_reset();
    (void)color_step(f.s, present_cb, NULL);
    f.s->deadline = 0;
    (void)color_step(f.s, present_cb, NULL);
    drain_output(f.s, f.fd[0]);
    f.s->pacing = 1;
    check("terminal_refuses_while_pacing_with_a_settled_credit",
          f.s->wire.eos == f.s->wire.consumed && f.s->pacing == 1 &&
          color_terminal(f.s, 0, 0) == -1);
    f.s->pacing = 0;
    fixture_stop(&f);

    /* a failure reason becomes an ERROR record */
    fixture_start(&f, 30, 1);
    {
        d200_vs_header h;
        int rc = color_terminal(f.s, D200_VS_RESOURCE_LIMIT, 0);
        h = output_header(f.s);
        check("terminal_emits_error_for_a_failure_reason",
              rc == 0 && h.kind == D200_VS_ERROR && h.payload_length == 4 &&
              d200_vs_get_u32(f.s->output + 40) == D200_VS_RESOURCE_LIMIT);
    }
    fixture_stop(&f);

    /* a cancel-class reason becomes CANCELLED */
    fixture_start(&f, 30, 1);
    {
        d200_vs_header h;
        int rc = color_terminal(f.s, D200_VS_RESULT_CANCELLED, 0);
        h = output_header(f.s);
        check("terminal_emits_cancelled_for_a_cancel_reason",
              rc == 0 && h.kind == D200_VS_CANCELLED && h.payload_length == 4 &&
              d200_vs_get_u32(f.s->output + 40) == D200_VS_RESULT_CANCELLED);
    }
    fixture_stop(&f);

    /* cleanup failure overrides the reason */
    fixture_start(&f, 30, 1);
    {
        d200_vs_header h;
        int rc = color_terminal(f.s, D200_VS_RESOURCE_LIMIT, 1);
        h = output_header(f.s);
        check("cleanup_failure_overrides_the_terminal_reason",
              rc == 0 && h.kind == D200_VS_ERROR &&
              d200_vs_get_u32(f.s->output + 40) == D200_VS_CLEANUP_FAILED);
    }
    fixture_stop(&f);
}

/* ------------------------------------------------------------------ D: diagnostics */

static void test_diagnostics(void) {
    struct fixture f;
    uint8_t fragment[4] = {5, 5, 5, 5};
    char json[COLOR_SUMMARY_CAPACITY];
    int fd[2];

    fixture_start(&f, 30, 1);
    (void)send_frame(&f, 0, 4, 0, fragment, 4);
    (void)receive_record(&f);
    present_reset();
    (void)color_step(f.s, present_cb, NULL);
    /* `framesConsumed` is wire-level consumption: the frame only counts once its CONSUMED record
     * has been emitted and taken by the socket, exactly as it does on the deck. */
    f.s->deadline = 0;
    (void)color_step(f.s, present_cb, NULL);
    drain_output(f.s, f.fd[0]);
    color_cleanup_finished(f.s, monotonic_ns());
    {
        int n = color_summary(f.s, json, sizeof(json), 0);
        check("summary_carries_the_documented_keys", n > 0 && (size_t)n < sizeof(json));
        emit_json("terminal", json);
    }
    f.s->ready_observed = 0;
    f.s->submission_observed = 0;
    f.s->presentation_attempts = 0;
    f.s->cleanup_observed = 0;
    {
        int n = color_summary(f.s, json, sizeof(json), D200_VS_TIMEOUT);
        check("summary_reports_null_for_unobserved_milestones", n > 0 && (size_t)n < sizeof(json));
        emit_json("nulls", json);
    }
    f.s->metrics_saturated = 0;
    f.s->submissions = UINT64_MAX;
    color_counter(f.s, &f.s->submissions, 1);
    {
        int n = color_summary(f.s, json, sizeof(json), 0);
        check("counter_saturation_is_reported",
              n > 0 && f.s->submissions == UINT64_MAX && f.s->metrics_saturated == 1);
        emit_json("saturated", json);
    }
    fixture_stop(&f);

    {
        struct color_startup startup = {0};
        struct color_startup vendor = {0};
        char out[384];
        int n = color_startup_json(&startup, out, sizeof(out));
        startup.ready = 1;
        if (n > 0) n = color_startup_json(&startup, out, sizeof(out));
        check("startup_json_reports_a_ready_outcome", n > 0);
        emit_json("startup-ready", out);

        color_startup_fail(&startup, "loader.sys", COLOR_STARTUP_LOADER, 0, 0);
        n = color_startup_json(&startup, out, sizeof(out));
        check("startup_json_reports_a_loader_stage", n > 0 && startup.vendor_return == 0);
        emit_json("startup-loader", out);

        check("startup_vendor_records_the_raw_return_code",
              color_startup_vendor(&vendor, "vendor.MI_SYS_Init", -3) == -1 &&
              vendor.vendor_return == -3 && vendor.domain == COLOR_STARTUP_VENDOR &&
              vendor.error == 0);
        n = color_startup_json(&vendor, out, sizeof(out));
        check("startup_json_reports_the_vendor_return", n > 0 && vendor.vendor_return == -3);
        emit_json("startup-vendor", out);
    }

    /* The diagnostic write must not leave the sink nonblocking. The full flag word is deliberately
     * NOT compared: macOS normalises it on F_SETFL (a bare set-then-clear of O_NONBLOCK on a pipe
     * re-reads as 0x10001 here), so the contract that matters is the O_NONBLOCK bit itself. */
    if (!pipe(fd)) {
        char payload[] = "{\"event\":\"probe\"}\n";
        int rc = color_write_summary(fd[1], payload, sizeof(payload) - 1);
        int flags_after = fcntl(fd[1], F_GETFL);
        char received[64];
        ssize_t got = read(fd[0], received, sizeof(received));
        check("write_summary_restores_the_descriptor_flags",
              rc == 0 && !(flags_after & O_NONBLOCK) && got == (ssize_t)(sizeof(payload) - 1) &&
              !memcmp(received, payload, sizeof(payload) - 1));
        close(fd[0]);
        close(fd[1]);
    } else {
        check("write_summary_restores_the_descriptor_flags", 0);
    }

    /* A sink that is already nonblocking keeps that mode: the restore is conditional. */
    if (!pipe(fd)) {
        char payload[] = "{\"event\":\"probe\"}\n";
        (void)fcntl(fd[1], F_SETFL, fcntl(fd[1], F_GETFL) | O_NONBLOCK);
        {
            int rc = color_write_summary(fd[1], payload, sizeof(payload) - 1);
            int flags_after = fcntl(fd[1], F_GETFL);
            check("write_summary_preserves_an_existing_nonblocking_mode",
                  rc == 0 && (flags_after & O_NONBLOCK) != 0);
        }
        close(fd[0]);
        close(fd[1]);
    } else {
        check("write_summary_preserves_an_existing_nonblocking_mode", 0);
    }

    if (!pipe(fd)) {
        char filler[512];
        memset(filler, 'x', sizeof(filler));
        (void)fcntl(fd[1], F_SETFL, fcntl(fd[1], F_GETFL) | O_NONBLOCK);
        for (;;) { if (write(fd[1], filler, sizeof(filler)) < (ssize_t)sizeof(filler)) break; }
        {
            int rc = color_write_summary(fd[1], filler, 32);
            check("write_summary_reports_a_short_write",
                  rc == -1 && (fcntl(fd[1], F_GETFL) & O_NONBLOCK) != 0);
        }
        close(fd[0]);
        close(fd[1]);
    } else {
        check("write_summary_reports_a_short_write", 0);
    }

    /* one-shot startup record */
    if (!pipe(fd)) {
        struct color_startup startup = {0};
        char received[1024];
        color_startup_fail(&startup, "allocation.stream", COLOR_STARTUP_RESOURCE, 0, 0);
        ssize_t first = color_startup_failure(&startup, SESSION, fd[1], D200_VS_RESOURCE_LIMIT);
        ssize_t second = color_startup_failure(&startup, SESSION, fd[1], D200_VS_RESOURCE_LIMIT);
        ssize_t got = read(fd[0], received, sizeof(received) - 1);
        if (got < 0) got = 0;
        received[got] = '\0';
        check("startup_failure_emits_exactly_one_record",
              first == 0 && second == 0 && got > 0 && startup.emitted == 1);
        if (got > 0) emit_json("startup-failure", received);
        close(fd[0]);
        close(fd[1]);
    } else {
        check("startup_failure_emits_exactly_one_record", 0);
    }

    /* a fresh startup with no stage emits nothing */
    {
        struct color_startup startup = {0};
        check("startup_failure_is_silent_without_a_stage",
              color_startup_failure(&startup, SESSION, STDERR_FILENO, 0) == 0 &&
              startup.emitted == 0);
    }
}

/* ------------------------------------------------------------------ E: failure notice */

static void test_failure_notice(void) {
    struct fixture f;

    /* a non-cancel reason is queued as an immediate ERROR record */
    fixture_start(&f, 30, 1);
    drain_output(f.s, f.fd[0]);
    {
        d200_vs_header h;
        int rc = color_failure_notice(f.s, f.fd[0], D200_VS_RESOURCE_LIMIT);
        h = output_header(f.s);
        check("failure_notice_queues_an_error_record",
              rc == 1 && h.kind == D200_VS_ERROR && f.s->out_size == 0);
    }
    fixture_stop(&f);

    /* a cancel-class reason is not queued here; the stream is shut down instead */
    fixture_start(&f, 30, 1);
    drain_output(f.s, f.fd[0]);
    check("failure_notice_shuts_down_a_cancelled_stream",
          color_failure_notice(f.s, f.fd[0], D200_VS_RESULT_CANCELLED) == -1 &&
          f.s->out_size == 0);
    fixture_stop(&f);

    /* a pending record is never replaced */
    fixture_start(&f, 30, 1);
    (void)color_ready(f.s, SESSION, 30, 1);
    check("failure_notice_does_not_replace_a_pending_record",
          f.s->out_size != 0 && color_failure_notice(f.s, f.fd[0], D200_VS_TIMEOUT) == -1 &&
          f.s->out_size != 0);
    fixture_stop(&f);
}

/* ------------------------------------------------------------------ F: vendor seams */

static unsigned get_buf_calls;
static int get_buf_fails;
static int get_buf_bad_format;
static int get_buf_null_destination;
static unsigned put_buf_discards;
static uint8_t y_scratch[64], uv_scratch[64];

static void poke32(void *base, size_t offset, uint32_t value) {
    put32((uint8_t *)base + offset, value);
}

static mi_s32 probing_get_buf(channel_port *port, void *config, void *info,
                              mi_sys_buf_handle *held, mi_s32 timeout) {
    (void)port; (void)config; (void)timeout;
    ++get_buf_calls;
    if (get_buf_fails) return -1;
    memset(info, 0, 272);
    poke32(info, 0x10, get_buf_bad_format ? 9u : 1u);
    poke32(info, 0x34, 2);
    poke32(info, 0x38, WIDTH);
    poke32(info, 0x3a, HEIGHT);
    poke32(info, 0x3c, get_buf_null_destination ? 0u : 1u);
    poke32(info, 0x40, get_buf_null_destination ? 0u : 1u);
    poke32(info, 0x60, WIDTH);
    poke32(info, 0x64, WIDTH);
    poke32(info, 0x6c, (uint32_t)(WIDTH * HEIGHT + WIDTH * (HEIGHT / 2)));
    *held = 3;
    return 0;
}

static mi_s32 probing_put_buf(mi_sys_buf_handle handle, void *info, mi_bool discard) {
    (void)handle; (void)info;
    if (discard) ++put_buf_discards;
    return 0;
}

static void test_vendor_seams(void) {
    struct divp_context d;
    struct color_startup startup = {0};

    /* The MI device node exists only on the deck. Both host cases below are behavioural when the
     * dependency is absent, and say so explicitly when it is not, rather than assuming the host. */
    int node_absent = access("/dev/mi_sys", F_OK) != 0;
    memset(&d, 0, sizeof(d));
    {
        int rc = initialized_unbind(&d);
        check("unbind_ioctl_fails_closed_without_the_device_node", !node_absent || rc == -1);
        check("unbind_probe_ran_with_the_device_node_absent", node_absent);
    }

    /* the vendor libraries are ELF so they cannot load on a macOS host */
    int loader_absent = access("/lib/libmi_sys.so", F_OK) != 0;
    memset(&d, 0, sizeof(d));
    {
        int rc = divp_open(&d, 30, 1, 0, &startup);
        check("divp_open_names_the_failing_loader_stage",
              !loader_absent || (rc == -1 && startup.stage != NULL &&
                                 !strcmp(startup.stage, "loader.sys") &&
                                 startup.domain == COLOR_STARTUP_LOADER));
        check("divp_open_probe_ran_without_the_vendor_library", loader_absent);
    }

    /* a missing vendor buffer is reported, and retried a bounded number of times */
    memset(&d, 0, sizeof(d));
    get_buf_calls = 0;
    get_buf_fails = 1;
    put_buf_discards = 0;
    d.get_buf = probing_get_buf;
    d.put_buf = probing_put_buf;
    check("present_planes_reports_a_missing_buffer",
          present_planes(&d, y_scratch, uv_scratch, uv_scratch) == -2 &&
          get_buf_calls == 4 && d.held_acquired == 0);
    get_buf_fails = 0;

    /* a buffer with no destination address is handed back, never written */
    memset(&d, 0, sizeof(d));
    get_buf_calls = 0;
    get_buf_fails = 0;
    get_buf_bad_format = 0;
    get_buf_null_destination = 1;
    put_buf_discards = 0;
    d.get_buf = probing_get_buf;
    d.put_buf = probing_put_buf;
    check("present_planes_rejects_a_buffer_without_a_destination",
          present_planes(&d, y_scratch, uv_scratch, uv_scratch) == -3 &&
          put_buf_discards == 1 && d.held_acquired == 0);

    /* a buffer whose format does not match is handed back, never written */
    memset(&d, 0, sizeof(d));
    get_buf_calls = 0;
    get_buf_bad_format = 1;
    get_buf_null_destination = 0;
    put_buf_discards = 0;
    d.get_buf = probing_get_buf;
    d.put_buf = probing_put_buf;
    check("present_planes_rejects_a_buffer_with_the_wrong_format",
          present_planes(&d, y_scratch, uv_scratch, uv_scratch) == -3 &&
          put_buf_discards == 1 && d.held_acquired == 0);
    get_buf_bad_format = 0;

    /* the BT.601 range tables are the documented limits, inclusive */
    {
        uint8_t y_table[256], c_table[256];
        initialize_range_tables(y_table, c_table);
        check("range_tables_follow_the_documented_limits",
              y_table[0] == 16 && y_table[255] == 235 &&
              c_table[0] == 16 && c_table[255] == 240);
        check("clamp_saturates_out_of_range_values",
              clamp8(-1) == 0 && clamp8(0) == 0 && clamp8(255) == 255 &&
              clamp8(256) == 255 && clamp8(1000000) == 255);
    }

    /* signal handling selects the documented reason */
    {
        volatile sig_atomic_t saved_running = running;
        volatile sig_atomic_t saved_reason = signal_reason;
        stop_signal(SIGUSR1);
        check("signal_selects_the_source_failure_reason",
              running == 0 && signal_reason == (sig_atomic_t)D200_VS_SOURCE_FAILURE);
        stop_signal(SIGTERM);
        check("signal_selects_the_cancelled_reason",
              signal_reason == (sig_atomic_t)D200_VS_RESULT_CANCELLED);
        running = saved_running;
        signal_reason = saved_reason;
    }
}

/* ------------------------------------------------------------------ G: teardown */

static char cleanup_trace[64];
static unsigned cleanup_trace_size;

static void cleanup_note(char tag) {
    if (cleanup_trace_size + 1 < sizeof(cleanup_trace)) cleanup_trace[cleanup_trace_size++] = tag;
    cleanup_trace[cleanup_trace_size] = '\0';
}

static mi_s32 fake_put_buf(mi_sys_buf_handle handle, void *info, mi_bool discard) {
    (void)handle; (void)info;
    cleanup_note(discard ? 'D' : 'P');
    return 0;
}
static mi_s32 fake_unbind(struct divp_context *d) { (void)d; cleanup_note('U'); return 0; }
static mi_s32 fake_stop(mi_u32 channel) { (void)channel; cleanup_note('S'); return 0; }
static mi_s32 fake_destroy(mi_u32 channel) { (void)channel; cleanup_note('X'); return 0; }
static mi_s32 fake_destroy_failing(mi_u32 channel) { (void)channel; cleanup_note('X'); return -1; }
static mi_s32 fake_deinit(void) { cleanup_note('I'); return 0; }
static mi_s32 fake_disp_deinit(void) { cleanup_note('V'); return 0; }
static mi_s32 fake_sys_exit(void) { cleanup_note('Y'); return 0; }

static void arm_full_context(struct divp_context *d) {
    memset(d, 0, sizeof(*d));
    d->put_buf = fake_put_buf;
    d->unbind = fake_unbind;
    d->stop = fake_stop;
    d->destroy = fake_destroy;
    d->deinit = fake_deinit;
    d->disp_deinit = fake_disp_deinit;
    d->sys_exit = fake_sys_exit;
    d->held = 7;
    d->held_acquired = 1;
    d->bound = d->started = d->created = 1;
    d->disp_initialized = d->sys_initialized = 1;
    memset(cleanup_trace, 0, sizeof(cleanup_trace));
    cleanup_trace_size = 0;
}

static void test_cleanup(void) {
    struct divp_context d;

    arm_full_context(&d);
    check("cleanup_releases_every_stage_in_order",
          divp_cleanup(&d) == 0 && !strcmp(cleanup_trace, "DUSXIVY"));
    check("cleanup_clears_every_stage_flag",
          d.held_acquired == 0 && d.bound == 0 && d.started == 0 && d.created == 0 &&
          d.disp_initialized == 0 && d.sys_initialized == 0 && d.cleanup_failed == 0);

    arm_full_context(&d);
    d.unbind = NULL;
    d.destroy = fake_destroy_failing;
    {
        int rc = divp_cleanup(&d);
        check("cleanup_reports_a_failing_stage",
              rc == -1 && d.cleanup_failed == 1 && !strcmp(cleanup_trace, "DSXIVY"));
    }

    memset(&d, 0, sizeof(d));
    memset(cleanup_trace, 0, sizeof(cleanup_trace));
    cleanup_trace_size = 0;
    d.cleanup_failed = 1;
    check("cleanup_failure_is_sticky", divp_cleanup(&d) == -1 && d.cleanup_failed == 1 &&
                                        cleanup_trace[0] == '\0');
}

/* ------------------------------------------------------------------ predicates */

static void test_predicates(void) {
    struct fixture f;

    fixture_start(&f, 30, 1);
    drain_output(f.s, f.fd[0]);
    f.s->pacing = 0;
    check("poll_timeout_is_short_while_idle", color_poll_timeout(f.s) == 10);
    f.s->pacing = 1;
    f.s->deadline = 0;
    check("poll_timeout_is_immediate_when_overdue", color_poll_timeout(f.s) == 0);
    f.s->deadline = monotonic_ns() + UINT64_C(5000000);
    {
        int timeout = color_poll_timeout(f.s);
        check("poll_timeout_tracks_the_remaining_slice", timeout > 0 && timeout <= 10);
    }
    f.s->period = UINT64_C(1000000000) / 30;
    f.s->progress = monotonic_ns();
    check("timeout_is_quiet_while_progress_is_recent", color_timeout(f.s) == 0);
    {
        uint64_t now = monotonic_ns();
        f.s->progress = now > UINT64_C(31000000000) ? now - UINT64_C(31000000000) : 0;
    }
    check("timeout_fires_after_the_progress_budget", color_timeout(f.s) != 0);
    fixture_stop(&f);
}

int main(void) {
    printf("ENUM D200_VS_PROTOCOL %d\n", (int)D200_VS_PROTOCOL);
    printf("ENUM D200_VS_PRESENTATION %d\n", (int)D200_VS_PRESENTATION);
    printf("ENUM D200_VS_DISCONNECTED %d\n", (int)D200_VS_DISCONNECTED);
    printf("ENUM D200_VS_RESULT_CANCELLED %d\n", (int)D200_VS_RESULT_CANCELLED);
    printf("ENUM D200_VS_EMPTY_SOURCE %d\n", (int)D200_VS_EMPTY_SOURCE);
    printf("ENUM D200_VS_CONSUMED %d\n", (int)D200_VS_CONSUMED);
    printf("ENUM D200_VS_CANCELLED %d\n", (int)D200_VS_CANCELLED);

    test_receive();
    test_pacing();
    test_terminal();
    test_diagnostics();
    test_failure_notice();
    test_vendor_seams();
    test_cleanup();
    test_predicates();

    printf("TOTAL %u\n", cases);
    printf("FAILED %u\n", failures);
    return failures ? 1 : 0;
}
'''


# The case names the driver declares with a literal name. Parsed rather than assumed.
DRIVER_CASE_SITES = frozenset(re.findall(r'check\(\s*"([^"]+)"\s*,', DRIVER))


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
    # Never unregistered: a module-scoped teardown runs before pytest's terminal summary, so
    # unregistering here would silently drop the report.
    request.config.pluginmanager.register(_coverage, "devicecolor-agent-coverage")
    yield


# --------------------------------------------------------------------------- assembly


def _extract_core() -> str:
    """Slice the production core out of the agent, and refuse to compile anything else."""
    source = AGENT.read_text(encoding="utf-8")
    for name, (begin, end) in SEAM_MARKERS.items():
        assert begin in source and end in source, (
            f"the {name} extraction seam is no longer declared in {AGENT}; if the markers were "
            f"renamed, this harness is silently slicing the wrong text"
        )
    start = source.find(CORE_START)
    stop = source.find(CORE_END)
    assert 0 <= start < stop, f"cannot locate the production core inside {AGENT}"
    core = source[start:stop]
    assert "/* STREAM_CORE_BEGIN" in core, "the slice stops short of the STREAM_CORE_BEGIN marker"
    for token in FORBIDDEN_IN_CORE:
        assert token not in core, (
            f"the extracted core now contains {token!r}, so it is no longer the device-free unit "
            f"this harness can compile"
        )
    return core


def _translation_unit() -> str:
    return PREAMBLE + _extract_core() + DRIVER


def _core_functions(core: str) -> frozenset[str]:
    return frozenset(re.findall(r"^static[^\n;{]*?\b([A-Za-z_]\w*)\s*\(", core, re.M))


# --------------------------------------------------------------------------- process helpers


def _host_cc() -> str | None:
    return shutil.which("cc") or shutil.which("clang")


def _child_env(home: Path) -> dict[str, str]:
    """Every child gets a HOME inside the pytest temp dir (BRIEF rule 7)."""
    home.mkdir(parents=True, exist_ok=True)
    assert home.resolve() != REAL_HOME, f"refusing to hand the real HOME to a child: {home}"
    env = dict(os.environ)
    env["HOME"] = str(home)
    return env


def _compile(cc, source: Path, binary: Path, extra, env) -> subprocess.CompletedProcess:
    return subprocess.run(
        [cc, *CC_FLAGS, *extra, "-pthread", f"-I{ROOT / 'device'}", "-o", str(binary), str(source)],
        capture_output=True,
        text=True,
        env=env,
    )


def _run(tmp_path: Path, label: str, extra, home: Path):
    cc = _host_cc()
    assert cc is not None
    source = tmp_path / f"agent_{label}.c"
    binary = tmp_path / f"agent_{label}"
    source.write_text(_translation_unit(), encoding="utf-8")
    env = _child_env(home)
    compiled = _compile(cc, source, binary, extra, env)
    assert compiled.returncode == 0, f"[{label}] compiler output:\n{compiled.stdout}{compiled.stderr}"
    ran = subprocess.run([str(binary)], capture_output=True, text=True, env=env)
    statuses = {
        match.group(1): match.group(2)
        for match in re.finditer(r"^CASE (\S+) (PASS|FAIL)$", ran.stdout, re.M)
    }
    return statuses, ran.stdout + ran.stderr


def _records(output: str) -> dict[str, dict]:
    found = {}
    for line in output.splitlines():
        if line.startswith("JSON "):
            _, label, payload = line.split(" ", 2)
            found[label] = json.loads(payload)
    return found


# --------------------------------------------------------------------------- the tests


def test_agent_core_compiles_and_runs_device_free(tmp_path):
    """Compile and run the agent's production core on this host, with no device involved."""
    if _host_cc() is None:
        pytest.skip("no host C compiler (cc/clang) available")

    core = _extract_core()
    for name in sorted(_core_functions(core)):
        if name in REACHED_TRANSITIVELY:
            continue
        assert f"{name}(" in DRIVER, (
            f"the core defines {name} but the driver never calls it; the production core has grown "
            f"an untested function"
        )
    stale = sorted(REACHED_TRANSITIVELY - _core_functions(core))
    assert not stale, f"the transitively-reached allowlist is stale: {stale}"

    statuses, output = _run(tmp_path, "plain", (), tmp_path / "home-plain")
    _coverage.lines = [f"devicecolor-agent[{line}]" for line in output.splitlines()
                       if line.startswith(("CASE ", "TOTAL", "FAILED"))]

    assert statuses, f"the driver printed no CASE lines:\n{output}"
    failed = sorted(name for name, status in statuses.items() if status != "PASS")
    assert not failed, f"driver cases failed: {failed}\n{output}"
    missing = sorted(CONTRACT_CASES - statuses.keys())
    assert not missing, f"contract cases the driver never reported: {missing}\n{output}"
    assert statuses.keys() == DRIVER_CASE_SITES, (
        f"the driver ran a different case set than it declares: "
        f"{sorted(statuses.keys() ^ DRIVER_CASE_SITES)}\n{output}"
    )
    totals = dict(re.findall(r"^(TOTAL|FAILED) (\d+)$", output, re.M))
    assert totals.get("FAILED") == "0", output
    assert int(totals.get("TOTAL", "0")) == len(statuses), output

    # The wire constants the driver echoes must be the header's own.
    header = WIRE_HEADER.read_text(encoding="utf-8")
    declared = {name: int(value) for name, value in
                re.findall(r"^#define D200_VS_(\w+) (\d+)u$", header, re.M)}
    echoed = {name: int(value) for name, value in
              re.findall(r"^ENUM D200_VS_(\S+) (-?\d+)$", output, re.M)}
    assert echoed, f"the driver never echoed the wire constants:\n{output}"
    for name, value in echoed.items():
        assert declared.get(name) == value, (
            f"driver reports D200_VS_{name}={value} but the header defines "
            f"{declared.get(name)}"
        )

    # The diagnostic record is the contract the host side reads; check it key for key.
    records = _records(output)
    assert set(records) == {
        "terminal", "nulls", "saturated", "startup-ready", "startup-loader", "startup-vendor",
        "startup-failure",
    }, sorted(records)

    summary = records["terminal"]
    assert set(summary) == SUMMARY_KEYS, sorted(set(summary) ^ SUMMARY_KEYS)
    assert set(summary["startup"]) == STARTUP_KEYS, sorted(set(summary["startup"]) ^ STARTUP_KEYS)
    assert summary["event"] == "native-video-terminal"
    assert summary["session"] == "00112233445566778899aabbccddeeff"
    assert summary["epoch"] == 1
    assert summary["clock"] == "device-monotonic"
    assert summary["pixelProof"] is False
    assert summary["framesReceived"] == 1
    assert summary["framesConsumed"] == 1
    assert summary["successfulSubmissions"] == 1
    assert summary["jpegBytesReceived"] == 4
    assert summary["queueHighwaterFrames"] == 1
    assert summary["lateSubmissions"] <= 1
    assert summary["presentationAttempts"] == 1
    assert summary["countersSaturated"] is False
    assert summary["terminalCode"] == 0
    assert summary["readyNs"] is not None and summary["firstSubmissionNs"] is not None
    assert summary["presentationTotalNs"] is not None
    assert summary["presentationMaxNs"] is not None
    assert summary["cleanupDurationNs"] is not None
    assert summary["startup"]["outcome"] == "ready"

    unobserved = records["nulls"]
    for key in ("readyNs", "firstSubmissionNs", "presentationTotalNs", "presentationMaxNs",
                "cleanupDurationNs"):
        assert unobserved[key] is None, f"{key} should be null when the milestone was never reached"
    assert unobserved["terminalCode"] == 6

    saturated = records["saturated"]
    assert saturated["countersSaturated"] is True
    assert saturated["successfulSubmissions"] == 2 ** 64 - 1

    ready = records["startup-ready"]
    assert ready["outcome"] == "ready" and ready["stage"] is None and ready["domain"] is None

    loader = records["startup-loader"]
    assert loader["outcome"] == "failed"
    assert loader["stage"] == "loader.sys" and loader["domain"] == "loader"
    assert loader["vendorReturn"] is None and loader["vendorReturnU32"] is None
    assert loader["errno"] is None and loader["observedNs"] is not None

    vendor = records["startup-vendor"]
    assert vendor["outcome"] == "failed" and vendor["domain"] == "vendor"
    assert vendor["stage"] == "vendor.MI_SYS_Init"
    assert vendor["vendorReturn"] == -3 and vendor["vendorReturnU32"] == 4294967293

    failure = records["startup-failure"]
    assert failure["event"] == "native-startup-failure"
    assert failure["session"] == "00112233445566778899aabbccddeeff"
    assert failure["terminalCode"] == 15 and failure["pixelProof"] is False
    assert failure["startup"]["stage"] == "allocation.stream"
    assert failure["startup"]["domain"] == "resource"

    _coverage.lines.append(
        f"devicecolor-agent[coverage] {len(statuses)} cases, {len(_core_functions(core))} core "
        f"functions, {len(SUMMARY_KEYS)} diagnostic keys"
    )


def test_agent_core_is_clean_under_asan_and_ubsan(tmp_path):
    """The same driver under `-fsanitize=address,undefined`, trusted only after liveness controls."""
    cc = _host_cc()
    if cc is None:
        pytest.skip("no host C compiler (cc/clang) available")

    env = _child_env(tmp_path / "home-sanitized")
    probe = tmp_path / "probe.c"
    probe.write_text("int main(void) { return 0; }\n", encoding="utf-8")
    if _compile(cc, probe, tmp_path / "probe", SANITIZE_FLAGS, env).returncode != 0:
        _coverage.lines.append("devicecolor-agent[sanitizer] UNAVAILABLE on this host")
        pytest.skip("no address/undefined sanitizer on this host")

    silent = []
    for name, marker, text in SANITIZER_CONTROLS:
        source = tmp_path / f"control_{name}.c"
        source.write_text(text, encoding="utf-8")
        compiled = _compile(cc, source, tmp_path / f"control_{name}", SANITIZE_FLAGS, env)
        if compiled.returncode != 0:
            silent.append(f"{name} (control did not compile)")
            continue
        ran = subprocess.run([str(tmp_path / f"control_{name}")], capture_output=True, text=True,
                             env=env)
        if marker not in ran.stdout + ran.stderr:
            silent.append(f"{name} (control ran but was not reported)")
    if silent:
        _coverage.lines.append("devicecolor-agent[sanitizer] INERT: " + "; ".join(silent))
        pytest.skip("the sanitizer links but does not report its own control bug: "
                    + "; ".join(silent))

    statuses, output = _run(tmp_path, "sanitized", SANITIZE_FLAGS, tmp_path / "home-san")
    for marker in ("AddressSanitizer", "runtime error:", "LeakSanitizer"):
        assert marker not in output, f"sanitizer report ({marker}):\n{output}"
    failed = sorted(name for name, status in statuses.items() if status != "PASS")
    assert not failed, f"driver cases failed under sanitizers: {failed}\n{output}"
    assert statuses.keys() >= CONTRACT_CASES, output
    _coverage.lines.append(
        f"devicecolor-agent[sanitizer] live (both controls detected), clean, {len(statuses)} cases"
    )


def test_agent_harness_cannot_reach_the_real_home(tmp_path):
    """Containment (BRIEF rule 7): this harness cannot touch the real `~/.ghostdeck` state."""
    for token in ("getenv(", "system(", "popen(", "fork(", "execv", "unlink(", "remove("):
        assert token not in DRIVER, (
            f"the driver must stay pure computation; it now contains {token!r}"
        )
    isolated = Path(_child_env(tmp_path / "probe-home")["HOME"])
    assert isolated != REAL_HOME and isolated.is_dir()
    with pytest.raises(AssertionError):
        _child_env(REAL_HOME)

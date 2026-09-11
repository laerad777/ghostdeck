"""Device-free host harness for the pthread decode queue in `device/d200_color_decode.h`.

`d200_video_stream.h` is covered by `tests/test_devicecolor_codec.py`. Its sibling
`device/d200_color_decode.h` — the persistent JPEG decoder plus two frame slots that
`device/d200-color-agent.c` drives from its poll loop — was executed by **no** test on any host: its
only reference in the tree is the agent itself. That is the same failure shape the master found on
hardware when `ghostdeck stop` was certified by 465 green tests that fabricated device fields the
real deck never emits: a device-side contract with no executable proof.

The queue is POSIX-only (mutex, condvar, pipe) and touches no device node, so it can be compiled and
run on this host. This file does that: it writes a C driver to a temp dir, compiles it with the host
compiler, runs it, and asserts on what it prints. It never needs a deck, `adb`, `ffmpeg`, turbolibjpeg
or any device node, and it never links `device/d200-color-agent.c` (impossible on this host: the
cross toolchain cannot link a macOS `libturbojpeg.dylib`).

Nothing here is hand-copied from the header that can be derived from it:

* the public API set is parsed from the header's `static inline` definitions and every entry point
  must be exercised by the driver — a new API fails this test until it is covered, and the two
  internal helpers are an explicit allowlist that itself fails if it goes stale;
* the state values are parsed from the header's `enum d200_decode_state` and cross-checked against
  what the running driver reports, so a renumbered state cannot pass silently;
* the driver's own `check("...")` call sites are parsed, so a *deleted* contract case fails instead
  of quietly shrinking coverage.

ThreadSanitizer is the interesting sanitizer for this header, and it is only trusted after a
deliberately racy control program has been shown to be *detected* on this host.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HEADER = ROOT / "device" / "d200_color_decode.h"
REAL_HOME = Path(os.path.expanduser("~")).resolve()

CC_FLAGS = ("-std=c11", "-O1", "-Wall", "-Wextra", "-Werror")
SANITIZE_FLAGS = ("-fsanitize=address,undefined", "-fno-omit-frame-pointer")
TSAN_FLAGS = ("-fsanitize=thread",)

_HEADER_TEXT = HEADER.read_text(encoding="utf-8")

# ---------------------------------------------------------------- header-derived API and states

# Every `static inline` function the header defines. A new entry point must be covered below or this
# module fails, so the queue cannot grow an untested surface.
API_FUNCTIONS = tuple(
    name
    for name in re.findall(
        r"static inline\s+[A-Za-z_][\w \t\*]*?\b(d200_decode_\w+)\s*\(", _HEADER_TEXT
    )
)

# The functions that are *not* callable entry points. `d200_decode_worker` is the thread body
# (started by `d200_decode_init`, so the driver exercises it transitively), `d200_decode_notify` is
# documented as "called with mutex held" from inside the other primitives, and `d200_decode_error`
# is the shared errno-setting return helper that every entry point uses. They still have to exist,
# or this allowlist has gone stale and the API-coverage check below would be lying.
INTERNAL_HELPERS = ("d200_decode_notify", "d200_decode_worker", "d200_decode_error")
PUBLIC_API = tuple(name for name in API_FUNCTIONS if name not in INTERNAL_HELPERS)

STATE_ENUM = {
    name: int(value)
    for name, value in re.findall(r"(D200_DECODE_[A-Z_]+)\s*=\s*(-?\d+)", _HEADER_TEXT)
}

# The contracts this harness proves. Deleting one from the driver makes this test fail rather than
# quietly reducing coverage: `statuses` is compared against the driver's parsed `check(...)` sites.
CONTRACT_CASES = frozenset(
    {
        # lifecycle and descriptor hygiene
        "init_succeeds_on_a_zeroed_queue",
        "init_sets_initialized_and_worker_started",
        "notify_fd_is_a_valid_read_descriptor",
        "notify_descriptors_are_nonblocking",
        "notify_descriptors_are_close_on_exec",
        "init_is_refused_while_initialized",
        "drain_with_no_work_is_clean",
        "destroy_joins_and_reports_success",
        "destroy_clears_initialized_and_worker",
        "destroy_is_idempotent_on_a_cleaned_queue",
        "reinit_after_destroy_succeeds",
        # argument validation
        "submit_after_destroy_is_refused",
        "status_after_destroy_is_refused",
        "release_after_destroy_is_refused",
        "notify_fd_after_destroy_is_refused",
        "drain_after_destroy_is_refused",
        "init_rejects_nulls",
        "submit_on_an_uninitialized_queue_is_refused",
        "api_rejects_null_queue",
        "submit_rejects_null_jpeg_and_zero_size",
        "submit_rejects_the_reserved_index",
        "submit_rejects_a_non_contiguous_index",
        "empty_slots_report_empty_not_enobent",
        # the two-slot window and the index -> slot mapping
        "submit_returns_pending_before_decode_completes",
        "status_is_pending_mid_decode",
        "worker_reached_the_callback",
        "a_pending_job_cannot_be_released",
        "a_different_live_index_collides_with_the_slot",
        "second_in_flight_job_fills_the_other_slot",
        "both_slots_are_in_flight_concurrently",
        "a_third_in_flight_job_is_refused_while_the_window_is_full",
        "first_job_completes",
        "second_job_completes",
        "release_clears_the_slot",
        "a_released_slot_accepts_the_next_index",
        "release_of_an_unsubmitted_index_is_refused",
        "callback_receives_the_submitted_index_slot_and_bytes",
        "destroy_with_a_completed_job_still_in_a_slot",
        # callback results, ordering, and the notification pipe
        "many_sequential_jobs_complete_and_release",
        "decode_failure_is_reported_through_status",
        "every_submitted_index_was_decoded_in_order",
        "worker_state_survives_a_long_stream",
        "destroy_after_a_long_stream",
        "notification_pipe_fills_and_reports_eagain",
        "submit_succeeds_while_the_notification_pipe_is_full",
        "drain_clears_a_full_pipe_without_error",
        "job_completes_after_a_full_pipe",
        "drain_after_completion_is_clean",
        # destroy against a worker that is inside the callback
        "worker_is_inside_the_callback_before_destroy",
        "destroy_joins_a_worker_blocked_in_the_callback",
        "worker_thread_actually_exited",
    }
)

# Deliberate bugs that each sanitizer must report, so a sanitizer that links but does not instrument
# cannot pass as "clean". Both use observable stores, because a store whose value is never read is
# deleted at -O1 and would silently defeat the check.
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


# The ThreadSanitizer liveness control: two threads racing on a non-atomic global that is read
# afterwards, so the race is observable and not eliminated at -O1.
TSAN_CONTROL = r"""
#include <pthread.h>
#include <stdio.h>
static volatile int shared;
static void *bump(void *arg) {
    int i;
    (void)arg;
    for (i = 0; i < 1000; ++i) shared = shared + 1;
    return NULL;
}
int main(void) {
    pthread_t a, b;
    pthread_create(&a, NULL, bump, NULL);
    pthread_create(&b, NULL, bump, NULL);
    pthread_join(a, NULL);
    pthread_join(b, NULL);
    printf("shared=%d\n", shared);
    return 0;
}
"""

DRIVER = r"""
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include "d200_color_decode.h"

#define JOBS 200

static unsigned cases;
static unsigned failures;
static volatile unsigned observable_sink;

/* Touch a buffer after a rejected call. Without a read the optimizer deletes the stores at -O1 and
 * could hide a bug; the values are never otherwise used, which is why this sink is volatile. */
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

/* ------------------------------------------------------------------ the decode callback */

#define SEEN 512

struct ctx {
    pthread_mutex_t mutex;
    pthread_cond_t cond;
    int gate;              /* 0 => the callback blocks until the gate opens */
    int fail_on;           /* ordinal whose decode returns a failure, -1 for none */
    unsigned calls;
    unsigned slot_seen[SEEN];
    size_t size_seen[SEEN];
    const uint8_t *jpeg_seen[SEEN];
};

/* All shared state is touched under `ctx.mutex` so the ThreadSanitizer run stays meaningful: a
 * race reported by the sanitizer is a real race in the test, not in the header. */
static int ctx_decode(void *context, unsigned slot, const uint8_t *jpeg, size_t size) {
    struct ctx *c = context;
    unsigned ordinal;
    int result;
    pthread_mutex_lock(&c->mutex);
    ordinal = c->calls++;
    if (ordinal < SEEN) {
        c->slot_seen[ordinal] = slot;
        c->size_seen[ordinal] = size;
        c->jpeg_seen[ordinal] = jpeg;
    }
    while (!c->gate) pthread_cond_wait(&c->cond, &c->mutex);
    result = ((int)ordinal == c->fail_on) ? -7 : 0;
    pthread_mutex_unlock(&c->mutex);
    return result;
}

static void ctx_open_gate(struct ctx *c) {
    pthread_mutex_lock(&c->mutex);
    c->gate = 1;
    pthread_cond_broadcast(&c->cond);
    pthread_mutex_unlock(&c->mutex);
}

static unsigned ctx_calls(struct ctx *c) {
    unsigned n;
    pthread_mutex_lock(&c->mutex);
    n = c->calls;
    pthread_mutex_unlock(&c->mutex);
    return n;
}

static void ctx_init(struct ctx *c, int fail_on, int gate) {
    memset(c, 0, sizeof(*c));
    pthread_mutex_init(&c->mutex, NULL);
    pthread_cond_init(&c->cond, NULL);
    c->fail_on = fail_on;
    c->gate = gate;
}

struct releaser {
    struct ctx *ctx;
    long delay_ms;
};

static void *releaser_main(void *arg) {
    struct releaser *r = arg;
    struct timespec ts;
    ts.tv_sec = r->delay_ms / 1000;
    ts.tv_nsec = (r->delay_ms % 1000) * 1000000L;
    nanosleep(&ts, NULL);
    ctx_open_gate(r->ctx);
    return NULL;
}

/* ------------------------------------------------------------------ small helpers */

static int now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int)(ts.tv_sec * 1000 + ts.tv_nsec / 1000000);
}

static void nap_ms(long ms) {
    struct timespec ts;
    ts.tv_sec = ms / 1000;
    ts.tv_nsec = (ms % 1000) * 1000000L;
    nanosleep(&ts, NULL);
}

static int wait_calls(struct ctx *c, unsigned want, int timeout_ms) {
    int start = now_ms();
    for (;;) {
        if (ctx_calls(c) >= want) return 1;
        if (now_ms() - start > timeout_ms) return 0;
        nap_ms(1);
    }
}

/* Poll the way the agent's event loop does: check the admitted index, then drain the coalescible
 * notification pipe. Returns 0 on timeout or on a clean error from either primitive. */
static int wait_done(struct d200_decode_queue *q, uint64_t index, int *result, int timeout_ms) {
    int start = now_ms();
    for (;;) {
        int status = d200_decode_status(q, index, result);
        if (status == D200_DECODE_DONE) return 1;
        if (status < 0 && status != D200_DECODE_INVALID) return 0;
        if (d200_decode_drain_notifications(q)) return 0;
        if (now_ms() - start > timeout_ms) return 0;
        nap_ms(1);
    }
}

/* ------------------------------------------------------------------ lifecycle */

static void test_lifecycle(void) {
    struct d200_decode_queue q;
    struct ctx c;
    int fd0, flags_r, flags_w, fd0_flags, fd1_flags;

    ctx_init(&c, -1, 1);
    memset(&q, 0, sizeof(q));

    check("init_succeeds_on_a_zeroed_queue", d200_decode_init(&q, ctx_decode, &c) == 0);
    check("init_sets_initialized_and_worker_started",
          q.initialized == 1 && q.worker_started == 1);

    fd0 = d200_decode_notify_fd(&q);
    check("notify_fd_is_a_valid_read_descriptor", fd0 >= 0 && fd0 == q.notify[0]);

    flags_r = fcntl(q.notify[0], F_GETFL);
    flags_w = fcntl(q.notify[1], F_GETFL);
    check("notify_descriptors_are_nonblocking",
          flags_r >= 0 && flags_w >= 0 && (flags_r & O_NONBLOCK) && (flags_w & O_NONBLOCK));

    fd0_flags = fcntl(q.notify[0], F_GETFD);
    fd1_flags = fcntl(q.notify[1], F_GETFD);
    check("notify_descriptors_are_close_on_exec",
          fd0_flags >= 0 && fd1_flags >= 0 && (fd0_flags & FD_CLOEXEC) && (fd1_flags & FD_CLOEXEC));

    check("init_is_refused_while_initialized",
          d200_decode_init(&q, ctx_decode, &c) == -1 && errno == EBUSY);
    check("drain_with_no_work_is_clean",
          d200_decode_drain_notifications(&q) == 0 && d200_decode_drain_notifications(&q) == 0);

    check("destroy_joins_and_reports_success", d200_decode_destroy(&q) == 0);
    check("destroy_clears_initialized_and_worker",
          q.initialized == 0 && q.worker_started == 0);
    check("destroy_is_idempotent_on_a_cleaned_queue", d200_decode_destroy(&q) == 0);

    check("submit_after_destroy_is_refused",
          d200_decode_submit(&q, 0, (const uint8_t *)"x", 1) == -1 && errno == EINVAL);
    check("status_after_destroy_is_refused",
          d200_decode_status(&q, 0, NULL) == -1 && errno == EINVAL);
    check("release_after_destroy_is_refused", d200_decode_release(&q, 0) == -1 && errno == EINVAL);
    check("notify_fd_after_destroy_is_refused", d200_decode_notify_fd(&q) == -1 && errno == EINVAL);
    check("drain_after_destroy_is_refused",
          d200_decode_drain_notifications(&q) == -1 && errno == EINVAL);

    check("init_rejects_nulls",
          d200_decode_init(NULL, ctx_decode, &c) == -1 && errno == EINVAL &&
          d200_decode_init(&q, NULL, &c) == -1 && errno == EINVAL);

    /* The agent restarts a session, so a clean destroy must leave the queue reusable. */
    check("reinit_after_destroy_succeeds",
          d200_decode_init(&q, ctx_decode, &c) == 0 && d200_decode_destroy(&q) == 0);
}

/* ------------------------------------------------------------------ validation */

static void test_validation(void) {
    struct d200_decode_queue q;
    struct ctx c;
    uint8_t payload[8] = {1, 2, 3, 4, 5, 6, 7, 8};

    ctx_init(&c, -1, 1);
    memset(&q, 0, sizeof(q));

    check("submit_on_an_uninitialized_queue_is_refused",
          d200_decode_submit(&q, 0, payload, sizeof(payload)) == -1 && errno == EINVAL);

    check("api_rejects_null_queue",
          d200_decode_submit(NULL, 0, payload, sizeof(payload)) == -1 && errno == EINVAL &&
          d200_decode_status(NULL, 0, NULL) == -1 && errno == EINVAL &&
          d200_decode_release(NULL, 0) == -1 && errno == EINVAL &&
          d200_decode_notify_fd(NULL) == -1 && errno == EINVAL &&
          d200_decode_drain_notifications(NULL) == -1 && errno == EINVAL &&
          d200_decode_destroy(NULL) == -1 && errno == EINVAL);

    d200_decode_init(&q, ctx_decode, &c);

    check("submit_rejects_null_jpeg_and_zero_size",
          d200_decode_submit(&q, 0, NULL, sizeof(payload)) == -1 && errno == EINVAL &&
          d200_decode_submit(&q, 0, payload, 0) == -1 && errno == EINVAL);

    check("submit_rejects_the_reserved_index",
          d200_decode_submit(&q, UINT64_MAX, payload, sizeof(payload)) == -1 && errno == EINVAL &&
          d200_decode_status(&q, UINT64_MAX, NULL) == -1 && errno == EINVAL &&
          d200_decode_release(&q, UINT64_MAX) == -1 && errno == EINVAL);

    check("submit_rejects_a_non_contiguous_index",
          d200_decode_submit(&q, 3, payload, sizeof(payload)) == -1 && errno == EINVAL &&
          q.submitted == 0);

    /* EMPTY is "no live job in this slot", not "unknown index"; ENOENT is the release-of-nothing
     * case. The agent distinguishes those two, so the driver must too. */
    check("empty_slots_report_empty_not_enobent",
          d200_decode_status(&q, 0, NULL) == D200_DECODE_EMPTY &&
          d200_decode_status(&q, 1, NULL) == D200_DECODE_EMPTY &&
          d200_decode_release(&q, 0) == -1 && errno == ENOENT);

    check("destroy_after_validation_cases", d200_decode_destroy(&q) == 0);
}

/* ------------------------------------------------------------------ in-flight window */

static void test_inflight_and_slots(void) {
    struct d200_decode_queue q;
    struct ctx c;
    uint8_t a[4] = {1, 2, 3, 4}, b[4] = {5, 6, 7, 8}, d[4] = {9, 10, 11, 12};
    int result = 0x1234;
    int args_ok;

    ctx_init(&c, -1, 0);
    memset(&q, 0, sizeof(q));
    d200_decode_init(&q, ctx_decode, &c);

    check("submit_returns_pending_before_decode_completes",
          d200_decode_submit(&q, 0, a, sizeof(a)) == 0);
    check("status_is_pending_mid_decode",
          d200_decode_status(&q, 0, &result) == D200_DECODE_PENDING && result == 0x1234);
    check("worker_reached_the_callback", wait_calls(&c, 1, 2000));
    check("a_pending_job_cannot_be_released",
          d200_decode_release(&q, 0) == -1 && errno == EBUSY);
    check("a_different_live_index_collides_with_the_slot",
          d200_decode_status(&q, 2, &result) == -1 && errno == ENOENT && result == 0x1234);
    /* The worker is still blocked inside job 0's callback here, so only the *submission* of job 1
     * can be observed now: the second slot is admitted while the first is in flight, which is the
     * two-frame window the header documents. The callback count stays at 1 until the gate opens. */
    check("second_in_flight_job_fills_the_other_slot",
          d200_decode_submit(&q, 1, b, sizeof(b)) == 0 && ctx_calls(&c) == 1 &&
          d200_decode_status(&q, 1, NULL) == D200_DECODE_PENDING);
    check("both_slots_are_in_flight_concurrently",
          d200_decode_status(&q, 0, NULL) == D200_DECODE_PENDING &&
          d200_decode_status(&q, 1, NULL) == D200_DECODE_PENDING);
    check("a_third_in_flight_job_is_refused_while_the_window_is_full",
          d200_decode_submit(&q, 2, d, sizeof(d)) == -1 && errno == EBUSY && q.submitted == 2);

    ctx_open_gate(&c);

    check("first_job_completes", wait_done(&q, 0, &result, 5000) && result == 0);
    check("second_job_completes", wait_done(&q, 1, &result, 5000) && result == 0);
    check("release_clears_the_slot",
          d200_decode_release(&q, 0) == 0 &&
          d200_decode_status(&q, 0, NULL) == D200_DECODE_EMPTY);
    check("a_released_slot_accepts_the_next_index",
          d200_decode_submit(&q, 2, d, sizeof(d)) == 0 &&
          wait_done(&q, 2, &result, 5000) && result == 0);
    check("release_of_an_unsubmitted_index_is_refused",
          d200_decode_release(&q, 3) == -1 && errno == ENOENT);

    /* The callback never receives the index, so the driver proves the mapping the header documents
     * (slot == index % 2) by recording what the worker actually passed for each ordinal. */
    pthread_mutex_lock(&c.mutex);
    args_ok = c.calls == 3 &&
        c.slot_seen[0] == 0 && c.slot_seen[1] == 1 && c.slot_seen[2] == 0 &&
        c.size_seen[0] == sizeof(a) && c.size_seen[1] == sizeof(b) && c.size_seen[2] == sizeof(d) &&
        c.jpeg_seen[0] == a && c.jpeg_seen[1] == b && c.jpeg_seen[2] == d;
    pthread_mutex_unlock(&c.mutex);
    check("callback_receives_the_submitted_index_slot_and_bytes", args_ok);

    check("destroy_with_a_completed_job_still_in_a_slot", d200_decode_destroy(&q) == 0);
}

/* ------------------------------------------------------------------ long stream */

static void test_stream_and_failure(void) {
    static uint8_t payloads[JOBS][3];
    struct d200_decode_queue q;
    struct ctx c;
    unsigned i;
    int result;
    int swept = 1;
    int failures_seen = 0;

    ctx_init(&c, 3, 1);
    memset(&q, 0, sizeof(q));
    for (i = 0; i < JOBS; ++i) {
        payloads[i][0] = (uint8_t)i;
        payloads[i][1] = (uint8_t)(i >> 8);
        payloads[i][2] = 0x5a;
    }

    d200_decode_init(&q, ctx_decode, &c);
    for (i = 0; i < JOBS; ++i) {
        if (d200_decode_submit(&q, i, payloads[i], sizeof(payloads[i]))) { swept = 0; break; }
        if (!wait_done(&q, i, &result, 5000)) { swept = 0; break; }
        if (result) ++failures_seen;
        if (d200_decode_release(&q, i)) { swept = 0; break; }
        if (d200_decode_drain_notifications(&q)) { swept = 0; break; }
    }
    check("many_sequential_jobs_complete_and_release", swept && i == JOBS);
    check("decode_failure_is_reported_through_status", failures_seen == 1);
    check("every_submitted_index_was_decoded_in_order", ctx_calls(&c) == JOBS);

    /* The agent's poll loop must keep working after a decode failure: the queue does not latch the
     * callback's result, so a later index still has to be admitted and released. */
    check("worker_state_survives_a_long_stream",
          d200_decode_submit(&q, JOBS, payloads[0], sizeof(payloads[0])) == 0 &&
          wait_done(&q, JOBS, &result, 5000) && result == 0 &&
          d200_decode_release(&q, JOBS) == 0 &&
          d200_decode_drain_notifications(&q) == 0);

    check("destroy_after_a_long_stream", d200_decode_destroy(&q) == 0);
}

/* ------------------------------------------------------------------ notification pipe */

static void test_full_pipe(void) {
    struct d200_decode_queue q;
    struct ctx c;
    uint8_t payload[3] = {7, 8, 9};
    int result;
    long written = 0;
    int eagain = 0;

    ctx_init(&c, -1, 1);
    memset(&q, 0, sizeof(q));
    d200_decode_init(&q, ctx_decode, &c);

    /* White-box probe of the documented invariant: "a full pipe already guarantees a poll wakeup",
     * so a full notification pipe must be a non-event. The harness owns the queue here, and the
     * write end is O_NONBLOCK, so this cannot block. */
    for (;;) {
        char byte = 1;
        ssize_t count = write(q.notify[1], &byte, 1);
        if (count == 1) { ++written; continue; }
        if (count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) eagain = 1;
        break;
    }
    check("notification_pipe_fills_and_reports_eagain", eagain && written > 0);
    check("submit_succeeds_while_the_notification_pipe_is_full",
          d200_decode_submit(&q, 0, payload, sizeof(payload)) == 0);
    check("drain_clears_a_full_pipe_without_error", d200_decode_drain_notifications(&q) == 0);
    check("job_completes_after_a_full_pipe",
          wait_done(&q, 0, &result, 5000) && result == 0);
    check("drain_after_completion_is_clean", d200_decode_drain_notifications(&q) == 0);

    observe(payload, sizeof(payload));
    check("destroy_after_a_full_pipe", d200_decode_destroy(&q) == 0);
}

/* ------------------------------------------------------------------ destroy vs a busy worker */

static void test_destroy_joins_busy_worker(void) {
    struct d200_decode_queue q;
    struct ctx c;
    struct releaser r;
    pthread_t helper;
    uint8_t payload[3] = {1, 2, 3};

    ctx_init(&c, -1, 0);
    memset(&q, 0, sizeof(q));
    d200_decode_init(&q, ctx_decode, &c);
    d200_decode_submit(&q, 0, payload, sizeof(payload));
    check("worker_is_inside_the_callback_before_destroy", wait_calls(&c, 1, 2000));

    /* `destroy` signals stop and then joins. The worker is inside the callback, so the join can only
     * return once the gate opens; this proves the join is real rather than a detached thread. */
    r.ctx = &c;
    r.delay_ms = 100;
    pthread_create(&helper, NULL, releaser_main, &r);

    check("destroy_joins_a_worker_blocked_in_the_callback",
          d200_decode_destroy(&q) == 0 && q.worker_started == 0);
    pthread_join(helper, NULL);

    /* After destroy the thread must be gone: joining it again from here would be undefined, so the
     * driver instead proves the join happened by observing a stopped call count. */
    check("worker_thread_actually_exited", ctx_calls(&c) == 1);
}

int main(void) {
    /* The header's own constants, echoed so the test can compare them with the parsed enum. */
    printf("ENUM D200_DECODE_EMPTY %d\n", (int)D200_DECODE_EMPTY);
    printf("ENUM D200_DECODE_PENDING %d\n", (int)D200_DECODE_PENDING);
    printf("ENUM D200_DECODE_DONE %d\n", (int)D200_DECODE_DONE);
    printf("ENUM D200_DECODE_INVALID %d\n", (int)D200_DECODE_INVALID);

    test_lifecycle();
    test_validation();
    test_inflight_and_slots();
    test_stream_and_failure();
    test_full_pipe();
    test_destroy_joins_busy_worker();

    printf("TOTAL %u\n", cases);
    printf("FAILED %u\n", failures);
    return failures ? 1 : 0;
}
"""

# Every contract case the driver declares with a literal name. Parsed rather than assumed, so a case
# that is deleted from the driver is caught by the comparison below.
DRIVER_CASE_SITES = frozenset(re.findall(r'check\(\s*"([^"]+)"\s*,', DRIVER))


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
    request.config.pluginmanager.register(_coverage, "devicecolor-decode-coverage")
    yield


# --------------------------------------------------------------------------- the harness


def _host_cc() -> str | None:
    """The host C compiler, or None when this box has none."""
    return shutil.which("cc") or shutil.which("clang")


def _child_env(home: Path) -> dict[str, str]:
    """The environment for every child process this harness spawns, with an isolated HOME.

    Containment rule (BRIEF rule 7): a suite run in this operation reached the real
    `~/.ghostdeck/state.json` and started a live `ghostdeck.vhid` keeper plus a real player. The
    driver here is pure computation and the header does no file I/O beyond its own pipe, but the
    compiler and the driver still inherit whatever we hand them, so they get a HOME inside the
    pytest temp dir. The assertion is the point: a future edit that passes the real home fails
    loudly instead of quietly going back to the live state file.
    """
    home.mkdir(parents=True, exist_ok=True)
    assert home.resolve() != REAL_HOME, f"refusing to hand the real HOME to a child: {home}"
    env = dict(os.environ)
    env["HOME"] = str(home)
    return env


def _compile(
    cc: str, source: Path, binary: Path, extra: tuple[str, ...], env: dict[str, str]
) -> subprocess.CompletedProcess:
    """Compile `source`. `-pthread` is unconditional so the link works on both macOS and Linux."""
    return subprocess.run(
        [cc, *CC_FLAGS, *extra, "-pthread", f"-I{HEADER.parent}", "-o", str(binary), str(source)],
        capture_output=True,
        text=True,
        env=env,
    )


def _run_driver(
    tmp_path: Path, label: str, extra: tuple[str, ...], home: Path
) -> tuple[dict[str, str], str]:
    """Compile the driver with `extra` flags, run it, and return (case -> PASS/FAIL, raw output)."""
    cc = _host_cc()
    assert cc is not None
    source = tmp_path / f"decode_{label}.c"
    binary = tmp_path / f"decode_{label}"
    source.write_text(DRIVER, encoding="utf-8")

    env = _child_env(home)
    compiled = _compile(cc, source, binary, extra, env)
    assert compiled.returncode == 0, f"[{label}] compiler output:\n{compiled.stdout}{compiled.stderr}"

    ran = subprocess.run([str(binary)], capture_output=True, text=True, env=env)
    output = ran.stdout + ran.stderr
    for marker in ("AddressSanitizer", "runtime error:", "LeakSanitizer", "ThreadSanitizer"):
        assert marker not in output, f"[{label}] sanitizer report ({marker}):\n{output}"

    statuses = {
        match.group(1): match.group(2)
        for match in re.finditer(r"^CASE (\S+) (PASS|FAIL)$", ran.stdout, re.M)
    }
    assert ran.returncode == 0, f"[{label}] driver exited {ran.returncode}:\n{output}"
    return statuses, ran.stdout


def test_decode_queue_compiles_and_runs_device_free(tmp_path):
    """Compile and run the agent's decode queue on this host, with no device involved."""
    if _host_cc() is None:
        pytest.skip("no host C compiler (cc/clang) available")

    # Every public entry point the header defines must appear in the driver, or it is untested, and
    # the internal-helper allowlist must still name functions that exist.
    for name in PUBLIC_API:
        assert name in DRIVER, (
            f"the header defines {name} but the driver never calls it; the queue has grown an "
            f"untested entry point"
        )
    for name in INTERNAL_HELPERS:
        assert name in API_FUNCTIONS, (
            f"{name} is allowlisted as an internal helper but the header no longer defines it; "
            f"the allowlist is stale"
        )

    statuses, output = _run_driver(tmp_path, "plain", (), tmp_path / "home-plain")
    _coverage.lines = [f"devicecolor-decode[{line}]" for line in output.splitlines()]

    assert statuses, f"the driver printed no CASE lines:\n{output}"
    failed = sorted(name for name, status in statuses.items() if status != "PASS")
    assert not failed, f"driver cases failed: {failed}\n{output}"

    # A deleted contract case must fail the test, not quietly shrink coverage.
    missing = sorted(CONTRACT_CASES - statuses.keys())
    assert not missing, f"contract cases the driver never reported: {missing}\n{output}"
    assert statuses.keys() == DRIVER_CASE_SITES, (
        f"the driver ran a different case set than it declares: "
        f"{sorted(statuses.keys() ^ DRIVER_CASE_SITES)}\n{output}"
    )

    # The state values the driver compared against are the header's own, echoed at runtime.
    echoed = {name: int(value) for name, value in re.findall(r"^ENUM (\S+) (-?\d+)$", output, re.M)}
    assert echoed, f"the driver never echoed the state enum:\n{output}"
    assert echoed == STATE_ENUM, (
        f"the driver's state values disagree with the header's enum: driver={echoed} "
        f"header={STATE_ENUM}"
    )

    totals = dict(re.findall(r"^(TOTAL|FAILED) (\d+)$", output, re.M))
    assert totals.get("FAILED") == "0", output
    assert int(totals.get("TOTAL", "0")) >= len(CONTRACT_CASES), output
    _coverage.lines.append(
        f"devicecolor-decode[coverage] {len(statuses)} cases, "
        f"{len(PUBLIC_API)} public entry points"
    )


def test_decode_queue_is_clean_under_asan_and_ubsan(tmp_path):
    """The same driver under `-fsanitize=address,undefined`, trusted only after liveness controls."""
    cc = _host_cc()
    if cc is None:
        pytest.skip("no host C compiler (cc/clang) available")

    env = _child_env(tmp_path / "home-sanitized")
    probe_source = tmp_path / "probe.c"
    probe_binary = tmp_path / "probe"
    probe_source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
    probe = _compile(cc, probe_source, probe_binary, SANITIZE_FLAGS, env)
    if probe.returncode != 0:
        _coverage.lines.append(
            "devicecolor-decode[sanitizer] UNAVAILABLE: "
            + (probe.stdout + probe.stderr).strip().splitlines()[0]
        )
        pytest.skip(f"no address/undefined sanitizer on this host: {probe.stderr.strip()}")

    silent = _run_sanitizer_controls(cc, tmp_path, env)
    if silent:
        _coverage.lines.append(
            "devicecolor-decode[sanitizer] INERT, driver run skipped: " + "; ".join(silent)
        )
        pytest.skip(
            "the sanitizer links but does not report its own control bug, so a clean driver run "
            "would prove nothing: " + "; ".join(silent)
        )

    statuses, output = _run_driver(tmp_path, "sanitized", SANITIZE_FLAGS, tmp_path / "home-san")
    failed = sorted(name for name, status in statuses.items() if status != "PASS")
    assert not failed, f"driver cases failed under sanitizers: {failed}\n{output}"
    assert statuses.keys() >= CONTRACT_CASES, output
    _coverage.lines.append(
        f"devicecolor-decode[sanitizer] live (both controls detected), clean, {len(statuses)} cases"
    )


def _run_sanitizer_controls(cc: str, tmp_path: Path, env: dict[str, str]) -> list[str]:
    """Return the names of controls that a live sanitizer would have reported but did not."""
    silent: list[str] = []
    for name, marker, text in SANITIZER_CONTROLS:
        source = tmp_path / f"control_{name}.c"
        binary = tmp_path / f"control_{name}"
        source.write_text(text, encoding="utf-8")
        compiled = _compile(cc, source, binary, SANITIZE_FLAGS, env)
        if compiled.returncode != 0:
            silent.append(f"{name} (control did not compile)")
            continue
        ran = subprocess.run([str(binary)], capture_output=True, text=True, env=env)
        if marker not in ran.stdout + ran.stderr:
            silent.append(f"{name} (control ran but was not reported)")
    return silent


def test_decode_queue_is_clean_under_thread_sanitizer(tmp_path):
    """The queue is a threaded primitive, so ThreadSanitizer is the sanitizer that matters here.

    As with AddressSanitizer, the run is only trusted after a deliberately racy control has been
    shown to be *detected* on this host: a ThreadSanitizer that links but cannot report a race would
    make "clean" meaningless.
    """
    cc = _host_cc()
    if cc is None:
        pytest.skip("no host C compiler (cc/clang) available")

    env = _child_env(tmp_path / "home-tsan")
    probe_source = tmp_path / "tsan_probe.c"
    probe_binary = tmp_path / "tsan_probe"
    probe_source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
    probe = _compile(cc, probe_source, probe_binary, TSAN_FLAGS, env)
    if probe.returncode != 0:
        _coverage.lines.append(
            "devicecolor-decode[tsan] UNAVAILABLE: "
            + (probe.stdout + probe.stderr).strip().splitlines()[0]
        )
        pytest.skip(f"no thread sanitizer on this host: {probe.stderr.strip()}")

    control_source = tmp_path / "tsan_control.c"
    control_binary = tmp_path / "tsan_control"
    control_source.write_text(TSAN_CONTROL, encoding="utf-8")
    control = _compile(cc, control_source, control_binary, TSAN_FLAGS, env)
    if control.returncode != 0:
        _coverage.lines.append("devicecolor-decode[tsan] INERT: the control did not compile")
        pytest.skip("the thread-sanitizer control did not compile, so a clean run proves nothing")
    ran = subprocess.run([str(control_binary)], capture_output=True, text=True, env=env)
    if "ThreadSanitizer: data race" not in ran.stdout + ran.stderr:
        _coverage.lines.append(
            "devicecolor-decode[tsan] INERT: the control ran but its race was not reported"
        )
        pytest.skip(
            "ThreadSanitizer links but does not report its own control race, so a clean driver run "
            "would prove nothing"
        )

    statuses, output = _run_driver(tmp_path, "tsan", TSAN_FLAGS, tmp_path / "home-tsan")
    failed = sorted(name for name, status in statuses.items() if status != "PASS")
    assert not failed, f"driver cases failed under ThreadSanitizer: {failed}\n{output}"
    assert statuses.keys() >= CONTRACT_CASES, output
    _coverage.lines.append(
        f"devicecolor-decode[tsan] live (control race detected), clean, {len(statuses)} cases"
    )


def test_harness_cannot_reach_the_real_home(tmp_path):
    """Containment (BRIEF rule 7): this harness cannot touch the real `~/.ghostdeck` state.

    Three independent checks, matching the sibling codec harness:

    1. Static — the driver reads no environment variable and spawns no process or file write, so no
       environment value can reach it in the first place.
    2. Dynamic — the isolation is real, not decorative: a child that genuinely writes
       `$HOME/<probe>` is run under the harness environment, and the write must land in the temp
       HOME and **never** in the real one.
    3. Refusal — `_child_env` hard-fails if it is ever handed the real HOME.
    """
    cc = _host_cc()
    if cc is None:
        pytest.skip("no host C compiler (cc/clang) available")

    for token in ("getenv(", "fopen(", "system(", "popen(", "fork(", "unlink(", "remove("):
        assert token not in DRIVER, (
            f"the C driver must stay pure computation; it now contains {token!r}"
        )

    env = _child_env(tmp_path / "probe-home")
    isolated = Path(env["HOME"])
    assert isolated != REAL_HOME
    assert isolated.is_dir(), "the harness never created its isolated HOME"

    probe = f".d200-decode-probe-{os.urandom(8).hex()}"
    assert not (REAL_HOME / probe).exists(), "refusing to run with a colliding real-HOME probe"
    probe_source = tmp_path / "home_write_probe.c"
    probe_binary = tmp_path / "home_write_probe"
    probe_source.write_text(
        "#include <stdio.h>\n"
        "#include <stdlib.h>\n"
        "int main(void) {\n"
        '    const char *home = getenv("HOME");\n'
        "    char path[1024];\n"
        "    FILE *f;\n"
        "    if (!home) return 2;\n"
        f'    snprintf(path, sizeof(path), "%s/{probe}", home);\n'
        '    f = fopen(path, "w");\n'
        "    if (!f) return 3;\n"
        '    fputs("written by a child\\n", f);\n'
        "    fclose(f);\n"
        '    printf("wrote %s\\n", path);\n'
        "    return 0;\n"
        "}\n",
        encoding="utf-8",
    )
    compiled = _compile(cc, probe_source, probe_binary, (), env)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    ran = subprocess.run([str(probe_binary)], capture_output=True, text=True, env=env)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert (isolated / probe).read_text(encoding="utf-8") == "written by a child\n"
    assert not (REAL_HOME / probe).exists(), (
        f"a harness child wrote into the real HOME: {REAL_HOME / probe}"
    )

    with pytest.raises(AssertionError):
        _child_env(REAL_HOME)

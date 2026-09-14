"""Host-only coverage for the `done:` tail's signal-reason guard in device/d200-color-agent.c.

`worker()` cannot be compiled on a 64-bit host: the `done:` tail calls divp_cleanup() and
d200_decode_destroy(), which need the deck's vendor libraries. That is also why `worker()` sits
*after* the `/* STREAM_CORE_END */` seam and is NOT reached by the committed 64-case driver in
tests/test_devicecolor_agent.py -- so before this file, the guard line had no repo coverage at all.

What is real here and what is the driver's:

* The extraction (`#define WIDTH 540` .. `/* STREAM_CORE_END */`), `running`, `signal_reason`,
  `stop_signal`, `color_ready`, `color_flush`, `color_failure_notice`, `color_cleanup_finished` and
  `color_terminal` are the production code, compiled from the agent itself.
* The guard line is spliced into the driver **verbatim from the agent source** (asserted to match
  exactly once), so the driver cannot disagree with the revision under test: revert the guard and
  these cases fail.
* The only re-authored part is the tail's *ordering* around the guard. That ordering is the
  production one (guard -> startup-failure record -> owned ERROR notice -> cleanup -> terminal),
  copied by hand because the real tail is not host-compilable.

Contract under test: the loop's own cause wins; the signal supplies a reason only while none was
reached. Before the fix (`if (!running)`), a detected D200_VS_DISCONNECTED was reported as
D200_VS_RESULT_CANCELLED, the agent exited 0, and the owned ERROR record that the proxy uses as its
cleanup/reap budget was skipped.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "device" / "d200-color-agent.c"
REAL_HOME = Path(os.path.expanduser("~")).resolve()

CORE_START = "#define WIDTH 540"
CORE_END = "/* STREAM_CORE_END */"
CC_FLAGS = ("-std=c11", "-O1", "-Wall", "-Wextra", "-Werror")
SANITIZE_FLAGS = ("-fsanitize=address,undefined", "-fno-omit-frame-pointer")

# The guard this file exists for. Matched so the driver splices the real text, never a copy.
GUARD_RE = re.compile(r"^[ \t]*if[ \t]*\([ \t]*!running.*signal_reason.*;[ \t]*$", re.M)

PREAMBLE = r'''#define _DARWIN_C_SOURCE 1
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

# The tail replay. __GUARD__ is replaced by the agent's own guard line.
DRIVER = r'''
static const uint8_t SESSION[16] = {
    0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88,
    0x99, 0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0x0f, 0x10};

static unsigned cases, failures;
static void check(const char *name, int ok) {
    ++cases;
    printf("CASE %s %s\n", name, ok ? "PASS" : "FAIL");
    if (!ok) ++failures;
}

static struct color_stream *stream_open(int fd[2]) {
    struct color_stream *s;
    if (socketpair(AF_UNIX, SOCK_STREAM, 0, fd)) return NULL;
    s = calloc(1, sizeof(*s));
    if (!s) return NULL;
    s->slots[0] = calloc(1, D200_VS_MAX_JPEG);
    s->slots[1] = calloc(1, D200_VS_MAX_JPEG);
    d200_vs_state_init(&s->wire, SESSION, 30, 1, NULL);
    if (color_ready(s, SESSION, 30, 1)) return NULL;
    while (s->out_size) { if (color_flush(s, fd[0])) break; }
    s->out_size = 0;
    return s;
}

static void stream_close(struct color_stream *s, int fd[2]) {
    free(s->slots[0]); free(s->slots[1]); free(s);
    close(fd[0]); close(fd[1]);
}

/* One replay of the production `done:` tail. `loop_reason` is what the receive loop reached
 * (0 when the loop never observed a failure). Reports the terminal reason, the exit code, and
 * whether the owned ERROR notice was queued. */
static uint32_t tail(struct color_stream *s, int fd, uint32_t loop_reason,
                     int *exit_code, int *error_queued) {
    uint32_t reason = loop_reason;
    int sent = 0, terminal_queued = 0, notice_failed = 0, cleanup_failed = 0;
    __GUARD__
    (void)color_startup_failure(&s->startup, s->wire.session, STDERR_FILENO, reason);
    if (reason && !d200_vs_reason(reason)) {
        int notice = color_failure_notice(s, fd, reason);
        terminal_queued = notice == 1;
        notice_failed = notice < 0;
    }
    color_cleanup_finished(s, monotonic_ns());
    uint64_t end = monotonic_ns() + UINT64_C(2000000000);
    if (notice_failed) sent = 0;
    else if (terminal_queued) sent = !s->out_size;
    else if (!color_terminal(s, reason, cleanup_failed)) {
        while (s->out_size && monotonic_ns() < end) {
            if (color_flush(s, fd)) break;
            struct pollfd pf = {fd, POLLOUT, 0}; (void)poll(&pf, 1, 10);
        }
        sent = !s->out_size;
    }
    if (exit_code) *exit_code = sent && !cleanup_failed ? 0 : D200_VS_CLEANUP_FAILED;
    if (error_queued) *error_queued = terminal_queued;
    return reason;
}

/* Each case starts from the same signal state, so cases cannot leak into one another. */
static void signal_reset(void) { running = 1; signal_reason = D200_VS_RESULT_CANCELLED; }

int main(void) {
    int fd[2];
    struct color_stream *s;
    uint32_t reported;
    int exit_code, error_queued;

    /* This driver replays the tail only. The rest of the extracted core is still the unit under
     * test, so reference every function it declares -- otherwise -Werror fails the build for the
     * functions this driver legitimately does not call. Generated from the core, never hand-kept. */
    __TOUCH__

    /* 1. The loop detected the disconnect; SIGTERM also arrived. The cause must survive. */
    s = stream_open(fd);
    if (!s) return 2;
    signal_reset(); stop_signal(SIGTERM);
    exit_code = error_queued = -1;
    reported = tail(s, fd[0], D200_VS_DISCONNECTED, &exit_code, &error_queued);
    check("detected_disconnect_survives_sigterm", reported == D200_VS_DISCONNECTED);
    check("detected_disconnect_is_not_reported_as_a_cancel", !d200_vs_reason(reported));
    check("detected_disconnect_queues_the_owned_error", error_queued == 1);
    stream_close(s, fd);

    /* 2. SIGUSR1 after a detected disconnect behaves the same way. */
    s = stream_open(fd);
    if (!s) return 2;
    signal_reset(); stop_signal(SIGUSR1);
    reported = tail(s, fd[0], D200_VS_DISCONNECTED, NULL, NULL);
    check("detected_disconnect_survives_sigusr1", reported == D200_VS_DISCONNECTED);
    stream_close(s, fd);

    /* 3. Nothing was detected: the signal must still supply the reason (idle host cancel). */
    s = stream_open(fd);
    if (!s) return 2;
    signal_reset(); stop_signal(SIGTERM);
    exit_code = -1;
    reported = tail(s, fd[0], 0, &exit_code, NULL);
    check("idle_cancel_still_reports_cancelled", reported == D200_VS_RESULT_CANCELLED);
    check("idle_cancel_exits_clean", exit_code == 0);
    stream_close(s, fd);

    /* 4. The SIGUSR1/SOURCE_FAILURE reason still reaches the agent when the loop has none. */
    s = stream_open(fd);
    if (!s) return 2;
    signal_reset(); stop_signal(SIGUSR1);
    reported = tail(s, fd[0], 0, NULL, NULL);
    check("idle_sigusr1_still_reports_source_failure", reported == D200_VS_SOURCE_FAILURE);
    stream_close(s, fd);

    printf("TOTAL %u\nFAILED %u\n", cases, failures);
    return failures ? 1 : 0;
}
'''


def _guard_line() -> str:
    """The guard, read from the agent. Exactly one, or this file is watching the wrong line."""
    hits = GUARD_RE.findall(AGENT.read_text(encoding="utf-8"))
    assert len(hits) == 1, (
        f"expected exactly one `if (!running ... signal_reason ...)` guard in {AGENT}, found "
        f"{len(hits)}: {hits}. If the guard was renamed or restructured, this file is silently "
        f"testing nothing."
    )
    return hits[0].strip()


def _extract_core() -> str:
    source = AGENT.read_text(encoding="utf-8")
    start, stop = source.find(CORE_START), source.find(CORE_END)
    assert 0 <= start < stop, f"cannot locate the production core inside {AGENT}"
    return source[start:stop]


def _core_functions(core: str) -> list[str]:
    return sorted(set(re.findall(r"^static[^\n;{]*?\b([A-Za-z_]\w*)\s*\(", core, re.M)))


def _translation_unit() -> str:
    core = _extract_core()
    unit = PREAMBLE + core + DRIVER
    guard = _guard_line()
    assert "__GUARD__" in unit
    unit = unit.replace("__GUARD__", guard)
    touch = "\n".join(f"    (void)&{name};" for name in _core_functions(core))
    assert "__TOUCH__" in unit and touch
    return unit.replace("__TOUCH__", touch)


def _host_cc() -> str | None:
    return shutil.which("cc") or shutil.which("clang")


def _child_env(home: Path) -> dict[str, str]:
    """Every child gets a HOME inside the pytest temp dir (BRIEF rule 7)."""
    home.mkdir(parents=True, exist_ok=True)
    assert home.resolve() != REAL_HOME, f"refusing to hand the real HOME to a child: {home}"
    env = dict(os.environ)
    env["HOME"] = str(home)
    return env


def _run(tmp_path: Path, extra: tuple[str, ...] = ()) -> tuple[dict[str, str], str]:
    cc = _host_cc()
    if cc is None:
        pytest.skip("no host C compiler (cc/clang) available")
    source = tmp_path / "tail_driver.c"
    binary = tmp_path / "tail_driver"
    source.write_text(_translation_unit(), encoding="utf-8")
    env = _child_env(tmp_path / "home")
    compiled = subprocess.run(
        [cc, *CC_FLAGS, *extra, "-pthread", f"-I{ROOT / 'device'}", "-o", str(binary), str(source)],
        capture_output=True, text=True, env=env,
    )
    assert compiled.returncode == 0, f"compiler output:\n{compiled.stdout}{compiled.stderr}"
    ran = subprocess.run([str(binary)], capture_output=True, text=True, env=env)
    statuses = {
        match.group(1): match.group(2)
        for match in re.finditer(r"^CASE (\S+) (PASS|FAIL)$", ran.stdout, re.M)
    }
    return statuses, ran.stdout + ran.stderr


EXPECTED_CASES = (
    "detected_disconnect_survives_sigterm",
    "detected_disconnect_is_not_reported_as_a_cancel",
    "detected_disconnect_queues_the_owned_error",
    "detected_disconnect_survives_sigusr1",
    "idle_cancel_still_reports_cancelled",
    "idle_cancel_exits_clean",
    "idle_sigusr1_still_reports_source_failure",
)


def test_tail_guard_keeps_the_loop_cause(tmp_path):
    """The regression this file exists for, plus the property it must not break."""
    statuses, output = _run(tmp_path)
    missing = sorted(set(EXPECTED_CASES) - set(statuses))
    assert not missing, f"cases the driver never reported: {missing}\n{output}"
    failed = sorted(name for name, verdict in statuses.items() if verdict == "FAIL")
    assert not failed, f"driver cases failed: {failed}\n{output}"


def test_tail_guard_is_clean_under_asan_and_ubsan(tmp_path):
    statuses, output = _run(tmp_path, extra=SANITIZE_FLAGS)
    for marker in ("AddressSanitizer", "runtime error:", "LeakSanitizer"):
        assert marker not in output, f"sanitizer report ({marker}):\n{output}"
    failed = sorted(name for name, verdict in statuses.items() if verdict == "FAIL")
    assert not failed, f"driver cases failed under sanitizers: {failed}\n{output}"
    assert statuses.keys() >= set(EXPECTED_CASES), output

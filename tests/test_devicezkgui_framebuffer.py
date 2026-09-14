"""Executable proof that the preload's framebuffer teardown keeps its descriptor-ownership invariant.

`device/d200-zkgui-preload.c` acquires a private `/dev/fb0` handle, forces the panel black, and owes
one restore. It does that work through a *raw descriptor number*, and the application may close,
`dup2`-overwrite or recycle that number behind the interposer's back. B-105 is the finding that the
number cannot be trusted: a recycled number that is confidently written through or closed destroys
someone else's descriptor, which is worse than the residual it was fixing.

That property is a branch-and-target property, not a value: it is invisible to any check that only
looks at the shipped text, and a macOS host has no `/dev/fb0` to exercise it against. So this file
does what `tests/test_devicecolor_conformance.py` does for the wire codec: it splices the framebuffer
section of the shipped translation unit **verbatim** (located by content anchors, never by line
numbers) between a prelude and a driver, compiles it with the host compiler, and *executes* it against
**real descriptors** — real `open`, real `fcntl(F_GETFD)`, real `close`, so "was a foreign descriptor
touched" is observable rather than asserted. Only the `/dev/fb0` ioctl contract is simulated, because
it is the one thing that cannot exist here.

Three things make the guard non-tautological, which is the whole risk with a harness like this:

1. Every scenario asserts **positive** evidence that the intended thing happened (a reopen was
   attempted, the restore landed on a different descriptor and carried the right bytes), so a model
   where nothing runs cannot pass by violating nothing.
2. A global invariant across all scenarios is asserted from the harness's own accounting: **zero**
   `FB_SET_COLOR_KEY` calls and **zero** `close()` calls on any descriptor that did not just answer
   the `/dev/fb0` probe.
3. `test_guard_bites_on_each_regression` sabotages the spliced section four ways and requires the
   model to *fail* each time, with the specific assertion named. The sabotage texts are matched
   exactly and a missing anchor is a hard failure, so a future refactor cannot quietly turn this
   file into a no-op.

What this file does **not** prove, stated plainly: there is no framebuffer here, so the ioctl contract
is simulated. It proves which branch runs, which descriptor it targets, and the ordering and ownership
discipline around it. It does **not** prove that the deck's driver accepts the restore, that the panel
returns to the saved colour key, or that the key was captured correctly — that is real-hardware
evidence and stays unverified on a host by construction.

No device, no `adb`, no `ffmpeg`, no vendor bridge: the model is pure computation over `open`/`close`/
`fcntl`/`ioctl` and never references `HOME`, the environment, or a spawned process.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PRELOAD = ROOT / "device" / "d200-zkgui-preload.c"
REAL_HOME = Path(os.path.expanduser("~")).resolve()

CC_FLAGS = ("-O1", "-Wall", "-Wextra", "-pthread")

# Content anchors. Line numbers would silently splice the wrong text after any edit above the
# section, so the section is located by the comment that opens it and the declaration that follows it.
SECTION_START = "/* Revalidate the private control handle"
SECTION_END = "static int mode_required(int flags)"
REQUIRED_NAMES = (
    "framebuffer_handle_valid",
    "restore_color_key_through_fresh_handle",
    "release_framebuffer_fd",
    "restore_framebuffer",
)

# (label, needle, replacement, assertion the sabotaged model must fail on).
# Each needle must occur exactly once, so an edit that moves or rewrites the arm is a loud failure
# here instead of a silently weaker guard.
SABOTAGES = (
    (
        "release path drops the saved key instead of restoring it through a fresh handle",
        "        framebuffer_fd = -1;\n"
        "        framebuffer_teardown = 1;\n"
        "        if (framebuffer_configured)\n"
        "            restore_color_key_through_fresh_handle();\n",
        "        framebuffer_fd = -1;\n"
        "        framebuffer_teardown = 1;\n"
        "        framebuffer_configured = 0;\n",
        "the release path restored through exactly one fresh handle",
    ),
    (
        "release path clears the obligation when the discharge failed (the B-134/B-141 residual)",
        "        framebuffer_fd = -1;\n"
        "        framebuffer_teardown = 1;\n"
        "        if (framebuffer_configured)\n"
        "            restore_color_key_through_fresh_handle();\n",
        "        framebuffer_fd = -1;\n"
        "        framebuffer_teardown = 1;\n"
        "        if (framebuffer_configured)\n"
        "            restore_color_key_through_fresh_handle();\n"
        "        framebuffer_configured = 0;\n",
        "the failed discharge left the black-out obligation pending",
    ),
    (
        "probe-failure arm drops the handle without reopening (the pre-T6 shape)",
        "        framebuffer_fd = -1;\n"
        "        if (framebuffer_configured)\n"
        "            restore_color_key_through_fresh_handle();\n"
        "        framebuffer_configured = 0;\n",
        "        framebuffer_fd = -1;\n"
        "        framebuffer_configured = 0;\n",
        "the fresh handle, not the failing number, received the restore",
    ),
    (
        "validation trusts fcntl(F_GETFD) alone, so a recycled number passes (the B-105 bug class)",
        "    return fd >= 0 && fcntl(fd, F_GETFD) >= 0 &&\n"
        "           ioctl(fd, FB_GET_COLOR_KEY, &probe) == 0;\n",
        "    (void)probe;\n"
        "    return fd >= 0 && fcntl(fd, F_GETFD) >= 0;\n",
        "the recycled number was never written through",
    ),
)

PRELUDE = r"""/* Framebuffer-ownership model harness, prelude.
 *
 * The framebuffer section of device/d200-zkgui-preload.c is spliced in verbatim after this text, so
 * every globals/types/name the section uses is redeclared here exactly as the translation unit has
 * it. Nothing in the spliced text is retyped or edited.
 *
 * Descriptors are REAL (opened on /dev/null), so fcntl(F_GETFD) and close() behave exactly as the
 * kernel would and "was a descriptor that is not ours touched" is observable. The fb0 ioctl contract
 * is simulated, because this host has no /dev/fb0 and the probe-failure branch is otherwise
 * unreachable.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

typedef int (*open_fn)(const char *, int, ...);
typedef int (*close_fn)(int);

struct color_key {
    unsigned char enable;
    unsigned char red;
    unsigned char green;
    unsigned char blue;
};

#define FB_GET_COLOR_KEY 0x80044666UL
#define FB_SET_COLOR_KEY 0x40044667UL

/* Same names, types and linkage as the shipped translation unit. */
static open_fn real_open_fn;
static close_fn real_close_fn;
static pthread_mutex_t framebuffer_lock = PTHREAD_MUTEX_INITIALIZER;
static int framebuffer_fd = -1;
static int framebuffer_configured;
static int framebuffer_teardown;
static struct color_key original_color_key;

/* ---- model instrumentation (never referenced by the spliced section) ---- */
#define FDMAX 256
static int fd_answers_probe[FDMAX];   /* this number answers the /dev/fb0 probe */
static int get_on_probe_fd[FDMAX];
static int get_on_other_fd[FDMAX];
static int set_on_probe_fd[FDMAX];
static int set_on_other_fd[FDMAX];
static int close_on_probe_fd[FDMAX];
static int close_on_other_fd[FDMAX];
static int n_fb_open;                 /* /dev/fb0 opens in the current scenario */
static int total_fb_opens;            /* cumulative, deliberately not cleared    */
static int total_set_other;
static int total_close_other;
static int total_set_probe;
static int total_close_probe;
static int probe_fail_fd = -1;        /* GET fails here while fcntl() succeeds  */
static int fail_next_fb_open;         /* the next /dev/fb0 open fails transiently */
static int last_set_fd = -1;
static struct color_key last_set_key;
static struct color_key live_key;
static int failures;

static void check(const char *what, int ok)
{
    printf("  [%s] %s\n", ok ? "PASS" : "FAIL", what);
    if (!ok) ++failures;
}

static void reset(void)
{
    memset(fd_answers_probe, 0, sizeof(fd_answers_probe));
    memset(get_on_probe_fd, 0, sizeof(get_on_probe_fd));
    memset(get_on_other_fd, 0, sizeof(get_on_other_fd));
    memset(set_on_probe_fd, 0, sizeof(set_on_probe_fd));
    memset(set_on_other_fd, 0, sizeof(set_on_other_fd));
    memset(close_on_probe_fd, 0, sizeof(close_on_probe_fd));
    memset(close_on_other_fd, 0, sizeof(close_on_other_fd));
    n_fb_open = 0;
    probe_fail_fd = -1;
    fail_next_fb_open = 0;
    last_set_fd = -1;
    framebuffer_fd = -1;
    framebuffer_configured = 0;
    framebuffer_teardown = 0;
    memset(&original_color_key, 0, sizeof(original_color_key));
    memset(&last_set_key, 0, sizeof(last_set_key));
    memset(&live_key, 0, sizeof(live_key));
    live_key.enable = 1;   /* the panel, forced black by the interposer */
}

/* real_open_fn target: a real descriptor tagged as answering the fb0 probe. */
static int model_open(const char *path, int flags, ...)
{
    va_list ap;
    int mode = 0;
    int fd;
    if ((flags & O_CREAT) != 0) {
        va_start(ap, flags);
        mode = va_arg(ap, int);
        va_end(ap);
    }
    if (path != NULL && strcmp(path, "/dev/fb0") == 0) {
        if (fail_next_fb_open) {         /* injected ENODEV: no handle is handed out */
            fail_next_fb_open = 0;
            errno = ENODEV;
            return -1;
        }
        fd = open("/dev/null", O_RDWR, mode);
        if (fd >= 0 && fd < FDMAX) {
            fd_answers_probe[fd] = 1;
            ++n_fb_open;
            ++total_fb_opens;
        }
        return fd;
    }
    return open(path, flags, mode);
}

/* real_close_fn target: the only caller in this program is the spliced code. */
static int model_close(int fd)
{
    if (fd >= 0 && fd < FDMAX) {
        if (fd_answers_probe[fd]) {
            ++close_on_probe_fd[fd];
            ++total_close_probe;
        } else {
            ++close_on_other_fd[fd];
            ++total_close_other;
        }
        fd_answers_probe[fd] = 0;
    }
    return close(fd);
}

/* Simulated fb0 driver: never reaches libSystem's ioctl. */
int ioctl(int fd, unsigned long request, ...)
{
    va_list ap;
    void *arg;
    va_start(ap, request);
    arg = va_arg(ap, void *);
    va_end(ap);
    if (fd < 0 || fd >= FDMAX || fcntl(fd, F_GETFD) < 0) {
        errno = EBADF;
        return -1;
    }
    if (request == FB_GET_COLOR_KEY) {
        if (!fd_answers_probe[fd]) {
            ++get_on_other_fd[fd];
            errno = ENOTTY;              /* not a framebuffer: the probe fails */
            return -1;
        }
        ++get_on_probe_fd[fd];
        if (fd == probe_fail_fd) {       /* fcntl() ok, the ioctl probe fails  */
            errno = EINVAL;
            return -1;
        }
        *(struct color_key *)arg = live_key;
        return 0;
    }
    if (request == FB_SET_COLOR_KEY) {
        if (!fd_answers_probe[fd]) {
            ++set_on_other_fd[fd];
            ++total_set_other;
            errno = ENOTTY;
            return -1;
        }
        ++set_on_probe_fd[fd];
        ++total_set_probe;
        live_key = *(struct color_key *)arg;
        last_set_fd = fd;
        last_set_key = live_key;
        return 0;
    }
    errno = ENOTTY;
    return -1;
}
"""

DRIVER = r"""/* Framebuffer-ownership model driver.
 *
 * argv[1] is an existing regular file in the caller's temp dir, used as the "unrelated descriptor"
 * the application might take, or the number a recycled handle could collide with. Nothing outside
 * argv[1] and /dev/null is opened.
 */
static const char *app_path;

static void app_close(int fd)
{
    if (close(fd) == 0 && fd >= 0 && fd < FDMAX) fd_answers_probe[fd] = 0;
}

static struct color_key key(unsigned char r, unsigned char g, unsigned char b)
{
    struct color_key k;
    k.enable = 1;
    k.red = r;
    k.green = g;
    k.blue = b;
    return k;
}

static void scenario_probe_failure_with_the_descriptor_open(void)
{
    int priv, before;
    printf("Scenario 1: descriptor still open, only the ioctl probe fails (the dispatched residual)\n");
    reset();
    priv = model_open("/dev/fb0", O_RDWR | O_CLOEXEC);
    framebuffer_fd = priv;
    framebuffer_configured = 1;
    original_color_key = key(0xff, 0x80, 0x40);
    probe_fail_fd = priv;
    before = n_fb_open;
    restore_framebuffer();
    check("private descriptor still open at teardown (fcntl >= 0)", fcntl(priv, F_GETFD) >= 0);
    check("exactly one reopen of /dev/fb0 was attempted", n_fb_open - before == 1);
    check("the fresh handle, not the failing number, received the restore",
          last_set_fd >= 0 && last_set_fd != priv);
    check("the saved key was restored byte-exact through the fresh handle",
          last_set_fd >= 0 && set_on_probe_fd[last_set_fd] == 1 &&
          memcmp(&last_set_key, &original_color_key, sizeof(original_color_key)) == 0);
    check("no SET ever reached the failing number",
          set_on_probe_fd[priv] == 0 && set_on_other_fd[priv] == 0);
    check("the obligation was discharged (framebuffer_configured == 0)", framebuffer_configured == 0);
    check("teardown latched and the stored number dropped",
          framebuffer_teardown == 1 && framebuffer_fd == -1);
    check("the failing number was NOT closed (a number that did not validate is never closed)",
          close_on_probe_fd[priv] == 0 && close_on_other_fd[priv] == 0);
    app_close(priv);
}

static void scenario_recycled_after_an_interposed_close(void)
{
    int priv, app, before;
    printf("\nScenario 2: recycled number after an interposed close (the B-105 case)\n");
    reset();
    priv = model_open("/dev/fb0", O_RDWR | O_CLOEXEC);
    framebuffer_fd = priv;
    framebuffer_configured = 1;
    original_color_key = key(0x11, 0x22, 0x33);
    before = n_fb_open;
    release_framebuffer_fd(priv);         /* the close() interposer path */
    check("release latched teardown and dropped the stored number",
          framebuffer_teardown == 1 && framebuffer_fd == -1);
    check("the release path restored through exactly one fresh handle",
          n_fb_open - before == 1 && last_set_fd >= 0 && last_set_fd != priv &&
          set_on_probe_fd[last_set_fd] == 1 &&
          memcmp(&last_set_key, &original_color_key, sizeof(original_color_key)) == 0);
    check("the release path did not close the number it had just dropped",
          close_on_probe_fd[priv] == 0 && close_on_other_fd[priv] == 0);
    app_close(priv);
    app = open(app_path, O_RDWR);
    check("an unrelated open() recycled the released number", app == priv);
    before = n_fb_open;
    restore_framebuffer();                /* exit-time teardown */
    check("exit-time teardown did not reopen, probe, write or close the recycled number",
          n_fb_open == before && get_on_probe_fd[app] == 0 && get_on_other_fd[app] == 0 &&
          set_on_probe_fd[app] == 0 && set_on_other_fd[app] == 0 &&
          close_on_probe_fd[app] == 0 && close_on_other_fd[app] == 0);
    check("the recycled number is still open and writable for the application",
          fcntl(app, F_GETFD) >= 0 && write(app, "x", 1) == 1);
    app_close(app);
}

static void scenario_recycle_while_the_obligation_is_live(void)
{
    int priv, app, before;
    printf("\nScenario 3: adversarial recycle while the black-out obligation is still live\n");
    reset();
    priv = model_open("/dev/fb0", O_RDWR | O_CLOEXEC);
    framebuffer_fd = priv;
    framebuffer_configured = 1;
    original_color_key = key(0x44, 0x55, 0x66);
    /* The application releases the number without the close() interposer seeing it (dup2 overwrite),
     * then an unrelated open() takes the number back while the obligation is still live. */
    app_close(priv);
    app = open(app_path, O_RDWR);
    check("the number was recycled while framebuffer_configured was still 1",
          app == priv && framebuffer_configured == 1);
    before = n_fb_open;
    restore_framebuffer();
    check("the recycled number was never written through",
          set_on_probe_fd[app] == 0 && set_on_other_fd[app] == 0);
    check("the recycled number was never closed",
          close_on_probe_fd[app] == 0 && close_on_other_fd[app] == 0);
    check("the black-out was still discharged through one fresh handle",
          n_fb_open - before == 1 && last_set_fd >= 0 && last_set_fd != app &&
          set_on_probe_fd[last_set_fd] == 1 &&
          memcmp(&last_set_key, &original_color_key, sizeof(original_color_key)) == 0);
    check("the recycled number is still open and writable for the application",
          fcntl(app, F_GETFD) >= 0 && write(app, "x", 1) == 1);
    check("teardown ended latched, handleless and obligation-free",
          framebuffer_teardown == 1 && framebuffer_fd == -1 && framebuffer_configured == 0);
    printf("  note: the recycled number WAS the target of %d FB_GET_COLOR_KEY read probe(s) into a\n"
           "        local struct; that read is the validation itself and the reason no SET or close\n"
           "        ever reaches the number.\n",
           get_on_probe_fd[app] + get_on_other_fd[app]);
    app_close(app);
}

static void scenario_release_stays_a_pass_through(void)
{
    int app, o0, s0, c0, o1, s1, c1;
    printf("\nScenario 4: release_framebuffer_fd() stays a pure pass-through for other fds\n");
    reset();
    app = open(app_path, O_RDWR);
    o0 = n_fb_open; s0 = total_set_other + total_set_probe; c0 = total_close_other + total_close_probe;
    release_framebuffer_fd(app);          /* a foreign fd: close() must forward it */
    release_framebuffer_fd(-1);           /* the negative-fd guard                 */
    o1 = n_fb_open; s1 = total_set_other + total_set_probe; c1 = total_close_other + total_close_probe;
    check("no open, ioctl or close on either call", o1 == o0 && s1 == s0 && c1 == c0);
    check("neither call latched teardown or took the handle",
          framebuffer_teardown == 0 && framebuffer_fd == -1 && framebuffer_configured == 0);
    check("the foreign descriptor is untouched",
          fcntl(app, F_GETFD) >= 0 && write(app, "x", 1) == 1);
    app_close(app);
}

static void scenario_release_discharge_failure_is_retried(void)
{
    int priv, before, writes;
    printf("\nScenario 6: the release path's own reopen fails, then the teardown retries\n");
    reset();
    priv = model_open("/dev/fb0", O_RDWR | O_CLOEXEC);
    framebuffer_fd = priv;
    framebuffer_configured = 1;
    original_color_key = key(0xaa, 0xbb, 0xcc);
    before = n_fb_open;
    writes = total_set_probe + total_set_other;
    fail_next_fb_open = 1;                /* the discharge's one fresh reopen fails */
    release_framebuffer_fd(priv);         /* the close() interposer path */
    check("the release path attempted exactly one reopen of /dev/fb0, and it failed",
          fail_next_fb_open == 0 && n_fb_open == before);
    check("the failed discharge left the black-out obligation pending",
          framebuffer_configured == 1 && framebuffer_fd == -1 && framebuffer_teardown == 1);
    check("no write was attempted while the discharge had no validated handle",
          total_set_probe + total_set_other == writes && set_on_probe_fd[priv] == 0);
    restore_framebuffer();                /* exit-time teardown, with the fault cleared */
    check("the teardown retry restored the saved key through a freshly validated handle",
          last_set_fd >= 0 && last_set_fd != priv && set_on_probe_fd[last_set_fd] == 1 &&
          memcmp(&last_set_key, &original_color_key, sizeof(original_color_key)) == 0);
    check("the retry never wrote through or closed the released number",
          set_on_probe_fd[priv] == 0 && set_on_other_fd[priv] == 0 &&
          close_on_probe_fd[priv] == 0 && close_on_other_fd[priv] == 0);
    check("teardown ended latched, handleless and obligation-free",
          framebuffer_teardown == 1 && framebuffer_fd == -1 && framebuffer_configured == 0);
    app_close(priv);
}

static void scenario_recycled_number_that_answers_the_probe(void)
{
    int priv, app;
    printf("\nScenario 5: boundary -- a recycled number that itself answers the fb0 probe\n");
    reset();
    priv = model_open("/dev/fb0", O_RDWR | O_CLOEXEC);
    framebuffer_fd = priv;
    framebuffer_configured = 1;
    original_color_key = key(0x77, 0x88, 0x99);
    app_close(priv);
    app = open(app_path, O_RDWR);
    /* Injected: on the deck this means the application itself holds an open /dev/fb0 handle at that
     * number. The guarantee is "only a descriptor that just answered the probe is written through or
     * closed", so this case is the intended action on the intended device, not a violation. */
    fd_answers_probe[app] = 1;
    restore_framebuffer();
    check("a number that PASSES the probe is treated as the framebuffer",
          set_on_probe_fd[app] == 1 &&
          memcmp(&last_set_key, &original_color_key, sizeof(original_color_key)) == 0);
    check("...and is then closed by the framebuffer code (the application's /dev/fb0 descriptor)",
          close_on_probe_fd[app] == 1 && fcntl(app, F_GETFD) < 0);
}

int main(int argc, char **argv)
{
    if (argc != 2) { fprintf(stderr, "usage: %s <scratch-file>\n", argv[0]); return 2; }
    app_path = argv[1];
    real_open_fn = model_open;
    real_close_fn = model_close;

    scenario_probe_failure_with_the_descriptor_open();
    scenario_recycled_after_an_interposed_close();
    scenario_recycle_while_the_obligation_is_live();
    scenario_release_stays_a_pass_through();
    scenario_recycled_number_that_answers_the_probe();
    scenario_release_discharge_failure_is_retried();

    printf("\nGlobal invariant across all scenarios:\n");
    printf("  SETs through a descriptor that did not answer the /dev/fb0 probe: %d\n", total_set_other);
    printf("  closes of a descriptor that did not answer the /dev/fb0 probe:   %d\n", total_close_other);
    check("no unvalidated descriptor was ever written through or closed",
          total_set_other == 0 && total_close_other == 0);
    printf("  (validated SETs=%d, validated closes=%d)\n", total_set_probe, total_close_probe);
    check("the invariant was measured against live traffic, not an idle model",
          total_set_probe >= 4 && total_close_probe >= 4 && total_fb_opens >= 4);
    printf("  (cumulative /dev/fb0 opens attempted by the section: %d)\n", total_fb_opens);

    printf("\nRESULT: %s (%d failed assertion(s))\n", failures ? "FAIL" : "PASS", failures);
    return failures ? 1 : 0;
}
"""


# ------------------------------------------------------------------ reporting


class _ModelReport:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def pytest_terminal_summary(self, terminalreporter) -> None:  # pragma: no cover - pytest hook
        for line in self.lines:
            terminalreporter.write_line(line)


_model_report = _ModelReport()


@pytest.fixture(scope="module", autouse=True)
def _publish_model_run(request):
    request.config.pluginmanager.register(_model_report, "devicezkgui-framebuffer-model")
    yield


# ------------------------------------------------------------------ splice plumbing


def _host_cc() -> str | None:
    return shutil.which("cc") or shutil.which("clang")


def _framebuffer_section() -> str:
    """The shipped framebuffer section, verbatim, located by content anchors."""
    text = PRELOAD.read_text(encoding="utf-8")
    try:
        start = text.index(SECTION_START)
        end = text.index(SECTION_END, start)
    except ValueError:  # pragma: no cover - only on a refactor of the anchored text
        pytest.fail(
            f"{PRELOAD} no longer contains the anchors {SECTION_START!r} .. {SECTION_END!r}. "
            "The framebuffer-ownership guard cannot find the code it is supposed to execute; "
            "repoint the anchors rather than deleting this test."
        )
    section = text[start:end]
    missing = [name for name in REQUIRED_NAMES if name not in section]
    assert not missing, (
        f"the anchored section of {PRELOAD} no longer defines {missing}; the model would silently "
        "exercise less than the shipped framebuffer logic"
    )
    return section.rstrip("\n") + "\n"


def _model_source(section: str) -> str:
    return PRELUDE + "\n" + section + "\n" + DRIVER


def _materialise(source: str, tmp_path: Path, tag: str) -> Path:
    workspace = tmp_path / tag
    workspace.mkdir(parents=True, exist_ok=True)
    scratch = workspace / "scratch.bin"
    if not scratch.exists():
        scratch.write_bytes(b"")
    (workspace / "model.c").write_text(source, encoding="utf-8")
    return workspace


def _run(source: str, tmp_path: Path, tag: str) -> tuple[int, str, str]:
    """Compile and execute the model. Returns (returncode, stdout, stderr)."""
    cc = _host_cc()
    assert cc is not None
    workspace = _materialise(source, tmp_path, tag)
    binary = workspace / "model"
    compiled = subprocess.run(
        [cc, *CC_FLAGS, "-o", str(binary), str(workspace / "model.c")],
        capture_output=True, text=True,
    )
    assert compiled.returncode == 0, (
        f"the spliced model did not compile:\n{compiled.stdout}{compiled.stderr}"
    )
    ran = subprocess.run(
        [str(binary), str(workspace / "scratch.bin")], capture_output=True, text=True
    )
    return ran.returncode, ran.stdout, ran.stderr


def _sabotaged(section: str, needle: str, replacement: str) -> str:
    occurrences = section.count(needle)
    assert occurrences == 1, (
        f"the sabotage anchor occurs {occurrences} times in {PRELOAD}, expected exactly 1. The arm "
        "this guard exercises was rewritten; update the anchor instead of weakening the guard.\n"
        f"anchor:\n{needle}"
    )
    return section.replace(needle, replacement)


def _digest(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ------------------------------------------------------------------ tests


def test_shipped_framebuffer_section_keeps_the_descriptor_ownership_invariant(tmp_path):
    """The shipped bytes must pass every scenario, with a live-traffic sanity check."""
    if _host_cc() is None:
        pytest.skip("no host C compiler (cc/clang) available")
    if not PRELOAD.is_file():
        pytest.skip(f"missing {PRELOAD}")

    returncode, stdout, stderr = _run(_model_source(_framebuffer_section()), tmp_path, "shipped")
    for line in stdout.splitlines():
        if line.startswith("Scenario") or line.startswith("RESULT"):
            _model_report.lines.append(f"devicezkgui-framebuffer[{line}]")
    assert returncode == 0, (
        f"the shipped framebuffer section failed the ownership model (exit {returncode}):\n"
        f"{stdout}{stderr}"
    )
    assert "RESULT: PASS (0 failed assertion(s))" in stdout, stdout
    assert "no unvalidated descriptor was ever written through or closed" in stdout


@pytest.mark.parametrize("label,needle,replacement,expected_failure", SABOTAGES,
                         ids=[s[0][:48] for s in SABOTAGES])
def test_guard_bites_on_each_regression(tmp_path, label, needle, replacement, expected_failure):
    """A sabotaged section must be caught, by the named assertion, so the guard is not a no-op."""
    if _host_cc() is None:
        pytest.skip("no host C compiler (cc/clang) available")
    if not PRELOAD.is_file():
        pytest.skip(f"missing {PRELOAD}")

    section = _framebuffer_section()
    sabotaged = _sabotaged(section, needle, replacement)
    assert sabotaged != section, f"the sabotage {label!r} changed nothing"

    returncode, stdout, stderr = _run(_model_source(sabotaged), tmp_path, f"sabotage-{label[:24]}")
    assert returncode != 0, (
        f"the guard did NOT bite on {label!r}: the model exited 0, so this regression would ship "
        f"unnoticed.\n{stdout}{stderr}"
    )
    assert f"[FAIL] {expected_failure}" in stdout, (
        f"{label!r} was caught, but not by the expected assertion {expected_failure!r}:\n{stdout}"
    )


def test_anchored_section_is_a_verbatim_slice_of_the_shipped_file(tmp_path):
    """The model input must be the shipped bytes, and the harness must stay device- and HOME-free."""
    if not PRELOAD.is_file():
        pytest.skip(f"missing {PRELOAD}")

    section = _framebuffer_section()
    text = PRELOAD.read_text(encoding="utf-8")
    assert section in text, "the modelled section is not a verbatim slice of the shipped file"
    assert section.count(SECTION_START) == 1
    # A splice that collapsed to nothing would compile and pass trivially; refuse it.
    assert len(section.splitlines()) > 60, f"the anchored section looks truncated ({len(section)} bytes)"

    harness = PRELUDE + DRIVER
    for token in ("getenv(", "system(", "popen(", "fork(", "exec", "HOME", "adb", "hidg"):
        assert token not in harness, f"the model must stay pure computation; it contains {token!r}"
    assert '"/dev/null"' in PRELUDE, (
        "the model must redirect the /dev/fb0 open onto /dev/null; it must never open the real device"
    )
    assert not Path("/dev/fb0").exists(), (
        "this host unexpectedly has a real framebuffer; the model's simulation would no longer be "
        "describing a device-free host"
    )


def test_model_never_touches_the_real_home(tmp_path):
    """Containment: the model runs under an isolated HOME and leaves the real state file untouched."""
    if _host_cc() is None:
        pytest.skip("no host C compiler (cc/clang) available")
    if not PRELOAD.is_file():
        pytest.skip(f"missing {PRELOAD}")

    state = REAL_HOME / ".ghostdeck" / "state.json"
    before = _digest(state)
    isolated = tmp_path / "home"
    isolated.mkdir(parents=True, exist_ok=True)
    assert isolated.resolve() != REAL_HOME

    cc = _host_cc()
    assert cc is not None
    workspace = _materialise(_model_source(_framebuffer_section()), tmp_path, "containment")
    binary = workspace / "model"
    env = dict(os.environ)
    env["HOME"] = str(isolated)
    compiled = subprocess.run(
        [cc, *CC_FLAGS, "-o", str(binary), str(workspace / "model.c")],
        capture_output=True, text=True, env=env,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    ran = subprocess.run([str(binary), str(workspace / "scratch.bin")],
                         capture_output=True, text=True, env=env)
    assert ran.returncode == 0, ran.stdout + ran.stderr

    assert _digest(state) == before, "the framebuffer model wrote into the real HOME state file"

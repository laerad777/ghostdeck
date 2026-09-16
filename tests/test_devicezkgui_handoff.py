"""Executable proof that the proxy and the preload agree on the environment handoff between them.

`device/d200-zkgui-proxy.c` and `device/d200-zkgui-preload.c` are two translation units that only ever
meet through the environment of a forked child. The proxy chooses a private descriptor for the
preload's diagnostics, clears `FD_CLOEXEC` so it survives the `exec`, and exports its *number* as a
string; the preload reads that name, parses the digits, re-validates the descriptor against the file
table, re-applies `FD_CLOEXEC`, and consumes the variable. Three more names travel the same way —
`D200_VIDEO_UNDER_STUDIO` (which arms the framebuffer black-out), and `D200_HIDG0_SOCKET` /
`D200_HIDG1_SOCKET` (which redirect `/dev/hidg0` / `/dev/hidg1`) — plus `LD_PRELOAD`, which is how the
interposer gets loaded at all.

Nothing in the repository executed any of that. A rename on either side, or a dropped flag in the
proxy's `open`, compiles cleanly, links cleanly, and silently disables the black-out, the HID
redirection and every diagnostic at once — the same silent-no-op shape as B-127, and the same
"each side only ever verified against itself" shape the repo already documented for the JPEG cap.

So this file follows the convention established by `tests/test_devicecolor_conformance.py`: splice the
shipped text **verbatim** and *execute* it. Both sides of the handoff are compiled into one program,
a real **pipe** stands in for the diagnostic sink, and the real `prepare_usb_diagnostic()` from the
proxy is driven straight into the real `initialize_usb_diagnostic()` from the preload, followed by a
real call through the interposer's entry point (`d200_system_properties_set_string`). The verdict is
therefore about the two files agreeing, not about either file agreeing with this test.

Two boundaries, stated up front so nobody over-reads the result:

* Only `D200_USB_DIAGNOSTIC_FD` can be exercised end to end, because it is the only one of the four
  names that carries a value this host can supply and observe. The other three are covered by
  `test_every_name_the_preload_reads_is_a_name_the_proxy_sets`, which is a *textual contract* check —
  deliberately the weaker kind, and named as such.
* `resolve_symbols()` is `dlsym(RTLD_NEXT, ...)` plumbing, not part of the handoff: the model wires
  `real_open_fn` / `real_close_fn` / `real_set_string_fn` to the model's equivalents (one third of the
  prelude's whole reason to exist). Everything else spliced here is the shipped text, byte for byte.

No device, no `adb`, no fork, no `exec`: the model is pure computation over `pipe`/`open`/`fcntl`/
`close`, it never reads `HOME`, and it never spawns a process.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROXY = ROOT / "device" / "d200-zkgui-proxy.c"
PRELOAD = ROOT / "device" / "d200-zkgui-preload.c"
REAL_HOME = Path(os.path.expanduser("~")).resolve()

CC_FLAGS = ("-O1", "-Wall", "-Wextra", "-pthread")

# Content anchors: line numbers would silently splice the wrong text after any edit above a section.
PRELOAD_CTOR_START = "__attribute__((constructor)) static void initialize_usb_diagnostic(void)"
PRELOAD_CTOR_END = "__attribute__((destructor)) static void close_usb_diagnostic(void)"
PRELOAD_USB_START = "static const char *usb_value_class(const char *value)"
PRELOAD_USB_END = "\nint close(int fd)"
PRELOAD_CTOR_NAMES = ("initialize_usb_diagnostic",)
PRELOAD_USB_NAMES = ("usb_value_class", "usb_request_time", "usb_diagnostic",
                     "d200_system_properties_set_string")
PROXY_PREPARE_START = "static int prepare_usb_diagnostic(void)"
PROXY_PREPARE_END = "static int launch_child(struct state *s)"
PROXY_PREPARE_NAMES = ("prepare_usb_diagnostic",)

# The one assertion each sabotage must trip. Exact substrings of the model's own output.
HANDOFF_ACCEPTED = "the preload accepted the descriptor the proxy prepared"
LOW_FD_REFUSED = "the proxy refused a sink at or below stderr and exported nothing"
RECORD_DELIVERED = "a diagnostic record came back out of the sink end to end"

SABOTAGES = (
    (
        "proxy exports the number under a name the preload does not read",
        '    if (setenv("D200_USB_DIAGNOSTIC_FD", descriptor, 1)) goto failed;',
        '    if (setenv("D200_USB_DIAG_FD", descriptor, 1)) goto failed;',
        HANDOFF_ACCEPTED,
    ),
    (
        "preload reads a name the proxy does not set",
        '    const char *text = getenv("D200_USB_DIAGNOSTIC_FD");',
        '    const char *text = getenv("D200_USB_DIAG_FD");',
        HANDOFF_ACCEPTED,
    ),
    (
        "proxy opens the sink without O_NONBLOCK, so the preload must reject it",
        '    fd = open("/proc/self/fd/2", O_WRONLY | O_NONBLOCK | O_CLOEXEC | O_APPEND);',
        '    fd = open("/proc/self/fd/2", O_WRONLY | O_CLOEXEC | O_APPEND);',
        HANDOFF_ACCEPTED,
    ),
    (
        "proxy drops its own guard against a sink at or below stderr",
        "    if (fd <= STDERR_FILENO || fcntl(fd, F_SETFD, 0)) goto failed;",
        "    if (fcntl(fd, F_SETFD, 0)) goto failed;",
        LOW_FD_REFUSED,
    ),
)

PRELUDE = r"""/* Environment-handoff model, prelude.
 *
 * The shipped constructor and the shipped USB record path are spliced in verbatim after this text,
 * so every name, type and global they use is declared here exactly as their translation units have
 * it. The single substitution is resolve_symbols(), documented below: it is dlsym plumbing rather
 * than handoff logic, and it cannot run as written because the model's open/close are model code.
 *
 * The sink is a real pipe. The proxy's real prepare_usb_diagnostic() opens it through the model's
 * open() (which is how "/proc/self/fd/2" becomes a host pipe write end), and the preload's real
 * initialize_usb_diagnostic() validates the resulting descriptor with real fcntl() calls.
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
#include <time.h>
#include <unistd.h>

/* The shipped constructor must be callable per scenario here, so the attribute is neutralised AFTER
 * every system header, and only for the spliced text that follows this line. */
#define __attribute__(x)

typedef int (*open_fn)(const char *, int, ...);
typedef int (*close_fn)(int);
typedef int (*set_string_fn)(const char *, const char *);

/* Same names, types and linkage as the shipped translation units. */
static open_fn real_open_fn;
static close_fn real_close_fn;
static set_string_fn real_set_string_fn;
static int usb_diagnostic_fd = -1;
static pthread_once_t init_once = PTHREAD_ONCE_INIT;

/* ---- model instrumentation (never referenced by the spliced text) ---- */
static int sink_read_fd = -1;
static int sink_write_fd = -1;
static int model_force_low_fd;      /* make the prepared sink land on stderr */
static int model_open_calls;
static int model_set_string_calls;
static int model_n_closed;
static int model_closed_fds[32];
static int failures;

static void check(const char *what, int ok)
{
    printf("  [%s] %s\n", ok ? "PASS" : "FAIL", what);
    if (!ok) ++failures;
}

static void model_record_close(int fd)
{
    if (model_n_closed < 32) model_closed_fds[model_n_closed++] = fd;
}

static int model_close(int fd)
{
    model_record_close(fd);
    return close(fd);
}

static int model_set_string(const char *key, const char *value)
{
    (void)key;
    (void)value;
    ++model_set_string_calls;
    return 0;
}

/* The host substitution, delimited to exactly one path.
 *
 * `prepare_usb_diagnostic()` runs in the PROXY process, before `LD_PRELOAD` names this interposer to
 * the zkgui child, so in production its `open()`/`close()` are plain libc calls - not interposed code.
 * It opens `/proc/self/fd/2` to get an independent description of the diagnostic sink, which is a
 * Linux-only path: this host has no `/proc`, so the success path of the handoff is unreachable unless
 * the file table is emulated. That emulation is what the interposed `open()` below does and it is the
 * ONLY substitution in this model, alongside resolve_symbols(); every flag the caller requests is
 * applied to the returned descriptor, so a caller that forgets `O_NONBLOCK` really does get a
 * descriptor without it, and the shipped preload acceptance test is what rejects it. */
static int model_sink_open(int flags)
{
    int fd, status = 0;
    if (sink_write_fd < 0) { errno = ENOENT; return -1; }
    if (model_force_low_fd) {
        if (dup2(sink_write_fd, STDERR_FILENO) < 0) return -1;
        fd = STDERR_FILENO;
    } else {
        fd = dup(sink_write_fd);
        if (fd < 0) return -1;
    }
    if (flags & O_NONBLOCK) status |= O_NONBLOCK;
    if (flags & O_APPEND) status |= O_APPEND;
    if (fcntl(fd, F_SETFL, status) < 0) { (void)close(fd); return -1; }
    if (fcntl(fd, F_SETFD, (flags & O_CLOEXEC) ? FD_CLOEXEC : 0) < 0) {
        (void)close(fd);
        return -1;
    }
    return fd;
}

/* Bare `open` in the spliced proxy text binds here, exactly as the preload's own interposer binds for
 * the child. Every other path is forwarded untouched through `openat`, which is not interposed. */
int open(const char *path, int flags, ...)
{
    va_list ap;
    int mode = 0;
    if ((flags & O_CREAT) != 0) {
        va_start(ap, flags);
        mode = va_arg(ap, int);
        va_end(ap);
    }
    if (path != NULL && strcmp(path, "/proc/self/fd/2") == 0) {
        ++model_open_calls;
        return model_sink_open(flags);
    }
    return openat(AT_FDCWD, path, flags, (mode_t)mode);
}

/* resolve_symbols() in the shipped preload is dlsym(RTLD_NEXT, ...) plumbing. The model performs the
 * same wiring with the model's equivalents, which is what lets the shipped acceptance logic run
 * without a dynamic-loader chain. This and the `open` above are the model's only two substitutions. */
static void resolve_symbols(void)
{
    real_open_fn = open;
    real_close_fn = model_close;
    real_set_string_fn = model_set_string;
}

static void state_reset(void)
{
    usb_diagnostic_fd = -1;
    init_once = (pthread_once_t)PTHREAD_ONCE_INIT;
    model_force_low_fd = 0;
    model_open_calls = 0;
    model_set_string_calls = 0;
    model_n_closed = 0;
}

static void sink_setup(void)
{
    int pair[2];
    if (pipe(pair) != 0) { perror("pipe"); exit(2); }
    sink_read_fd = pair[0];
    sink_write_fd = pair[1];
    (void)fcntl(sink_read_fd, F_SETFL, O_NONBLOCK);
}

static void sink_teardown(void)
{
    if (sink_read_fd >= 0) (void)close(sink_read_fd);
    if (sink_write_fd >= 0) (void)close(sink_write_fd);
    sink_read_fd = sink_write_fd = -1;
}

/* Drain a descriptor, splitting the stream on the record terminator. The caller must have put the
 * descriptor in non-blocking mode: under a sabotage the preload legitimately writes nothing, and a
 * blocking read here would hang the model instead of failing the assertion. Every read end this model
 * reads from is set O_NONBLOCK at creation for exactly that reason. */
static int drain_from(int fd, char *buffer, size_t capacity, char *records[], int max_records)
{
    ssize_t n = read(fd, buffer, capacity - 1);
    int count = 0, start = 0, i;
    if (n <= 0) { buffer[0] = '\0'; return 0; }
    buffer[n] = '\0';
    for (i = 0; i < (int)n; ++i) {
        if (buffer[i] == '\n' && count < max_records) {
            buffer[i] = '\0';
            records[count++] = buffer + start;
            start = i + 1;
        }
    }
    return count;
}

static int closed_somewhere(int fd)
{
    int i;
    for (i = 0; i < model_n_closed; ++i) if (model_closed_fds[i] == fd) return 1;
    return 0;
}
"""

DRIVER = r"""/* Environment-handoff model driver.
 *
 * Every scenario drives the shipped functions: prepare_usb_diagnostic() from the proxy and
 * initialize_usb_diagnostic() / usb_diagnostic() / d200_system_properties_set_string() from the
 * preload. The model supplies only the file table and the sink.
 */
static void header(const char *text)
{
    printf("%s\n", text);
}

/* 1. The handoff that has to work: proxy prepares a number, preload accepts that exact number, and a
 *    real record travels out of the sink. */
static void scenario_handoff_is_agreed(void)
{
    static char buffer[2048];
    char *records[8];
    const char *exported;
    int prepared, accepted, count;
    header("Scenario 1: the proxy prepares a descriptor and the preload accepts that exact number");
    sink_setup();
    state_reset();
    (void)unsetenv("D200_USB_DIAGNOSTIC_FD");

    prepared = prepare_usb_diagnostic();
    check("the proxy prepared a sink above the standard descriptors", prepared > STDERR_FILENO);
    exported = getenv("D200_USB_DIAGNOSTIC_FD");
    check("the proxy exported the number under the name the preload reads",
          exported != NULL && atoi(exported) == prepared);
    check("the proxy cleared FD_CLOEXEC so the sink survives the exec",
          prepared >= 0 && fcntl(prepared, F_GETFD) == 0);

    initialize_usb_diagnostic();
    accepted = usb_diagnostic_fd;
    check("the preload accepted the descriptor the proxy prepared", accepted == prepared);
    check("the preload consumed the handoff name (single use)",
          getenv("D200_USB_DIAGNOSTIC_FD") == NULL);
    check("the preload re-applied FD_CLOEXEC so unrelated helpers cannot inherit the sink",
          accepted >= 0 && (fcntl(accepted, F_GETFD) & FD_CLOEXEC) != 0);

    usb_diagnostic("hid", "suppressed", "null", 0, 0);
    count = drain_from(sink_read_fd, buffer, sizeof(buffer), records, 8);
    check("a diagnostic record came back out of the sink end to end", count == 1);
    check("the record is the bounded USB-config record",
          count == 1 &&
          strncmp(records[0], "{\"event\":\"d200-usb-config\"",
                  strlen("{\"event\":\"d200-usb-config\"")) == 0 &&
          strlen(records[0]) < 384);

    sink_teardown();
}

/* 2. The real interposer entry point, through the handoff, producing both of its records. */
static void scenario_interposer_records_travel_the_handoff(void)
{
    static char buffer[2048];
    char *records[8];
    int count, forwarded;
    header("\nScenario 2: the interposer's own entry point writes through the handoff");
    sink_setup();
    state_reset();
    (void)unsetenv("D200_USB_DIAGNOSTIC_FD");
    (void)prepare_usb_diagnostic();
    initialize_usb_diagnostic();

    (void)d200_system_properties_set_string("sys.usb.config", "hid");   /* suppressed */
    forwarded = model_set_string_calls;
    count = drain_from(sink_read_fd, buffer, sizeof(buffer), records, 8);
    check("a suppressed request produced exactly its two bounded records", count == 2);
    check("the suppressed request was never forwarded to SystemProperties", forwarded == 0);
    check("both records name the suppressed disposition",
          count == 2 && strstr(records[0], "\"disposition\":\"suppressed\"") != NULL &&
          strstr(records[1], "\"disposition\":\"suppressed\"") != NULL);
    check("the pair is request-then-return with a result only on the return record",
          count == 2 && strstr(records[0], "\"phase\":\"request\"") != NULL &&
          strstr(records[0], "\"result\":null") != NULL &&
          strstr(records[1], "\"phase\":\"return\"") != NULL &&
          strstr(records[1], "\"result\":null") == NULL);

    (void)d200_system_properties_set_string("sys.usb.config", "adb");   /* forwarded */
    forwarded = model_set_string_calls;
    count = drain_from(sink_read_fd, buffer, sizeof(buffer), records, 8);
    check("a forwarded request was forwarded exactly once", forwarded == 1);
    check("a forwarded request also produced its two bounded records", count == 2);
    check("the forwarded records name the forwarded disposition and a real result",
          count == 2 && strstr(records[0], "\"disposition\":\"forwarded\"") != NULL &&
          strstr(records[1], "\"result\":0") != NULL);

    sink_teardown();
}

/* 3. The proxy's own guard: a sink at or below stderr must be refused, not exported. */
static void scenario_proxy_refuses_a_low_sink(void)
{
    int prepared, saved, stderr_still_open;
    header("\nScenario 3: the proxy refuses a sink at or below stderr");
    sink_setup();
    state_reset();
    (void)unsetenv("D200_USB_DIAGNOSTIC_FD");
    saved = dup(STDERR_FILENO);            /* this scenario really does close fd 2 */
    model_force_low_fd = 1;
    prepared = prepare_usb_diagnostic();
    model_force_low_fd = 0;
    stderr_still_open = fcntl(STDERR_FILENO, F_GETFD) >= 0;
    if (saved >= 0) { (void)dup2(saved, STDERR_FILENO); (void)close(saved); }
    check("the proxy refused a sink at or below stderr and exported nothing",
          prepared == -1 && getenv("D200_USB_DIAGNOSTIC_FD") == NULL);
    check("the proxy closed the descriptor it had opened before refusing", !stderr_still_open);
    sink_teardown();
}

/* 4. A stale name inherited from somewhere else: the preload must reject it and consume it. */
static void scenario_stale_name_is_rejected_and_consumed(void)
{
    static char buffer[1024];
    char *records[4];
    int count;
    header("\nScenario 4: a stale or foreign handoff name is rejected, consumed, and never written to");
    sink_setup();
    state_reset();

    (void)setenv("D200_USB_DIAGNOSTIC_FD", "-3", 1);      /* not a number it accepts */
    initialize_usb_diagnostic();
    check("a negative handoff number was rejected", usb_diagnostic_fd == -1);
    check("the rejected name was consumed anyway (no retry on the next load)",
          getenv("D200_USB_DIAGNOSTIC_FD") == NULL);

    state_reset();
    (void)setenv("D200_USB_DIAGNOSTIC_FD", "0", 1);       /* stderr: never a sink */
    initialize_usb_diagnostic();
    check("a handoff number at or below stderr was rejected", usb_diagnostic_fd == -1);

    state_reset();
    (void)setenv("D200_USB_DIAGNOSTIC_FD", "99999999999999999999", 1);
    initialize_usb_diagnostic();
    check("an out-of-range handoff number was rejected", usb_diagnostic_fd == -1);

    state_reset();
    (void)setenv("D200_USB_DIAGNOSTIC_FD", "", 1);        /* empty: the proxy never writes this */
    initialize_usb_diagnostic();
    check("an empty handoff value was rejected", usb_diagnostic_fd == -1);

    usb_diagnostic("hid", "suppressed", "null", 0, 0);
    count = drain_from(sink_read_fd, buffer, sizeof(buffer), records, 4);
    check("a rejected handoff produced no record at all", count == 0);
    sink_teardown();
}

/* 5. The documented threat-model boundary: the name is not authenticated. */
static void scenario_unauthenticated_name_is_honoured(void)
{
    static char buffer[1024];
    char *records[4];
    int foreign[2], named, accepted, count;
    header("\nScenario 5: boundary -- an unrelated descriptor already sitting at the named number");
    sink_setup();
    state_reset();
    if (pipe(foreign) != 0) { perror("pipe"); exit(2); }
    (void)fcntl(foreign[0], F_SETFL, O_NONBLOCK);
    named = dup(foreign[1]);                /* writable, nonblocking, NOT our sink */
    (void)fcntl(named, F_SETFL, O_NONBLOCK);
    check("the model placed an unrelated writable nonblocking descriptor at a number above stderr",
          named > STDERR_FILENO && (fcntl(named, F_GETFL) & O_NONBLOCK) != 0);

    (void)snprintf(buffer, sizeof(buffer), "%d", named);
    (void)setenv("D200_USB_DIAGNOSTIC_FD", buffer, 1);
    initialize_usb_diagnostic();
    accepted = usb_diagnostic_fd;
    check("the preload honours a name the environment supplies, not one the proxy chose",
          accepted == named);
    check("the accepted descriptor is one the preload does not own (it only read the name)",
          accepted >= 0 && !closed_somewhere(accepted));

    usb_diagnostic("hid", "suppressed", "null", 0, 0);
    count = drain_from(foreign[0], buffer, sizeof(buffer), records, 4);
    check("diagnostics went to the descriptor the environment named", count == 1);
    count = drain_from(sink_read_fd, buffer, sizeof(buffer), records, 4);
    check("diagnostics did not go to the proxy's sink", count == 0);
    header("  boundary: the name is not authenticated; what closes this in practice is that only the");
    header("            proxy sets it, in its own forked child, to a descriptor it opened one line");
    header("            earlier and then never shares. An operator who can set this name can also");
    header("            set LD_PRELOAD, so the boundary is the threat model, not this check.");

    (void)close(named);
    (void)close(foreign[0]);
    (void)close(foreign[1]);
    sink_teardown();
}

int main(void)
{
    scenario_handoff_is_agreed();
    scenario_interposer_records_travel_the_handoff();
    scenario_proxy_refuses_a_low_sink();
    scenario_stale_name_is_rejected_and_consumed();
    scenario_unauthenticated_name_is_honoured();

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
    request.config.pluginmanager.register(_model_report, "devicezkgui-handoff-model")
    yield


# ------------------------------------------------------------------ splice plumbing


def _host_cc() -> str | None:
    return shutil.which("cc") or shutil.which("clang")


def _slice(path: Path, start: str, end: str, names: tuple[str, ...]) -> str:
    """One verbatim region of a shipped translation unit, located by content anchors."""
    text = path.read_text(encoding="utf-8")
    try:
        begin = text.index(start)
        finish = text.index(end, begin)
    except ValueError:  # pragma: no cover - only on a refactor of the anchored text
        pytest.fail(
            f"{path} no longer contains the anchors {start!r} .. {end!r}. This guard cannot find the "
            "code it is supposed to execute; repoint the anchors rather than deleting the test."
        )
    section = text[begin:finish]
    missing = [name for name in names if name not in section]
    assert not missing, (
        f"the anchored region of {path} no longer defines {missing}; the model would silently "
        "exercise less than the shipped handoff"
    )
    return section.rstrip("\n") + "\n"


def _preload_ctor() -> str:
    return _slice(PRELOAD, PRELOAD_CTOR_START, PRELOAD_CTOR_END, PRELOAD_CTOR_NAMES)


def _preload_usb_helpers() -> str:
    return _slice(PRELOAD, PRELOAD_USB_START, PRELOAD_USB_END, PRELOAD_USB_NAMES)


def _proxy_prepare() -> str:
    return _slice(PROXY, PROXY_PREPARE_START, PROXY_PREPARE_END, PROXY_PREPARE_NAMES)


def _model_source(ctor: str, helpers: str, prepare: str) -> str:
    return PRELUDE + "\n" + ctor + "\n" + helpers + "\n" + prepare + "\n" + DRIVER


def _severed() -> tuple[str, str, str]:
    return _preload_ctor(), _preload_usb_helpers(), _proxy_prepare()


def _run(source: str, tmp_path: Path, tag: str) -> tuple[int, str, str]:
    cc = _host_cc()
    assert cc is not None
    workspace = tmp_path / re.sub(r"[^A-Za-z0-9_.-]", "_", tag)
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "model.c").write_text(source, encoding="utf-8")
    binary = workspace / "model"
    compiled = subprocess.run(
        [cc, *CC_FLAGS, "-o", str(binary), str(workspace / "model.c")],
        capture_output=True, text=True,
    )
    assert compiled.returncode == 0, (
        f"the spliced handoff model did not compile:\n{compiled.stdout}{compiled.stderr}"
    )
    ran = subprocess.run([str(binary)], capture_output=True, text=True)
    return ran.returncode, ran.stdout, ran.stderr


def _sabotaged(section: str, needle: str, replacement: str, origin: Path) -> str:
    occurrences = section.count(needle)
    assert occurrences == 1, (
        f"the sabotage anchor occurs {occurrences} times in {origin}, expected exactly 1. The arm this "
        "guard exercises was rewritten; update the anchor instead of weakening the guard.\n"
        f"anchor:\n{needle}"
    )
    return section.replace(needle, replacement)


ENV_READ = re.compile(r'getenv\("([A-Za-z_][A-Za-z0-9_]*)"\)')
ENV_SET = re.compile(r'setenv\("([A-Za-z_][A-Za-z0-9_]*)"')


# ------------------------------------------------------------------ tests


def test_proxy_and_preload_agree_on_the_handoff_end_to_end(tmp_path):
    """Both shipped sides, executed, with a real record travelling through the real handoff."""
    if _host_cc() is None:
        pytest.skip("no host C compiler (cc/clang) available")
    if not PRELOAD.is_file() or not PROXY.is_file():
        pytest.skip(f"missing {PRELOAD} or {PROXY}")

    returncode, stdout, stderr = _run(_model_source(*_severed()), tmp_path, "shipped")
    for line in stdout.splitlines():
        if line.startswith("Scenario") or line.startswith("RESULT"):
            _model_report.lines.append(f"devicezkgui-handoff[{line}]")
    assert returncode == 0, (
        f"the shipped handoff failed the model (exit {returncode}):\n{stdout}{stderr}"
    )
    assert "RESULT: PASS (0 failed assertion(s))" in stdout, stdout


@pytest.mark.parametrize("label,needle,replacement,expected_failure", SABOTAGES,
                         ids=[s[0][:52] for s in SABOTAGES])
def test_guard_bites_on_each_handoff_regression(tmp_path, label, needle, replacement,
                                                expected_failure):
    """Each side's half of the contract must be individually load-bearing."""
    if _host_cc() is None:
        pytest.skip("no host C compiler (cc/clang) available")
    if not PRELOAD.is_file() or not PROXY.is_file():
        pytest.skip(f"missing {PRELOAD} or {PROXY}")

    ctor, helpers, prepare = _severed()
    origin = PRELOAD if needle in ctor or needle in helpers else PROXY
    if needle in prepare:
        prepare = _sabotaged(prepare, needle, replacement, PROXY)
    elif needle in ctor:
        ctor = _sabotaged(ctor, needle, replacement, PRELOAD)
    elif needle in helpers:
        helpers = _sabotaged(helpers, needle, replacement, PRELOAD)
    else:
        pytest.fail(f"the sabotage {label!r} matched neither translation unit")

    returncode, stdout, stderr = _run(_model_source(ctor, helpers, prepare), tmp_path,
                                     f"sabotage-{label[:24]}")
    assert returncode != 0, (
        f"the guard did NOT bite on {label!r} ({origin.name}): the model exited 0, so this regression "
        f"would ship unnoticed.\n{stdout}{stderr}"
    )
    assert f"[FAIL] {expected_failure}" in stdout, (
        f"{label!r} was caught, but not by the expected assertion {expected_failure!r}:\n{stdout}"
    )


def test_every_name_the_preload_reads_is_a_name_the_proxy_sets():
    """The textual half of the contract, for the three names this host cannot execute.

    `D200_VIDEO_UNDER_STUDIO` and the two socket names are consumed only inside a forked, `exec`ed
    child on the deck, so no host test can drive them. This check is deliberately weaker than the
    executable one above and is named as such: it proves the *names* still line up, nothing more.
    """
    if not PRELOAD.is_file() or not PROXY.is_file():
        pytest.skip(f"missing {PRELOAD} or {PROXY}")

    preload_text = PRELOAD.read_text(encoding="utf-8")
    proxy_text = PROXY.read_text(encoding="utf-8")
    read_by_preload = set(ENV_READ.findall(preload_text))
    set_by_proxy = set(ENV_SET.findall(proxy_text))

    assert read_by_preload, "the preload reads no environment names at all; the scan is broken"
    assert set_by_proxy, "the proxy sets no environment names at all; the scan is broken"
    missing = sorted(read_by_preload - set_by_proxy)
    assert not missing, (
        f"the preload reads {missing} but the proxy never sets it: the interposer would silently "
        "disable the feature that name carries. Renaming one side without the other compiles and "
        "links cleanly, which is exactly why this check exists."
    )
    # The mechanism itself must stay intact in both directions.
    assert 'setenv("LD_PRELOAD", s->preload, 1)' in proxy_text, (
        "the proxy no longer loads the interposer through LD_PRELOAD"
    )
    assert proxy_text.count('unsetenv("LD_PRELOAD")') >= 2, (
        "the proxy must clear LD_PRELOAD before setting its own, and must not let the interposer "
        "load into the colour agent's child"
    )



def test_model_is_device_free_and_contains_the_shipped_text(tmp_path):
    """The model must be a verbatim splice of both files, and must stay off the real device."""
    if not PRELOAD.is_file() or not PROXY.is_file():
        pytest.skip(f"missing {PRELOAD} or {PROXY}")

    ctor, helpers, prepare = _severed()
    for section, path in ((ctor, PRELOAD), (helpers, PRELOAD), (prepare, PROXY)):
        assert section in path.read_text(encoding="utf-8"), (
            f"the modelled text is not a verbatim slice of {path}"
        )
        assert len(section.splitlines()) > 8, f"the anchored region of {path} looks truncated"

    harness = PRELUDE + DRIVER
    # Process-spawning APIs would let the harness drive the real device. `"adb"` is deliberately NOT
    # in this list: it occurs legitimately as the USB-config VALUE in the spliced entry point's
    # exercise, and forbidding the substring would forbid the protocol itself.
    for token in ("getenv(\"HOME\")", "system(", "popen(", "fork(", "execl", "execv", "posix_spawn"):
        assert token not in harness, f"the model must stay pure computation; it contains {token!r}"
    for node in ("/dev/hidg0", "/dev/hidg1", "/dev/fb0", "/cache", "/data/local"):
        assert node not in harness, (
            f"the model must never name the real device node {node!r}; only the spliced shipped text "
            "may mention the paths it is responsible for"
        )
    assert harness.count('strcmp(path, "/proc/self/fd/2")') == 1, (
        "the model must substitute exactly the one host-missing path; a second special case would "
        "mean the model is reimplementing more of the file table than this host lacks"
    )
    assert "openat(AT_FDCWD" in PRELUDE, (
        "the interposed open must forward every other path untouched through openat"
    )
    assert "pipe(pair)" in PRELUDE, "the sink must be a real pipe, not a simulation"
    assert "(void)fcntl(foreign[0], F_SETFL, O_NONBLOCK);" in DRIVER, (
        "every pipe read end in this model must be non-blocking: a sabotage makes the preload write "
        "nothing, and a blocking read would hang the guard instead of failing it"
    )


def test_model_never_touches_the_real_home(tmp_path):
    """Containment (BRIEF rule 7): the model runs under an isolated HOME."""
    if _host_cc() is None:
        pytest.skip("no host C compiler (cc/clang) available")
    if not PRELOAD.is_file() or not PROXY.is_file():
        pytest.skip(f"missing {PRELOAD} or {PROXY}")

    state = REAL_HOME / ".ghostdeck" / "state.json"
    before = state.read_bytes() if state.is_file() else None
    isolated = tmp_path / "home"
    isolated.mkdir(parents=True, exist_ok=True)
    assert isolated.resolve() != REAL_HOME

    cc = _host_cc()
    assert cc is not None
    workspace = tmp_path / "containment"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "model.c").write_text(_model_source(*_severed()), encoding="utf-8")
    binary = workspace / "model"
    env = dict(os.environ)
    env["HOME"] = str(isolated)
    compiled = subprocess.run(
        [cc, *CC_FLAGS, "-o", str(binary), str(workspace / "model.c")],
        capture_output=True, text=True, env=env,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    ran = subprocess.run([str(binary)], capture_output=True, text=True, env=env)
    assert ran.returncode == 0, ran.stdout + ran.stderr

    after = state.read_bytes() if state.is_file() else None
    assert after == before, "the handoff model wrote into the real HOME state file"

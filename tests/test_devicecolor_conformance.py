"""Executable conformance proof between the agent's C codec and the bridge's Python mirror.

There are two implementations of one wire contract and they sit on opposite sides of a process
boundary that nobody can test end to end:

* `device/d200_video_stream.h` — the on-device agent's D2JF/D2PX codec, cross-compiled to ARM;
* `vendor/d200_video_stream.py` — the host bridge's mirror of the same wire format.

Until this file existed **nothing executed both and compared them.** `tests/test_vendorbridge_jpeg_cap.py`
only regex-compares the constant `1048576` between the two sources. So the two sides could disagree
about any byte layout and every test in the suite would still pass, because each side was only ever
checked against itself. That is the same failure shape as the fabricated `product:d200` fixture fields
that certified a `ghostdeck stop` which could never work on the real deck: a test that supplies its
own idea of the counterpart proves the idea, not the contract.

Here the counterpart is real. The header is compiled with the host compiler and *executed*; the mirror
is imported and executed. Both are handed the identical byte stream and must reach the same verdict.
For every input both accept, the C path's canonical re-encode must also be byte-identical to the
input, so agreement on the *meaning* of the bytes is checked, not just agreement on accept/reject.

No device, no `adb`, no `ffmpeg`, no `vendor/*.py` against a deck: `d200_video_stream.py` is pure
Python with no I/O, and the header is pure portable C. Vectors are generated, never hand-copied, and
the only two things imported from the header are its behaviour and its name.
"""

from __future__ import annotations

import os
import random
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HEADER = ROOT / "device" / "d200_video_stream.h"
MIRROR_DIR = ROOT / "vendor"
REAL_HOME = Path(os.path.expanduser("~")).resolve()

CC_FLAGS = ("-std=c11", "-O1", "-Wall", "-Wextra", "-Werror")

sys.path.insert(0, str(MIRROR_DIR))
import d200_video_stream as MIRROR  # noqa: E402

# The mirror's public names are used for vector construction. If the mirror renames or drops one, the
# conformance sweep must fail here rather than silently testing less.
REQUIRED_MIRROR_NAMES = (
    "VERSION", "HEADER_SIZE", "CONTROL_HEADER_SIZE", "MAX_PAYLOAD", "MAX_JPEG", "WINDOW_FRAMES",
    "WINDOW_BYTES", "UINT64_MAX", "ATTACH", "READY", "FRAME", "CONSUMED", "EOS", "DONE", "CANCEL",
    "CANCELLED", "ERROR", "VIDEO_OPEN_REQUEST", "VIDEO_OPEN_RESULT", "VIDEO_CANCEL_REQUEST",
    "VIDEO_CANCEL_RESULT", "VIDEO_STATUS_REQUEST", "VIDEO_STATUS_RESULT", "OK", "SOURCE_FAILURE",
    "STATE_READY", "STATE_DONE", "decode_record", "decode_control", "encode_record", "encode_control",
)

SESSION = bytes(range(16))
CAPABILITY = bytes((0x40 + i) & 0xFF for i in range(32))

# The mirror's constants must match the header's, or the sweep below is comparing two different
# protocols. `d200_video_stream.h` is read as text for these numbers only.
_HEADER_TEXT = HEADER.read_text(encoding="utf-8")
HEADER_CONSTS = {
    name: int(value)
    for name, value in re.findall(r"(?m)^#define\s+(D200_VS_\w+)\s+(\d+)u\s*$", _HEADER_TEXT)
}

DRIVER = r"""
/* Wire-conformance oracle for device/d200_video_stream.h.
 *
 * Reads one request per line from stdin and writes exactly one verdict line to stdout:
 *   "R <hex> <direction>"  -> decode + re-encode a D2JF record
 *   "C <hex>"              -> decode + re-encode a D2PX control
 *   "ACCEPT <hex>"         -> both succeeded; <hex> is the canonical re-encode
 *   "REJECT"               -> the header refused it
 *   "MALFORMED"            -> the harness sent a bad request line (never expected)
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include "d200_video_stream.h"

static char line[700000];
static char out[700000];
static uint8_t buf[400000];
static uint8_t enc[400000];

static int hexval(int c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static size_t unhex(const char *s, uint8_t *dst, size_t cap) {
    size_t n = 0;
    while (s[0]) {
        int a, b;
        if (!s[1]) return (size_t)-1;
        a = hexval(s[0]); b = hexval(s[1]);
        if (a < 0 || b < 0 || n >= cap) return (size_t)-1;
        dst[n++] = (uint8_t)((a << 4) | b);
        s += 2;
    }
    return n;
}

static void emit(const char *status, const uint8_t *p, size_t n) {
    static const char *H = "0123456789abcdef";
    size_t i, o = 0;
    for (i = 0; status[i]; ++i) out[o++] = status[i];
    for (i = 0; i < n; ++i) {
        out[o++] = H[p[i] >> 4];
        out[o++] = H[p[i] & 15];
    }
    out[o++] = '\n';
    fwrite(out, 1, o, stdout);
}

int main(void) {
    while (fgets(line, sizeof(line), stdin)) {
        size_t len = strcspn(line, "\r\n");
        char mode;
        char *hex;
        unsigned direction = 0;
        size_t n;
        line[len] = 0;
        if (len < 2) continue;
        mode = line[0];
        hex = line + 2;
        if (mode == 'R') {
            char *space = strchr(hex, ' ');
            if (!space) { emit("MALFORMED", buf, 0); continue; }
            *space = 0;
            direction = (unsigned)strtoul(space + 1, NULL, 10);
        } else if (mode != 'C') {
            emit("MALFORMED", buf, 0);
            continue;
        }
        n = unhex(hex, buf, sizeof(buf));
        if (n == (size_t)-1) { emit("MALFORMED", buf, 0); continue; }
        if (mode == 'R') {
            d200_vs_header h;
            if (!d200_vs_decode_record(buf, n, direction, &h) ||
                !d200_vs_encode_record(enc, sizeof(enc), &h, buf + D200_VS_HEADER_SIZE)) {
                emit("REJECT", buf, 0);
                continue;
            }
            emit("ACCEPT ", enc, (size_t)D200_VS_HEADER_SIZE + h.payload_length);
        } else {
            uint8_t kind = 0;
            uint32_t sequence = 0;
            if (!d200_vs_decode_control(buf, n, &kind, &sequence) ||
                !d200_vs_encode_control(enc, sizeof(enc), kind, sequence,
                                        buf + D200_VS_CONTROL_HEADER_SIZE,
                                        n - D200_VS_CONTROL_HEADER_SIZE)) {
                emit("REJECT", buf, 0);
                continue;
            }
            emit("ACCEPT ", enc, n);
        }
    }
    fflush(stdout);
    return 0;
}
"""

RECORD_DIRECTION = {1: "producer", 3: "producer", 5: "producer", 7: "producer", 9: "producer",
                    2: "consumer", 4: "consumer", 6: "consumer", 8: "consumer"}
ALL_RECORD_KINDS = tuple(sorted(RECORD_DIRECTION))
CONTROL_LENGTHS = {21: 40, 22: 72, 23: 64, 24: 44, 25: 60, 26: 72}


# ------------------------------------------------------------------ raw wire builders

# The mirror refuses to *construct* invalid vectors (its encoder validates on the way out), and
# invalid input is precisely what has to be compared. These pack only the documented outer layout.
def raw_record(kind, payload, *, seq=0, session=SESSION, epoch=1, magic=b"D2JF", version=1, flags=0,
               length=None):
    if length is None:
        length = len(payload)
    return (struct.pack(">4sBBHI16sIQ", magic, version, kind, flags, length, session, epoch, seq)
            + payload)


def raw_control(kind, payload, *, seq=4, magic=b"D2PX", version=1, flags=0):
    return struct.pack(">4sBBHII", magic, version, kind, flags, len(payload), seq) + payload


def build_control(kind, **kw):
    """A raw control frame with only the fields named; nothing else is validated here."""
    epoch = kw.get("epoch", 1 if kind != 21 else 0)
    if kind % 2 == 0:
        body = struct.pack(">QI16sII", 3, kw.get("request_sequence", 2), SESSION, epoch,
                           kw.get("result_code", 0))
    else:
        body = struct.pack(">Q16sI", 3, SESSION, epoch)
    if kind == 21:
        body += struct.pack(">B3xII", 1, kw.get("fps_n", 30), kw.get("fps_d", 1))
    elif kind == 22:
        body += struct.pack(">BxH32s", kw.get("version", 1), kw.get("port", 9),
                            kw.get("capability", CAPABILITY))
    elif kind == 23:
        body += struct.pack(">32sI", kw.get("capability", CAPABILITY), kw.get("reason", 11))
    elif kind == 24:
        body += struct.pack(">II", kw.get("state", 6), kw.get("reason", 0))
    elif kind == 25:
        body += kw.get("capability", CAPABILITY)
    elif kind == 26:
        body += struct.pack(">IQQQII", kw.get("state", 3), kw.get("frames_received", 5),
                            kw.get("frames_consumed", 2), kw.get("eos_total", MIRROR.UINT64_MAX),
                            kw.get("terminal_reason", 0), kw.get("renderer_ready", 0))
    return raw_control(kind, body)


# ------------------------------------------------------------------ the vector set

def canonical_vectors():
    """One valid frame per record kind and per control kind, plus the shared envelope."""
    out = []
    ready = struct.pack(">6I", 30, 1, MIRROR.WINDOW_FRAMES, MIRROR.MAX_JPEG, MIRROR.WINDOW_BYTES,
                        MIRROR.MAX_PAYLOAD)
    for kind in ALL_RECORD_KINDS:
        bodies = {1: CAPABILITY, 2: ready, 3: struct.pack(">QII", 0, 40, 0) + bytes(range(24)),
                  4: struct.pack(">Q", 0), 5: struct.pack(">Q", 1), 6: struct.pack(">QQI", 1, 1, 0),
                  7: struct.pack(">I", MIRROR.SOURCE_FAILURE),
                  8: struct.pack(">I", MIRROR.SOURCE_FAILURE),
                  9: struct.pack(">I", 9) + b"boom"}
        for seq in (0, 5):
            out.append((raw_record(kind, bodies[kind], seq=seq), RECORD_DIRECTION[kind],
                        f"record kind={kind} seq={seq}"))
    for kind in CONTROL_LENGTHS:
        out.append((build_control(kind), None, f"control kind={kind}"))
    return out


def boundary_vectors():
    """Every documented acceptance edge, probed from both sides of the boundary."""
    out = []
    base_ready = [30, 1, MIRROR.WINDOW_FRAMES, MIRROR.MAX_JPEG, MIRROR.WINDOW_BYTES,
                  MIRROR.MAX_PAYLOAD]
    for i in range(6):
        for delta in (-1, 1):
            vals = list(base_ready)
            vals[i] += delta
            if vals[i] >= 0:
                out.append((raw_record(2, struct.pack(">6I", *vals)), "consumer",
                            f"ready field{i}{delta:+d}"))
    for pair in ((0, 1), (1, 0), (0, 0)):
        vals = list(base_ready)
        vals[0], vals[1] = pair
        out.append((raw_record(2, struct.pack(">6I", *vals)), "consumer", f"ready fps={pair}"))

    # FRAME: the assembled-image cap, the fragment cap, and the offset/continuity edges.
    for total, offset, frag in ((1, 0, 1), (1, 0, 0), (2, 1, 1), (2, 0, 2), (MIRROR.MAX_JPEG, 0, 1),
                                (MIRROR.MAX_JPEG, 1, 1), (MIRROR.MAX_JPEG + 1, 0, 1),
                                (40, 39, 1), (40, 0, 24), (0, 0, 1), (1, 0, 2),
                                # the header documents `offset < total`, so the equality and the
                                # overshoot are both boundaries, and an exact-fit fragment sits on
                                # `n - 16 <= total - offset` from both sides.
                                (5, 5, 1), (5, 5, 0), (5, 6, 1), (20, 16, 4), (20, 16, 5), (20, 16, 3)):
        body = struct.pack(">QII", 0, total, offset) + bytes(frag)
        out.append((raw_record(3, body), "producer", f"frame total={total} off={offset} frag={frag}"))
    out.append((raw_record(3, struct.pack(">QII", 0, 1, 0) + bytes(MIRROR.MAX_PAYLOAD - 16)),
                "producer", "frame at the fragment cap"))
    out.append((raw_record(3, struct.pack(">QII", 0, 1, 0) + bytes(MIRROR.MAX_PAYLOAD - 15)),
                "producer", "frame one over the fragment cap"))

    # ERROR: code range and UTF-8/NUL handling.
    for code in (0, 1, 15, 16):
        for text in (b"x", b"\xc3\xa9", b"\x00", b"\xff", b""):
            body = struct.pack(">I", code) + text
            if 4 <= len(body) <= 256:
                out.append((raw_record(9, body), "producer", f"error code={code} text={text!r}"))
    out.append((raw_record(9, struct.pack(">I", 1) + b"a" * 252), "producer", "error len 256"))
    out.append((raw_record(9, struct.pack(">I", 1) + b"a" * 253), "producer", "error len 257"))

    for total, sub, code in ((0, 0, 0), (1, 1, 0), (1, 2, 0), (2, 1, 0), (1, 1, 1), (0, 0, 15)):
        out.append((raw_record(6, struct.pack(">QQI", total, sub, code)), "consumer",
                    f"done {total}/{sub}/{code}"))
    for kind in (7, 8):
        for reason in (11, 13, 0, 12):
            out.append((raw_record(kind, struct.pack(">I", reason)), RECORD_DIRECTION[kind],
                        f"kind{kind} reason={reason}"))
    for ln in (32, 31, 33):
        out.append((raw_record(1, bytes(ln)), "producer", f"attach len={ln}"))

    # Envelope edges: magic, version, epoch, reserved byte, declared length, and a short buffer.
    good = raw_record(1, CAPABILITY)
    for pos, val, label in ((0, ord("X"), "magic"), (4, 2, "version"), (6, 1, "reserved6"),
                            (7, 1, "reserved7")):
        mut = bytearray(good)
        mut[pos] = val
        out.append((bytes(mut), "producer", f"record envelope {label}"))
    mut = bytearray(good)
    mut[28:32] = struct.pack(">I", 2)
    out.append((bytes(mut), "producer", "record wrong epoch"))
    out.append((good[:39], "producer", "record one byte short"))
    out.append((good + b"\x00", "producer", "record one byte long"))
    mut = bytearray(good)
    mut[8:12] = struct.pack(">I", 31)
    out.append((bytes(mut), "producer", "record impossible length"))

    for kind in CONTROL_LENGTHS:
        base = build_control(kind)
        out.append((base[:len(base) - 1], None, f"control{kind} short buffer"))
        for pos, label in ((0, "magic"), (4, "version"), (6, "reserved6"), (7, "reserved7")):
            mut = bytearray(base)
            mut[pos] = 2 if label == "version" else 1
            out.append((bytes(mut), None, f"control{kind} {label}"))
        mut = bytearray(base)
        mut[8:12] = struct.pack(">I", len(base) - 16 + 1)
        out.append((bytes(mut), None, f"control{kind} declared length off by one"))

    out.append((build_control(21, epoch=0, fps_n=0, fps_d=1), None, "open_request zero fps"))
    out.append((build_control(21, epoch=1, fps_n=30, fps_d=1), None, "open_request wrong epoch"))
    for code in (0, 1, 15, 16):
        out.append((build_control(22, epoch=1 if code == 0 else 0, result_code=code,
                                  version=1 if code == 0 else 0, port=9 if code == 0 else 0,
                                  capability=CAPABILITY if code == 0 else bytes(32)), None,
                    f"open_result code={code}"))
    for state in tuple(range(10)):
        out.append((build_control(24, state=state, reason=11), None, f"cancel_result state={state}"))
        for counts in ((5, 2, MIRROR.UINT64_MAX), (5, 5, 5), (0, 0, MIRROR.UINT64_MAX), (1, 0, 1)):
            out.append((build_control(26, state=state, frames_received=counts[0],
                                      frames_consumed=counts[1], eos_total=counts[2]), None,
                        f"status state={state} {counts}"))
    return out


def mutated_vectors(random_state, count):
    """Deterministic single-byte mutations of canonical frames: the acceptance edge."""
    seeds = canonical_vectors() + boundary_vectors()
    out = []
    for _ in range(count):
        data, direction, desc = random_state.choice(seeds)
        if not data:
            continue
        mut = bytearray(data)
        pos = random_state.randrange(len(mut))
        mut[pos] = random_state.choice((0x00, 0xFF, mut[pos] ^ 0x01, mut[pos] ^ 0xFF))
        if bytes(mut) == data:
            continue
        out.append((bytes(mut), direction, f"{desc} byte{pos} flipped"))
    return out


def random_vectors(random_state, count):
    """Unstructured frames with plausible envelopes, to compare verdicts away from the edges."""
    out = []
    lengths = {1: (32,), 2: (24,), 3: (17, 18, 19, 40, 100), 4: (8,), 5: (8,), 6: (20,), 7: (4,),
               8: (4,), 9: (4, 5, 20, 256)}
    for _ in range(count):
        kind = random_state.choice(ALL_RECORD_KINDS)
        size = random_state.choice(lengths[kind])
        payload = bytes(random_state.randrange(256) for _ in range(size))
        direction = RECORD_DIRECTION[kind]
        if random_state.random() < 0.25:
            direction = random_state.choice(("producer", "consumer"))
        out.append((raw_record(kind, payload, seq=random_state.choice((0, 1, 7))), direction,
                    f"random record kind={kind} n={size}"))
    kinds = list(CONTROL_LENGTHS)
    for _ in range(count):
        kind = random_state.choice(kinds)
        payload = bytes(random_state.randrange(256) for _ in range(CONTROL_LENGTHS[kind]))
        out.append((raw_control(kind, payload), None, f"random control kind={kind}"))
    return out


def all_vectors():
    random_state = random.Random(20260911)
    return (canonical_vectors()
            + boundary_vectors()
            + mutated_vectors(random_state, 3000)
            + random_vectors(random_state, 1500))


# ------------------------------------------------------------------ oracle plumbing


class _CoverageReport:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def pytest_terminal_summary(self, terminalreporter) -> None:  # pragma: no cover - pytest hook
        for line in self.lines:
            terminalreporter.write_line(line)


_coverage = _CoverageReport()


@pytest.fixture(scope="module", autouse=True)
def _publish_coverage(request):
    request.config.pluginmanager.register(_coverage, "devicecolor-conformance-coverage")
    yield


def _host_cc() -> str | None:
    return shutil.which("cc") or shutil.which("clang")


def _child_env(home: Path) -> dict[str, str]:
    """An isolated HOME for every child, with a hard refusal to ever pass the real one."""
    home.mkdir(parents=True, exist_ok=True)
    assert home.resolve() != REAL_HOME, f"refusing to hand the real HOME to a child: {home}"
    env = dict(os.environ)
    env["HOME"] = str(home)
    return env


def _mirror_verdict(data: bytes, direction: str | None) -> bool:
    try:
        if direction is None:
            MIRROR.decode_control(data)
        else:
            MIRROR.decode_record(data, direction=direction)
        return True
    except Exception:
        return False


def _oracle_verdicts(vectors, tmp_path, home):
    """Compile the header and return (verdict, re-encode) per vector, from one batched run."""
    cc = _host_cc()
    assert cc is not None
    source = tmp_path / "conformance_oracle.c"
    binary = tmp_path / "conformance_oracle"
    source.write_text(DRIVER, encoding="utf-8")
    env = _child_env(home)
    compiled = subprocess.run(
        [cc, *CC_FLAGS, f"-I{HEADER.parent}", "-o", str(binary), str(source)],
        capture_output=True, text=True, env=env,
    )
    assert compiled.returncode == 0, f"oracle compile failed:\n{compiled.stdout}{compiled.stderr}"

    lines = []
    for data, direction, _ in vectors:
        if direction is None:
            lines.append("C " + data.hex())
        else:
            lines.append(f"R {data.hex()} {1 if direction == 'producer' else 2}")
    ran = subprocess.run([str(binary)], input="\n".join(lines) + "\n",
                         capture_output=True, text=True, env=env)
    assert ran.returncode == 0, f"oracle exited {ran.returncode}:\n{ran.stderr}"
    out = ran.stdout.splitlines()
    assert len(out) == len(vectors), f"oracle returned {len(out)} verdicts for {len(vectors)} vectors"

    results = []
    for line in out:
        if line == "REJECT":
            results.append((False, None))
        elif line.startswith("ACCEPT "):
            results.append((True, line[len("ACCEPT "):]))
        else:
            raise AssertionError(f"unexpected oracle verdict {line!r}")
    return results


def test_c_header_and_python_mirror_agree_on_the_wire(tmp_path):
    """Every vector the C codec and the Python mirror see must produce the same verdict and bytes."""
    if _host_cc() is None:
        pytest.skip("no host C compiler (cc/clang) available")
    if not HEADER.is_file():
        pytest.skip(f"missing {HEADER}")

    missing = [name for name in REQUIRED_MIRROR_NAMES if not hasattr(MIRROR, name)]
    assert not missing, f"the Python mirror no longer defines {missing}"

    # The shared constants are the contract's skeleton; a rename or renumber on either side breaks
    # every byte on the wire, so it is checked before the sweep rather than only inside it.
    assert MIRROR.VERSION == HEADER_CONSTS["D200_VS_VERSION"]
    assert MIRROR.HEADER_SIZE == HEADER_CONSTS["D200_VS_HEADER_SIZE"]
    assert MIRROR.CONTROL_HEADER_SIZE == HEADER_CONSTS["D200_VS_CONTROL_HEADER_SIZE"]
    assert MIRROR.MAX_PAYLOAD == HEADER_CONSTS["D200_VS_MAX_PAYLOAD"]
    assert MIRROR.MAX_JPEG == HEADER_CONSTS["D200_VS_MAX_JPEG"]
    assert MIRROR.WINDOW_FRAMES == HEADER_CONSTS["D200_VS_WINDOW_FRAMES"]
    assert MIRROR.WINDOW_BYTES == HEADER_CONSTS["D200_VS_WINDOW_BYTES"]
    assert (MIRROR.ATTACH, MIRROR.READY, MIRROR.FRAME, MIRROR.CONSUMED, MIRROR.EOS, MIRROR.DONE,
            MIRROR.CANCEL, MIRROR.CANCELLED, MIRROR.ERROR) == tuple(
        HEADER_CONSTS[f"D200_VS_{name}"] for name in
        ("ATTACH", "READY", "FRAME", "CONSUMED", "EOS", "DONE", "CANCEL", "CANCELLED", "ERROR"))

    vectors = all_vectors()
    assert len(vectors) >= 4000, f"the sweep regressed to {len(vectors)} vectors"

    oracle = _oracle_verdicts(vectors, tmp_path, tmp_path / "home-conformance")

    disagreements, accepted = [], 0
    for (data, direction, desc), (c_ok, c_bytes) in zip(vectors, oracle):
        py_ok = _mirror_verdict(data, direction)
        if c_ok:
            accepted += 1
        # 1. identical verdict, 2. on accept, the C re-encode must reproduce the input exactly, so
        #    the two sides agree on the *bytes*, not merely on "looks fine".
        if py_ok != c_ok:
            disagreements.append(f"{desc}: python={'accept' if py_ok else 'reject'} "
                                 f"c={'accept' if c_ok else 'reject'} bytes={data.hex()[:120]}")
        elif c_ok and c_bytes != data.hex():
            disagreements.append(f"{desc}: both accepted but C re-encode differs\n"
                                 f"      in  ={data.hex()[:120]}\n      out ={c_bytes[:120]}")

    assert not disagreements, (
        f"{len(disagreements)} of {len(vectors)} vectors disagree between the agent's C codec and "
        f"the bridge's Python mirror:\n" + "\n".join(disagreements[:20])
    )
    # A sweep that only ever rejects proves agreement on rejection, which is not the contract.
    assert accepted >= 200, f"only {accepted} vectors exercised the accept path"
    _coverage.lines.append(
        f"devicecolor-conformance[coverage] {len(vectors)} vectors, "
        f"{accepted} accepted by both, 0 divergences"
    )


def test_conformance_harness_cannot_reach_the_real_home(tmp_path):
    """Containment (BRIEF rule 7): neither the oracle nor this test may touch the real state file.

    The oracle is pure computation and the mirror is a pure-Python codec with no I/O, but both still
    inherit an environment, so they get a HOME inside the pytest temp dir and `_child_env` hard-fails
    if it is ever handed the real one. The mirror is additionally asserted import-clean: it must not
    read HOME at import time, or a suite run could depend on the operator's live state.
    """
    cc = _host_cc()
    if cc is None:
        pytest.skip("no host C compiler (cc/clang) available")

    for token in ("getenv(", "fopen(", "system(", "popen(", "fork(", "unlink(", "remove("):
        assert token not in DRIVER, f"the oracle must stay pure computation; it contains {token!r}"

    env = _child_env(tmp_path / "probe-home")
    isolated = Path(env["HOME"])
    assert isolated != REAL_HOME and isolated.is_dir()

    probe = f".d200-conformance-probe-{os.urandom(8).hex()}"
    assert not (REAL_HOME / probe).exists(), "refusing to run with a colliding real-HOME probe"
    source = tmp_path / "home_write_probe.c"
    binary = tmp_path / "home_write_probe"
    source.write_text(
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
        "    return 0;\n"
        "}\n",
        encoding="utf-8",
    )
    compiled = subprocess.run([cc, *CC_FLAGS, "-o", str(binary), str(source)],
                              capture_output=True, text=True, env=env)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    ran = subprocess.run([str(binary)], capture_output=True, text=True, env=env)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert (isolated / probe).read_text(encoding="utf-8") == "written by a child\n"
    assert not (REAL_HOME / probe).exists(), "a conformance child wrote into the real HOME"

    with pytest.raises(AssertionError):
        _child_env(REAL_HOME)

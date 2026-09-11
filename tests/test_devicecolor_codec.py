"""Device-free host harness for the D2JF/D2PX wire codecs in `device/d200_video_stream.h`.

The header is pure portable C with no I/O and no platform dependencies, so it can be compiled and
executed on this host instead of only cross-compiled for the deck. That matters because the bridge
and the on-device agent both depend on these exact byte layouts: a silent regression here breaks
the deck with nothing in Python failing.

This file compiles a C driver with the host compiler and asserts on what it prints. It never needs
a deck, `adb`, `ffmpeg`, or a device node. It skips (never fails) when no host C compiler exists.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEVICE = ROOT / "device"

CC_FLAGS = ("-std=c11", "-O1", "-Wall", "-Wextra", "-Werror", f"-I{DEVICE}")


def _host_cc() -> str | None:
    """The host C compiler, or None when this box has none."""
    return shutil.which("cc") or shutil.which("clang")


DRIVER = r"""
#include <stdio.h>
#include <string.h>
#include "d200_video_stream.h"

int main(void) {
    const char *session = "0123456789abcdef";
    d200_vs_header out, back;
    uint8_t buf[D200_VS_HEADER_SIZE];
    int n = 0;

    memset(&out, 0, sizeof(out));
    out.kind = D200_VS_ATTACH;
    out.payload_length = 32;
    out.epoch = 1;
    out.sequence = 7;
    memcpy(out.session, session, 16);

    if (!d200_vs_encode_header(buf, sizeof(buf), &out)) return 1;
    if (memcmp(buf, "D2JF", 4)) return 2;
    if (!d200_vs_decode_header(buf, sizeof(buf), &back)) return 3;
    if (back.kind != out.kind) return 4;
    if (back.payload_length != out.payload_length) return 5;
    if (back.epoch != out.epoch) return 6;
    if (back.sequence != out.sequence) return 7;
    if (memcmp(back.session, session, 16)) return 8;
    n++;

    printf("OK %d\n", n);
    return 0;
}
"""


def test_header_codec_round_trips_on_the_host(tmp_path):
    cc = _host_cc()
    if cc is None:
        pytest.skip("no host C compiler (cc/clang) available")

    source = tmp_path / "driver.c"
    binary = tmp_path / "driver"
    source.write_text(DRIVER, encoding="utf-8")

    compiled = subprocess.run(
        [cc, *CC_FLAGS, "-o", str(binary), str(source)],
        capture_output=True,
        text=True,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr

    ran = subprocess.run([str(binary)], capture_output=True, text=True)
    assert ran.returncode == 0, (ran.returncode, ran.stdout, ran.stderr)
    assert ran.stdout.startswith("OK"), ran.stdout

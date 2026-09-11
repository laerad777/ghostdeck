"""Build ARM device helpers into ~/.ghostdeck/bin. Binaries are not committed."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from ghostdeck import state as gdstate
from ghostdeck import tree

ROOT = tree.candidate_root()
DEVICE = ROOT / "device"
VENDOR = ROOT / "vendor"
NAMES = ("d200-zkgui-proxy", "libd200-zkgui-preload.so", "d200-color-agent")
AGENT_RECIPE = DEVICE / "build-color-agent.sh"
TURBOJPEG_HINT = (
    "an ARM Linux static libturbojpeg is also required (Debian/Ubuntu: "
    "libturbojpeg0-dev); a macOS/Homebrew libturbojpeg is not usable"
)


def gcc() -> str:
    path = shutil.which("armv7-linux-gnueabihf-gcc")
    if path is None:
        raise RuntimeError("armv7-linux-gnueabihf-gcc not on PATH")
    return path


def _stale(source: Path, output: Path) -> bool:
    """True when `output` must be (re)built from `source`: absent, unreadable, or older than it.

    The caches under `~/.ghostdeck/bin` used to be trusted on `is_file()` alone, so once an output
    existed an edit to `device/*.c` was never compiled again -- and `ensure()` then re-copied that
    stale cache into `vendor/`, so the staged files looked freshly built (A-166). A user who had ever
    run `build`/`play` kept the binaries of that moment with no signal and no `--force`.

    A source that cannot be stat()ed counts as stale: producing the artifact is the safe answer, and
    it keeps the failure at the compiler (which can explain itself) rather than here.
    """
    if not output.is_file():
        return True
    try:
        return source.stat().st_mtime > output.stat().st_mtime
    except OSError:
        return True


def ensure() -> None:
    # Validate the tree before anything else: `ROOT`/`DEVICE`/`VENDOR` are candidates, not proof, and
    # without this the first symptom of an installed copy was `device sources missing under
    # <wrong path>` -- which reads like a broken checkout rather than a wheel that ships no sources
    # (C-158). `cli` gates this too; this keeps the library entry point honest on its own.
    tree.root()
    gdstate.BIN_DIR.mkdir(parents=True, exist_ok=True)
    _compile_proxy_preload()
    _ensure_agent()
    for name in NAMES:
        built = gdstate.BIN_DIR / name
        if not built.is_file():
            raise RuntimeError(f"device binary missing after build: {name}")
        dest = VENDOR / name
        shutil.copy2(built, dest)


def _compile_proxy_preload() -> None:
    compiler = gcc()
    proxy = DEVICE / "d200-zkgui-proxy.c"
    preload = DEVICE / "d200-zkgui-preload.c"
    if not proxy.is_file() or not preload.is_file():
        raise RuntimeError(f"device sources missing under {DEVICE}")
    out_proxy = gdstate.BIN_DIR / "d200-zkgui-proxy"
    out_preload = gdstate.BIN_DIR / "libd200-zkgui-preload.so"
    if _stale(proxy, out_proxy):
        subprocess.run(
            [compiler, "-O2", "-Wall", "-Wextra", str(proxy), "-o", str(out_proxy), "-pthread"],
            check=True,
            timeout=120,
        )
    if _stale(preload, out_preload):
        subprocess.run(
            [
                compiler,
                "-O2",
                "-Wall",
                "-Wextra",
                "-shared",
                "-fPIC",
                str(preload),
                "-o",
                str(out_preload),
                "-ldl",
                "-pthread",
            ],
            check=True,
            timeout=120,
        )


def _ensure_agent() -> None:
    dest = gdstate.BIN_DIR / "d200-color-agent"
    sibling = ROOT.parent / "d200-color-agent"
    # A rebuilt sibling wins over an older cached copy (A-166). The agent itself is compiled by the
    # recipe, not here, so this is the one place a fresh build can reach the cache.
    if sibling.is_file() and _stale(sibling, dest):
        shutil.copy2(sibling, dest)
        return
    if dest.is_file():
        return
    gcc_hint = (
        "Install an ARMv7 Linux hard-float toolchain"
        if shutil.which("armv7-linux-gnueabihf-gcc") is None
        else "An ARMv7 Linux hard-float toolchain is on PATH"
    )
    raise RuntimeError(
        "d200-color-agent is not built.\n"
        f"  Run: {AGENT_RECIPE}\n"
        f"  {gcc_hint}; {TURBOJPEG_HINT}.\n"
        f"  Or place a prebuilt agent at {dest} (or a sibling at {sibling})."
    )

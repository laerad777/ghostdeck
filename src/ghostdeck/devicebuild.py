"""Build ARM device helpers into ~/.ghostdeck/bin. Binaries are not committed."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from ghostdeck import state as gdstate
from ghostdeck import tree

ROOT = tree.candidate_root()
DEVICE = ROOT / "device"
VENDOR = ROOT / "vendor"
NAMES = ("d200-zkgui-proxy", "libd200-zkgui-preload.so", "d200-color-agent")
AGENT_RECIPE = DEVICE / "build-color-agent.sh"
AGENT_C = DEVICE / "d200-color-agent.c"
# The only way to adopt a prebuilt agent that the recipe did not just produce in BIN_DIR.
AGENT_SOURCE_ENV = "GHOSTDECK_AGENT_SOURCE"
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
    proxy = DEVICE / "d200-zkgui-proxy.c"
    preload = DEVICE / "d200-zkgui-preload.c"
    if not proxy.is_file() or not preload.is_file():
        raise RuntimeError(f"device sources missing under {DEVICE}")
    out_proxy = gdstate.BIN_DIR / "d200-zkgui-proxy"
    out_preload = gdstate.BIN_DIR / "libd200-zkgui-preload.so"
    # Resolved only once something needs compiling. `gcc()` used to be called unconditionally at the
    # top, so a host with a complete cache and no cross compiler on PATH -- the normal machine after
    # the toolchain is removed, and every launchd context, which starts with no PATH at all -- died
    # at "armv7-linux-gnueabihf-gcc not on PATH" before the cache was ever consulted. Reproduced with
    # `env -i PYTHONPATH=src python -c 'from ghostdeck import devicebuild; devicebuild.ensure()'`:
    # RuntimeError, although all three artifacts under ~/.ghostdeck/bin were present. The A-166 rule
    # is that a fresh source wins; it never said an unreachable compiler invalidates a fresh cache.
    compiler: str | None = None
    if _stale(proxy, out_proxy):
        compiler = gcc()
        subprocess.run(
            [compiler, "-O2", "-Wall", "-Wextra", str(proxy), "-o", str(out_proxy), "-pthread"],
            check=True,
            timeout=120,
        )
    if _stale(preload, out_preload):
        if compiler is None:
            compiler = gcc()
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


def agent_source() -> Path | None:
    """The prebuilt agent named by `GHOSTDECK_AGENT_SOURCE`, or None when the opt-in is unset.

    An earlier revision adopted `ROOT.parent / "d200-color-agent"` implicitly (T12). `ROOT.parent`
    is a directory this project does not control -- for a clone into `~/Downloads` or a shared
    checkout it is wherever the user happened to put it -- and that file was copied into
    `~/.ghostdeck/bin`, re-copied into `vendor/`, and executed on the deck by the bridge. Promoting
    an executable from outside the repository to a trusted device artifact is a real workflow, but
    it is not a default: it happens only when this variable names the file, and the copy is
    announced on stderr.
    """
    raw = os.environ.get(AGENT_SOURCE_ENV, "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def _ensure_agent() -> None:
    dest = gdstate.BIN_DIR / "d200-color-agent"
    source = agent_source()
    if source is not None:
        if not source.is_file():
            raise RuntimeError(
                f"{AGENT_SOURCE_ENV} names {source}, which is not a file.\n"
                f"  Fix the path, or unset {AGENT_SOURCE_ENV} and build the agent with {AGENT_RECIPE}."
            )
        # A rebuilt source wins over an older cached copy (A-166). The agent itself is compiled by
        # the recipe, not here, so this is the one place a fresh build can reach the cache.
        if _stale(source, dest):
            shutil.copy2(source, dest)
            # Provenance on stderr: the user has to be able to see that the binary now staged for
            # the deck did not come from this tree.
            print(
                f"d200-color-agent: adopting {source} as {dest} "
                f"({AGENT_SOURCE_ENV} is set; not compiled from {AGENT_C}).",
                file=sys.stderr,
            )
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
        f"  Or install a prebuilt agent at {dest}, or set {AGENT_SOURCE_ENV}=/path/to/d200-color-agent\n"
        f"  to adopt a prebuilt binary from outside this tree."
    )

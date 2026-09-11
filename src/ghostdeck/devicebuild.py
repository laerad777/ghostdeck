"""Build ARM device helpers into ~/.ghostdeck/bin. Binaries are not committed."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from ghostdeck import state as gdstate

ROOT = Path(__file__).resolve().parents[2]
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


def ensure() -> None:
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
    if not out_proxy.is_file():
        subprocess.run(
            [compiler, "-O2", "-Wall", "-Wextra", str(proxy), "-o", str(out_proxy), "-pthread"],
            check=True,
            timeout=120,
        )
    if not out_preload.is_file():
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
    if dest.is_file():
        return
    sibling = ROOT.parent / "d200-color-agent"
    if sibling.is_file():
        shutil.copy2(sibling, dest)
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

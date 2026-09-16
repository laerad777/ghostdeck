"""Offline tests for the optional-backend misreport (A-004) and the build recipe (C-020).

A real D200 is attached to this host, so every test that touches `usb` replaces the HID
backend with a fake before anything can enumerate hardware. No test calls `enable_adb()`
against a real backend, and no test writes to a device.
"""

from __future__ import annotations


import os
import re
import shutil
import stat
import subprocess
import sys
import time
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from ghostdeck import devicebuild, usb

RECIPE = ROOT / "device" / "build-color-agent.sh"


class _FakeHid:
    """Stands in for the `hid` module so no hardware is ever enumerated."""

    def __init__(self, entries=()):
        self.entries = list(entries)

    def enumerate(self, vid, pid):
        return list(self.entries)


def _no_hardware(monkeypatch, hid_entries=()):
    """Replace the backends: fake HID, and no pyusb discovery."""
    monkeypatch.setattr(usb, "_hid_module", lambda: _FakeHid(hid_entries))
    monkeypatch.setattr(usb, "_usb_find", lambda vid, pid: None)


def _hid_unavailable(monkeypatch):
    def _boom():
        raise usb.MissingDependency(usb.HID_INSTALL_HINT)

    monkeypatch.setattr(usb, "_hid_module", _boom)
    monkeypatch.setattr(usb, "_usb_find", lambda vid, pid: None)


# --------------------------------------------------------------------------- A-004


def test_missing_hid_is_reported_as_a_package_not_a_deck(monkeypatch):
    _hid_unavailable(monkeypatch)
    monkeypatch.setattr(usb, "_importable", lambda name: name != "hid")
    found = usb.detect()
    assert found["mode"] == "none"
    assert found["dependency"] == usb.HID_INSTALL_HINT
    assert "hidapi" in found["dependency"]
    assert "pip install hidapi" in found["dependency"]


def test_detect_shape_is_unchanged_apart_from_the_dependency_hint(monkeypatch):
    _no_hardware(monkeypatch)
    monkeypatch.setattr(usb, "_importable", lambda name: True)
    found = usb.detect()
    assert set(found) == {"serial", "vid", "pid", "mode"}
    assert found == {"serial": None, "vid": None, "pid": None, "mode": "none"}


def test_empty_enumeration_is_still_a_hardware_verdict(monkeypatch):
    """hidapi present, deck absent: no dependency hint, so the caller may blame the deck."""
    _no_hardware(monkeypatch)
    monkeypatch.setattr(usb, "_importable", lambda name: True)
    found = usb.detect()
    assert "dependency" not in found
    assert found["mode"] == "none"
    assert usb.missing_dependency() is None


def test_usb_fallback_hint_is_reported_when_only_pyusb_is_missing(monkeypatch):
    _no_hardware(monkeypatch)
    monkeypatch.setattr(usb, "_importable", lambda name: name != "usb")
    found = usb.detect()
    assert found["dependency"] == usb.USB_INSTALL_HINT
    assert "pyusb" in found["dependency"]


def test_hid_present_returns_a_distinct_signal_when_the_backend_is_missing(monkeypatch):
    _hid_unavailable(monkeypatch)
    assert usb._hid_present() is None  # neither True nor False
    assert usb._hid_serial() is None


def test_hid_present_is_a_real_bool_when_the_backend_works(monkeypatch):
    _no_hardware(monkeypatch, hid_entries=[{"interface_number": 0, "path": b"/dev/x"}])
    assert usb._hid_present() is True
    _no_hardware(monkeypatch, hid_entries=[])
    assert usb._hid_present() is False


def test_virtual_hid_enumerated_stays_a_bool_without_the_backend(monkeypatch):
    """vhid.status() calls this on the `ghostdeck status` path; it must never raise."""
    _hid_unavailable(monkeypatch)
    monkeypatch.setattr(usb, "_adb_device", lambda: {"serial": "S", "mode": "adb"})
    assert usb.virtual_hid_enumerated() is False


def test_hid_iface0_raises_missing_dependency_not_none(monkeypatch):
    _hid_unavailable(monkeypatch)
    with pytest.raises(usb.MissingDependency) as excinfo:
        usb._hid_iface0(timeout=0)
    assert usb.HID_INSTALL_HINT in str(excinfo.value)
    assert isinstance(excinfo.value, RuntimeError)


def test_hid_iface0_raises_when_an_installed_backend_cannot_enumerate(monkeypatch):
    """A *broken* hidapi must not be reported as an absent deck (T13, the same class as A-102).

    `enumerate` raising was swallowed into an empty list, so `enable_adb` blamed the hardware. The
    module already refuses that confusion for pyusb (`_usb_find` raises on a `usb.core` that will not
    import); this is the same boundary through the hidapi call. Measured before the fix with an
    installed hidapi whose `enumerate` raises `OSError`: `_hid_iface0 -> None`, then `enable_adb`
    reported "D200 HID interface 0 not found".
    """
    class Exploding:
        def enumerate(self, vid, pid):
            raise OSError("hidapi internal failure")

    monkeypatch.setattr(usb, "_hid_module", lambda: Exploding())
    with pytest.raises(usb.MissingDependency) as excinfo:
        usb._hid_iface0(timeout=0)
    message = str(excinfo.value)
    assert "hidapi is installed" in message
    assert "OSError" in message, message


def test_hid_iface0_returns_none_for_a_genuinely_empty_bus(monkeypatch):
    """The other half: an empty bus is not an error, and `enable_adb` still reports the deck absence."""
    class Empty:
        def enumerate(self, vid, pid):
            return []

    monkeypatch.setattr(usb, "_hid_module", lambda: Empty())
    assert usb._hid_iface0(timeout=0) is None


def test_hid_iface0_retries_a_transient_enumerate_failure(monkeypatch):
    """The retry loop exists for a settling bus, so a later success must still win."""
    calls = []

    class Flaky:
        def enumerate(self, vid, pid):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("not ready yet")
            return [{"interface_number": 0, "path": b"/dev/deck"}]

    monkeypatch.setattr(usb, "_hid_module", lambda: Flaky())
    assert usb._hid_iface0(timeout=2.0) == {
        "interface_number": 0,
        "path": b"/dev/deck",
    }, "a transient failure was treated as final"


def test_enable_adb_does_not_blame_the_deck_for_a_missing_package(monkeypatch):
    _hid_unavailable(monkeypatch)
    monkeypatch.setattr(usb, "_importable", lambda name: name != "hid")
    with pytest.raises(usb.MissingDependency) as excinfo:
        usb.enable_adb(timeout=0)
    message = str(excinfo.value)
    assert usb.HID_INSTALL_HINT in message
    assert "D200 HID interface 0 not found" not in message


def test_hardware_fault_message_survives_when_backends_are_present(monkeypatch):
    _no_hardware(monkeypatch)
    monkeypatch.setattr(usb, "_importable", lambda name: True)
    with pytest.raises(RuntimeError) as excinfo:
        usb.enable_adb(timeout=0)
    assert isinstance(excinfo.value, usb.MissingDependency) is False
    assert "D200 HID interface 0 not found" in str(excinfo.value)


def test_public_signatures_used_by_play_and_cli_are_unchanged():
    import inspect

    assert list(inspect.signature(usb.detect).parameters) == []
    assert inspect.signature(usb.enable_adb).parameters["timeout"].kind is inspect.Parameter.KEYWORD_ONLY
    assert usb.switch_to_adb is usb.enable_adb
    assert usb.switch_hid_to_adb is usb.enable_adb
    assert usb.send_00ff is usb.enable_adb
    assert callable(usb.virtual_hid_enumerated)


def test_extras_are_declared_for_every_backend_the_code_imports():
    import tomllib

    with (ROOT / "pyproject.toml").open("rb") as handle:
        data = tomllib.load(handle)
    extras = data["project"].get("optional-dependencies")
    assert extras is not None, "pyproject.toml declares no optional dependencies"
    assert "hidapi" in extras["hid"]
    assert "pyusb" in extras["usb"]
    # Both backends really are imported by usb.py.
    text = (SRC / "ghostdeck" / "usb.py").read_text(encoding="utf-8")
    assert "import hid" in text
    assert "import usb.core" in text
    # And nothing is promoted to a hard requirement.
    assert "dependencies" not in data["project"]


# --------------------------------------------------------------------------- C-020


def test_recipe_exists_and_is_executable():
    assert RECIPE.is_file(), f"missing {RECIPE}"
    assert stat.S_IMODE(RECIPE.stat().st_mode) & stat.S_IXUSR
    assert RECIPE.read_text(encoding="utf-8").startswith("#!")


def test_recipe_is_valid_posix_sh():
    result = subprocess.run(["/bin/sh", "-n", str(RECIPE)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_recipe_documents_the_toolchain_and_static_library_requirements():
    text = RECIPE.read_text(encoding="utf-8")
    assert "armv7-linux-gnueabihf-gcc" in text
    assert "turbojpeg.h" in text
    assert "libturbojpeg.a" in text
    assert "libturbojpeg0-dev" in text
    assert "-fsyntax-only" in text, "the only cross-compile proof available here must be documented"


def test_recipe_fails_clearly_without_the_cross_compiler(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    result = subprocess.run(
        ["/bin/sh", str(RECIPE)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "armv7-linux-gnueabihf-gcc" in combined
    assert "not on PATH" in combined
    assert "Traceback" not in combined


@pytest.mark.skipif(
    not Path("/opt/homebrew/lib/libturbojpeg.a").is_file(),
    reason="no Homebrew libturbojpeg on this host to reject",
)
def test_recipe_rejects_a_host_static_library(tmp_path):
    """The Homebrew .a is Mach-O arm64; it must be rejected, not silently linked.

    `--check-abi` is the ABI guard and does not need the cross compiler. The
    full recipe dies at 'not on PATH' on a GHA image that has jpeg-turbo but
    no armv7-linux-gnueabihf-gcc, which never reached this assertion.
    """
    result = subprocess.run(
        ["/bin/sh", str(RECIPE), "--check-abi", "/opt/homebrew/lib/libturbojpeg.a"],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        cwd=str(ROOT),
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "ELF" in combined
    assert "Mach-O" in combined


def test_ensure_agent_error_names_the_recipe_and_the_prerequisite(monkeypatch, tmp_path):
    monkeypatch.setattr(devicebuild.gdstate, "BIN_DIR", tmp_path / "bin")
    monkeypatch.setattr(devicebuild, "ROOT", tmp_path / "repo")
    monkeypatch.delenv(devicebuild.AGENT_SOURCE_ENV, raising=False)
    with pytest.raises(RuntimeError) as excinfo:
        devicebuild._ensure_agent()
    message = str(excinfo.value)
    assert str(devicebuild.AGENT_RECIPE) in message
    assert "build-color-agent.sh" in message
    assert "libturbojpeg" in message
    assert str(devicebuild.AGENT_RECIPE) == str(ROOT / "device" / "build-color-agent.sh")
    # T12: the message must name the documented location and the opt-in, and must not advertise
    # the implicit `ROOT.parent` adoption that no longer exists.
    assert str(tmp_path / "bin" / "d200-color-agent") in message
    assert devicebuild.AGENT_SOURCE_ENV in message
    assert "sibling" not in message
    # The prebuilt is a GitHub release, not a file next to the checkout. A message that says
    # "install a prebuilt" without a URL is how this used to strand a host without a cross compiler.
    assert devicebuild.AGENT_RELEASE_URL in message
    assert "releases/latest/download/d200-color-agent" in message
    assert "github.com/laerad777/ghostdeck" in message


def test_ensure_agent_prefers_an_existing_binary(monkeypatch, tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "d200-color-agent").write_text("#!/bin/sh\n")
    monkeypatch.setattr(devicebuild.gdstate, "BIN_DIR", bindir)
    monkeypatch.delenv(devicebuild.AGENT_SOURCE_ENV, raising=False)
    assert devicebuild._ensure_agent() is None


def test_ensure_agent_ignores_a_file_beside_the_repository(monkeypatch, tmp_path):
    """T12 (a): a prebuilt agent in `ROOT.parent` must NOT be adopted implicitly.

    That file is copied into `~/.ghostdeck/bin`, re-copied into `vendor/`, and executed on the deck
    by the bridge, and `ROOT.parent` is wherever the user happened to clone the tree. The old
    revision adopted it with no flag, prompt or doc line, which also made the README wrong on a host
    that had one. This test fails on that revision.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    sibling = tmp_path / "d200-color-agent"
    sibling.write_bytes(b"\x7fELF")
    monkeypatch.setattr(devicebuild.gdstate, "BIN_DIR", bindir)
    monkeypatch.setattr(devicebuild, "ROOT", repo)
    monkeypatch.delenv(devicebuild.AGENT_SOURCE_ENV, raising=False)
    with pytest.raises(RuntimeError) as excinfo:
        devicebuild._ensure_agent()
    assert "d200-color-agent is not built" in str(excinfo.value)
    assert not (bindir / "d200-color-agent").exists(), "the sibling was adopted implicitly"


def test_ensure_agent_adopts_the_opted_in_source_and_says_so(monkeypatch, tmp_path, capsys):
    """T12 (b): the capability survives, gated on `GHOSTDECK_AGENT_SOURCE`, with provenance."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    source = tmp_path / "prebuilt" / "d200-color-agent"
    source.parent.mkdir()
    source.write_bytes(b"\x7fELF-prebuild")
    monkeypatch.setattr(devicebuild.gdstate, "BIN_DIR", bindir)
    monkeypatch.setenv(devicebuild.AGENT_SOURCE_ENV, str(source))
    devicebuild._ensure_agent()
    assert (bindir / "d200-color-agent").read_bytes() == b"\x7fELF-prebuild"
    err = capsys.readouterr().err
    assert str(source) in err, "the adoption was silent"
    assert str(bindir / "d200-color-agent") in err
    assert devicebuild.AGENT_SOURCE_ENV in err


def test_ensure_agent_rejects_an_opt_in_that_names_no_file(monkeypatch, tmp_path):
    """An opt-in typo has to fail loudly: falling back to "not built" would hide it."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    missing = tmp_path / "nope" / "d200-color-agent"
    monkeypatch.setattr(devicebuild.gdstate, "BIN_DIR", bindir)
    monkeypatch.setenv(devicebuild.AGENT_SOURCE_ENV, str(missing))
    with pytest.raises(RuntimeError) as excinfo:
        devicebuild._ensure_agent()
    message = str(excinfo.value)
    assert devicebuild.AGENT_SOURCE_ENV in message
    assert str(missing) in message
    assert not (bindir / "d200-color-agent").exists()


def test_agent_source_is_unset_by_default_and_expands_a_home(monkeypatch, tmp_path):
    monkeypatch.delenv(devicebuild.AGENT_SOURCE_ENV, raising=False)
    assert devicebuild.agent_source() is None
    monkeypatch.setenv(devicebuild.AGENT_SOURCE_ENV, "   ")
    assert devicebuild.agent_source() is None
    monkeypatch.setenv(devicebuild.AGENT_SOURCE_ENV, str(tmp_path / "a"))
    assert devicebuild.agent_source() == tmp_path / "a"
    monkeypatch.setenv(devicebuild.AGENT_SOURCE_ENV, "~/agent")
    assert devicebuild.agent_source() == Path.home() / "agent"


def test_proxy_preload_behaviour_is_not_weakened():
    """C-020 item 3: the proxy/preload path must be untouched."""
    source = (SRC / "ghostdeck" / "devicebuild.py").read_text(encoding="utf-8")
    assert "armv7-linux-gnueabihf-gcc" in source
    assert "-shared" in source and "-fPIC" in source
    assert "device sources missing under" in source
    assert "device binary missing after build" in source


# --------------------------------------------------------------------------- A-166
# The ARM caches under `~/.ghostdeck/bin` were trusted on `is_file()` alone, so once an output
# existed an edit to `device/*.c` was never compiled again -- and `ensure()` then re-copied that
# stale cache into `vendor/`, which made the staged files look freshly built. Everything below runs
# against a recording stub in place of the cross compiler and against temp directories: no real
# toolchain run, and nothing written inside the repo.


def _stub_devicebuild(monkeypatch, tmp_path):
    """`devicebuild` redirected to temp dirs, with the compiler replaced by a recorder.

    Returns `(device, bindir, vendor, runs)`. The stub writes a distinguishable body per invocation,
    so a test can tell a freshly compiled artifact from a re-stamped cache by its bytes.
    """
    device = tmp_path / "device"
    device.mkdir()
    for name in ("d200-zkgui-proxy.c", "d200-zkgui-preload.c"):
        (device / name).write_text("int v1;\n", encoding="utf-8")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    # A prebuilt agent is adopted only through the opt-in (T12).
    (tmp_path / "d200-color-agent").write_bytes(b"agent")
    monkeypatch.setenv(devicebuild.AGENT_SOURCE_ENV, str(tmp_path / "d200-color-agent"))
    runs = []

    def _run(argv, **_kwargs):
        runs.append([str(part) for part in argv])
        Path(argv[argv.index("-o") + 1]).write_bytes(b"built-run-%d" % len(runs))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(devicebuild, "DEVICE", device)
    monkeypatch.setattr(devicebuild, "VENDOR", vendor)
    monkeypatch.setattr(devicebuild, "ROOT", repo)
    monkeypatch.setattr(devicebuild.gdstate, "BIN_DIR", bindir)
    monkeypatch.setattr(devicebuild, "gcc", lambda: "stub-armv7-gcc")
    monkeypatch.setattr(devicebuild.subprocess, "run", _run)
    return device, bindir, vendor, runs


def test_a_cached_object_is_rebuilt_when_its_source_changes(tmp_path, monkeypatch):
    """A-166: `if not out.is_file()` meant the compiler ran once, ever, for a given output."""
    device, _, _, runs = _stub_devicebuild(monkeypatch, tmp_path)

    devicebuild._compile_proxy_preload()
    assert len(runs) == 2, "a cold cache must compile both objects"

    devicebuild._compile_proxy_preload()
    assert len(runs) == 2, "an unchanged source must not recompile: the cache still has to work"

    newer = time.time() + 5
    for name in ("d200-zkgui-proxy.c", "d200-zkgui-preload.c"):
        os.utime(device / name, (newer, newer))
    devicebuild._compile_proxy_preload()
    assert len(runs) == 4, (
        f"an edited source was not recompiled: {len(runs) - 2} ran, the other 2 were stale"
    )


def test_a_complete_cache_does_not_need_the_cross_compiler(tmp_path, monkeypatch):
    """`gcc()` was resolved before the cache was consulted, so a warm cache still required it.

    Measured with `env -i PYTHONPATH=src python -c 'from ghostdeck import devicebuild;
    devicebuild.ensure()'` (the environment every launchd context starts in, which has no PATH at
    all): `RuntimeError: armv7-linux-gnueabihf-gcc not on PATH`, although all three artifacts under
    `~/.ghostdeck/bin` were present and nothing needed compiling. The A-166 rule is that a newer
    source wins; it never said an unreachable compiler invalidates a current cache.
    """
    device, bindir, _, runs = _stub_devicebuild(monkeypatch, tmp_path)

    def _no_compiler():
        raise RuntimeError("armv7-linux-gnueabihf-gcc not on PATH")

    monkeypatch.setattr(devicebuild, "gcc", _no_compiler)

    # First pass with a working compiler to fill the cache, then re-run with none available.
    monkeypatch.setattr(devicebuild, "gcc", lambda: "stub-armv7-gcc")
    devicebuild._compile_proxy_preload()
    assert len(runs) == 2, "precondition: the cache had to be cold"

    monkeypatch.setattr(devicebuild, "gcc", _no_compiler)
    devicebuild._compile_proxy_preload()  # must not raise: both objects are current
    assert len(runs) == 2, "the cache was bypassed even though nothing was stale"

    # The other half: a stale source must still demand the compiler.
    newer = time.time() + 5
    os.utime(device / "d200-zkgui-proxy.c", (newer, newer))
    with pytest.raises(RuntimeError) as excinfo:
        devicebuild._compile_proxy_preload()
    assert "armv7-linux-gnueabihf-gcc not on PATH" in str(excinfo.value)


def test_ensure_republishes_rebuilt_binaries_into_vendor(tmp_path, monkeypatch):
    """The other half of A-166: `vendor/` was re-stamped from the cache, so staleness was invisible."""
    device, _, vendor, runs = _stub_devicebuild(monkeypatch, tmp_path)

    devicebuild.ensure()
    assert len(runs) == 2
    assert (vendor / "d200-zkgui-proxy").read_bytes() == b"built-run-1"
    assert (vendor / "libd200-zkgui-preload.so").read_bytes() == b"built-run-2"

    newer = time.time() + 5
    os.utime(device / "d200-zkgui-proxy.c", (newer, newer))
    devicebuild.ensure()
    assert (vendor / "d200-zkgui-proxy").read_bytes() == b"built-run-3", (
        "vendor/ was re-stamped from the stale cache instead of the rebuilt object"
    )
    assert (vendor / "libd200-zkgui-preload.so").read_bytes() == b"built-run-2", (
        "the untouched preload was recompiled anyway"
    )


def test_a_rebuilt_agent_source_replaces_the_cached_copy(tmp_path, monkeypatch):
    """The agent is compiled by the recipe; a fresher opt-in source must reach the cache (A-166)."""
    _, bindir, _, _ = _stub_devicebuild(monkeypatch, tmp_path)
    source = tmp_path / "d200-color-agent"

    devicebuild._ensure_agent()
    assert (bindir / "d200-color-agent").read_bytes() == b"agent"

    source.write_bytes(b"agent-v2")
    newer = time.time() + 5
    os.utime(source, (newer, newer))
    devicebuild._ensure_agent()
    assert (bindir / "d200-color-agent").read_bytes() == b"agent-v2", (
        "a rebuilt agent was never published to the cache"
    )


# --------------------------------------------------------------------------- B-107
# The ABI guards must be real guards: a missing inspection tool may not turn them
# off. They read the ELF header directly (POSIX `od`) and never consult `file`.

ELF_ARM_OBJECT = (
    b"\x7fELF"
    + b"\x01\x01\x01\x00"  # EI_CLASS 32-bit, EI_DATA little-endian
    + b"\x00" * 8  # EI_PAD
    + b"\x01\x00"  # e_type = ET_REL
    + b"\x28\x00"  # e_machine = EM_ARM (ARMv7)
    + b"\x01\x00\x00\x00"  # e_version
)
MACHO_ARM64_OBJECT = b"\xcf\xfa\xed\xfe" + b"\x00" * 16
# Little-endian, 64-bit, e_machine = EM_AARCH64 (183): the right family and the right
# byte order, but the wrong target for this ARMv7 deck.
ELF_AARCH64_OBJECT = (
    b"\x7fELF"
    + b"\x02\x01\x01\x00"  # EI_CLASS 64-bit, EI_DATA little-endian
    + b"\x00" * 8  # EI_PAD
    + b"\x01\x00"  # e_type = ET_REL
    + b"\xb7\x00"  # e_machine = EM_AARCH64 (183)
    + b"\x01\x00\x00\x00"  # e_version
)

# Everything the recipe invokes, so a single missing name can be tested in isolation.
RECIPE_TOOLS = ("od", "tr", "mktemp", "rm", "mkdir", "chmod", "dirname", "ar")


def _run_recipe(*args, path="/usr/bin:/bin", home, extra_env=None):
    env = {"PATH": path, "HOME": str(home)}
    env.update(extra_env or {})
    return subprocess.run(
        ["/bin/sh", str(RECIPE), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
    )


def _tool_path(tmp_path, tools):
    """A PATH holding just `tools`, so the absence of one is expressible."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name in tools:
        target = shutil.which(name)
        if target is not None:
            (bin_dir / name).symlink_to(target)
    return str(bin_dir)


def test_check_abi_accepts_an_armv7_object(tmp_path):
    obj = tmp_path / "arm.o"
    obj.write_bytes(ELF_ARM_OBJECT)
    result = _run_recipe("--check-abi", str(obj), home=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ARM" in result.stdout


def test_check_abi_rejects_a_mach_o_object(tmp_path):
    obj = tmp_path / "mac.o"
    obj.write_bytes(MACHO_ARM64_OBJECT)
    result = _run_recipe("--check-abi", str(obj), home=tmp_path)
    assert result.returncode != 0
    assert "Mach-O" in result.stdout + result.stderr


def test_check_abi_rejects_a_truncated_elf(tmp_path):
    obj = tmp_path / "short.o"
    obj.write_bytes(b"\x7fELF\x01\x01")
    result = _run_recipe("--check-abi", str(obj), home=tmp_path)
    assert result.returncode != 0
    assert "not an ARMv7 little-endian ELF" in result.stdout + result.stderr


def test_check_abi_rejects_a_64_bit_arm_object(tmp_path):
    """AArch64 is the wrong target for this deck, not merely a different machine."""
    obj = tmp_path / "aarch64.o"
    obj.write_bytes(ELF_AARCH64_OBJECT)
    result = _run_recipe("--check-abi", str(obj), home=tmp_path)
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "ARM64" in combined
    # "ARM64" must come from the parsed header (class/data/machine at offsets 4/5/18),
    # not from a static hint string that would appear even for an unparsed object.
    assert "ELF 64-bit little-endian ARM64 relocatable object" in combined


def _recipe_code():
    """The recipe's executable lines. Its prose may describe the `file` history it avoids."""
    return "\n".join(
        line
        for line in RECIPE.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


def _command_words(line):
    """The command-position words of one shell line, split on pipelines/lists/`$(`.

    Catches a bare `file` in `file -b x`, `kind=$(file x)`, `... | file`, `a && file x`.
    """
    for segment in re.split(r"\|\||&&|[|;]|\$\(", line):
        words = segment.split()
        if words:
            yield words[0]


def test_the_recipe_never_consults_file():
    """The old guards were gated on the `file` utility being installed, which silently
    skipped them; no executable line may bring that back."""
    code = _recipe_code()
    assert "command -v file" not in code
    assert "file -b" not in code
    # Those are just two spellings; a bare `file` in command position is the same
    # dependency, so the check must be on the utility rather than on two strings.
    for number, line in enumerate(code.splitlines(), start=1):
        for word in _command_words(line):
            assert word != "file", f"line {number} calls the `file` utility: {line.strip()}"


def test_the_guard_fails_closed_when_od_is_missing(tmp_path):
    obj = tmp_path / "arm.o"
    obj.write_bytes(ELF_ARM_OBJECT)
    path = _tool_path(tmp_path, [t for t in RECIPE_TOOLS if t != "od"])
    assert subprocess.run(["/bin/sh", "-c", "command -v od"], env={"PATH": path}).returncode != 0
    result = _run_recipe("--check-abi", str(obj), path=path, home=tmp_path)
    assert result.returncode != 0
    assert "cannot verify the object ABI" in result.stdout + result.stderr


def _gnu_arm_archive(tmp_path):
    """A real GNU-ar archive holding a real ARMv7 object, as libturbojpeg0-dev ships."""
    gcc = shutil.which("armv7-linux-gnueabihf-gcc")
    ar = shutil.which("armv7-linux-gnueabihf-ar")
    work = tmp_path / "gnuarch"
    work.mkdir()
    source = work / "arm.c"
    source.write_text("int gd_probe(void) { return 0; }\n", encoding="utf-8")
    obj = work / "arm.o"
    archive = work / "libarm.a"
    subprocess.run([gcc, "-c", "-o", str(obj), str(source)], check=True)
    subprocess.run([ar, "rcs", str(archive), str(obj)], check=True)
    listed = subprocess.run([ar, "t", str(archive)], capture_output=True, text=True, check=True)
    assert listed.stdout.split() == ["arm.o"], listed.stdout
    return archive


needs_cross_toolchain = pytest.mark.skipif(
    shutil.which("armv7-linux-gnueabihf-gcc") is None or shutil.which("armv7-linux-gnueabihf-ar") is None,
    reason="needs the ARM cross toolchain to build a genuine ARM archive",
)


@needs_cross_toolchain
def test_the_guard_accepts_a_gnu_archive_holding_an_arm_member(tmp_path):
    """The accept side must work too: the guard may not simply reject every archive."""
    archive = _gnu_arm_archive(tmp_path)
    path = _tool_path(tmp_path, [*RECIPE_TOOLS, "armv7-linux-gnueabihf-ar"])
    result = _run_recipe("--check-abi", str(archive), path=path, home=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "first ARMv7 ELF member" in result.stdout


@needs_cross_toolchain
def test_the_guard_prefers_the_toolchain_archiver_over_a_bare_ar(tmp_path):
    """A BSD `ar` lists a GNU archive's members and then cannot extract any of them, so
    the toolchain's own archiver must win. With no bare `ar` on PATH the archive still
    has to be inspected and accepted; before T6 it was rejected as 'no ELF members'."""
    archive = _gnu_arm_archive(tmp_path)
    path = _tool_path(
        tmp_path,
        [*(t for t in RECIPE_TOOLS if t != "ar"), "armv7-linux-gnueabihf-gcc", "armv7-linux-gnueabihf-ar"],
    )
    assert subprocess.run(["/bin/sh", "-c", "command -v ar"], env={"PATH": path}).returncode != 0
    result = _run_recipe("--check-abi", str(archive), path=path, home=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "first ARMv7 ELF member" in result.stdout


def test_the_guard_fails_closed_when_the_archiver_cannot_list_members(tmp_path):
    """An unusable archiver is a 'cannot verify', never a verdict about the library."""
    archive = tmp_path / "broken.a"
    archive.write_bytes(b"!<arch>\n" + b"\x00" * 32)
    fake_ar = tmp_path / "fake-ar"
    fake_ar.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    fake_ar.chmod(0o755)
    result = _run_recipe(
        "--check-abi", str(archive), home=tmp_path, extra_env={"AR": str(fake_ar)}
    )
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "cannot verify the object ABI" in combined
    assert "contains no ELF object members" not in combined


def test_a_missing_archive_is_a_prerequisite_not_a_corrupt_archive(tmp_path):
    """T6 item 3 (B-107): `TURBOJPEG_LIB` pointing at a directory that holds no archive used to
    reach the ABI guard and come back as "cannot verify the object ABI ... 'ar' failed to list its
    members", which blames a *corrupt* archive for one that is merely absent and sends the user
    looking for a damage that does not exist. The prerequisite keeps its own message.

    `CC` is a stub on purpose: the assertion is about the check that runs *before* any compilation,
    so a real cross toolchain would only add a skip on machines that lack one.
    """
    libdir = tmp_path / "lib"
    libdir.mkdir()
    stub_cc = tmp_path / "stub-cc"
    stub_cc.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    stub_cc.chmod(0o755)
    result = _run_recipe(
        str(tmp_path / "out" / "d200-color-agent"),
        home=tmp_path,
        extra_env={"CC": str(stub_cc), "TURBOJPEG_LIB": str(libdir)},
    )
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "no libturbojpeg.a found" in combined
    assert "cannot verify the object ABI" not in combined, (
        "a missing archive was reported as an unreadable one"
    )


@pytest.mark.skipif(
    shutil.which("armv7-linux-gnueabihf-gcc") is None
    or not Path("/opt/homebrew/lib/libturbojpeg.a").is_file(),
    reason="needs both the cross toolchain and the Homebrew archive to reject",
)
def test_the_guard_still_fires_without_file_on_path(tmp_path):
    """The pre-T6 recipe skipped both ELF guards here and fell through to a raw
    linker error ('undefined reference to tj3Destroy'); the guard must run anyway."""
    path = _tool_path(tmp_path, [*RECIPE_TOOLS, "armv7-linux-gnueabihf-gcc", "armv7-linux-gnueabihf-ar"])
    assert subprocess.run(["/bin/sh", "-c", "command -v file"], env={"PATH": path}).returncode != 0
    result = _run_recipe(path=path, home=tmp_path)
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "ELF" in combined and "Mach-O" in combined
    assert "tj3Destroy" not in combined, "the misleading linker error must not be the first thing seen"


def test_the_agent_is_linked_dynamically_with_a_documented_reason():
    text = RECIPE.read_text(encoding="utf-8")
    compile_line = next(line for line in text.splitlines() if line.startswith('"$CC" -O2'))
    assert "-static" not in compile_line, "-static around dlopen() is the defect B-107 flagged"
    assert "-ldl" in text
    assert "in statically linked applications requires at runtime" in text


# The member probe is a `mktemp -d "${TMPDIR}/tjprobe.XXXXXX"` removed by a trap. These two
# tests assert on a *private* TMPDIR rather than on `/tmp/tjprobe*`: every worker runs this same
# recipe from `pytest tests/ -q` on this one host, so a global count around the subprocess cannot
# tell a recipe leak from a neighbour's in-flight probe. Precisely: a neighbour's probe directory
# only reds the global version if it appears *between* the before/after globs - one held for the
# whole test cancels out in the set difference `after - before`. The race is load-dependent, not
# narrow (4/25 under one concurrent recipe loop, 13/25 re-measured in the T6 verification pane with
# a tighter loop), so a private TMPDIR - 0/25 reds in that same harness - is the assertion surface
# that tests the recipe instead of the neighbours' load.
def test_recipe_leaves_no_temp_files_behind(tmp_path):
    """The probe directory is created before the archiver is consulted, so this die path has to
    clean it up as well."""
    probe_root = tmp_path / "private-tmp"
    probe_root.mkdir()
    fake_ar = tmp_path / "fake-ar"
    fake_ar.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    fake_ar.chmod(0o755)
    archive = tmp_path / "lib.a"
    archive.write_bytes(b"!<arch>\n" + b"\x00" * 16)
    result = _run_recipe(
        "--check-abi",
        str(archive),
        home=tmp_path,
        extra_env={"TMPDIR": str(probe_root), "AR": str(fake_ar)},
    )
    assert result.returncode != 0
    assert list(probe_root.iterdir()) == [], "the recipe leaked its member-probe directory"


@needs_cross_toolchain
def test_the_accepting_path_leaves_no_temp_files_behind(tmp_path):
    """Same hygiene check on the path that succeeds and runs to completion."""
    probe_root = tmp_path / "private-tmp-accept"
    probe_root.mkdir()
    archive = _gnu_arm_archive(tmp_path)
    path = _tool_path(tmp_path, [*RECIPE_TOOLS, "armv7-linux-gnueabihf-ar"])
    result = _run_recipe(
        "--check-abi", str(archive), path=path, home=tmp_path, extra_env={"TMPDIR": str(probe_root)}
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert list(probe_root.iterdir()) == [], "the recipe leaked its member-probe directory"


# --------------------------------------------------------------------------- A-108
# The pyusb half of the A-004 hint could never reach the user: ADB-mode enumeration is only visible
# through pyusb (`detect` -> `_adb_device` -> `_usb_find`), so with pyusb missing the post-switch
# poll timed out and `enable_adb()` reported the hardware verdict "D200 did not enumerate through
# ADB" while `missing_dependency()` already knew the real cause.


class _FakeHidModule:
    """Stands in for `hid` so the switch write happens without touching the attached deck."""

    class _Device:
        def open_path(self, path):
            return None

        def write(self, packet):
            return len(packet)

        def close(self):
            return None

    def device(self):
        return _FakeHidModule._Device()


def _drive_enable_adb(monkeypatch, *, usb_present: bool):
    """Reach the post-switch poll with pyusb present or absent, over an empty bus.

    `_hid_iface0`, the hidapi device and the pyusb handle are faked: `detect()` and `enable_adb()`
    are left real, so the verdict under test is the one the product computes. The handle is faked
    because the alternative is the physical bus, and these three cases are about the state after a
    switch that did not take: with the deck attached as ADB, `detect()` answered `mode=adb` and
    `enable_adb()` returned early, so this file only passed on a host without the deck
    (`test_the_hardware_verdict_is_still_reached_when_every_backend_works`, the same class of
    environment coupling as the macOS CI failures). An empty bus is what "the deck is not there in
    either mode" means; the backend-availability verdicts still come from the patched `_importable`.
    """
    monkeypatch.setattr(usb, "_importable", lambda name: name != "usb" or usb_present)
    monkeypatch.setattr(usb, "_hid_iface0", lambda *, timeout: {"path": b"/dev/fake"})
    monkeypatch.setattr(usb, "_hid_module", lambda: _FakeHidModule())
    monkeypatch.setattr(usb, "_usb_find", lambda vid, pid: None)


def test_a_missing_pyusb_is_reported_as_a_package_not_an_unswitched_deck(monkeypatch):
    _drive_enable_adb(monkeypatch, usb_present=False)
    assert "pyusb" in (usb.missing_dependency() or ""), "precondition: pyusb is the missing backend"
    with pytest.raises(usb.MissingDependency) as excinfo:
        usb.enable_adb(timeout=0)
    message = str(excinfo.value)
    assert "pyusb" in message
    assert "did not enumerate" not in message
    assert "install" in message


def test_the_hardware_verdict_is_still_reached_when_every_backend_works(monkeypatch):
    """The other half: a deck that genuinely did not switch is still reported as one."""
    _drive_enable_adb(monkeypatch, usb_present=True)
    assert usb.missing_dependency() is None, "precondition: both backends import"
    with pytest.raises(RuntimeError) as excinfo:
        usb.enable_adb(timeout=0)
    assert isinstance(excinfo.value, usb.MissingDependency) is False
    assert "did not enumerate through ADB" in str(excinfo.value)


def test_the_missing_backend_is_reported_before_the_switch_poll_is_spent(monkeypatch):
    """A missing backend must not cost the caller the whole readiness timeout first."""
    _drive_enable_adb(monkeypatch, usb_present=False)
    polls = []
    monkeypatch.setattr(usb, "detect", lambda: polls.append(1) or {"mode": "none"})
    with pytest.raises(usb.MissingDependency):
        usb.enable_adb(timeout=5)
    # Exactly one call: `enable_adb`'s own pre-switch detection. Zero poll iterations, so the
    # 5s readiness timeout was not spent proving something that could never succeed.
    assert polls == [1], f"the post-switch poll ran {len(polls) - 1} time(s) anyway"


# --------------------------------------------------------------------------- T13
# `missing_dependency()` asked whether the `usb` *package* imports, while the code that needs it
# imports the `usb.core` *submodule*. An importable-but-incomplete pyusb therefore passed the check
# and `_usb_find()` swallowed the ImportError, so `ghostdeck detect` said "no device" with exit 1
# instead of the documented exit 2 -- the A-004/A-108 class on the pyusb path. The stubs below are
# real packages on a real PYTHONPATH, not patched internals, so the predicate under test is the
# product's own.

HID_STUB_SOURCE = """\
\"\"\"A hidapi stand-in that enumerates nothing: the empty bus must not hide the verdict.\"\"\"


def enumerate(vid, pid):
    return []


class device:
    def open_path(self, path):
        return None

    def write(self, payload):
        return len(payload)

    def close(self):
        return None
"""

# A pyusb that imports but has no `usb.core` at all (partial, interrupted or shadowed install).
USB_STUB_WITHOUT_CORE = None

USB_STUB_WORKING = """\
\"\"\"A smallest-thing-that-works `usb.core`.\"\"\"


class _Device:
    serial_number = "STUB-SERIAL"


def find(idVendor=None, idProduct=None):
    return _Device()
"""

# The boundary case: the submodule imports, but talking to the bus fails at runtime.
USB_STUB_UNUSABLE_AT_RUNTIME = """\"\"\"A pyusb whose transfers fail: a runtime error, not a missing module.\"\"\"


def find(idVendor=None, idProduct=None):
    raise IOError("libusb reported no usable backend")
"""

_BACKEND_MODULES = ("usb", "usb.core", "hid")


def _write_backend_stub(root: Path, usb_core: str | None) -> Path:
    """A directory holding an importable `usb` (with `usb/core.py` only when given) and a `hid`."""
    stub = root / "stub"
    package = stub / "usb"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('__version__ = "stub"\n', encoding="utf-8")
    if usb_core is not None:
        (package / "core.py").write_text(usb_core, encoding="utf-8")
    (stub / "hid.py").write_text(HID_STUB_SOURCE, encoding="utf-8")
    return stub


@pytest.fixture
def stub_backends(tmp_path):
    """Resolve `usb`/`hid` to temp stubs and restore `sys.modules` exactly afterwards."""
    saved = {name: sys.modules.get(name) for name in _BACKEND_MODULES}

    def _install(usb_core: str | None) -> Path:
        stub = _write_backend_stub(tmp_path, usb_core)
        for name in _BACKEND_MODULES:
            sys.modules.pop(name, None)
        sys.path.insert(0, str(stub))
        spec = importlib.util.find_spec("usb")
        assert spec is not None and str(spec.origin).startswith(str(stub)), (
            f"the stub is not what `usb` resolves to ({spec and spec.origin}); a real pyusb on this "
            "host would make this test prove nothing"
        )
        return stub

    yield _install

    while sys.path and sys.path[0].startswith(str(tmp_path)):
        sys.path.pop(0)
    for name, module in saved.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


def test_an_importable_usb_without_usb_core_is_a_broken_install(stub_backends):
    """The predicate must answer the question the code asks: `usb.core`, not `usb`."""
    stub_backends(USB_STUB_WITHOUT_CORE)
    assert usb._importable("usb") is True, "precondition: the stub package imports"
    assert usb._importable("usb.core") is False, "precondition: it has no `usb.core`"
    # This is the whole defect: `hid` imports, so the old check returned None here.
    assert usb.missing_dependency() == usb.USB_BROKEN_HINT
    assert "pyusb" in usb.USB_BROKEN_HINT


def test_a_broken_pyusb_cannot_become_a_missing_deck_verdict(stub_backends):
    stub_backends(USB_STUB_WITHOUT_CORE)
    found = usb.detect()
    assert found["mode"] == "none"
    assert found["dependency"] == usb.USB_BROKEN_HINT, (
        "a broken backend reached the caller as a hardware verdict"
    )
    with pytest.raises(usb.MissingDependency) as excinfo:
        usb._usb_find(0x2207, 0x0019)
    assert usb.USB_BROKEN_HINT in str(excinfo.value)
    # `vhid.status()` reports host-side fields and keeps its bool contract. The environment verdict
    # is what goes to the user, and `detect()` above is where it is made.
    assert usb.virtual_hid_enumerated() is False


def test_a_healthy_pyusb_still_reaches_the_hardware_verdict(stub_backends):
    """The other half: a working `usb.core` must be used, not reported as an environment problem."""
    stub_backends(USB_STUB_WORKING)
    assert usb.missing_dependency() is None
    assert usb.detect() == {
        "serial": "STUB-SERIAL",
        "vid": usb.ADB_VID,
        "pid": usb.ADB_PID,
        "mode": "adb",
    }


def test_a_pyusb_that_fails_at_runtime_is_still_only_no_device(stub_backends):
    """T13 boundary: the import of the submodule is the verdict. A transfer that fails while a
    device is present keeps its current behaviour -- None, not MissingDependency."""
    stub_backends(USB_STUB_UNUSABLE_AT_RUNTIME)
    assert usb.missing_dependency() is None
    assert usb._usb_find(0x2207, 0x0019) is None
    assert usb.detect()["mode"] == "none"


def test_detect_exits_2_for_a_broken_pyusb_instead_of_blaming_the_deck(tmp_path):
    """End to end, in a subprocess, exactly as the user runs it."""
    stub = _write_backend_stub(tmp_path, USB_STUB_WITHOUT_CORE)
    env = {
        "PYTHONPATH": f"{stub}{os.pathsep}{SRC}",
        "HOME": str(tmp_path / "home"),
        "PATH": os.environ.get("PATH", ""),
    }
    Path(env["HOME"]).mkdir()
    result = subprocess.run(
        [sys.executable, "-m", "ghostdeck.cli", "detect"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
        timeout=60,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "pyusb" in combined
    assert "no device" not in combined, "the broken install was reported as a missing deck"


def test_the_hid_backend_has_no_submodule_for_the_same_hole():
    """T13 item 4: the same reasoning applied to `hid`. It is imported as a top-level module only
    (`import hid`; hidapi ships as a single extension module with no submodules), so there is no
    `hid.<x>` import that a partial install could silently break. `_hid_present()`/`_hid_serial()`
    already return None -- not a hardware verdict -- when `_hid_module()` raises, and
    `missing_dependency()` covers the package itself."""
    text = (SRC / "ghostdeck" / "usb.py").read_text(encoding="utf-8")
    assert re.search(r"^\s*(import|from)\s+hid\.[A-Za-z_]", text, re.MULTILINE) is None
    assert "import hid\n" in text

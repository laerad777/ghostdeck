"""Offline tests for the optional-backend misreport (A-004) and the build recipe (C-020).

A real D200 is attached to this host, so every test that touches `usb` replaces the HID
backend with a fake before anything can enumerate hardware. No test calls `enable_adb()`
against a real backend, and no test writes to a device.
"""

from __future__ import annotations


import shutil
import stat
import subprocess
import sys
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
    """The Homebrew .a is Mach-O arm64; it must be rejected, not silently linked."""
    env = {
        "PATH": "/opt/homebrew/bin:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "TURBOJPEG_LIB": "/opt/homebrew/lib",
        "TURBOJPEG_INC": "/opt/homebrew/include",
    }
    result = subprocess.run(
        ["/bin/sh", str(RECIPE)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "ELF" in combined
    assert "Mach-O" in combined


def test_recipe_leaves_no_temp_files_behind(tmp_path):
    before = {p.name for p in Path("/tmp").glob("tjprobe*")}
    subprocess.run(
        ["/bin/sh", str(RECIPE)],
        capture_output=True,
        text=True,
        env={"PATH": "/opt/homebrew/bin:/usr/bin:/bin", "HOME": str(tmp_path)},
        cwd=str(ROOT),
    )
    after = {p.name for p in Path("/tmp").glob("tjprobe*")}
    assert after - before == set()


def test_ensure_agent_error_names_the_recipe_and_the_prerequisite(monkeypatch, tmp_path):
    monkeypatch.setattr(devicebuild.gdstate, "BIN_DIR", tmp_path / "bin")
    monkeypatch.setattr(devicebuild, "ROOT", tmp_path / "repo")
    with pytest.raises(RuntimeError) as excinfo:
        devicebuild._ensure_agent()
    message = str(excinfo.value)
    assert str(devicebuild.AGENT_RECIPE) in message
    assert "build-color-agent.sh" in message
    assert "libturbojpeg" in message
    assert str(devicebuild.AGENT_RECIPE) == str(ROOT / "device" / "build-color-agent.sh")


def test_ensure_agent_prefers_an_existing_binary(monkeypatch, tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "d200-color-agent").write_text("#!/bin/sh\n")
    monkeypatch.setattr(devicebuild.gdstate, "BIN_DIR", bindir)
    assert devicebuild._ensure_agent() is None


def test_ensure_agent_copies_a_sibling_build(monkeypatch, tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    sibling = tmp_path / "d200-color-agent"
    sibling.write_bytes(b"\x7fELF")
    monkeypatch.setattr(devicebuild.gdstate, "BIN_DIR", bindir)
    monkeypatch.setattr(devicebuild, "ROOT", repo)
    devicebuild._ensure_agent()
    assert (bindir / "d200-color-agent").read_bytes() == b"\x7fELF"


def test_proxy_preload_behaviour_is_not_weakened():
    """C-020 item 3: the proxy/preload path must be untouched."""
    source = (SRC / "ghostdeck" / "devicebuild.py").read_text(encoding="utf-8")
    assert "armv7-linux-gnueabihf-gcc" in source
    assert "-shared" in source and "-fPIC" in source
    assert "device sources missing under" in source
    assert "device binary missing after build" in source


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
    assert "ARM64" in result.stdout + result.stderr


def _recipe_code():
    """The recipe's executable lines. Its prose may describe the `file` history it avoids."""
    return "\n".join(
        line
        for line in RECIPE.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


def test_the_recipe_never_consults_file():
    """The old guards were gated on the `file` utility being installed, which silently
    skipped them; no executable line may bring that back."""
    code = _recipe_code()
    assert "command -v file" not in code
    assert "file -b" not in code


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

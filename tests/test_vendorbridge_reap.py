"""Regression proof for FIX-5-T10 (H2 stale sibling directories, H4 a failed stage).

H2: `_remove_remote_dir()` validated and removed only the directory of the session
doing the teardown. Any bridge that died -- SIGKILL, host crash, a failed stage --
never removed its own, and nothing else aged them out: seven ~68 KB directories
were still on the real deck, the oldest dated Sep 5. The reap is driven here
against a recorded command list: no adb, no device, no `/tmp/d200-*`, and HOME is
redirected so the admission-lock probe can never touch the operator's real lock.

H4: a stage that failed because adb did not see the device reached the top level as
a raw `DeviceCommandError` traceback, naming no step the user could act on.

The four behaviours the dispatch requires:
  a stale sibling with the staged contents -> reaped
  a name that only matches the shape     -> NOT reaped (contents are the proof)
  another bridge holding the admission   -> no sibling work at all
  a failed stage                         -> one line, exit 1, no traceback
"""

from __future__ import annotations

from contextlib import ExitStack
import fcntl
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor"
BRIDGE_PATH = VENDOR / "d200-local-bridge.py"

sys.path.insert(0, str(VENDOR))

spec = importlib.util.spec_from_file_location("d200_local_bridge_reap", BRIDGE_PATH)
bridge = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bridge
spec.loader.exec_module(bridge)

PROXY_BYTES = b"p" * 96
PRELOAD_BYTES = b"l" * 48
STALE = "/tmp/.d200-zkgui-0123456789abcdef"
FOREIGN = "/tmp/.d200-zkgui-deadbeefdeadbeef"
# Captured hardware fact (tasks/FIX-5-T10.md): the seven session directories the
# master's real-deck run found hold `preload.so` 14,216 B + `proxy` 53,572 B, i.e.
# they were staged by an EARLIER build than this host's artifacts (14,656 / 62,140
# B when this file was written). The listing below uses those deck sizes on
# purpose: a fixture that generated both the binaries and the listing from one
# constant could only ever test the classifier against numbers this code produced
# (C-152).
DECK_PRELOAD_BYTES = 14216
DECK_PROXY_BYTES = 53572
BUILD_ARTIFACTS = ("d200-zkgui-proxy", "d200-color-agent", "libd200-zkgui-preload.so")


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Every test runs against a throwaway HOME, never the operator's state root."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture()
def staged(tmp_path):
    """The local build artifacts, at stand-in sizes unrelated to the deck listing."""
    proxy_binary = tmp_path / "d200-zkgui-proxy"
    preload_library = tmp_path / "libd200-zkgui-preload.so"
    proxy_binary.write_bytes(PROXY_BYTES)
    preload_library.write_bytes(PRELOAD_BYTES)
    return proxy_binary, preload_library


class RecordingProxy(bridge.DeviceProxy):
    """DeviceProxy with adb replaced by a recorded command list."""

    def __init__(self, *arguments, listing="", **keywords):
        super().__init__(*arguments, **keywords)
        self.commands = []
        self.listing = listing

    def _run(self, *arguments, timeout=15):
        self.commands.append((arguments, timeout))
        if arguments == ("shell", bridge.STALE_SIBLING_LIST_COMMAND):
            return subprocess.CompletedProcess([], 0, stdout=self.listing.encode())
        return None


def make_proxy(staged, listing=""):
    proxy_binary, preload_library = staged
    return RecordingProxy(
        "unused-adb", "unused", proxy_binary, preload_library, listing=listing,
    )


def staged_listing(*directories, proxy_bytes=DECK_PROXY_BYTES, preload_bytes=DECK_PRELOAD_BYTES):
    """A deck listing holding another session's staged pair, as the device prints it."""
    return "".join(
        f"{directory}|{name}|{size}\n"
        for directory in directories
        for name, size in (("proxy", proxy_bytes), ("preload.so", preload_bytes))
    )


def issued(proxy):
    return [command for command, _timeout in proxy.commands]


def test_a_stale_sibling_with_the_staged_contents_is_reaped(staged):
    """The own directory goes first, then the listed leftover; nothing else is touched."""
    proxy = make_proxy(staged, staged_listing(STALE))
    proxy.remote_dir_staged = True

    proxy._remove_remote_dir()

    assert issued(proxy) == [
        ("shell", f"rm -rf {proxy.remote_dir}"),
        ("shell", bridge.STALE_SIBLING_LIST_COMMAND),
        ("shell", f"rm -rf {STALE}"),
    ]
    assert proxy.remote_dir_staged is False


def test_a_name_that_only_matches_the_shape_is_not_reaped(staged):
    """`deadbeefdeadbeef` exists on the real deck: the shape is not proof of ownership."""
    listing = f"{FOREIGN}|notes.txt|12\n" + staged_listing(FOREIGN)
    proxy = make_proxy(staged, listing)
    proxy.remote_dir_staged = True

    proxy._remove_remote_dir()

    assert issued(proxy) == [
        ("shell", f"rm -rf {proxy.remote_dir}"),
        ("shell", bridge.STALE_SIBLING_LIST_COMMAND),
    ]


def test_a_hidden_extra_entry_disqualifies_a_sibling(staged):
    """`ls -A`, not `*`: an entry the user cannot see still means 'not exactly my pair'."""
    proxy = make_proxy(staged, staged_listing(STALE) + f"{STALE}|.keep|7\n")
    proxy.remote_dir_staged = True

    proxy._remove_remote_dir()

    assert issued(proxy) == [
        ("shell", f"rm -rf {proxy.remote_dir}"),
        ("shell", bridge.STALE_SIBLING_LIST_COMMAND),
    ]


def test_a_sibling_from_an_earlier_build_is_reaped(staged):
    """The deck's measured pair is 53,572/14,216 B while this build stages 96/48 B.

    C-142/C-152: comparing the listing against the CURRENT build's byte sizes made
    every pre-existing leftover unclassifiable, so the reap removed none of the
    seven directories H2 was dispatched for.
    """
    listing = f"{STALE}|proxy|{DECK_PROXY_BYTES}\n{STALE}|preload.so|{DECK_PRELOAD_BYTES}\n"
    proxy = make_proxy(staged, listing)
    proxy.remote_dir_staged = True

    proxy._remove_remote_dir()

    assert (
        proxy.proxy_binary.stat().st_size, proxy.preload_library.stat().st_size,
    ) != (DECK_PROXY_BYTES, DECK_PRELOAD_BYTES), "the fixture must not derive both sides"
    assert issued(proxy) == [
        ("shell", f"rm -rf {proxy.remote_dir}"),
        ("shell", bridge.STALE_SIBLING_LIST_COMMAND),
        ("shell", f"rm -rf {STALE}"),
    ]


def test_a_sibling_with_an_empty_entry_is_not_reaped(staged):
    """An empty file is not a pushed artifact; the pair must hold real bytes."""
    listing = staged_listing(STALE, proxy_bytes=0)
    proxy = make_proxy(staged, listing)
    proxy.remote_dir_staged = True

    proxy._remove_remote_dir()

    assert issued(proxy) == [
        ("shell", f"rm -rf {proxy.remote_dir}"),
        ("shell", bridge.STALE_SIBLING_LIST_COMMAND),
    ]


def test_a_missing_or_unmeasured_file_is_not_reaped(staged):
    """A pair with no readable size cannot be classified at all."""
    listing = f"{STALE}|proxy|{DECK_PROXY_BYTES}\n{STALE}|preload.so|\n"
    proxy = make_proxy(staged, listing)
    proxy.remote_dir_staged = True

    proxy._remove_remote_dir()

    assert issued(proxy) == [
        ("shell", f"rm -rf {proxy.remote_dir}"),
        ("shell", bridge.STALE_SIBLING_LIST_COMMAND),
    ]


def test_unreadable_output_only_disqualifies_its_own_directory(staged):
    proxy = make_proxy(staged, staged_listing(STALE) + f"{FOREIGN}|proxy|\n")
    proxy.remote_dir_staged = True

    proxy._remove_remote_dir()

    assert ("shell", f"rm -rf {STALE}") in issued(proxy)
    assert ("shell", f"rm -rf {FOREIGN}") not in issued(proxy)


def test_a_live_bridge_admission_stops_the_reap_before_any_device_work(staged, isolated_home):
    """Holding the admission is what makes 'no live bridge owns a sibling' a proof."""
    lock = isolated_home / ".ghostdeck" / "device-admission.lock"
    lock.parent.mkdir()
    lock.write_text("{}\n")
    holder = os.open(lock, os.O_RDWR)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        proxy = make_proxy(staged, staged_listing(STALE))
        proxy.remote_dir_staged = True
        proxy._remove_remote_dir()
    finally:
        os.close(holder)

    assert issued(proxy) == [("shell", f"rm -rf {proxy.remote_dir}")], (
        "another live bridge must stop the reap before it lists, let alone removes"
    )


def test_the_admitted_instance_reaps_without_taking_the_lock_again(staged):
    """The session's own admission already excludes a second bridge."""
    proxy = make_proxy(staged, staged_listing(STALE))
    proxy.remote_dir_staged = True
    with ExitStack() as admission:
        proxy.admission = admission
        proxy._remove_remote_dir()

    assert issued(proxy) == [
        ("shell", f"rm -rf {proxy.remote_dir}"),
        ("shell", bridge.STALE_SIBLING_LIST_COMMAND),
        ("shell", f"rm -rf {STALE}"),
    ]


def test_a_failed_sibling_removal_is_best_effort(staged):
    """Reaping a leftover must never fail the teardown of this session's own directory."""
    proxy = make_proxy(staged, staged_listing(STALE, FOREIGN))
    proxy.remote_dir_staged = True

    def failing_run(*arguments, timeout=15):
        if arguments == ("shell", f"rm -rf {STALE}"):
            raise bridge.DeviceCommandError(1)
        return RecordingProxy._run(proxy, *arguments, timeout=timeout)

    proxy._run = failing_run
    proxy._remove_remote_dir()

    assert ("shell", f"rm -rf {FOREIGN}") in issued(proxy)
    assert proxy.remote_dir_staged is False


def test_an_unlistable_deck_does_not_fail_the_teardown(staged):
    proxy = make_proxy(staged)
    proxy.remote_dir_staged = True

    def failing_run(*arguments, timeout=15):
        if arguments == ("shell", bridge.STALE_SIBLING_LIST_COMMAND):
            proxy.commands.append((arguments, timeout))
            raise bridge.DeviceCommandError(1)
        return RecordingProxy._run(proxy, *arguments, timeout=timeout)

    proxy._run = failing_run
    proxy._remove_remote_dir()

    assert issued(proxy) == [
        ("shell", f"rm -rf {proxy.remote_dir}"),
        ("shell", bridge.STALE_SIBLING_LIST_COMMAND),
    ]
    assert proxy.remote_dir_staged is False


def test_the_session_prefix_is_never_removed_by_wildcard():
    source = BRIDGE_PATH.read_text(encoding="utf-8")
    assert f"rm -rf {bridge.SESSION_DIR_PREFIX}*" not in source
    assert f"'{bridge.SESSION_DIR_PREFIX}*'" not in source


class FailingStageProxy(RecordingProxy):
    """Fails the first staging command, the way an adb server without the device does."""

    def _run(self, *arguments, timeout=15):
        if arguments[0] == "shell" and arguments[1].startswith("rm -rf"):
            self.commands.append((arguments, timeout))
            raise bridge.DeviceCommandError(1)
        return super()._run(*arguments, timeout=timeout)


def test_a_failed_stage_names_the_step_and_keeps_the_command_internal(staged):
    proxy_binary, preload_library = staged
    (proxy_binary.with_name("d200-color-agent")).write_bytes(b"a" * 16)
    proxy = FailingStageProxy("unused-adb", "unused", proxy_binary, preload_library)

    with pytest.raises(bridge.StagingError) as raised:
        proxy._stage()

    message = str(raised.value)
    assert "create the session directory" in message
    assert "exit 1" in message
    assert isinstance(raised.value, RuntimeError), "callers catching RuntimeError must still work"
    assert isinstance(raised.value.__cause__, bridge.DeviceCommandError)
    assert proxy.remote_dir_staged is True, (
        "close() must still remove the session directory a failed stage left behind"
    )


def test_a_stage_failure_removes_the_half_staged_directory(staged):
    proxy_binary, preload_library = staged
    (proxy_binary.with_name("d200-color-agent")).write_bytes(b"a" * 16)
    proxy = FailingStageProxy("unused-adb", "unused", proxy_binary, preload_library)
    proxy.remote_dir_staged = True

    proxy.close()

    assert issued(proxy)[0] == ("shell", f"rm -rf {proxy.remote_dir}")
    assert proxy.remote_dir_staged is False
    assert proxy.closed is True


@pytest.fixture()
def short_scratch():
    """A short absolute scratch path: an AF_UNIX endpoint lives in 104 bytes."""
    directory = Path(tempfile.mkdtemp(prefix="vendorbridge-reap-", dir="/tmp"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture()
def deckless_bridge(tmp_path):
    """The real bridge next to the build artifacts it stages, driven by an adb that fails."""
    target = tmp_path / "deckless"
    target.mkdir()
    for name in ("d200-local-bridge.py", "d200_process_control.py", "d200_video_stream.py"):
        shutil.copy(VENDOR / name, target / name)
    for name in BUILD_ARTIFACTS:
        (target / name).write_bytes(b"x" * 32)
    adb = target / "adb"
    adb.write_text("#!/bin/sh\necho 'error: device offline' >&2\nexit 1\n")
    adb.chmod(0o755)
    return target


def test_a_failed_stage_exits_non_zero_with_one_clean_line(deckless_bridge, short_scratch):
    socket_path = short_scratch / "bridge.sock"
    result = subprocess.run(
        [
            sys.executable, str(deckless_bridge / "d200-local-bridge.py"),
            "--socket", str(socket_path),
            "--serial", "unused",
            "--adb", str(deckless_bridge / "adb"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 1, result.stderr
    assert "bridge_stage_failed" in result.stderr
    assert "could not create the session directory on the device" in result.stderr
    assert "Traceback" not in result.stderr
    assert "DeviceCommandError" not in result.stderr
    assert not socket_path.exists()

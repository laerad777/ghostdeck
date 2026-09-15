"""Local hidshim Studio copy. Official /Applications/Ulanzi Studio.app is never written."""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from ghostdeck import adb, devicebuild, tree, usb

ORIGINAL = Path("/Applications/Ulanzi Studio.app")
COPY = Path.home() / "Applications" / "Ulanzi Studio ADB.app"
SOCKET = Path("/tmp/d200-adb-bridge.sock")
# The bridge's own state file. `_spawn_bridge` has always passed this path and, until A-133, nothing
# ever read it back; it is the only on-disk record of which process is serving SOCKET.
BRIDGE_STATE = Path("/tmp/d200-local-bridge.pid")
# Documented in README.md: `ghostdeck studio` appends the bridge's stdout/stderr here.
BRIDGE_LOG = Path("/tmp/d200-local-bridge.log")
ROOT = tree.candidate_root()
VENDOR = ROOT / "vendor"
BRIDGE = VENDOR / "d200-local-bridge.py"
HIDSHIM_SRC = ROOT / "reference" / "hidshim.c"
SHIM = COPY / "Contents/Frameworks/libhidapi.0.dylib"
REAL = COPY / "Contents/Frameworks/libhidapi.0.real.dylib"
EXE = COPY / "Contents/MacOS/UlanziDeck"

_BUILD_TOOLS = ("ditto", "xcrun", "clang", "install_name_tool", "codesign")

# Endpoint probe outcomes. `dead` is the only state that permits removing the path.
_ENDPOINT_LIVE = "live"
_ENDPOINT_DEAD = "dead"
_ENDPOINT_UNDETERMINABLE = "undeterminable"
_PROBE_TIMEOUT = 0.4
# The one phrase that means "the bridge `play` needs is not up". `play` owns the refusal (it is the
# command that needs the bridge) and the GUI matches on this phrase to decide whether running
# `studio` can fix the failure it just saw. Shared rather than duplicated so the two cannot drift:
# a reworded refusal would otherwise silently stop the window from recovering.
BRIDGE_DOWN = "the hidshim bridge is not running"

# A deck that has just re-enumerated through ADB answers device commands late, the HID-to-ADB
# switch report itself is flaky, and the bridge exits on the first rejected device command, so
# bridge bring-up is prepared and retried instead of reported from a single attempt.
BRIDGE_WAIT = 15.0
BRIDGE_ATTEMPTS = 3
BRIDGE_READY_TIMEOUT = 25.0
BRIDGE_RETRY_DELAY = 2.0


def _require_build_tools() -> None:
    """Fail with the missing tool named instead of a bare FileNotFoundError mid-copy."""
    for tool in _BUILD_TOOLS:
        if shutil.which(tool) is None:
            raise RuntimeError(f"{tool} not on PATH: install it (xcode-select --install)")


def copy_exists() -> bool:
    return COPY.is_dir() and SHIM.is_file() and REAL.is_file() and EXE.is_file()


def _copy_pids() -> list[int]:
    """PIDs whose live command line IS this copy's own executable, as argv[0].

    The marker must be the whole argv[0], not a substring of the command line: a stranger that
    merely mentions the path (an editor, a `grep`, a `python -c` carrier) is not our copy. That
    difference is the C-103 defect class, which `play.py` had already closed; `studio.py` had not
    been brought along, so `running()` false-positived and `_quit_copy()` SIGTERM'd a stranger
    (A-105).

    Matching argv[0] exactly is also what keeps the official /Applications/Ulanzi Studio.app and a
    recycled pid safe: neither has this copy's resolved executable as its argv[0].

    The marker contains spaces ("Ulanzi Studio ADB.app"), so this cannot be a token comparison:
    argv[0] is the marker exactly, or the marker followed by its own arguments. `-ww` genuinely
    prevents ps truncation here and `LC_ALL=C` keeps the output stable, which the previous argv
    (`ps -axo pid=,command=`) did not deliver despite this docstring claiming it.
    """
    if not COPY.is_dir() or not EXE.is_file():
        return []
    marker = str(EXE.resolve())
    try:
        listed = subprocess.check_output(
            ["ps", "-ww", "-axo", "pid=,command="],
            text=True,
            env=dict(os.environ, LC_ALL="C"),
        )
    except (subprocess.CalledProcessError, OSError):
        return []
    pids = []
    for line in listed.splitlines():
        pid, _, command = line.strip().partition(" ")
        if not pid.isdigit():
            continue
        command = command.strip()
        if command == marker or command.startswith(f"{marker} "):
            pids.append(int(pid))
    return pids


def running() -> bool:
    return bool(_copy_pids())


def _quit_copy(*, timeout: float = 15.0) -> None:
    """Stop only this copy, never the official app. A dead shim needs a fresh Studio."""
    pids = _copy_pids()
    if not pids:
        return
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _copy_pids():
            return
        time.sleep(0.25)
    raise RuntimeError("hidshim Studio copy did not stop")


def launch() -> None:
    ensure_copy()
    devicebuild.ensure()
    endpoint, reason = _socket_state()
    if endpoint == _ENDPOINT_UNDETERMINABLE:
        # Refuse before touching the copy: quitting a healthy shim for a bridge that then cannot
        # be started would leave the user with neither.
        raise _undeterminable_endpoint(reason)
    if endpoint == _ENDPOINT_LIVE and not _bridge_owner_live():
        # A-133: something is listening on our predictable socket path, and it is not the bridge we
        # spawn (no live record of it in BRIDGE_STATE). Opening Studio here would point the shim at a
        # stranger's socket and silently report success. Refuse instead — and note that the A-114
        # property is untouched: nothing is unlinked, and no second bridge is spawned over it.
        raise RuntimeError(
            f"a listener holds {SOCKET} but no live {BRIDGE.name} of ours owns it; refusing to open "
            f"Studio against an unidentified bridge. Stop that process, or remove {SOCKET} if it is "
            f"a leftover, and retry"
        )
    if endpoint == _ENDPOINT_DEAD:
        # Studio holds HID interface 0 while it runs, so the HID-to-ADB switch needs
        # the copy stopped first; a restarted bridge also leaves an already running
        # copy holding a dead shim, so it is relaunched either way.
        _quit_copy()
    _ensure_bridge()
    subprocess.run(["/usr/bin/open", str(COPY)], check=True, timeout=15)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if running():
            return
        time.sleep(0.25)
    raise RuntimeError("hidshim Studio copy did not stay running")


def ensure_copy() -> None:
    if copy_exists():
        return
    _require_build_tools()
    if not ORIGINAL.is_dir():
        raise RuntimeError(f"install official Studio at {ORIGINAL}")
    if not HIDSHIM_SRC.is_file():
        raise RuntimeError(f"missing hidshim source: {HIDSHIM_SRC}")
    COPY.parent.mkdir(parents=True, exist_ok=True)
    if COPY.exists():
        subprocess.run(["/bin/rm", "-rf", str(COPY)], check=True, timeout=60)
    subprocess.run(["/usr/bin/ditto", str(ORIGINAL), str(COPY)], check=True, timeout=120)
    info_path = COPY / "Contents/Info.plist"
    info = plistlib.loads(info_path.read_bytes())
    info["CFBundleIdentifier"] = "ulanzi.UlanziStudioADB"
    info["CFBundleName"] = "Ulanzi Studio ADB"
    info["CFBundleDisplayName"] = "Ulanzi Studio ADB"
    info_path.write_bytes(plistlib.dumps(info, sort_keys=False))
    frameworks = COPY / "Contents/Frameworks"
    shim = frameworks / "libhidapi.0.dylib"
    real = frameworks / "libhidapi.0.real.dylib"
    if real.exists():
        real.unlink()
    shim.rename(real)
    subprocess.run(
        ["install_name_tool", "-id", "@rpath/libhidapi.0.real.dylib", str(real)],
        check=True,
        timeout=30,
    )
    sdk = subprocess.check_output(
        ["xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True, timeout=30
    ).strip()
    subprocess.run(
        [
            "clang",
            "-arch",
            "arm64",
            "-dynamiclib",
            "-O2",
            "-isysroot",
            sdk,
            str(HIDSHIM_SRC),
            "-o",
            str(shim),
            "-framework",
            "IOKit",
            "-framework",
            "CoreFoundation",
            "-lpthread",
        ],
        check=True,
        timeout=60,
    )
    subprocess.run(
        ["install_name_tool", "-id", "@rpath/libhidapi.0.dylib", str(shim)],
        check=True,
        timeout=30,
    )
    subprocess.run(
        ["codesign", "--force", "--deep", "--sign", "-", str(COPY)],
        check=True,
        timeout=120,
    )


def _open_bridge_log():
    """Open the bridge log privately: 0600, and never through a planted symlink (A-115).

    The path is predictable and lives in world-writable `/tmp`, so a bare `open(..., "ab")` happily
    writes through a symlink somebody planted there and creates the file at the ambient umask. Both
    were observed on this host: `/tmp/d200-local-bridge.log` was mode 0644, 213 KB.

    `O_NOFOLLOW` refuses to traverse a symlink, and the mode (plus `fchmod`, so an older
    world-readable file is remediated rather than inherited) makes the file private. A path that
    cannot be opened safely is refused rather than written through — the same rule the endpoint probe
    already follows for a path it cannot classify.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(BRIDGE_LOG, flags, 0o600)
    except OSError as error:
        raise RuntimeError(
            f"cannot open the bridge log {BRIDGE_LOG} ({type(error).__name__}: {error}); refusing to "
            f"write through a path that is not a regular file"
        ) from error
    os.fchmod(descriptor, 0o600)
    return os.fdopen(descriptor, "ab", buffering=0)


def _pid_argv(pid: int) -> str:
    """The live argv of `pid`, or "" when it cannot be read. `-ww` + LC_ALL=C, as in `_copy_pids`."""
    try:
        listed = subprocess.check_output(
            ["ps", "-ww", "-axo", "pid=,command="],
            text=True,
            env=dict(os.environ, LC_ALL="C"),
        )
    except (subprocess.CalledProcessError, OSError):
        return ""
    for line in listed.splitlines():
        found, _, command = line.strip().partition(" ")
        if found.isdigit() and int(found) == pid:
            return command.strip()
    return ""


def _bridge_owner_live() -> bool:
    """True only when OUR bridge is alive and therefore serving SOCKET (A-133).

    `_socket_state()` cannot tell our bridge from any other listener: a successful `connect()` is all
    it has, and that verdict is deliberately ownership-agnostic because it exists to answer "may this
    path be unlinked?" (A-114), not "who owns it?". Every listener therefore looked like our bridge,
    so `launch()` opened Studio against a stranger.

    Ownership comes from the bridge's own `--state-file`, which `_spawn_bridge` has always passed and
    nothing read until now: it records the bridge's pid. The pid must be alive AND its argv must name
    our bridge script — the same argv discipline `play._player_identity` and `_copy_pids` use, so a
    recycled pid cannot satisfy it, and a live bridge that refused to bind (it exits with
    `bridge_socket_in_use` when another listener holds the path) is not live at all.

    Residual, stated rather than hidden: a bridge started by hand WITHOUT `--state-file` writes no
    record, so this returns False and `launch()` refuses. That is the safe direction (refusing beats
    opening Studio against an unidentified socket) and the message says what to do about it.
    """
    try:
        record = json.loads(BRIDGE_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(record, dict):
        return False
    pid = record.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    argv = _pid_argv(pid)
    return bool(argv) and str(BRIDGE) in argv


def _socket_state() -> tuple[str, str]:
    """Classify the bridge endpoint: ``("live" | "dead" | "undeterminable", reason)``.

    Same discipline as `socket_listener_live` in vendor/d200-local-bridge.py: only a refused
    connection or a missing path proves that no listener owns the endpoint. Every other failure
    (EMFILE, a drain timeout, EACCES, a path that is not a socket) means liveness cannot be
    excluded, so a caller must not unlink the path.

    The socket construction sits inside the `try`, so fd exhaustion cannot escape as a raise: this
    is a probe, and its answer is a state, not an exception.
    """
    if not SOCKET.exists():
        return _ENDPOINT_DEAD, "endpoint is absent"
    try:
        if not SOCKET.is_socket():
            return _ENDPOINT_UNDETERMINABLE, "path exists and is not a socket"
    except OSError as error:
        return _ENDPOINT_UNDETERMINABLE, f"{type(error).__name__}: {error}"
    probe = None
    try:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(_PROBE_TIMEOUT)
        probe.connect(str(SOCKET))
        return _ENDPOINT_LIVE, ""
    except (ConnectionRefusedError, FileNotFoundError):
        return _ENDPOINT_DEAD, "no listener accepted the connection"
    except OSError as error:
        return _ENDPOINT_UNDETERMINABLE, f"{type(error).__name__}: {error}"
    finally:
        if probe is not None:
            probe.close()


def _socket_live() -> bool:
    """True only when a probe reached a listening bridge. Undeterminable is not live.

    Only for callers whose question really is "is the bridge up yet?" — the post-spawn readiness
    poll. Every decision that can *destroy* something (reclaiming the path, spawning a second
    bridge) goes through `_socket_state()`, because collapsing undeterminable into False is what
    let the caller remove a live endpoint.
    """
    return _socket_state()[0] == _ENDPOINT_LIVE


def _undeterminable_endpoint(reason: str) -> RuntimeError:
    """One-line refusal used wherever the endpoint cannot be classified."""
    return RuntimeError(
        f"cannot verify whether a bridge is already listening on {SOCKET} ({reason}); "
        f"leaving the endpoint alone and not starting a second bridge"
    )


def _bus_serial() -> str:
    """The deck's serial as reported by the bus. Never stored, never hard-coded."""
    found = usb.detect()
    if found and found.get("mode") == "adb" and found.get("serial"):
        return str(found["serial"])
    return adb.serial_from_devices() or ""


def _usb_reports_adb(serial: str) -> bool:
    """True only when the USB layer itself reports this serial in ADB mode (H3).

    Deliberately not `_bus_serial()`, which falls back to `adb devices`: a server that can
    already see the deck is not a stale server, and restarting it would kill a healthy one.
    An unusable backend is not evidence either, so it means "do not touch the server".
    """
    try:
        found = usb.detect()
    except Exception:
        return False
    if not found or found.get("mode") != "adb":
        return False
    reported = found.get("serial")
    return reported is not None and str(reported) == serial


def _adb_serial() -> str:
    """Bus serial, switching the deck off HID first.

    The deck enumerates as HID or as ADB, never both, so a deck that is still on
    HID is moved over with the same 0x00ff report `play` uses before the bridge
    can open a session.
    """
    serial = _bus_serial()
    if serial:
        return serial
    usb.enable_adb()
    serial = _bus_serial()
    if not serial:
        raise RuntimeError("D200 is not enumerating through ADB: no bridge serial")
    return serial


def _device_ready(serial: str, *, timeout: float) -> bool:
    """Wait until the deck runs an allowlisted shell command again.

    `getprop` is the cheapest command the adb allowlist already permits, and a
    successful one proves the device shell is answering, which is what the
    bridge's own staging needs.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            result = adb.run(
                ["-s", serial, "shell", "getprop sys.usb.config"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except subprocess.SubprocessError:
            result = None
        if result is not None and result.returncode == 0:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def _spawn_bridge(serial: str, log) -> subprocess.Popen:
    env = os.environ.copy()
    previous = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(VENDOR) if not previous else str(VENDOR) + os.pathsep + previous
    return subprocess.Popen(
        [
            sys.executable,
            "-B",
            "-u",
            str(BRIDGE),
            "--adb",
            adb.require_adb(),
            "--serial",
            serial,
            "--state-file",
            str(BRIDGE_STATE),
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env=env,
    )


def _stop_owned_bridge(child: subprocess.Popen, *, timeout: float = 5.0) -> None:
    """Reap this attempt's own child only; a foreign bridge is never touched."""
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=timeout)


def require_bridge() -> None:
    """One-line refusal when the bridge this project spawns is not serving `SOCKET`, else return.

    `play` needs a bridge and must not start one. The player's first device-side act is
    `connect_bridge(BRIDGE_SOCKET)` before `videoOpen` (vendor/d200-color-play.py), so with nothing
    listening the child dies with a raw `ConnectionRefusedError` and the user is told neither what
    was missing nor what to run. `_ensure_bridge()` is the thing that *starts* a bridge, and it does
    real device work (it moves the deck off HID and waits for it to answer device commands), so it
    stays where it is: `studio`, the command that owns the copied app the bridge exists to serve.
    The documented rule is that a tool which starts a bridge owns exactly the process it created and
    releases only that one; `play` has no lifecycle for a long-lived process it outlives (it returns
    as soon as the player survives the grace window, while the bridge must serve the whole session).
    The user needs `ghostdeck studio` anyway: simultaneous operation *is* the shim, so there is no
    path where `play` works without the copy running.

    Only `play` and `studio` depend on the bridge. `stop` - the recovery command - and the read-only
    `detect`/`status` never touch it, and that must stay true: they are exactly what a user
    needs while the bridge is down. Do not move this call into `stop`.

    Liveness is not ownership (A-133): a live listener that no live bridge of ours owns would leave
    the player talking to a stranger, so it is refused with the same reasoning `launch()` uses.
    """
    endpoint, probe_reason = _socket_state()
    if endpoint == _ENDPOINT_LIVE:
        if _bridge_owner_live():
            return
        raise RuntimeError(
            f"a listener holds {SOCKET} but no live {BRIDGE.name} of ours owns it; refusing to start "
            f"the player against an unidentified bridge. Stop that process, or remove {SOCKET} if it "
            f"is a leftover, and retry"
        )
    if endpoint == _ENDPOINT_UNDETERMINABLE:
        raise _undeterminable_endpoint(probe_reason)
    raise RuntimeError(
        f"{BRIDGE_DOWN} ({probe_reason}), and the player reaches the deck "
        f"through it: run `ghostdeck studio` first (it starts the bridge and the Studio copy), then "
        f"re-run `ghostdeck play`"
    )


def _ensure_bridge() -> None:
    endpoint, probe_reason = _socket_state()
    if endpoint == _ENDPOINT_LIVE:
        return
    if endpoint == _ENDPOINT_UNDETERMINABLE:
        raise _undeterminable_endpoint(probe_reason)
    if not BRIDGE.is_file():
        raise RuntimeError(f"bridge missing: {BRIDGE}")
    log = _open_bridge_log()
    reason = "hidshim bridge socket did not come up"
    server_restarted = False
    try:
        for attempt in range(BRIDGE_ATTEMPTS):
            if attempt:
                time.sleep(BRIDGE_RETRY_DELAY)
            try:
                serial = _adb_serial()
            except Exception as error:
                reason = str(error) or type(error).__name__
                continue
            ready = _device_ready(serial, timeout=BRIDGE_READY_TIMEOUT)
            if not ready and not server_restarted and _usb_reports_adb(serial):
                # H3, observed on the physical deck: the USB layer reports the deck in ADB mode
                # while the *host adb server* still has no transport for it, so `adb devices` is
                # empty and every allowlisted device command fails. That server will never start
                # answering on its own, so the 25s wait above cannot succeed however often it is
                # repeated; restarting the server is what fixed it on the deck. Bounded to one
                # restart per bring-up: a genuinely dead deck must not make this loop kill a
                # server that other tools are using, once per attempt.
                server_restarted = True
                try:
                    adb.restart_server()
                except Exception as error:
                    reason = f"the host adb server could not be restarted: {error}"
                    continue
                ready = _device_ready(serial, timeout=BRIDGE_READY_TIMEOUT)
            if not ready:
                reason = "D200 stopped answering device commands after switching to ADB"
                if server_restarted:
                    reason += (
                        "; the host adb server was restarted (adb kill-server; adb start-server) "
                        "because the deck already enumerated through ADB, and it still does not answer"
                    )
                continue
            # Re-probe immediately before spawning: a bridge that came up meanwhile is used as
            # is, an unclassifiable endpoint refuses, and only a proven-dead path is reclaimed.
            endpoint, probe_reason = _socket_state()
            if endpoint == _ENDPOINT_LIVE:
                return
            if endpoint == _ENDPOINT_UNDETERMINABLE:
                raise _undeterminable_endpoint(probe_reason)
            try:
                SOCKET.unlink()
            except FileNotFoundError:
                pass
            child = _spawn_bridge(serial, log)
            deadline = time.monotonic() + BRIDGE_WAIT
            while time.monotonic() < deadline:
                if _socket_live():
                    return
                if child.poll() is not None:
                    reason = f"hidshim bridge exited with status {child.returncode}"
                    break
                time.sleep(0.2)
            _stop_owned_bridge(child)
    finally:
        log.close()
    raise RuntimeError(reason)

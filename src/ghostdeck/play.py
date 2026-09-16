"""Host-side JPEG playback on the D200.

Lifecycle lives here: `start_play`, `playing`, `stop`. Player identity and the zkswe bounce
are `playident`; the measured waits after stop are `playwait`. The names tests and the CLI
patch stay on this module -- the split files read them from here at call time.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ghostdeck import adb, devicebuild, state as gdstate, studio, usb
from ghostdeck.playident import (
    TRANSPORT_READY,
    VENDOR_DIR,
    VENDOR_PLAY,
    _ADB_TIMEOUT,
    _DECK_FIELDS,
    _DECK_PROBE,
    _HOST_STATE,
    _PROBE_LIMIT,
    _PS_TIMEOUT,
    _adb_entries,
    _adb_mutate,
    _cleanup_device,
    _clear_play_records,
    _deck_serial,
    _identity_path,
    _is_our_player,
    _kill_play,
    _load_identity,
    _player_identity,
    _probe_is_deck,
    _probe_start_time,
    _record_identity,
    _remove_identity,
    _signal_host_stated_player,
    _signal_vendor_players,
    _vendor_player_pids,
    _write_identity,
    deck_transport,
    is_vendor_player_argv,
)
from ghostdeck.playwait import (
    _HID_RETURN_POLL,
    _HID_RETURN_TIMEOUT,
    _HID_STABLE_SAMPLES,
    _SESSION_RELEASE_POLL,
    _SESSION_RELEASE_TIMEOUT,
    _TRANSPORT_RECOVERY_POLL,
    _TRANSPORT_RECOVERY_TIMEOUT,
    _TRANSPORT_STABLE_SAMPLES,
    _await_hid_return,
    _await_transport_recovery,
    _hid_stuck_message,
    _record_owner_alive,
    _session_released,
    _usb_mode,
)

# A player that dies immediately (a bad source, a missing device-side tool) must not be reported as
# a successful start (A-103). The window is a grace period, not a health check: it is long enough to
# catch an interpreter that starts and exits, and short enough to stay invisible to the user.
_PLAY_GRACE = 1.0

_TOOL_HINTS = {
    "ffmpeg": "brew install ffmpeg",
    "ffprobe": "brew install ffmpeg",
    "yt-dlp": "brew install yt-dlp",
}


def _require_tools(source: str) -> None:
    """Fail before any device work when the player's own external tools are missing."""
    tools = ["ffmpeg", "ffprobe"]
    if "://" in source:
        tools.append("yt-dlp")
    for tool in tools:
        if shutil.which(tool) is None:
            raise RuntimeError(f"{tool} not on PATH: install it ({_TOOL_HINTS[tool]})")


def _validate_source(source: str) -> None:
    """Reject a source that is neither an existing file nor a URL before any device work (A-103).

    SOURCE is handed straight to the player's argv, so a typo used to travel all the way to a
    child process that died on its first read - and was still reported as a successful start.
    """
    if "://" in source:
        return
    if not Path(source).is_file():
        raise RuntimeError(f"source is not a file and is not a URL: {source}")


def start_play(source: str, fit: str = "auto", start: float = 0.0, loop: bool = True) -> None:
    _require_tools(source)
    adb.require_adb()
    # Before any device work: an unusable SOURCE must fail cheaply and name the user's own input,
    # not travel to a child process that dies on its first read (A-103).
    _validate_source(source)
    # T19: the player's first device-side act is `connect_bridge(BRIDGE_SOCKET)` (before
    # `videoOpen`), so with nothing listening it dies inside the child with a raw
    # `ConnectionRefusedError` and the user is told nothing actionable. The refusal therefore comes
    # before anything that costs something: no state dir, no device-binary build, no USB probe, no
    # player.
    #
    # What differs by host is only the *remedy*. `require_bridge` is right while Studio is installed,
    # because `studio` is then the command that owns a bridge -- a tool that starts one owns exactly
    # that process, and `play` returns before the session ends, so it has no lifecycle for one. The
    # bridge itself is a byte-transparent transport started from `--adb`/`--serial` alone, and Studio
    # is only the keys glued to it, so with no official app there is no `studio` command to run and
    # requiring it blocked video playback outright. On that host `play` brings the bridge up and owns
    # it, which is the same rule applied by the only command left that can.
    studio.require_bridge_or_start_it()
    gdstate.ensure_dirs()
    devicebuild.ensure()
    found = usb.detect()
    if found is None or found.get("mode") in (None, "none"):
        # A missing backend is not a missing deck (A-102): `detect()` attaches the hint when it
        # could not reach a hardware conclusion, and reporting "no D200 on USB" here would blame
        # the hardware for an incomplete Python environment.
        dependency = (found or {}).get("dependency")
        if dependency:
            raise usb.MissingDependency(dependency)
        raise RuntimeError("no D200 on USB")
    if found["mode"] == "hid":
        usb.switch_hid_to_adb()
        found = usb.detect()
    if found is None or found.get("mode") != "adb":
        raise RuntimeError("deck is not in ADB after switch")
    try:
        _kill_play()
    except RuntimeError as error:
        # A new player is about to take over the pid, so an unverifiable predecessor is not fatal.
        print(f"warning: {error}", file=sys.stderr)
    if not VENDOR_PLAY.is_file():
        raise RuntimeError(f"vendor player missing: {VENDOR_PLAY}")
    env = dict(os.environ)
    env["GHOSTDECK_SERIAL"] = found.get("serial") or _deck_serial() or ""
    env["PYTHONPATH"] = str(VENDOR_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    argv = [
        sys.executable,
        "-B",
        "-u",
        str(VENDOR_PLAY),
        source,
        "--fps",
        "source",
        "--quality",
        "12",
        "--crop",
        "auto",
        "--fit",
        fit,
    ]
    if loop:
        argv.append("--loop")
    if start > 0:
        argv.extend(["--start", f"{start:.3f}"])
    proc = subprocess.Popen(
        argv,
        start_new_session=True,
        env=env,
    )
    # A-103: a player that died on startup must not be recorded as a running session, and the
    # command must not exit 0 - the user would be told playback started while nothing is playing.
    # Nothing is written before this check, so there is no stale record to clear on the way out.
    try:
        returncode = proc.wait(timeout=_PLAY_GRACE)
    except subprocess.TimeoutExpired:
        returncode = None
    if returncode is not None:
        raise RuntimeError(
            f"player exited with status {returncode} before it started; nothing is playing"
        )
    data = gdstate.update(play_pid=proc.pid)
    if data.get("play_pid") != proc.pid:
        # A-104: fail loudly rather than silently reporting a session that was never recorded.
        raise RuntimeError(f"could not record the player pid {proc.pid} in {gdstate.STATE_PATH}")
    _record_identity(proc.pid)


def playing() -> bool:
    return _is_our_player(gdstate.load().get("play_pid")) is True

def stop() -> None:
    """Stop our player, then restore the deck.

    The player's identity decides the exit code only, never whether the deck is restored: an
    unverifiable pid is left exactly as it is (nothing signalled, nothing erased) and the cleanup
    still runs *when Studio is not holding the gadget*, because skipping it would leave the stock
    UI stopped with no other command able to restore them.

    The parent `launch-studio-adb.py --play` path only stops the loop player. It never restarts
    `zkswe` while the copied Studio and the local bridge are up. Ghostdeck `stop` used to bounce
    anyway, which is what made a working parent-style session look broken: the bounce drops HID,
    transportRevive yanks ADB back, and the keys go with the gadget.
    """
    identity_error = None
    record_is_disposable = True
    session_was_playing = gdstate.load().get("play_pid") is not None
    keep_gadget = studio._socket_live()
    try:
        _kill_play(keep_record=True)
    except RuntimeError as error:
        identity_error = error
        record_is_disposable = False
    # The stock-UI restart in `_cleanup_device()` tears down a live media session, so wait for the
    # player to release first (see `_session_released`). Only a session that was actually playing can
    # be holding the transport, so a stop with nothing playing skips the wait entirely.
    if session_was_playing and not _session_released():
        if identity_error is not None:
            raise RuntimeError(
                f"the media session did not release within {_SESSION_RELEASE_TIMEOUT:.0f}s, so the "
                f"stock UI was left alone rather than cut the transport mid-stream; "
                f"re-run `ghostdeck stop` (as well as: {identity_error})"
            )
        raise RuntimeError(
            f"the media session did not release within {_SESSION_RELEASE_TIMEOUT:.0f}s, so the stock "
            f"UI was left alone rather than cut the transport mid-stream; re-run `ghostdeck stop`"
        )
    if keep_gadget:
        try:
            if not studio.running():
                studio.launch()
        except Exception as error:
            print(f"hidshim Studio copy skipped: {error}", file=sys.stderr)
    else:
        try:
            _cleanup_device()
        except RuntimeError as cleanup_error:
            if identity_error is None:
                raise
            raise RuntimeError(f"{cleanup_error} (as well as: {identity_error})") from cleanup_error
        if session_was_playing and not _await_hid_return(require_hid=True):
            hid_error = RuntimeError(_hid_stuck_message())
            if identity_error is not None:
                raise RuntimeError(
                    f"{hid_error} (as well as: {identity_error})"
                ) from hid_error
            raise hid_error
    if record_is_disposable:
        _clear_play_records()
    if identity_error is not None:
        raise identity_error

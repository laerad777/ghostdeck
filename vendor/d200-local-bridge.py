#!/usr/bin/env python3
"""Byte-transparent copied-Studio transport to the stock D200 zkgui process."""

import argparse
import collections
from contextlib import contextmanager, ExitStack
import errno
import fcntl
import json
import os
from pathlib import Path
import secrets
import select
import signal
import socket
import socketserver
import stat
import struct
import subprocess
import sys
import threading
import time

from d200_process_control import (
    DeviceAdmissionError, StopEndpoint, admission_lock_path, emit_diagnostic,
    managed_device_admission,
)
# FIX-5-T1's private-file/symlink discipline, reused rather than re-implemented:
# the bridge's own --state-file writer must not be a second, weaker scheme.
from d200_process_control import _state_kind, _write_private_file
import d200_video_stream as video_wire

DEFAULT_SOCKET = Path('/tmp/d200-adb-bridge.sock')
# Fixed device path the stock zkgui proxy execs for video sessions; staged by
# this bridge and removed again by its teardown.
STAGED_AGENT = '/tmp/d200-color-agent'
# Per-session staging directory on the deck: this prefix plus half the session
# token, so two sessions never share one.
SESSION_DIR_PREFIX = '/tmp/.d200-zkgui-'
SESSION_DIR_TOKEN_HEX = 16
SESSION_DIR_PROXY = 'proxy'
SESSION_DIR_PRELOAD = 'preload.so'
# A blocking `input` request (timeoutMs -1) is served in windows of this length
# instead of parking until a report arrives. The macOS shim enforces one
# host-side transport budget per request (D200_RPC_BUDGET_MS, 15 s), so a peer
# that only ever answers real reports turns a healthy but idle deck into a
# caller-visible ETIMEDOUT every 15 s; answering the same empty report that an
# expired positive timeout already returns means "no report in this window",
# which the shim's parser accepts. It also keeps this handler from outliving a
# closing handle by more than one window.
INPUT_IDLE_TICK_SECONDS = 0.5
# How long one start waits for the device-side proxy's forwarded port to answer
# with a BOOTSTRAP frame. Named, because it is only half of a bring-up budget:
# the host's own wait for this bridge's unix socket (src/ghostdeck/studio.py,
# BRIDGE_WAIT) starts BEFORE `_stage()` and this one starts after it, so the host
# can expire first on a deck whose device commands are slow -- and it then kills a
# bridge that is still inside its own budget. `bridgeStartupAttempt` (below)
# reports both halves of every attempt so the next hardware run attributes a slow
# bring-up instead of guessing at it. The value is unchanged from the revision
# that was measured; only the name is new.
PROXY_READINESS_SECONDS = 15.0
# One line per staged entry: '<directory>|<name>|<bytes>'. `ls -A` so a hidden
# extra entry cannot pass for a clean session directory, and `wc -c` so no
# stat(1) is required on the deck. Unparseable output is never treated as proof
# of ownership; see DeviceProxy._stale_siblings.
STALE_SIBLING_LIST_COMMAND = (
    f'for d in {SESSION_DIR_PREFIX}*; do [ -d "$d" ] || continue; '
    'for n in $(ls -A "$d" 2>/dev/null); do '
    'echo "$d|$n|$(wc -c < "$d/$n" 2>/dev/null)"; done; done'
)
ADB_SERIAL = ""
MAX_MESSAGE = 4096
MAX_PAYLOAD = 64 * 1024
# ADB reverse USB stalls when a write exceeds a 4KiB delivery unit. Keep
# syscalls at 4KiB, disable Nagle, and space every producer write.
USB_FORWARD_CHUNK = 4 * 1024
USB_FORWARD_GAP = 0.002
REPORT_BYTES = 1025

HELLO = 1
READY = 2
OUTPUT0 = 3
OUTPUT1 = 4
INPUT0 = 5
INPUT1 = 6
STOP = 7
ERROR = 8
RESTORED = 9
OUTPUT_ACK = 10
BOOTSTRAP = 11
RESTORING = 12
PING = 13
PONG = 14


class ProtocolError(RuntimeError):
    pass


class BridgeSocketInUse(RuntimeError):
    """The bridge socket is served by another instance and was left bound."""


class DeviceCommandError(RuntimeError):
    """Keep RuntimeError catch behavior without retaining command or output text."""

    def __init__(self, returncode):
        super().__init__('device command failed')
        self.returncode = (returncode if type(returncode) is int and
                           -(2 ** 31) <= returncode < 2 ** 31 else None)


class StagingError(RuntimeError):
    """The bridge could not be staged on the deck.

    Raised instead of whatever failed underneath, because the message is shown to
    the user verbatim at the top level: it names the step that failed, while the
    device command, its output and its exit status stay internal. Callers that
    only need to know the session is dead catch RuntimeError as before.
    """


class StateFileError(RuntimeError):
    """The bridge could not publish its own ``--state-file``.

    Raised instead of whichever ``OSError`` failed underneath, because this write
    happens before any device effect and its message is shown to the user verbatim
    at the top level, next to ``bridge_socket_in_use`` / ``bridge_stage_failed``.
    Callers catching ``RuntimeError`` keep working.
    """


class StartupError(RuntimeError):
    """The device proxy did not come up, reported as one line instead of a traceback.

    A `TimeoutError` from the forwarded-port wait used to escape `main()` as an
    unhandled exception whose last frame named `_start` and nothing about why: the
    real-deck log recorded nine of those with no way to tell a slow proxy from one
    being killed underneath it. The message is built from fixed vocabulary only --
    never device-supplied text, never the session directory -- and the `attempt`
    counters and timings travel in the `bridgeStartupAttempt` diagnostic.

    `RuntimeError` rather than `TimeoutError` on purpose: `TimeoutError` is an
    `OSError` subclass, and every caller in this module catches `OSError` for
    "the device went away". This failure is not that.
    """


def diagnostic_errno(error):
    try:
        number = error.errno if isinstance(error, OSError) else None
        return number if type(number) is int and number in errno.errorcode else None
    except Exception:
        return None


def endpoint_identity(path):
    """(st_dev, st_ino) of an existing endpoint, or None. Never follows a symlink."""
    try:
        information = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISSOCK(information.st_mode):
        return None
    return (information.st_dev, information.st_ino)


def write_private_state_file(path, text):
    """Publish the bridge's state file under FIX-5-T1's discipline.

    The write itself is `d200_process_control._write_private_file`, imported rather
    than copied: a unique temp beside the destination, 0600 before the rename, an
    atomic replace, and the temp removed if any step fails.

    The guard in front of it is what the destination needs, because `--state-file`
    is operator-supplied. The previous revision wrote a fixed `state.tmp` sibling
    with `write_text`, so a planted symlink on that path chose the file that
    received the record (an arbitrary write, at the temp's 0644, carrying the
    control token). A destination that is not a regular file is dropped instead of
    followed, so the record only ever lands in a file this call just created.

    A destination that cannot be dropped -- a directory at the path, or a parent
    that cannot be written -- is reported as one ``StateFileError`` rather than
    escaping as a traceback from ``path.unlink()`` / ``tempfile.mkstemp``: the
    record is not published, and the path is left exactly as it was.
    """
    if _state_kind(path) == 'foreign':
        try:
            path.unlink()
        except OSError as error:
            raise StateFileError(
                f'{path} is not an owned regular file and cannot be replaced'
            ) from error
    try:
        _write_private_file(path, text)
    except OSError as error:
        raise StateFileError(f'cannot write {path}: {error.strerror or error}') from error


def own_state_record(path):
    """The state record at `path` when it is readable, else None -- read safely.

    Only a regular file is read, never through a symlink, a fifo or a device, and
    junk is simply not ours rather than an error thrown out of a `finally` block.
    """
    if _state_kind(path) != 'regular':
        return None
    try:
        stored = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return stored if isinstance(stored, dict) else None


def socket_listener_live(path):
    """True when the AF_UNIX endpoint at `path` is served or cannot be proven dead.

    Only a refused connection or a missing path proves that no listener owns the
    endpoint. Every other failure (EMFILE, EAGAIN, a timeout, EACCES) means
    liveness cannot be excluded, so a caller must not unlink the path.
    """
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(1)
            probe.connect(str(path))
        return True
    except (ConnectionRefusedError, FileNotFoundError):
        return False
    except OSError:
        return True


def pending_write_observation(transport):
    """SO_NWRITE is a non-clearing Darwin byte count (SDK sys/socket.h).

    Use only the Python-exported option and integer getsockopt API. This host's
    Python does not export it: no guessed option number or SO_ERROR fallback.
    """
    result = dict(bytes=None, status='unsupported', errno=None)
    try:
        option = getattr(socket, 'SO_NWRITE', None)
        if sys.platform != 'darwin' or type(option) is not int:
            return result
        if transport is None:
            result['status'] = 'unavailable'
            return result
        count = transport.getsockopt(socket.SOL_SOCKET, option)
        if type(count) is int and 0 <= count < 2 ** 31:
            result.update(bytes=count, status='observed')
        else:
            result['status'] = 'invalid-result'
    except Exception as error:
        result.update(status='error', errno=diagnostic_errno(error))
    return result


def disable_nagle(transport):
    try:
        transport.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass



def send_all(transport, data, timeout=10):
    deadline = time.monotonic() + timeout
    remaining_data = memoryview(data)
    while remaining_data:
        remaining_time = deadline - time.monotonic()
        if remaining_time <= 0 or not select.select([], [transport], [], remaining_time)[1]:
            raise TimeoutError('device proxy write timed out')
        try:
            written = transport.send(remaining_data, socket.MSG_DONTWAIT)
        except (BlockingIOError, InterruptedError):
            continue
        if written <= 0:
            raise BrokenPipeError('device proxy stopped accepting data')
        remaining_data = remaining_data[written:]


def framed_json(value):
    encoded = json.dumps(value, separators=(',', ':')).encode('utf-8') + b'\n'
    if len(encoded) > MAX_MESSAGE:
        raise ProtocolError('response exceeds maximum size')
    return encoded


def parse_message(data):
    if not data or len(data) > MAX_MESSAGE or not data.endswith(b'\n'):
        raise ProtocolError('invalid message framing')
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError('invalid JSON') from exc
    if not isinstance(value, dict) or value.get('schemaVersion') != 1:
        raise ProtocolError('unsupported message schema')
    return value


class DeviceProxy:
    def __init__(self, adb, serial, proxy_binary, preload_library):
        self.adb = str(adb)
        self.serial = serial
        self.proxy_binary = Path(proxy_binary)
        self.preload_library = Path(preload_library)
        self.session_token = secrets.token_hex(16)
        self.remote_dir = f'{SESSION_DIR_PREFIX}{self.session_token[:SESSION_DIR_TOKEN_HEX]}'
        # True from the first staging command until this instance has removed the
        # directory again, i.e. while a session directory of its own may exist on
        # the deck. close() must not short-circuit past the removal while it is set.
        self.remote_dir_staged = False
        self.process = None
        self.transport_socket = None
        self.reader_stream = None
        self.forward_port = None
        self.device_port = None
        self.current_device_port = None
        self.replacement_device_port = None
        self.replacement_socket = None
        self.replacement_forward_port = None
        self.tx_sequence = 0
        self.rx_sequence = 0
        self.connection_generation = 0
        self.completed_rotations = 0
        self.send_lock = threading.RLock()
        self.output_lock = threading.Lock()
        self.condition = threading.Condition()
        self.inputs = [collections.deque(), collections.deque()]
        self.outputs_acked = [0, 0]
        self.inputs_received = [0, 0]
        self.pending_acks = {}
        self.pending_video = {}
        self.last_control_observation = None
        self.control_failure_observation = {}
        self.hid_handles = None
        self.video = None
        self.video_opening = False
        # True only after this instance pushed the fixed-path agent itself.
        self.agent_staged = False
        # The device admission held for this proxy's whole session; see start().
        self.admission = None
        self.reconnect_requested = False
        self.connected_at = 0.0
        self.ready = False
        self.restoring = False
        self.closed = False
        self.error = None
        self.reader = None
        self.heartbeat = None
        # Startup instrumentation: the 1-based ordinal of the `_start` this process
        # is running, and the last device command this bridge issued. Both are
        # diagnostics only -- nothing reads them to make a decision.
        self.startup_attempt = 0
        self.last_device_command = None

    def _run(self, *arguments, timeout=15):
        """Run one device command, recording it for the attribution diagnostics.

        `arguments[0]` is the verb (`shell`, `push`, `forward`); the rest of the
        argument list can name the session directory, so only the verb is kept. The
        record is what makes a USB disappearance attributable from this side: it is
        the last thing the bridge asked the deck to do before the transport went
        away. Read-only and lock-free (one assignment of a fresh dict, so an
        interrupted `_run` cannot see a half-written record), and it never raises.
        """
        verb = arguments[0] if arguments and type(arguments[0]) is str else 'unknown'
        started = time.monotonic()
        try:
            result = subprocess.run(
                [self.adb, '-s', self.serial, *arguments],
                capture_output=True, check=False, timeout=timeout,
            )
        except BaseException as error:
            self.last_device_command = dict(
                verb=verb, outcome='error', category=type(error).__name__,
                errno=diagnostic_errno(error), returnCode=None,
                seconds=round(time.monotonic() - started, 3),
            )
            raise
        self.last_device_command = dict(
            verb=verb, outcome='nonzero' if result.returncode else 'ok',
            category=None, errno=None,
            returnCode=result.returncode if result.returncode else None,
            seconds=round(time.monotonic() - started, 3),
        )
        if result.returncode:
            raise DeviceCommandError(result.returncode)
        return result

    def _remove_remote_dir(self):
        """Remove this instance's staging directory, then provable dead leftovers.

        Validated as strictly as before, and it keeps removing its own directory
        first. The siblings come second because a bridge that dies (SIGKILL, host
        crash, a failed stage) never removes its own, and nothing else ages them
        out, so they accumulate on the deck's /tmp forever.
        """
        try:
            path = self.remote_dir
            if self._session_dir_shape(path):
                self._remove_staging_dir(path)
            self._reap_stale_remote_dirs()
        finally:
            self.remote_dir_staged = False

    @staticmethod
    def _session_dir_shape(path):
        """True only for this bridge's own directory name: prefix + 16 hex digits."""
        if (type(path) is not str or not path.startswith(SESSION_DIR_PREFIX) or
                len(path) != len(SESSION_DIR_PREFIX) + SESSION_DIR_TOKEN_HEX):
            return False
        return all(c in '0123456789abcdef' for c in path[len(SESSION_DIR_PREFIX):])

    def _remove_staging_dir(self, path):
        try:
            self._run('shell', f'rm -rf {path}', timeout=5)
        except (DeviceCommandError, OSError, subprocess.SubprocessError):
            pass

    def _reap_stale_remote_dirs(self):
        """Best-effort removal of sibling session directories that are dead leftovers.

        A sibling is removed only when it can be *positively* classified as a
        session directory of this bridge: the session-directory name shape, and
        exactly the staged ``proxy`` / ``preload.so`` entries, each with a readable
        non-empty size. The name shape alone is not proof -- a foreign
        ``deadbeefdeadbeef`` matches it -- so this is deliberately not a wildcard
        removal, and anything it cannot classify is left alone.

        While this instance holds the deck admission it is also the only admitted
        bridge, so every classified sibling must belong to a session that is gone.
        Without that admission the reap takes the lock first and holds it, so no
        live bridge can appear halfway through.
        """
        with ExitStack() as guard:
            if self.admission is None and not self._lock_out_other_bridges(guard):
                return
            try:
                listing = self._run('shell', STALE_SIBLING_LIST_COMMAND, timeout=5)
            except (DeviceCommandError, OSError, subprocess.SubprocessError):
                return
            for path in self._stale_siblings(listing):
                self._remove_staging_dir(path)

    def _lock_out_other_bridges(self, guard):
        """Take the admission lock into `guard`; False when another bridge owns it.

        Holding it for the whole reap is what turns "no live bridge owns a sibling"
        into a proof rather than a guess. A missing lock file means no bridge was
        ever admitted, and an unreadable one -- including a planted symlink, which
        `O_NOFOLLOW` refuses to follow -- is not proof either way, so the reap is
        skipped rather than run unguarded.
        """
        check = getattr(os, 'O_NOFOLLOW', 0)
        try:
            descriptor = os.open(admission_lock_path(), os.O_RDWR | check)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        guard.callback(os.close, descriptor)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _stale_siblings(self, listing):
        """The directories in a `STALE_SIBLING_LIST_COMMAND` listing that are ours.

        Fail closed: an entry whose name or size cannot be read poisons its whole
        directory, and a directory that is not exactly a staged pair is not a
        leftover this bridge can claim.

        The byte sizes are deliberately not compared with this build's binaries. A
        leftover is by definition left behind by an *earlier* revision -- the deck's
        measured backlog was staged on Sep 5/7 while this host's artifacts are
        today's -- and nothing on the deck records which build wrote a directory.
        Requiring equality with the current binary made every older leftover
        unclassifiable, so the reap that H2 asked for removed none of the seven
        directories it was measured on. What is left is the proof the deck can
        actually give: a session-directory name, exactly the two staged entry names
        (enumerated with `ls -A`, so a hidden third entry disqualifies it), and a
        readable, non-empty size for each entry.
        """
        stdout = getattr(listing, 'stdout', None)
        if not stdout:
            return []
        staged = sorted((SESSION_DIR_PROXY, SESSION_DIR_PRELOAD))
        contents = {}
        for line in stdout.decode('utf-8', 'replace').splitlines():
            fields = line.strip().split('|')
            directory = fields[0] if fields else ''
            entries = contents.setdefault(directory, {})
            try:
                if len(fields) != 3 or entries is None:
                    raise ValueError
                size = int(fields[2])
                if size <= 0:
                    raise ValueError
                entries[fields[1]] = size
            except ValueError:
                contents[directory] = None
        return sorted(
            directory for directory, entries in contents.items()
            if entries is not None and sorted(entries) == staged
            and directory != self.remote_dir
            and self._session_dir_shape(directory)
        )

    def _stage(self):
        if (not self.proxy_binary.is_file() or not self.preload_library.is_file() or
                not self.proxy_binary.with_name('d200-color-agent').is_file()):
            raise StagingError('build d200-zkgui-proxy, d200-color-agent and libd200-zkgui-preload.so first')
        self.remote_dir_staged = True
        for step, arguments in (
            ('create the session directory',
             ('shell', f'rm -rf {self.remote_dir}; mkdir -m 700 {self.remote_dir}')),
            ('push the proxy',
             ('push', str(self.proxy_binary), f'{self.remote_dir}/{SESSION_DIR_PROXY}')),
            ('push the preload library',
             ('push', str(self.preload_library), f'{self.remote_dir}/{SESSION_DIR_PRELOAD}')),
            ('set the staged file modes',
             ('shell', f'chmod 700 {self.remote_dir}/{SESSION_DIR_PROXY}; '
                       f'chmod 600 {self.remote_dir}/{SESSION_DIR_PRELOAD}')),
        ):
            try:
                self._run(*arguments)
            except DeviceCommandError as error:
                status = '' if error.returncode is None else f' (exit {error.returncode})'
                raise StagingError(f'could not {step} on the device: {error}{status}') from error

    def _local_artifact_sizes(self):
        """The sizes of the two artifacts this build stages, or None if unmeasurable.

        Deliberately local-only: the T13 reap fix removed the sampling of these
        numbers against the deck, because a leftover is by definition from an
        earlier build. Here they are the expectation the deck's own report is
        checked against, which is a different question.
        """
        try:
            return {
                SESSION_DIR_PROXY: self.proxy_binary.stat().st_size,
                SESSION_DIR_PRELOAD: self.preload_library.stat().st_size,
            }
        except OSError:
            return None

    def _staged_entries_query(self):
        """One shell round trip reporting the deck's sizes for the two staged files."""
        return (
            f'for n in {SESSION_DIR_PROXY} {SESSION_DIR_PRELOAD}; do '
            f'echo "$n|$(wc -c < "{self.remote_dir}/$n" 2>/dev/null)"; done'
        )

    def _staged_entries_present(self):
        """True only when the deck provably still holds what this build stages.

        A transport revive happens while the USB link is being reconfigured, and
        the deck-side files usually survive it -- so re-pushing ~77 KB on every
        revive is traffic during exactly the moment the link is least able to carry
        it. The real-deck log this answers showed 56 revives against 60 dropped
        streams, i.e. dozens of avoidable push round trips per session.

        Anything this check cannot prove answers False, which means "stage again":
        a failed command, unparseable output, a missing entry, a size that differs
        from the local artifact. Fail-closed, so the only thing the skip can do is
        save commands; it can never conclude that files are present when they are
        not. The vocabulary is the same `echo` / `wc -c` the sibling listing already
        relies on, because the deck's shell is toybox and `stat(1)` is not assured.
        """
        expected = self._local_artifact_sizes()
        if expected is None:
            return False
        try:
            result = self._run('shell', self._staged_entries_query(), timeout=5)
        except (DeviceCommandError, OSError, subprocess.SubprocessError):
            return False
        stdout = getattr(result, 'stdout', None) or b''
        seen = {}
        for line in stdout.decode('utf-8', 'replace').splitlines():
            fields = line.strip().split('|')
            if len(fields) != 2:
                return False
            try:
                seen[fields[0]] = int(fields[1])
            except ValueError:
                return False
        return seen == expected

    def _restore_staged_modes(self):
        """Re-apply the staged modes after a reuse; False when the deck refused."""
        try:
            self._run(
                'shell',
                f'chmod 700 {self.remote_dir}; '
                f'chmod 700 {self.remote_dir}/{SESSION_DIR_PROXY}; '
                f'chmod 600 {self.remote_dir}/{SESSION_DIR_PRELOAD}',
                timeout=5,
            )
        except (DeviceCommandError, OSError, subprocess.SubprocessError):
            return False
        return True

    def _ensure_staged_for_revive(self):
        """Reuse the deck-side staging when it is provably intact, else re-stage.

        Returns True when it re-staged. The reuse branch restores the directory and
        file modes (`chmod`) because those are the part the check does not prove,
        and keeps the full `_stage()` as the answer to every uncertainty.
        """
        if self._staged_entries_present() and self._restore_staged_modes():
            emit_diagnostic(sys.stderr, dict(
                event='transportReviveReusedStage', pid=os.getpid(),
                clock='host-monotonic',
            ))
            return False
        self._stage()
        return True

    def _stage_video_agent(self, deadline):
        """Push the native agent to its fixed path; record that this instance owns it."""
        for arguments in (
            ('push', str(self.proxy_binary.with_name('d200-color-agent')), STAGED_AGENT),
            ('shell', f'chmod 700 {STAGED_AGENT}'),
        ):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('video agent staging timed out')
            self._run(*arguments, timeout=remaining)
        if time.monotonic() >= deadline:
            raise TimeoutError('video agent staging timed out')
        with self.condition:
            self.agent_staged = True

    def _remove_staged_agent(self):
        """Remove the fixed-path agent, after the proxy that could exec it is gone.

        The stock zkgui proxy execs /tmp/d200-color-agent by fixed path, so it
        survives a session unless the bridge that staged it removes it. The
        per-session directory has its own cleanup; this is the one path that had
        none, and only the instance that pushed it removes it.
        """
        with self.condition:
            if not self.agent_staged:
                return
        try:
            self._run('shell', f'rm -f {STAGED_AGENT}', timeout=5)
        except (DeviceCommandError, OSError, subprocess.SubprocessError) as error:
            print(f'bridge_agent_remove_failed path={STAGED_AGENT} error={type(error).__name__}',
                  file=sys.stderr, flush=True)
            return
        with self.condition:
            self.agent_staged = False
        print(f'bridge_agent_removed path={STAGED_AGENT}', file=sys.stderr, flush=True)

    def _read_exact(self, stream, length, deadline=None):
        output = bytearray()
        while len(output) < length:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('device proxy bootstrap timed out')
                self.transport_socket.settimeout(remaining)
            block = stream.read(length - len(output))
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError('device proxy bootstrap timed out')
            if not block:
                raise EOFError('device proxy stream closed')
            output.extend(block)
        return bytes(output)

    def _read_frame(self, deadline=None):
        stream = self.reader_stream
        generation = self.connection_generation
        header = self._read_exact(stream, 16, deadline)
        if header[:4] != b'D2PX' or header[4] != 1:
            raise ProtocolError('invalid device frame header')
        kind = header[5]
        flags, length, sequence = struct.unpack('>HII', header[6:])
        with self.condition:
            if generation != self.connection_generation:
                raise ProtocolError('connection changed during device frame')
            if flags or length > MAX_PAYLOAD or sequence != self.rx_sequence:
                raise ProtocolError('invalid device frame metadata')
            self.rx_sequence += 1
        payload = self._read_exact(stream, length, deadline) if length else b''
        with self.condition:
            if generation != self.connection_generation:
                raise ProtocolError('connection changed during device frame')
        return kind, payload

    def _send_frame(self, kind, payload=b'', *, timeout=10):
        if len(payload) > MAX_PAYLOAD:
            raise ProtocolError('device payload exceeds maximum size')
        with self.send_lock:
            if self.closed or self.error is not None or self.transport_socket is None:
                raise RuntimeError(self.error or 'device proxy is closed')
            if self.reconnect_requested:
                raise RuntimeError('device proxy connection is rotating')
            if self.tx_sequence >= 2 ** 32:
                raise ProtocolError('control sequence exhausted')
            sequence = self.tx_sequence
            self.tx_sequence += 1
            frame = (
                b'D2PX' + bytes((1, kind, 0, 0)) +
                struct.pack('>II', len(payload), sequence) + payload
            )
            try:
                send_all(self.transport_socket, frame, timeout=timeout)
            except (BrokenPipeError, OSError, ValueError) as exc:
                with self.condition:
                    self.error = str(exc)
                    self.closed = True
                    self.condition.notify_all()
                try:
                    self.transport_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                raise RuntimeError('device proxy write failed') from exc
            return sequence

    def _send_acknowledged(self, kind, payload=b'', *, timeout, failure):
        with self.send_lock:
            with self.condition:
                if self.closed or self.error is not None or self.reconnect_requested:
                    raise RuntimeError(self.error or 'device proxy connection unavailable')
                if len(self.pending_acks) >= 64:
                    raise ProtocolError('too many pending device acknowledgements')
                ticket = (self.connection_generation, self.tx_sequence)
                self.pending_acks[ticket] = False
            try:
                self._send_frame(kind, payload)
            except BaseException:
                with self.condition:
                    self.pending_acks.pop(ticket, None)
                raise
        deadline = time.monotonic() + timeout
        try:
            with self.condition:
                while True:
                    if (ticket[0] != self.connection_generation or
                            self.reconnect_requested):
                        raise RuntimeError('device proxy connection changed before acknowledgement')
                    if self.error is not None or self.closed:
                        raise RuntimeError(self.error or 'device proxy closed before acknowledgement')
                    if self.pending_acks.get(ticket):
                        return
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(failure)
                    self.condition.wait(remaining)
        finally:
            with self.condition:
                self.pending_acks.pop(ticket, None)

    def start(self):
        # Rejection must precede staging AND cleanup, which itself has device
        # effects. Recovery independently rejects already-running controllers.
        # Held for the whole session, not only for startup: two bridges that
        # serialize just their startup still both stage to, and drive, one deck.
        self.admission = ExitStack()
        try:
            self.admission.enter_context(managed_device_admission())
            self._start_with_cleanup()
        except BaseException:
            self._release_admission()
            raise

    def _release_admission(self):
        """Drop the deck admission. Idempotent: start()'s failure path and close()
        both call it, and a second close must not hold the deck forever."""
        admission, self.admission = self.admission, None
        if admission is not None:
            admission.close()

    def _start_with_cleanup(self):
        try:
            self._start()
        except Exception as error:
            with self.condition:
                self.error = str(error)
                self.closed = True
                self.condition.notify_all()
            if self.transport_socket is not None:
                try:
                    self.transport_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            try:
                self.close()
            except Exception as cleanup_error:
                error.add_note(f'startup cleanup failed: {cleanup_error}')
            raise

    def _begin_startup_attempt(self):
        self.startup_attempt += 1
        return self.startup_attempt

    def _report_startup(self, attempt, stage_started, readiness_started, error):
        """One greppable record per start attempt: what it cost and how it ended.

        Fixed keys and bounded values, written through the module's existing
        `emit_diagnostic`, so the next real-deck log can be counted (`grep -c
        bridgeStartupAttempt`) and the slow phase identified without a deck in
        hand. `stageSeconds` covers `_stage()`; `readinessSeconds` covers the
        forwarded-port wait, and is null when the failure happened before that wait
        began. `lastDeviceCommand` is the last command this bridge issued, which is
        what makes a device that vanished mid-attempt attributable.
        """
        try:
            now = time.monotonic()
            record = dict(
                event='bridgeStartupAttempt', pid=os.getpid(), attempt=attempt,
                clock='host-monotonic',
                stageSeconds=(round((readiness_started if readiness_started is not None
                                     else now) - stage_started, 3)),
                readinessSeconds=(None if readiness_started is None
                                  else round(now - readiness_started, 3)),
                outcome='ready' if error is None else 'failed',
                failure=None if error is None else type(error).__name__,
                lastDeviceCommand=(dict(self.last_device_command)
                                   if self.last_device_command else None),
            )
            emit_diagnostic(sys.stderr, record)
        except Exception:
            pass

    @staticmethod
    def _startup_failure_reason(error):
        """Fixed vocabulary only: never device-supplied text, never the session dir."""
        if type(error) is TimeoutError:
            return (f'the device proxy did not become ready within '
                    f'{PROXY_READINESS_SECONDS:g}s on its forwarded port')
        if type(error) is ProtocolError:
            return 'the device proxy did not enter raw transport mode'
        return f'the device proxy failed to start ({type(error).__name__})'

    def _start(self):
        """Start the device proxy, reporting each attempt and failing in one line.

        `_stage()` keeps raising its own types unchanged, so the H4
        `bridge_stage_failed` path is untouched. Everything after staging is a
        startup failure that used to escape as a traceback: it is reported through
        `bridgeStartupAttempt` and raised as `StartupError`, which `main()` turns
        into one line and a non-zero exit.
        """
        attempt = self._begin_startup_attempt()
        stage_started = time.monotonic()
        readiness_started = None
        try:
            self._stage()
            readiness_started = time.monotonic()
            self._start_proxy_transport()
        except BaseException as error:
            self._report_startup(attempt, stage_started, readiness_started, error)
            if readiness_started is None:
                raise
            raise StartupError(self._startup_failure_reason(error)) from error
        self._report_startup(attempt, stage_started, readiness_started, None)

    def _start_proxy_transport(self):
        device_port = 30000 + int(self.session_token[:4], 16) % 20000
        self.device_port = device_port
        self.current_device_port = device_port
        self.process = subprocess.Popen(
            [
                self.adb, '-s', self.serial, 'shell', 'exec',
                f'{self.remote_dir}/proxy', '--host-port', str(device_port),
                '--session-dir', self.remote_dir,
                '--preload', f'{self.remote_dir}/preload.so',
            ],
            stdin=subprocess.PIPE,
            stdout=sys.stderr,
            stderr=sys.stderr,
            start_new_session=True,
        )
        self.forward_port = self._create_forward(device_port)
        deadline = time.monotonic() + PROXY_READINESS_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('device proxy socket did not become ready')
            try:
                self.transport_socket = socket.create_connection(
                    ('127.0.0.1', self.forward_port), timeout=min(1, remaining),
                )
                disable_nagle(self.transport_socket)
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('device proxy socket did not become ready')
                time.sleep(max(0, min(0.1, deadline - time.monotonic())))
                continue
            self.reader_stream = self.transport_socket.makefile('rb', buffering=0)
            try:
                kind, payload = self._read_frame(deadline)
                break
            except EOFError:
                # ADB accepts locally before the device listener is ready.
                # Retry only before any complete header or HELLO was exchanged.
                if self.rx_sequence:
                    raise
                self.reader_stream.close()
                self.transport_socket.close()
                self.reader_stream = self.transport_socket = None
                time.sleep(max(0, min(0.1, deadline - time.monotonic())))
        if kind != BOOTSTRAP or payload:
            raise ProtocolError('device proxy did not enter raw transport mode')
        self.transport_socket.settimeout(None)
        self.reader = threading.Thread(target=self._reader_loop, name='d200-proxy-reader', daemon=True)
        self.reader.start()
        self._send_frame(HELLO, self.session_token.encode('ascii'),
                         timeout=max(0, deadline - time.monotonic()))
        with self.condition:
            while not self.ready and self.error is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('stock zkgui proxy readiness timed out')
                self.condition.wait(remaining)
            if self.error is not None:
                raise RuntimeError(self.error)
            if time.monotonic() >= deadline:
                raise TimeoutError('stock zkgui proxy readiness timed out')
        # Stock service startup can clear /tmp/d200-*; stage after it is ready,
        # before publishing the host bridge socket or accepting video OPEN.
        self._stage_video_agent(deadline)
        self.connected_at = time.monotonic()
        self.heartbeat = threading.Thread(
            target=self._heartbeat_loop, name='d200-proxy-heartbeat', daemon=True,
        )
        self.heartbeat.start()

    def _adb_ready(self):
        try:
            result = self._run('shell', 'echo', 'd200-adb-ready', timeout=2)
        except (DeviceCommandError, OSError, subprocess.SubprocessError):
            return False
        output = result.stdout.decode('utf-8', errors='replace').strip() if result.stdout else ''
        return output == 'd200-adb-ready'

    def _enable_adb(self):
        if self._adb_ready():
            return
        import hid
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self._adb_ready():
                return
            matches = [entry for entry in hid.enumerate(0x2207, 0x0019)
                       if entry.get('serial_number') == self.serial
                       and entry.get('interface_number') == 0]
            if len(matches) == 1:
                device = hid.device()
                try:
                    device.open_path(matches[0]['path'])
                    packet = bytearray(1025)
                    packet[1:3] = b'||'
                    packet[3:5] = (0x00ff).to_bytes(2, 'big')
                    written = device.write(packet)
                    if written != len(packet):
                        raise RuntimeError(f'short HID-to-ADB write: {written}')
                finally:
                    device.close()
                ready_deadline = time.monotonic() + 12
                while time.monotonic() < ready_deadline:
                    if self._adb_ready():
                        return
                    time.sleep(0.4)
            time.sleep(0.4)
        raise RuntimeError('D200 did not enumerate through ADB')

    def _reap_proxy_process(self):
        process = self.process
        self.process = None
        if process is None:
            return
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=2)
            except OSError:
                pass
        except OSError:
            pass

    def _revive_transport(self):
        """Rebuild the ADB proxy after control-eof without dropping the host unix server."""
        with self.condition:
            if self.closed:
                return
            self.ready = False
        self._enable_adb()
        with self.send_lock:
            with self.condition:
                if self.closed:
                    return
                self.connection_generation += 1
                if self.connection_generation >= 2 ** 64:
                    raise ProtocolError('control generation exhausted')
                self.pending_acks.clear()
                self.pending_video.clear()
                self.rx_sequence = 0
                self.tx_sequence = 0
            for port in {self.forward_port, self.replacement_forward_port} - {None}:
                try:
                    self._run('forward', '--remove', f'tcp:{port}', timeout=5)
                except (DeviceCommandError, OSError, subprocess.SubprocessError):
                    pass
            self.forward_port = self.replacement_forward_port = None
            for resource in (self.reader_stream, self.transport_socket):
                if resource is not None:
                    try:
                        resource.close()
                    except OSError:
                        pass
            self.reader_stream = self.transport_socket = None
            self._reap_proxy_process()
            # Skip the two pushes when the deck provably still holds them: a revive
            # is already the moment the link is least reliable, and re-staging on
            # every dropped stream was the bulk of the observed churn.
            self._ensure_staged_for_revive()
            device_port = 30000 + int(self.session_token[:4], 16) % 20000
            self.device_port = device_port
            self.current_device_port = device_port
            self.process = subprocess.Popen(
                [
                    self.adb, '-s', self.serial, 'shell', 'exec',
                    f'{self.remote_dir}/proxy', '--host-port', str(device_port),
                    '--session-dir', self.remote_dir,
                    '--preload', f'{self.remote_dir}/preload.so',
                ],
                stdin=subprocess.PIPE,
                stdout=sys.stderr,
                stderr=sys.stderr,
                start_new_session=True,
            )
            self.forward_port = self._create_forward(device_port)
            deadline = time.monotonic() + PROXY_READINESS_SECONDS
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('device proxy socket did not become ready')
                try:
                    self.transport_socket = socket.create_connection(
                        ('127.0.0.1', self.forward_port), timeout=min(1, remaining),
                    )
                    disable_nagle(self.transport_socket)
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('device proxy socket did not become ready')
                    time.sleep(max(0, min(0.1, deadline - time.monotonic())))
                    continue
                self.reader_stream = self.transport_socket.makefile('rb', buffering=0)
                try:
                    kind, payload = self._read_frame(deadline)
                    break
                except EOFError:
                    if self.rx_sequence:
                        raise
                    self.reader_stream.close()
                    self.transport_socket.close()
                    self.reader_stream = self.transport_socket = None
                    time.sleep(max(0, min(0.1, deadline - time.monotonic())))
            if kind != BOOTSTRAP or payload:
                raise ProtocolError('device proxy did not enter raw transport mode')
            self.transport_socket.settimeout(None)
            self._send_frame(HELLO, self.session_token.encode('ascii'),
                             timeout=max(0, deadline - time.monotonic()))
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('stock zkgui proxy readiness timed out')
                kind, payload = self._read_frame(deadline)
                if kind == READY:
                    break
                if kind == PONG:
                    continue
                raise ProtocolError(f'unexpected device frame kind {kind}')
            with self.condition:
                self.ready = True
                self.connected_at = time.monotonic()
                self.condition.notify_all()
            self._stage_video_agent(deadline)
            if self.heartbeat is None or not self.heartbeat.is_alive():
                self.heartbeat = threading.Thread(
                    target=self._heartbeat_loop, name='d200-proxy-heartbeat', daemon=True,
                )
                self.heartbeat.start()

    def _reader_loop(self):
        while True:
            with self.condition:
                generation = self.connection_generation
            try:
                kind, payload = self._read_frame()
                with self.condition:
                    if self.closed:
                        return
                    if generation != self.connection_generation or self.reconnect_requested:
                        continue
                    if kind == READY:
                        self.ready = True
                    elif kind == OUTPUT_ACK:
                        if len(payload) != 4:
                            raise ProtocolError('invalid output acknowledgement')
                        ticket = (generation, struct.unpack('>I', payload)[0])
                        if ticket in self.pending_acks:
                            self.pending_acks[ticket] = True
                    elif kind in (22, 24, 26):
                        envelope = (b'D2PX' + bytes((1, kind, 0, 0)) +
                                    struct.pack('>II', len(payload), 0) + payload)
                        try:
                            _, _, fields = video_wire.decode_control(envelope)
                        except video_wire.ProtocolError as exc:
                            raise ProtocolError('invalid video control reply') from exc
                        ticket = (fields['generation'], fields['request_sequence'])
                        pending = self.pending_video.get(ticket)
                        if (pending is not None and ticket[0] == generation and
                                pending['kind'] == kind and
                                pending['session'] == fields['session'] and
                                (fields['epoch'] == pending['epoch'] or
                                 (kind == 22 and fields['epoch'] == 1 and
                                  fields['result_code'] == 0))):
                            pending['reply'] = fields
                            self._control_stamp(pending.get('observation'), 'replyReceived')
                    elif kind in (INPUT0, INPUT1):
                        endpoint = 0 if kind == INPUT0 else 1
                        if len(self.inputs[endpoint]) >= 64:
                            raise ProtocolError(f'HID input queue overflow on interface {endpoint}')
                        self.inputs[endpoint].append(payload)
                        self.inputs_received[endpoint] += 1
                    elif kind == ERROR:
                        self.error = payload.decode('utf-8', errors='replace') or 'device proxy error'
                    elif kind == RESTORING:
                        self.restoring = True
                    elif kind == RESTORED:
                        self.restoring = True
                        self.closed = True
                    elif kind != PONG:
                        raise ProtocolError(f'unexpected device frame kind {kind}')
                    self.condition.notify_all()
                if kind in (ERROR, RESTORED):
                    return
            except (EOFError, OSError, ProtocolError) as exc:
                with self.condition:
                    if self.closed:
                        return
                    if generation != self.connection_generation:
                        continue
                    reconnect = self.reconnect_requested
                try:
                    if reconnect:
                        self._reconnect_transport()
                    else:
                        print(f'transport_revive reason={exc}', file=sys.stderr, flush=True)
                        emit_diagnostic(sys.stderr, dict(event='transportRevive', reason=str(exc)))
                        self._revive_transport()
                except (EOFError, OSError, ProtocolError, RuntimeError,
                        subprocess.SubprocessError, TimeoutError) as reconnect_error:
                    if reconnect:
                        with self.condition:
                            if not self.restoring and self.error is None:
                                self.error = str(reconnect_error)
                            self.closed = True
                            self.reconnect_requested = False
                            self.condition.notify_all()
                        return
                    print(f'transport_revive_failed error={reconnect_error}', file=sys.stderr, flush=True)
                    emit_diagnostic(sys.stderr, dict(
                        event='transportReviveFailed', error=str(reconnect_error)))
                    time.sleep(1)
                    continue
                else:
                    print('transport_revive_succeeded', file=sys.stderr, flush=True)
                    emit_diagnostic(sys.stderr, dict(event='transportReviveSucceeded'))
                    continue

    def _reconnect_transport(self):
        with self.send_lock:
            with self.condition:
                self.connection_generation += 1
                if self.connection_generation >= 2 ** 64:
                    raise ProtocolError('control generation exhausted')
                self.pending_acks.clear()
                self.pending_video.clear()
                for reports in self.inputs:
                    reports.clear()
                self.ready = False
                self.condition.notify_all()
            for resource in (self.reader_stream, self.transport_socket):
                if resource is not None:
                    try:
                        resource.close()
                    except OSError:
                        pass
            transport_socket = self.replacement_socket
            self.replacement_socket = None
            if transport_socket is None:
                deadline = time.monotonic() + 10
                while True:
                    try:
                        transport_socket = socket.create_connection(
                            ('127.0.0.1', self.forward_port), timeout=1,
                        )
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError('replacement proxy transport did not connect')
                        time.sleep(0.1)
            transport_socket.settimeout(10)
            disable_nagle(transport_socket)
            self.transport_socket = transport_socket
            self.reader_stream = transport_socket.makefile('rb', buffering=0)
            self.rx_sequence = 0
            self.tx_sequence = 0
            kind, payload = self._read_frame()
            if kind != BOOTSTRAP or payload:
                raise ProtocolError('replacement transport missed bootstrap')
            capability = self.session_token.encode('ascii')
            frame = (
                b'D2PX' + bytes((1, HELLO, 0, 0)) +
                struct.pack('>II', len(capability), self.tx_sequence) + capability
            )
            self.tx_sequence += 1
            send_all(transport_socket, frame)
            kind, payload = self._read_frame()
            if kind != READY or payload:
                raise ProtocolError('replacement transport missed readiness')
            old_forward_port = self.forward_port
            if self.replacement_forward_port is not None:
                self.forward_port = self.replacement_forward_port
                self.replacement_forward_port = None
                self.current_device_port = self.replacement_device_port
                self.replacement_device_port = None
                self._run('forward', '--remove', f'tcp:{old_forward_port}', timeout=5)
            transport_socket.settimeout(None)
            with self.condition:
                if self.closed or self.error is not None:
                    raise RuntimeError(self.error or 'device proxy closed during rotation')
                self.ready = True
                self.reconnect_requested = False
                self.connected_at = time.monotonic()
                self.completed_rotations += 1
                self.condition.notify_all()

    def _heartbeat_loop(self):
        while True:
            with self.condition:
                if self.closed or self.error is not None:
                    return
                reconnecting = self.reconnect_requested
                rotate = (not self.reconnect_requested and not self.hid_handles and
                          (self.video is None or self.video.status['cleanup'] == 'proven') and
                          time.monotonic() - self.connected_at >= 180)
            if reconnecting:
                time.sleep(0.1)
                continue
            if rotate:
                try:
                    self.rotate_transport()
                    owner = self.video
                    if owner is not None and owner.epoch == 1:
                        try:
                            owner.refresh(time.monotonic() + 5)
                        except (OSError, RuntimeError, ValueError):
                            pass
                except (OSError, RuntimeError, subprocess.SubprocessError):
                    return
                time.sleep(0.1)
                continue
            try:
                self._send_frame(PING, timeout=1)
            except TimeoutError:
                time.sleep(0.2)
                continue
            except (RuntimeError, TimeoutError):
                return
            time.sleep(1)

    def rotate_transport(self):
        try:
            self._rotate_transport()
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            with self.condition:
                self.error = self.error or str(exc)
                self.closed = True
                self.reconnect_requested = False
                self.condition.notify_all()
            for transport in (self.transport_socket, self.replacement_socket):
                if transport is not None:
                    try:
                        transport.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
            if self.replacement_socket is not None:
                self.replacement_socket.close()
                self.replacement_socket = None
            raise

    def _rotate_transport(self):
        with self.output_lock:
            with self.send_lock:
                with self.condition:
                    if self.closed or self.error is not None:
                        raise RuntimeError(self.error or 'device proxy closed')
                    if self.reconnect_requested:
                        return
                    # Maintenance must not cut the control connection while a
                    # video owner still needs its terminal cleanup proof.
                    # Recheck under the publication lock: videoOpen can race
                    # the heartbeat's earlier idle observation.
                    if self.hid_handles or (self.video is not None and self.video.status['cleanup'] != 'proven'):
                        return
                    self.reconnect_requested = True
                    self.condition.notify_all()
                deadline = time.monotonic() + 20
                replacement_device_port = (
                    self.device_port + 1
                    if self.current_device_port == self.device_port
                    else self.device_port
                )
                replacement_port = self._create_forward(replacement_device_port)
                self.replacement_forward_port = replacement_port
                self.replacement_device_port = replacement_device_port
                self.replacement_socket = socket.create_connection(
                    ('127.0.0.1', replacement_port), timeout=5,
                )
                self.replacement_socket.settimeout(None)
                try:
                    self.transport_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            with self.condition:
                while (self.reconnect_requested and self.error is None and not self.closed):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('replacement transport readiness timed out')
                    self.condition.wait(remaining)
                if self.error is not None or self.closed:
                    raise RuntimeError(self.error or 'device proxy closed during rotation')

    def output(self, endpoint, report):
        if endpoint not in (0, 1):
            raise ProtocolError('invalid HID interface')
        if len(report) != REPORT_BYTES or report[0] != 0:
            raise ProtocolError('Studio output must be 0 plus 1024 gadget bytes')
        with self.output_lock:
            self._send_acknowledged(
                OUTPUT0 if endpoint == 0 else OUTPUT1, report[1:], timeout=10,
                failure='native zkgui delivery acknowledgement timed out',
            )
            with self.condition:
                self.outputs_acked[endpoint] += 1

    def _create_forward(self, device_port, timeout=5):
        result = self._run('forward', 'tcp:0', f'tcp:{device_port}', timeout=timeout)
        value = result.stdout.strip()
        if not value.isdigit() or not 1 <= int(value) <= 65535:
            raise ProtocolError('invalid allocated forwarding port')
        return int(value)

    def _control_observation(self, kind, session, epoch):
        try:
            public = (session if type(session) is str and len(session) == 32 and
                      all(c in '0123456789abcdef' for c in session) else None)
            return dict(session=public, epoch=epoch if type(epoch) is int and epoch in (0, 1) else None,
                        requestKind=kind if type(kind) is int and kind in (21, 23, 25) else None,
                        requestSequence=None, connectionGeneration=None,
                        sendStart=None, sendDone=None, replyReceived=None)
        except Exception:
            return None

    def _control_stamp(self, observation, field, ticket=None):
        try:
            if observation is None:
                return
            if ticket is not None:
                observation.update(
                    connectionGeneration=ticket[0] if type(ticket[0]) is int and 0 <= ticket[0] < 2 ** 64 else None,
                    requestSequence=ticket[1] if type(ticket[1]) is int and 0 <= ticket[1] < 2 ** 32 else None)
                self.last_control_observation = observation
            elif field in ('sendStart', 'sendDone', 'replyReceived'):
                observation[field] = time.monotonic()
        except Exception:
            pass

    def _control_failure(self, observation, stage, error):
        """One bounded failure per proxy lifetime; never affects media/control flow."""
        try:
            if observation is None or self.control_failure_observation:
                return
            stage = stage if stage in ('acquire', 'prepare', 'send', 'reply') else 'unknown'
            timeout = type(error) in (TimeoutError, subprocess.TimeoutExpired)
            category = ('timeout' if timeout else 'os' if isinstance(error, OSError)
                        else 'protocol' if type(error) in (ProtocolError, video_wire.ProtocolError, ValueError)
                        else 'eof' if type(error) is EOFError else 'other')
            failure = dict(observation, event='videoControlFailure', clock='host-monotonic',
                           failureStage=stage,
                           timeoutStage=stage if timeout else None, category=category,
                           errno=diagnostic_errno(error), hostMonotonic=time.monotonic(),
                           pendingWrite=pending_write_observation(self.transport_socket))
            owner = self.video
            failure['media'] = (owner.transport_observation() if owner is not None and
                                owner.session == observation['session'] else None)
            # One atomic first-writer insertion; no additional acquisition or
            # lock-order change on the existing send/reply error paths.
            if self.control_failure_observation.setdefault('first', failure) is failure:
                emit_diagnostic(sys.stderr, failure)
        except Exception:
            pass

    def video_request(self, kind, session, epoch, *, deadline, before_send=None, **fields):
        """One correlated control request; no retries after a possible write."""
        observation = self._control_observation(kind, session, epoch)
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self.send_lock.acquire(timeout=max(0, remaining)):
            error = TimeoutError('video control unavailable')
            self._control_failure(observation, 'acquire', error)
            raise error
        ticket = None
        failure_stage = 'prepare'
        try:
            with self.condition:
                if (self.closed or self.error is not None or self.reconnect_requested or
                        not self.ready):
                    raise RuntimeError('video control unavailable')
                if len(self.pending_video) >= 16:
                    raise ProtocolError('too many pending video requests')
                if self.tx_sequence >= 2 ** 32:
                    raise ProtocolError('control sequence exhausted')
                ticket = (self.connection_generation, self.tx_sequence)
                self._control_stamp(observation, None, ticket)
                frame = video_wire.encode_control(
                    kind, ticket[1], generation=ticket[0], session=session,
                    epoch=epoch, **fields,
                )
                if time.monotonic() >= deadline:
                    raise TimeoutError('video control unavailable')
                if before_send is not None:
                    before_send()
                pending = {'kind': kind + 1, 'session': bytes.fromhex(session),
                           'epoch': epoch, 'reply': None, 'observation': observation}
                self.pending_video[ticket] = pending
                self.tx_sequence += 1
            try:
                failure_stage = 'send'
                self._control_stamp(observation, 'sendStart')
                send_all(self.transport_socket, frame, timeout=max(0, deadline - time.monotonic()))
                self._control_stamp(observation, 'sendDone')
            except (OSError, ValueError) as error:
                self._control_failure(observation, 'send', error)
                # A partial control envelope cannot be followed by another request.
                with self.condition:
                    self.error = 'video control write failed'
                    self.closed = True
                    self.condition.notify_all()
                try:
                    self.transport_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                raise
        except BaseException as error:
            self._control_failure(observation, failure_stage, error)
            if ticket is not None:
                with self.condition:
                    self.pending_video.pop(ticket, None)
            raise
        finally:
            self.send_lock.release()
        try:
            with self.condition:
                while True:
                    if (ticket[0] != self.connection_generation or self.reconnect_requested or
                            self.closed or self.error is not None):
                        raise RuntimeError('video control connection changed')
                    if time.monotonic() >= deadline:
                        raise TimeoutError('video control reply timed out')
                    if pending['reply'] is not None:
                        return pending['reply']
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('video control reply timed out')
                    self.condition.wait(remaining)
        except BaseException as error:
            self._control_failure(observation, 'reply', error)
            raise
        finally:
            with self.condition:
                self.pending_video.pop(ticket, None)

    def video_open(self, request):
        session = request['session']
        deadline = time.monotonic() + 10
        with self.condition:
            if (self.video_opening or (self.video is not None and
                    self.video.status['cleanup'] != 'proven')):
                return video_response(request, 1)
            if self.video is not None and self.video.session == session:
                return video_response(request, 4)
            self.video_opening = True
        owner = None
        attempted = False

        def opening_send():
            nonlocal attempted
            attempted = True

        try:
            fields = self.video_request(
                21, session, 0, deadline=deadline,
                before_send=opening_send,
                fps_n=request['fpsNumerator'], fps_d=request['fpsDenominator'],
            )
            if fields['result_code']:
                return video_response(request, fields['result_code'])
            owner = VideoSession(self, request, fields, deadline)
            with self.condition:
                self.video = owner
            return video_response(request, 0, epoch=1, capability=owner.capability)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            # A lost OPEN reply cannot be replayed or treated as a free reservation.
            if owner is None and attempted:
                with self.condition:
                    self.video = VideoSession.unresolved(self, request, deadline)
            return video_response(request, 6)
        finally:
            with self.condition:
                self.video_opening = False
                self.condition.notify_all()

    def video_owner(self, request):
        with self.condition:
            owner = self.video
            if (owner is None or owner.session != request['session'] or
                    owner.epoch != request['epoch'] or
                    not secrets.compare_digest(owner.capability, request['capability'])):
                return None
            return owner

    def input(self, endpoint, timeout_ms, cancelled):
        if endpoint not in (0, 1):
            raise ProtocolError('invalid HID interface')
        # A negative timeout is a blocking wait, but never an unbounded one: see
        # INPUT_IDLE_TICK_SECONDS for why the caller's own budget must not be
        # reached before this handler answers.
        window = timeout_ms / 1000 if timeout_ms >= 0 else INPUT_IDLE_TICK_SECONDS
        deadline = time.monotonic() + window
        with self.condition:
            generation = self.connection_generation
            while True:
                if cancelled():
                    raise RuntimeError('virtual HID handle closed')
                if generation != self.connection_generation or self.reconnect_requested:
                    raise RuntimeError('device proxy connection changed during input')
                if self.error is not None or self.closed:
                    raise RuntimeError(self.error or 'device proxy closed')
                if self.inputs[endpoint]:
                    report = self.inputs[endpoint].popleft()
                    self.condition.notify_all()
                    return report
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return b''
                self.condition.wait(min(remaining, 0.1))

    def close(self):
        if self.video is not None:
            self.video.interrupt()
        with self.condition:
            if (self.closed and self.transport_socket is None and
                    self.process is None and self.forward_port is None and
                    not self.remote_dir_staged):
                # Already torn down (a second close, or a failed start that
                # unwound): nothing device-side is left for this instance to own.
                # `remote_dir_staged` keeps a start that died *during* staging on
                # the long path, so its half-staged directory is still removed.
                self._release_admission()
                return
        try:
            if self.transport_socket is not None:
                try:
                    self._send_frame(STOP)
                except RuntimeError:
                    pass
                deadline = time.monotonic() + 12
                with self.condition:
                    while not self.restoring and not self.closed:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        self.condition.wait(remaining)
        finally:
            for stream in (self.reader_stream,):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
            if self.transport_socket is not None:
                try:
                    self.transport_socket.close()
                except OSError:
                    pass
            owned_ports = {self.forward_port, self.replacement_forward_port} - {None}
            self.forward_port = self.replacement_forward_port = None
            for port in owned_ports:
                try:
                    self._run('forward', '--remove', f'tcp:{port}', timeout=5)
                except (RuntimeError, OSError, subprocess.SubprocessError):
                    pass
            if self.video is not None:
                self.video.remove_forward()
            if self.process is not None:
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=3)
            with self.condition:
                self.closed = True
                self.condition.notify_all()
            self.process = None
            self._remove_remote_dir()
            self._remove_staged_agent()
            self.transport_socket = None
            self.reader_stream = None
            self.forward_port = None
            # Last: the deck is released only once this instance has removed what
            # it staged, so the next bridge cannot race the teardown.
            self._release_admission()


def video_response(request, code, *, epoch=None, **extra):
    session = request.get('session')
    if (not isinstance(session, str) or len(session) != 32 or
            any(char not in '0123456789abcdef' for char in session)):
        session = '0' * 32
    response_epoch = 0 if request['op'] == 'videoOpen' else request.get('epoch', 0)
    if type(response_epoch) is not int or not 0 <= response_epoch < 2 ** 32:
        response_epoch = 0
    return dict(schemaVersion=1, accepted=code == 0, op=request['op'],
                protocolVersion=1, session=session, epoch=response_epoch if epoch is None else epoch,
                resultCode=code, **extra)


def validate_video_request(request):
    operation = request['op']
    fields = {'schemaVersion', 'op', 'session'}
    if operation == 'videoOpen':
        fields.update(('fpsNumerator', 'fpsDenominator'))
    else:
        fields.update(('epoch', 'capability'))
        if operation == 'videoCancel':
            fields.add('reason')
    if set(request) != fields or type(request['schemaVersion']) is not int or request['schemaVersion'] != 1:
        raise ProtocolError('invalid video request fields')
    for name, length in [('session', 32)] + ([] if operation == 'videoOpen' else [('capability', 64)]):
        value = request[name]
        if (not isinstance(value, str) or len(value) != length or
                any(char not in '0123456789abcdef' for char in value)):
            raise ProtocolError('invalid video identity')
    if operation == 'videoOpen':
        n, d = request['fpsNumerator'], request['fpsDenominator']
        if (type(n) is not int or type(d) is not int or
                not 0 < n < 2 ** 32 or not 0 < d < 2 ** 32 or n > 240 * d):
            raise ProtocolError('invalid video frame rate')
    elif type(request['epoch']) is not int or not 0 < request['epoch'] < 2 ** 32:
        raise ProtocolError('invalid video epoch')
    if operation == 'videoCancel' and (type(request['reason']) is not int or request['reason'] not in (11, 13)):
        raise ProtocolError('invalid cancellation reason')


class VideoSession:
    """Single owned lane. Control waits never own media buffers or HID locks."""

    FAILURE_STAGES = frozenset((
        'local_configure', 'local_attach', 'local_authenticate', 'forward_create',
        'native_connect', 'native_configure', 'native_attach_send', 'pump_wait',
        'pump_read', 'pump_write', 'pump_header', 'pump_record',
        'terminal_forward', 'cleanup', 'unknown',
    ))
    FAILURE_DEADLINES = frozenset(('startup', 'record', 'progress', 'drain', 'cleanup'))

    def __init__(self, proxy, request, fields, deadline):
        self.proxy = proxy
        self.session = request['session']
        self.epoch = fields['epoch']
        self.capability = fields['capability'].hex()
        self.port = fields['port']
        self.fps_n = request['fpsNumerator']
        self.fps_d = request['fpsDenominator']
        self.startup_deadline = deadline
        self.authority = proxy.session_token
        self.condition = threading.Condition()
        self.abort = threading.Event()
        self.local_socket = None
        self.native_socket = None
        self.forward_port = None
        self.socket_buffers = {}
        self.forward_lock = threading.Lock()
        self.forward_cleanup_failed = False
        self.native_terminal = None
        self.diagnostic_emitted = False
        self.first_failure = None
        self.first_relay_observation = None
        self.media_io = dict(producerRead=None, producerWrite=None, consumerRead=None, consumerWrite=None)
        self.created_at = time.monotonic()
        self.initial_rotation_count = proxy.completed_rotations
        self.phase_times = {}
        self.relay_received = [0, 0]
        self.relay_sent = [0, 0]
        self.relay_highwater = [0, 0]
        self.cancel_deadline = None
        self.cancel_delivery_deadline = None
        self.cancel_result = None
        self.cancel_reason = None
        self.cancel_attempted = False
        self.status = dict(state=2, rendererReady=False, framesReceived=0,
                           framesConsumed=0, eosTotal=None, terminalCode=0,
                           cleanup='pending', cancelPhase='none')

    @classmethod
    def unresolved(cls, proxy, request, deadline):
        owner = cls(proxy, request, {'epoch': 0, 'capability': b'', 'port': 0}, deadline)
        owner.status.update(state=9, terminalCode=6, cleanup='unproven')
        return owner

    def snapshot(self):
        with self.condition:
            return dict(self.status)

    def refresh(self, deadline):
        with self.condition:
            if self.native_terminal is not None or self.status['cleanup'] == 'proven':
                return 0
        fields = self.proxy.video_request(
            25, self.session, self.epoch, deadline=deadline, capability=self.capability,
        )
        code = fields['result_code']
        if code:
            return code
        state, reason = fields['state'], fields['terminal_reason']
        # Native terminal STATUS is published only after checked cleanup/reap.
        # CLEANUP_FAILED explicitly reports the absence of that proof.
        proven = state in (6, 8, 9) and reason != 8
        with self.condition:
            phase = self.status['cancelPhase']
            if proven and self.cancel_deadline is not None:
                phase = 'proven'
            status = dict(
                state=state, rendererReady=bool(fields['renderer_ready']),
                framesReceived=fields['frames_received'], framesConsumed=fields['frames_consumed'],
                eosTotal=None if fields['eos_total'] == 2 ** 64 - 1 else fields['eos_total'],
                terminalCode=reason, cleanup='proven' if proven else (
                    'unproven' if state in (6, 8, 9) else 'pending'), cancelPhase=phase,
            )
            status = video_wire.validate_status(status)
        if proven:
            self.complete_terminal(status, deadline)
            return 0
        with self.condition:
            if self.forward_cleanup_failed:
                status.update(state=9, terminalCode=8, cleanup='unproven',
                              cancelPhase='unproven' if self.cancel_deadline is not None else 'none')
                self.status = status
            elif (self.native_terminal is None and self.status['cleanup'] != 'proven' and
                  status['framesReceived'] >= self.status['framesReceived'] and
                  status['framesConsumed'] >= self.status['framesConsumed'] and
                  (status['state'] >= self.status['state'] or status['state'] in (6, 8, 9))):
                self.status = status
            self.condition.notify_all()
        return 0

    def query(self, request):
        try:
            code = self.refresh(time.monotonic() + 5)
            return video_response(request, code, **({'status': self.snapshot()} if code == 0 else {}))
        except (OSError, RuntimeError, ValueError):
            return video_response(request, 6)

    def complete_terminal(self, status, deadline):
        """Native proof is private until exact owned-forward cleanup completes."""
        status = video_wire.validate_status(status)
        with self.condition:
            if self.forward_cleanup_failed:
                return False
            if self.native_terminal is not None:
                while self.status['cleanup'] == 'pending':
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    self.condition.wait(remaining)
                return self.status['cleanup'] == 'proven'
            self.native_terminal = status
            self.phase_times['nativeProof'] = time.monotonic()
            pending = dict(status)
            pending.update(state=5 if status['state'] == 6 else 7,
                           cleanup='pending', cancelPhase=self.status['cancelPhase'])
            self.status = pending
        self.remove_forward(deadline=deadline)
        with self.condition:
            if not self.forward_cleanup_failed:
                self.status = dict(status)
                self.phase_times['bridgeProof'] = time.monotonic()
            self.condition.notify_all()
            return self.status['cleanup'] == 'proven'

    def observe_media_io(self, direction, operation, accepted=None, error=None):
        """Four replace-only scalar slots; no payload/history, logging or locks."""
        try:
            key = direction + operation
            if key not in self.media_io:
                return
            self.media_io[key] = dict(
                acceptedBytes=accepted if type(accepted) is int and 0 <= accepted <= MAX_PAYLOAD + 40 else None,
                hostMonotonic=time.monotonic(), errno=diagnostic_errno(error))
        except Exception:
            pass

    def transport_observation(self):
        """Best-effort scalar snapshot, not an atomic cross-thread observation."""
        try:
            control = self.proxy.last_control_observation
            return dict(
                mediaIo=dict(self.media_io),
                control=None if control is None or control['session'] != self.session else dict(control),
                relayBytesReceived=list(self.relay_received), relayBytesSent=list(self.relay_sent),
                relayRecordHighwater=list(self.relay_highwater),
                pendingWrite=dict(producer=pending_write_observation(self.native_socket),
                                  consumer=pending_write_observation(self.local_socket)))
        except Exception:
            return dict(status='unavailable')

    def record_failure(self, stage, error, *, lane=None, deadline=None):
        """Keep only fixed vocabulary and bounded numbers, never exception content."""
        error_type = type(error)
        error_number = error.errno if isinstance(error, OSError) else None
        category = {
            TimeoutError: 'timeout', subprocess.TimeoutExpired: 'timeout',
            EOFError: 'eof', OSError: 'os', BrokenPipeError: 'os',
            BlockingIOError: 'os', InterruptedError: 'os',
            ConnectionResetError: 'os', ConnectionAbortedError: 'os',
            ConnectionRefusedError: 'os', FileNotFoundError: 'os',
            PermissionError: 'os', ProtocolError: 'protocol',
            video_wire.ProtocolError: 'protocol', ValueError: 'protocol',
            subprocess.CalledProcessError: 'subprocess',
            DeviceCommandError: 'subprocess',
        }.get(error_type, 'other')
        return_code = error.returncode if error_type in (subprocess.CalledProcessError, DeviceCommandError) else None
        # errno names are platform-defined; arbitrary integers are not diagnostic data.
        error_number = (error_number if type(error_number) is int and
                        error_number in errno.errorcode else None)
        return_code = (return_code if type(return_code) is int and
                       -(2 ** 31) <= return_code < 2 ** 31 else None)
        with self.condition:
            if self.first_failure is not None:
                return
            cancelled = self.cancel_deadline is not None
            self.first_failure = dict(
                stage=stage if stage in self.FAILURE_STAGES else 'unknown',
                category='cancellation' if cancelled and stage != 'cleanup' else category,
                lane=lane if lane in ('producer', 'consumer') else None,
                deadline=deadline if deadline in self.FAILURE_DEADLINES else None,
                errno=error_number, returnCode=return_code,
                hostMonotonic=time.monotonic(), cancellationRequested=cancelled,
            )
            try:
                self.first_relay_observation = self.transport_observation()
            except Exception:
                self.first_relay_observation = dict(status='unavailable')

    @contextmanager
    def failure_context(self, stage, *, lane=None, deadline=None):
        try:
            yield
        except Exception as error:
            self.record_failure(stage, error, lane=lane, deadline=deadline)
            raise

    def terminal_diagnostic(self):
        """One bounded nonsecret receipt; timestamps are host monotonic observations."""
        with self.condition:
            if self.diagnostic_emitted:
                return
            self.diagnostic_emitted = True
            now = time.monotonic()
            # session is public correlation, unlike capability and proxy authority.
            receipt = dict(event='videoBridgeTerminal', session=self.session, epoch=self.epoch,
                           clock='host-monotonic',
                           firstFailure=None if self.first_failure is None else dict(self.first_failure),
                           firstRelayObservation=self.first_relay_observation,
                           state=self.status['state'],
                           terminalCode=self.status['terminalCode'], cleanup=self.status['cleanup'],
                           cancelPhase=self.status['cancelPhase'], hostMonotonic=now,
                           elapsedSeconds=now - self.created_at, phases=dict(self.phase_times),
                           phaseElapsedSeconds={key: value - self.created_at for key, value in self.phase_times.items()},
                           cancelElapsedSeconds={key: value - self.phase_times['cancelRequested']
                                                 for key, value in self.phase_times.items()
                                                 if 'cancelRequested' in self.phase_times and value >= self.phase_times['cancelRequested']},
                           relayBytesReceived=list(self.relay_received), relayBytesSent=list(self.relay_sent),
                           relayRecordHighwater=list(self.relay_highwater),
                           controlRotations=self.proxy.completed_rotations - self.initial_rotation_count)
        emit_diagnostic(sys.stderr, receipt)

    def interrupt(self):
        self.abort.set()
        with self.condition:
            sockets = (self.local_socket, self.native_socket)
        for transport in sockets:
            if transport is not None:
                try:
                    transport.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def cancel(self, request):
        with self.condition:
            first = self.cancel_deadline is None
            if first:
                now = time.monotonic()
                self.cancel_deadline = now + 35
                self.cancel_delivery_deadline = now + 25
                self.cancel_reason = request['reason']
                self.status['cancelPhase'] = 'requested'
                self.phase_times['cancelRequested'] = now
        if first:
            self.interrupt()
            self._cancel_operation()
        with self.condition:
            while self.cancel_result is None:
                remaining = self.cancel_deadline - time.monotonic()
                if remaining <= 0:
                    self.status.update(cleanup='unproven', cancelPhase='unproven')
                    self.cancel_result = 6
                    self.condition.notify_all()
                    break
                self.condition.wait(remaining)
            return video_response(request, self.cancel_result, **(
                {'status': dict(self.status)} if self.cancel_result in (0, 6, 8) else {}))

    def _cancel_operation(self):
        code = 6
        try:
            while time.monotonic() < self.cancel_deadline:
                if self.proxy.session_token != self.authority or self.proxy.video is not self:
                    code = 2
                    break
                if self.snapshot()['cleanup'] == 'proven':
                    code = 0
                    break
                with self.proxy.condition:
                    available = (self.proxy.ready and not self.proxy.reconnect_requested and
                                 not self.proxy.closed and self.proxy.error is None)
                    if not available:
                        limit = self.cancel_deadline if self.cancel_attempted else self.cancel_delivery_deadline
                        remaining = limit - time.monotonic()
                        if remaining <= 0 or self.proxy.closed or self.proxy.error is not None:
                            break
                        self.proxy.condition.wait(min(.1, remaining))
                        continue
                now = time.monotonic()
                if not self.cancel_attempted:
                    if now >= self.cancel_delivery_deadline:
                        break

                    def attempted():
                        # Set before entering send: any exception after this point is ambiguous.
                        if (self.proxy.session_token != self.authority or self.proxy.video is not self or
                                time.monotonic() >= self.cancel_delivery_deadline):
                            raise RuntimeError('video cancellation authority unavailable')
                        with self.condition:
                            self.cancel_attempted = True
                            self.status['cancelPhase'] = 'delivery_unknown'
                            self.phase_times['deliveryUnknown'] = time.monotonic()

                    try:
                        reply = self.proxy.video_request(
                            23, self.session, self.epoch,
                            deadline=min(self.cancel_delivery_deadline, now + 5),
                            capability=self.capability, reason=self.cancel_reason,
                            before_send=attempted,
                        )
                        if reply['result_code'] == 0:
                            with self.condition:
                                self.status['cancelPhase'] = 'delivered'
                                self.phase_times['delivered'] = time.monotonic()
                        elif reply['result_code'] not in (6,):
                            code = reply['result_code']
                            break
                    except (OSError, RuntimeError, ValueError):
                        pass
                try:
                    code = self.refresh(min(self.cancel_deadline, time.monotonic() + 5))
                    if code in (2, 7, 8):
                        break
                    status = self.snapshot()
                    if status['cleanup'] == 'proven':
                        code = 0
                        break
                    if status['cleanup'] == 'unproven' and status['terminalCode'] == 8:
                        code = 8
                        break
                except (OSError, RuntimeError, ValueError):
                    code = 6
                with self.condition:
                    remaining = self.cancel_deadline - time.monotonic()
                    if remaining > 0:
                        self.condition.wait(min(.1, remaining))
            else:
                code = 6
        finally:
            self.remove_forward(deadline=self.cancel_deadline)
            with self.condition:
                if self.forward_cleanup_failed and code not in (2, 7):
                    code = 8
                if self.status['cleanup'] != 'proven':
                    self.status.update(cleanup='unproven', cancelPhase='unproven')
                    if code == 0:
                        code = 6
                else:
                    self.status['cancelPhase'] = 'proven'
                if self.cancel_result is None:
                    self.cancel_result = code
                self.condition.notify_all()
            self.terminal_diagnostic()

    def _configure_socket(self, transport, name):
        transport.setblocking(False)
        disable_nagle(transport)
        for option in (socket.SO_SNDBUF, socket.SO_RCVBUF):
            transport.setsockopt(socket.SOL_SOCKET, option, 65576)
        self.socket_buffers[name] = tuple(
            transport.getsockopt(socket.SOL_SOCKET, option)
            for option in (socket.SO_SNDBUF, socket.SO_RCVBUF)
        )

    def remove_forward(self, deadline=None):
        deadline = time.monotonic() + 5 if deadline is None else deadline
        remaining = max(0, deadline - time.monotonic())
        if not self.forward_lock.acquire(timeout=remaining):
            self.record_failure('cleanup', TimeoutError(), deadline='cleanup')
            with self.condition:
                self.forward_cleanup_failed = True
                self.status.update(state=9, terminalCode=8, cleanup='unproven',
                                   cancelPhase='unproven' if self.cancel_deadline is not None else 'none')
            return
        try:
            port, self.forward_port = self.forward_port, None
            if port is None:
                return
            try:
                remaining = min(5, deadline - time.monotonic())
                if remaining <= 0:
                    raise TimeoutError('video forward cleanup timed out')
                self.proxy._run('forward', '--remove', f'tcp:{port}', timeout=remaining)
            except (RuntimeError, OSError, subprocess.SubprocessError) as error:
                # Do not retry an ambiguous removal: that port may be reassigned.
                self.record_failure('cleanup', error, deadline='cleanup')
                with self.condition:
                    self.forward_cleanup_failed = True
                    self.status.update(state=9, terminalCode=8, cleanup='unproven',
                                       cancelPhase='unproven' if self.cancel_deadline is not None else 'none')
        finally:
            self.forward_lock.release()

    def _attach(self, local):
        data = bytearray()
        target = video_wire.HEADER_SIZE
        started = None
        while len(data) < target:
            now = time.monotonic()
            deadline = min(self.startup_deadline, started + 5 if started is not None else self.startup_deadline)
            if self.abort.is_set() or now >= deadline:
                self.record_failure('local_attach', TimeoutError(), lane='producer',
                                    deadline='record' if started is not None and
                                    started + 5 <= self.startup_deadline else 'startup')
                raise TimeoutError('video attachment timed out')
            try:
                readable = select.select([local], [], [], min(.1, deadline - now))[0]
            except (BlockingIOError, InterruptedError):
                continue
            if not readable:
                continue
            try:
                block = local.recv(target - len(data))
            except (BlockingIOError, InterruptedError):
                continue
            if not block:
                raise EOFError('video attachment closed')
            if started is None:
                started = time.monotonic()
            data.extend(block)
            if len(data) == video_wire.HEADER_SIZE:
                kind, length, *_ = video_wire.decode_record_header(bytes(data))
                if kind != video_wire.ATTACH:
                    raise ProtocolError('video attachment required')
                target += length
        return video_wire.decode_record(bytes(data), direction=video_wire.PRODUCER)

    def relay(self, local):
        terminal = False
        try:
            with self.condition:
                self.local_socket = local
            with self.failure_context('local_configure', lane='producer'):
                self._configure_socket(local, 'local')
            local_state = video_wire.StreamState(
                self.session, self.fps_n, self.fps_d, capability=self.capability,
            )
            with self.failure_context('local_attach', lane='producer', deadline='startup'):
                attach = self._attach(local)
            with self.failure_context('local_authenticate', lane='producer'):
                if not secrets.compare_digest(attach.payload, bytes.fromhex(self.capability)):
                    raise ProtocolError('invalid video attachment')
                local_state.accept(attach, video_wire.PRODUCER)
            remaining = self.startup_deadline - time.monotonic()
            with self.failure_context('forward_create', deadline='startup'):
                if self.abort.is_set() or remaining <= 0:
                    raise TimeoutError('video startup timed out')
                with self.forward_lock:
                    if self.abort.is_set() or self.native_terminal is not None:
                        raise RuntimeError('video startup cancelled')
                    self.forward_port = self.proxy._create_forward(self.port, timeout=remaining)
                    forward_port = self.forward_port
            remaining = self.startup_deadline - time.monotonic()
            with self.failure_context('native_connect', deadline='startup'):
                if self.abort.is_set() or remaining <= 0:
                    raise TimeoutError('video startup timed out')
                native = socket.create_connection(('127.0.0.1', forward_port), timeout=remaining)
                with self.condition:
                    self.native_socket = native
                    if self.abort.is_set():
                        raise RuntimeError('video startup cancelled')
            with self.failure_context('native_configure', lane='consumer'):
                self._configure_socket(native, 'native')
            native_state = video_wire.StreamState(
                self.session, self.fps_n, self.fps_d, capability=self.capability,
            )
            native_attach = video_wire.encode_record(video_wire.ATTACH, self.session, 1, 0, attach.payload)
            native_state.accept(video_wire.decode_record(native_attach), video_wire.PRODUCER)
            with self.failure_context('native_attach_send', lane='producer', deadline='startup'):
                send_all(native, native_attach, timeout=max(0, self.startup_deadline - time.monotonic()))
            with self.failure_context('pump_wait'):
                terminal = self._pump(local, native, local_state, native_state)
        except (EOFError, OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            self.record_failure('unknown', error)
            with self.condition:
                if self.status['cleanup'] != 'proven':
                    self.status.update(cleanup='unproven')
        finally:
            self.interrupt()
            if self.native_socket is not None:
                with self.failure_context('cleanup'):
                    self.native_socket.close()
            self.remove_forward()
            if not terminal and self.cancel_deadline is None:
                self.cancel(dict(op='videoCancel', session=self.session, epoch=self.epoch,
                                 capability=self.capability, reason=11))
            self.terminal_diagnostic()

    def _pump(self, local, native, local_state, native_state):
        """Two incremental record slots, no frame queue and no shared transport lock."""
        lanes = [dict(source=local, sink=native, state=local_state, peer=native_state,
                      direction=video_wire.PRODUCER, sequence=1),
                 dict(source=native, sink=local, state=native_state, peer=local_state,
                      direction=video_wire.CONSUMER, sequence=0)]
        for lane in lanes:
            lane.update(buffer=bytearray(), target=40, sent=0, writing=False,
                        started=None, terminal=False, last_send=0)
        ready = False
        progress = time.monotonic()
        progress_budget = max(30, 3 * self.fps_d / self.fps_n)
        drain_deadline = None
        while not self.abort.is_set():
            now = time.monotonic()
            if (not ready and now >= self.startup_deadline) or now - progress >= progress_budget:
                self.record_failure('pump_wait', TimeoutError(),
                                    deadline='startup' if not ready and now >= self.startup_deadline else 'progress')
                raise TimeoutError('video progress timed out')
            if drain_deadline is not None and now >= drain_deadline:
                self.record_failure('pump_wait', TimeoutError(), deadline='drain')
                raise TimeoutError('video drain timed out')
            for lane_index, lane in enumerate(lanes):
                if lane['started'] is not None and now - lane['started'] >= progress_budget:
                    self.record_failure('pump_wait', TimeoutError(),
                                        lane='producer' if lane_index == 0 else 'consumer', deadline='record')
                    raise TimeoutError('video record timed out')
            with self.failure_context('pump_wait'):
                try:
                    readable, writable, _ = select.select(
                        [lane['source'] for lane in lanes if not lane['writing']],
                        [lane['sink'] for lane in lanes if lane['writing']], [], .1,
                    )
                except (BlockingIOError, InterruptedError):
                    continue
            for lane_index, lane in enumerate(lanes):
                direction = 'producer' if lane_index == 0 else 'consumer'
                buffer = lane['buffer']
                if lane['writing'] and lane['sink'] in writable:
                    if (direction == 'producer' and
                            now - lane['last_send'] < USB_FORWARD_GAP):
                        continue
                    with self.failure_context('terminal_forward' if lane['terminal'] else 'pump_write',
                                              lane=direction):
                        try:
                            count = lane['sink'].send(
                                memoryview(buffer)[lane['sent']:lane['sent'] + USB_FORWARD_CHUNK])
                        except (BlockingIOError, InterruptedError) as error:
                            self.observe_media_io(direction, 'Write', error=error)
                            continue
                        except OSError as error:
                            self.observe_media_io(direction, 'Write', error=error)
                            raise
                        self.observe_media_io(direction, 'Write', accepted=count)
                        if count <= 0:
                            raise EOFError('video write closed')
                    lane['sent'] += count
                    lane['last_send'] = now
                    self.relay_sent[lane_index] += count
                    if lane['sent'] == len(buffer):
                        if lane['terminal']:
                            return True
                        buffer.clear()
                        lane.update(target=40, sent=0, writing=False, started=None)
                elif not lane['writing'] and lane['source'] in readable:
                    with self.failure_context('pump_read', lane=direction):
                        try:
                            block = lane['source'].recv(lane['target'] - len(buffer))
                        except (BlockingIOError, InterruptedError) as error:
                            self.observe_media_io(direction, 'Read', error=error)
                            continue
                        except OSError as error:
                            self.observe_media_io(direction, 'Read', error=error)
                            raise
                        self.observe_media_io(direction, 'Read', accepted=len(block))
                        if not block:
                            raise EOFError('video stream closed before terminal')
                    if lane['started'] is None:
                        lane['started'] = time.monotonic()
                    buffer.extend(block)
                    self.relay_received[lane_index] += len(block)
                    self.relay_highwater[lane_index] = max(self.relay_highwater[lane_index], len(buffer))
                    del block
                    if len(buffer) == 40 and lane['target'] == 40:
                        with self.failure_context('pump_header', lane=direction):
                            _, length, *_ = video_wire.decode_record_header(bytes(buffer))
                        lane['target'] = 40 + length
                    if len(buffer) != lane['target']:
                        continue
                    with self.failure_context('pump_record', lane=direction):
                        record = video_wire.decode_record(bytes(buffer), direction=lane['direction'])
                        lane['state'].accept(record, lane['direction'])
                    if record.kind == video_wire.ERROR:
                        # Forward the result code, never a peer-supplied secret-bearing diagnostic.
                        del buffer[44:]
                        struct.pack_into('>I', buffer, 8, 4)
                        record = video_wire.Record(record.kind, record.session, record.epoch,
                                                   record.sequence, record.payload[:4])
                    struct.pack_into('>Q', buffer, 32, lane['sequence'])
                    with self.failure_context('pump_record', lane=direction):
                        lane['peer'].accept(video_wire.Record(
                            record.kind, record.session, record.epoch, lane['sequence'], record.payload,
                        ), lane['direction'])
                    lane['sequence'] += 1
                    lane['writing'] = True
                    progress = time.monotonic()
                    if record.kind == video_wire.READY:
                        ready = True
                        with self.condition:
                            if self.native_terminal is None:
                                self.status.update(state=3, rendererReady=True)
                    elif record.kind == video_wire.EOS:
                        drain_deadline = progress + max(10, 2 * self.fps_d / self.fps_n + 5)
                    elif record.kind in (video_wire.DONE, video_wire.CANCELLED, video_wire.ERROR):
                        lane['terminal'] = True
                        # Native withholds these success records until checked cleanup/reap.
                        # ERROR is not proof and requires matching control STATUS.
                        if record.kind in (video_wire.DONE, video_wire.CANCELLED):
                            with self.failure_context('terminal_forward', lane=direction, deadline='cleanup'):
                                terminal_status = video_wire.validate_status(dict(
                                        state=6 if record.kind == video_wire.DONE else 8,
                                        rendererReady=ready,
                                        framesReceived=native_state.received,
                                        framesConsumed=native_state.consumed,
                                        eosTotal=native_state.eos,
                                        terminalCode=0 if record.kind == video_wire.DONE else struct.unpack('>I', record.payload)[0],
                                        cleanup='proven', cancelPhase='none' if self.cancel_deadline is None else 'proven',
                                ))
                                if not self.complete_terminal(terminal_status, time.monotonic() + 5):
                                    raise RuntimeError('video forwarding cleanup failed')
                    del record
        return False


class BridgeState:
    def __init__(self, transport):
        self.transport = transport
        # Ownership changes and input claims share the same reentrant condition.
        self.lock = transport.condition
        self.handles = {}
        # Publish the same registry, not a second ownership counter. Maintenance
        # observes it under the condition used by open/close below.
        with self.lock:
            transport.hid_handles = self.handles

    def open(self, handle, endpoint):
        if endpoint not in (0, 1):
            raise ProtocolError('invalid interface')
        capability = secrets.token_hex(32)
        with self.lock:
            if handle in self.handles:
                raise ProtocolError('handle already open')
            self.handles[handle] = (capability, endpoint)
        return capability

    def authorize(self, handle, capability):
        with self.lock:
            record = self.handles.get(handle)
        if record is None or not secrets.compare_digest(record[0], capability or ''):
            raise ProtocolError('invalid handle capability')
        return record[1]

    def close(self, handle, capability):
        with self.lock:
            self.authorize(handle, capability)
            self.handles.pop(handle, None)
            self.transport.condition.notify_all()

    def is_closed(self, handle, capability):
        with self.lock:
            record = self.handles.get(handle)
        return record is None or not secrets.compare_digest(record[0], capability or '')


class BridgeHandler(socketserver.StreamRequestHandler):
    # No buffered read-ahead: the byte after OPEN's newline belongs to D2JF.
    rbufsize = 0

    def handle(self):
        request = None
        owner = None
        try:
            if hasattr(self, 'connection'):
                self.connection.settimeout(5)
            data = self.rfile.readline(MAX_MESSAGE + 1)
            request = parse_message(data)
            response = self.server.dispatch(request)
            if request.get('op') == 'videoOpen' and response['accepted']:
                owner = self.server.state.transport.video
            encoded = framed_json(response)
        except Exception as exc:
            if request is not None and request.get('op') in ('videoOpen', 'videoStatus', 'videoCancel'):
                response = video_response(request, 4, error='invalid video request')
            else:
                response = {'schemaVersion': 1, 'accepted': False, 'error': str(exc)}
            try:
                encoded = framed_json(response)
            except ProtocolError:
                encoded = framed_json({
                    'schemaVersion': 1, 'accepted': False,
                    'error': 'error response exceeds maximum size',
                })
        try:
            self.wfile.write(encoded)
            self.wfile.flush()
            if owner is not None:
                owner.relay(self.connection)
        except OSError:
            if owner is not None:
                owner.cancel(dict(op='videoCancel', session=owner.session, epoch=owner.epoch,
                                  capability=owner.capability, reason=11))


class BridgeServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, path, state):
        self.path = Path(path)
        self.state = state
        # The exact endpoint this instance bound; None until bind() succeeds.
        self.endpoint = None
        if socket_listener_live(self.path):
            raise BridgeSocketInUse(
                f'{self.path} is served by another process or cannot be proven dead; leaving it bound')
        try:
            super().__init__(str(self.path), BridgeHandler)
        except OSError:
            # bind() failed. Only a path a second probe proves has no listener is
            # reclaimed, and only here, immediately before rebinding it ourselves.
            if socket_listener_live(self.path):
                raise BridgeSocketInUse(
                    f'{self.path} is served by another process or cannot be proven dead')
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            super().__init__(str(self.path), BridgeHandler)
        self.endpoint = endpoint_identity(self.path)
        os.chmod(self.path, 0o600)

    def dispatch(self, request):
        operation = request.get('op')
        if operation in ('videoOpen', 'videoStatus', 'videoCancel'):
            validate_video_request(request)
            if operation == 'videoOpen':
                return self.state.transport.video_open(request)
            owner = self.state.transport.video_owner(request)
            if owner is None:
                return video_response(request, 2)
            if operation == 'videoStatus':
                return owner.query(request)
            return owner.cancel(request)
        handle = request.get('handle')
        if not isinstance(handle, int) or handle < 0:
            raise ProtocolError('invalid handle')
        if operation == 'event':
            if self.state.transport.error:
                raise RuntimeError(self.state.transport.error)
            with self.state.lock:
                open_handles = len(self.state.handles)
            with self.state.transport.condition:
                outputs_acked = list(self.state.transport.outputs_acked)
                inputs_received = list(self.state.transport.inputs_received)
            return {
                'schemaVersion': 1,
                'accepted': True,
                'openHandles': open_handles,
                'outputsAcked': outputs_acked,
                'inputsReceived': inputs_received,
            }
        if operation == 'open':
            endpoint = request.get('interface', 0)
            capability = self.state.open(handle, endpoint)
            return {'schemaVersion': 1, 'accepted': True, 'capability': capability}
        capability = request.get('capability')
        endpoint = self.state.authorize(handle, capability)
        if operation == 'close':
            self.state.close(handle, capability)
            return {'schemaVersion': 1, 'accepted': True}
        if operation == 'output':
            report_hex = request.get('report')
            if not isinstance(report_hex, str):
                raise ProtocolError('missing report')
            try:
                report = bytes.fromhex(report_hex)
            except ValueError as exc:
                raise ProtocolError('invalid report encoding') from exc
            self.state.transport.output(endpoint, report)
            return {'schemaVersion': 1, 'accepted': True}
        if operation == 'input':
            timeout_ms = request.get('timeoutMs', -1)
            if not isinstance(timeout_ms, int) or timeout_ms < -1:
                raise ProtocolError('invalid timeout')
            report = self.state.transport.input(
                endpoint, timeout_ms,
                lambda: self.state.is_closed(handle, capability),
            )
            return {'schemaVersion': 1, 'accepted': True, 'report': report.hex()}
        raise ProtocolError('unknown operation')

    def server_close(self):
        super().server_close()
        # Unlink exactly the endpoint this instance bound. A second instance that
        # lost the bind race, or an endpoint another process has already replaced,
        # must never be deleted on this instance's way out.
        if self.endpoint is not None and endpoint_identity(self.path) == self.endpoint:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        self.endpoint = None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket', type=Path, default=DEFAULT_SOCKET)
    parser.add_argument('--serial', default=os.environ.get('D200_ADB_SERIAL', ADB_SERIAL))
    parser.add_argument('--adb', default=os.environ.get('ADB', 'adb'))
    parser.add_argument('--state-file', type=Path)
    arguments = parser.parse_args()
    # Refuse before any device effect: a live bridge keeps its endpoint.
    if socket_listener_live(arguments.socket):
        print(f'bridge_socket_in_use path={arguments.socket}', file=sys.stderr, flush=True)
        raise SystemExit(1)
    root = Path(__file__).resolve().parent
    transport = DeviceProxy(
        arguments.adb, arguments.serial,
        root / 'd200-zkgui-proxy', root / 'libd200-zkgui-preload.so',
    )
    server = None
    stopping = threading.Event()

    def stop(_signum=None, _frame=None):
        print(f'bridge_stop signal={_signum}', file=sys.stderr, flush=True)
        if stopping.is_set():
            return
        stopping.set()
        if server is not None:
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    stop_endpoint = StopEndpoint('bridge', stop)
    try:
        if arguments.state_file:
            # 0600, unique temp, atomic replace -- see write_private_state_file.
            try:
                write_private_state_file(
                    arguments.state_file,
                    json.dumps({'pid': os.getpid(), 'control': stop_endpoint.state()}),
                )
            except StateFileError as error:
                print(f'bridge_state_file_failed error={error}', file=sys.stderr, flush=True)
                raise SystemExit(1)
        try:
            transport.start()
        except DeviceAdmissionError as error:
            print(f'bridge_device_in_use error={error}', file=sys.stderr, flush=True)
            raise SystemExit(1)
        except StartupError as error:
            # One line, non-zero exit. The per-attempt numbers (staging seconds,
            # readiness seconds, the last command issued) are already on stderr as
            # a `bridgeStartupAttempt` diagnostic; this is the human-readable half.
            print(f'bridge_startup_failed error={error}', file=sys.stderr, flush=True)
            raise SystemExit(1)
        except StagingError as error:
            # The session is already unwound by the time this is raised (the
            # caller's cleanup ran), so the user gets one line and a status, not
            # a traceback naming a device command they cannot act on.
            print(f'bridge_stage_failed error={error}', file=sys.stderr, flush=True)
            raise SystemExit(1)
        if stopping.is_set():
            return
        state = BridgeState(transport)
        try:
            server = BridgeServer(arguments.socket, state)
        except BridgeSocketInUse as error:
            print(f'bridge_socket_in_use path={arguments.socket} error={error}',
                  file=sys.stderr, flush=True)
            raise SystemExit(1)
        if stopping.is_set():
            return

        def watch_transport():
            with transport.condition:
                while not stopping.is_set():
                    transport.condition.wait(timeout=1)
            server.shutdown()

        threading.Thread(
            target=watch_transport,
            name='d200-transport-watch',
            daemon=True,
        ).start()
        print('bridge_serve_forever', file=sys.stderr, flush=True)
        server.serve_forever(poll_interval=0.1)
        print('bridge_serve_returned', file=sys.stderr, flush=True)
    finally:
        if transport.error:
            print(f'transport_error={transport.error}', file=sys.stderr, flush=True)
        if server is not None:
            server.server_close()
        try:
            transport.close()
        finally:
            try:
                if arguments.state_file:
                    stored = own_state_record(arguments.state_file)
                    if stored is not None and stored.get('control') == stop_endpoint.state():
                        arguments.state_file.unlink()
            except FileNotFoundError:
                pass
            finally:
                stop_endpoint.close()


if __name__ == '__main__':
    main()

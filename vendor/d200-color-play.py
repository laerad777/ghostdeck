#!/usr/bin/env python3
"""Owned, bounded JPEG producer. Importing this module performs no device I/O."""
import argparse
from collections import Counter
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import re
import secrets
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import queue
import time

from d200_jpeg import JpegFramer, JpegFramingError
from d200_subprocess import SourceProcesses

_SOURCE_PROCESSES = SourceProcesses()
_MEDIA_CHILDREN = []
from d200_process_control import StopEndpoint, emit_diagnostic, publish_video_state, video_bridge_request
import d200_video_stream as wire

import shutil as _shutil
ADB = _shutil.which("adb") or "adb"
SERIAL = __import__("os").environ.get("GHOSTDECK_SERIAL") or ""
# The framer's budget is the deck's own cap, taken from the wire module so the two
# cannot drift; align_jpeg_payload applies the stricter padded budget below.
MAX_JPEG_BYTES = wire.MAX_JPEG
HOST_STATE = Path("/tmp/d200-color-host.json")
SEEK_PATH = Path("/tmp/d200-color-seek")
VOLUME_PATH = Path("/tmp/d200-color-volume")
OVERLAY_PATH = Path("/tmp/d200-color-overlay")
CURRENT_OVERLAY = 1.0
CURRENT_VOLUME = 1.0
BRIDGE_SOCKET = Path("/tmp/d200-adb-bridge.sock")
# Keep FRAME records under 12KiB; 14387-byte records stalled at upHave=12288.
FRAME_JPEG_CHUNK = 12288 - wire.HEADER_SIZE - 16
UNPROVEN_OPEN = "has not proven it released the deck"
# Measured: 0s between sessions -> CLEANUP_FAILED. Stop already waited for release.
OPEN_RETRY_WAIT = 1.0
class SeekRequested(Exception):
    def __init__(self, start):
        super().__init__(start)
        self.start = float(start)

class SourceEndedEarly(Exception):
    """An HTTP source stopped before the source's own end: resume from the playhead, not from 0."""


# An encoder whose playhead stops further than this short of the known duration did not reach the
# end: it lost the origin. A clean end on a long HTTP source lands within a frame or two of it.
EARLY_END_MARGIN = 5.0
MAX_RESUMES_PER_10_MIN = 3


def ended_early(source, duration, playhead):
    """True when an HTTP encoder exited cleanly well before the source's known end.

    ffmpeg exits 0 when its reconnect budget runs out, so an origin that went away mid-stream looks
    exactly like the end of the video. A googlevideo URL is signed for 6 hours, and a long video also
    rides out more network blips, so without this a 2h+ stream could silently stop (or, with loop on,
    jump back to 0) partway through. Files and unknown durations are never treated as early.
    """
    if not str(source).startswith(("http://", "https://")):
        return False
    try:
        duration, playhead = float(duration), float(playhead)
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(duration) and math.isfinite(playhead)) or duration <= 0:
        return False
    return playhead < duration - EARLY_END_MARGIN


class VolumeRequested(Exception):
    def __init__(self, gain):
        super().__init__(gain)
        self.gain = float(gain)


def take_seek_request(path=SEEK_PATH):
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        path.unlink()
    except OSError:
        pass
    try:
        value = float(raw)
    except ValueError:
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


def clamp_volume(value):
    try:
        gain = float(value)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(gain):
        return 1.0
    if gain < 0:
        return 0.0
    if gain > 1:
        return 1.0
    return gain


def take_volume_request(path=VOLUME_PATH):
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        path.unlink()
    except OSError:
        pass
    return clamp_volume(raw)


def take_overlay_request(path=OVERLAY_PATH):
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        path.unlink()
    except OSError:
        pass
    return clamp_volume(raw)


def push_studio_alpha(gain):
    n = int(round(clamp_volume(gain) * 255))
    try:
        run(ADB, "shell", f"echo {n} >/tmp/d200-studio-alpha", check=False)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass


def apply_encoder_volume(speaker, gain):
    """Set the running speaker's gain in place. True when the speaker took it.

    The speaker reads ffmpeg's interactive commands on stdin, so `volume@vol` changes on the next
    audio frame with no gap. Killing and respawning the speaker for every slider step was a gap per
    step: a new AudioQueue, a fresh HTTP open and a re-seek to the estimated playhead, and a drag
    sends many steps. False means the caller has to respawn (no speaker, or its stdin is closed).
    """
    global CURRENT_VOLUME
    CURRENT_VOLUME = clamp_volume(gain)
    stdin = getattr(speaker, "stdin", None)
    if speaker is None or stdin is None or speaker.poll() is not None:
        return False
    try:
        stdin.write(f"cvolume@vol -1 volume {CURRENT_VOLUME:.4f}\n".encode())
        stdin.flush()
    except (OSError, ValueError):
        return False
    return True


def speaker_playhead(start, started_ns, now_ns, rate=1.0):
    try:
        start = float(start)
        started_ns = float(started_ns)
        now_ns = float(now_ns)
        rate = float(rate)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(start) or start < 0:
        start = 0.0
    if not math.isfinite(rate) or rate <= 0:
        rate = 1.0
    if not math.isfinite(started_ns) or not math.isfinite(now_ns) or now_ns < started_ns:
        return start
    return start + (now_ns - started_ns) / 1e9 * rate



def align_jpeg_payload(frame):
    """Return the bytes to fragment into FRAME records for one JPEG.

    Two branches, and the distinction is the whole point of this helper:

    * The frame's own length must fit the deck. `total` is at least the frame
      length, and `d200_vs_validate_payload` case 3 requires
      `total <= D200_VS_MAX_JPEG`, so a longer frame is refused here by name.
    * Otherwise the frame is padded up to a whole `FRAME_JPEG_CHUNK` fragment
      *only while the padded total still fits the cap*. Past that the frame is
      returned unpadded, which costs nothing: the device completes a frame by
      accumulated offset (`d200_vs_state_accept`, kind 3:
      `s->partial_offset += h->payload_length - 16; if (s->partial_offset ==
      total)`), and the reader's own bound is the inequality
      `n - 16 <= total - offset`, i.e. a short final fragment is explicitly
      allowed. Padding was never a protocol requirement; the comment above this
      chunk size records a stall caused by records that were TOO BIG, and a
      shorter final record cannot re-introduce that.

    So for every frame whose padded length fits, the bytes returned are identical
    to the padded-only revision; only frames that used to be refused change, and
    they change from "error" to "sent with a short final fragment".
    """
    if len(frame) > wire.MAX_JPEG:
        raise JpegFramingError(
            f"JPEG frame of {len(frame)} bytes exceeds the {wire.MAX_JPEG}-byte deck cap; "
            "reduce tile size or check ffmpeg output"
        )
    remainder = len(frame) % FRAME_JPEG_CHUNK
    if not remainder:
        return frame
    padding = FRAME_JPEG_CHUNK - remainder
    if len(frame) + padding <= wire.MAX_JPEG:
        return frame + b'\x00' * padding
    return frame


def run(*arguments, check=True, capture=False):
    return _SOURCE_PROCESSES.run((ADB, "-s", SERIAL, *arguments[1:]) if arguments and arguments[0] == ADB
                          else arguments, check=check, capture_output=capture, text=capture,
                          timeout=90)


def validated_session(value):
    if not re.fullmatch(r"[0-9a-f]{32}", value):
        raise argparse.ArgumentTypeError("session must be 32 lowercase hexadecimal characters")
    return value


def should_retry_unproven_open(answer) -> bool:
    """True when OPEN was refused because the previous session has not released the deck."""
    if not isinstance(answer, dict) or answer.get("accepted"):
        return False
    return UNPROVEN_OPEN in (answer.get("error") or "")


def ensure_runtime_modules():
    """Verify dependency loading; old device adbd does not relay shell exit codes."""
    for module in ('mi_rgn', 'mi_divp'):
        result = run(ADB, 'shell', 'cat', '/proc/modules', capture=True)
        if module in {line.split()[0] for line in result.stdout.splitlines() if line.split()}:
            continue
        run(ADB, 'shell', 'insmod', f'/config/modules/{module}.ko', capture=True)
        result = run(ADB, 'shell', 'cat', '/proc/modules', capture=True)
        if module not in {line.split()[0] for line in result.stdout.splitlines() if line.split()}:
            raise RuntimeError(f'required device module did not load: {module}')


def probe_source_fps(source):
    try:
        result = run("ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                     "stream=avg_frame_rate,r_frame_rate", "-of", "json", source, capture=True)
    except FileNotFoundError as error:
        # C-004/C-122: ffprobe is a hard requirement of every run and its absence
        # must be a diagnosis, not a traceback out of the player.
        raise RuntimeError(f"ffprobe is not on PATH: install ffmpeg ({source})") from error
    except subprocess.CalledProcessError as error:
        status = "" if error.returncode is None else f" (exit {error.returncode})"
        raise RuntimeError(f"ffprobe could not read {source}{status}") from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"ffprobe timed out reading {source}") from error
    try:
        stream = (json.loads(result.stdout).get("streams") or [{}])[0]
    except json.JSONDecodeError:
        stream = {}
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = stream.get(key)
        if not raw or raw in ("0/0", "N/A"):
            continue
        try:
            fps = Fraction(raw)
        except (ValueError, ZeroDivisionError):
            continue
        if 0 < fps <= 240:
            return fps
    return Fraction(30, 1)


def _usable_letterbox_crop(frame_w, frame_h, crop_w, crop_h, crop_x, crop_y):
    """Keep true letter/pillar bars. Reject stage-dark zoom-ins."""
    if frame_w <= 0 or frame_h <= 0 or crop_w <= 0 or crop_h <= 0:
        return "none"
    if crop_x < 0 or crop_y < 0 or crop_x + crop_w > frame_w or crop_y + crop_h > frame_h:
        return "none"
    if crop_w / frame_w < 0.92 and crop_h / frame_h < 0.92:
        return "none"
    if crop_w / frame_w >= 0.98 and crop_h / frame_h >= 0.98:
        return "none"
    return f"{crop_w}:{crop_h}:{crop_x}:{crop_y}"


def detect_crop(source):
    try:
        probed = run("ffprobe", "-v", "error", "-select_streams", "v:0",
                     "-show_entries", "stream=width,height:format=duration",
                     "-of", "json", source, capture=True)
        payload = json.loads(probed.stdout)
        stream = (payload.get("streams") or [{}])[0]
        frame_w = int(stream.get("width") or 0)
        frame_h = int(stream.get("height") or 0)
        raw_duration = (payload.get("format") or {}).get("duration")
        duration = float(raw_duration) if raw_duration not in (None, "", "N/A") else 30.0
        if not math.isfinite(duration) or duration <= 0:
            duration = 30.0
        start = min(max(duration * 0.15, 1.0), max(0.0, duration - 4.0))
        sample = min(8.0, max(2.0, duration - start))
        seek = [] if source.startswith(("http://", "https://")) else ["-ss", f"{start:.3f}"]
        if source.startswith(("http://", "https://")):
            sample = min(2.0, sample)
        result = run("ffmpeg", "-hide_banner", *seek, "-i", source,
                     "-t", f"{sample:.3f}",
                     "-vf", "fps=2,cropdetect=24:2:0", "-f", "null", "-",
                     check=False, capture=True)
        candidates = re.findall(r"crop=(\d+:\d+:\d+:\d+)", result.stderr or "")
        if not candidates:
            return "none"
        crop, count = Counter(candidates).most_common(1)[0]
        if count < max(2, len(candidates) // 3):
            return "none"
        crop_w, crop_h, crop_x, crop_y = (int(part) for part in crop.split(":"))
        if frame_w and frame_h:
            return _usable_letterbox_crop(frame_w, frame_h, crop_w, crop_h, crop_x, crop_y)
        return crop
    except (OSError, ValueError, TypeError, json.JSONDecodeError, subprocess.CalledProcessError):
        return "none"


def probe_duration(source):
    """Seconds of the origin source. 0 if unknown. Never HID."""
    if source.startswith(("http://", "https://")):
        try:
            result = run(
                "yt-dlp", "--no-warnings", "--skip-download", "-O", "%(duration)s", source,
                capture=True,
            )
            value = float((result.stdout or "").strip())
            if math.isfinite(value) and value > 0:
                return value
        except (OSError, ValueError, TypeError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass
    try:
        result = run(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "csv=p=0", source, capture=True,
        )
        value = float((result.stdout or "").strip())
        if math.isfinite(value) and value > 0:
            return value
    except (OSError, ValueError, TypeError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass
    return 0.0



def parse_fps(value, source):
    if value == "source":
        fps = probe_source_fps(source)
        if fps > 30:
            fps = Fraction(30, 1)
    else:
        try:
            fps = Fraction(value)
        except (ValueError, ZeroDivisionError) as error:
            raise SystemExit("fps must be 'source', an integer, or a fraction such as 24000/1001") from error
        if fps <= 0 or fps > 60:
            raise SystemExit("fps must be in (0, 60]")
    wire.validate_fps(fps.numerator, fps.denominator)
    return fps


def drain_bounded(stream, storage, limit=64 * 1024):
    while True:
        block = stream.read(4096)
        if not block:
            return
        storage.extend(block)
        if len(storage) > limit:
            del storage[:-limit]
class FramePump:
    """Drain JPEG stdout in order. Drop oldest only when the queue is full."""

    def __init__(self, raw, max_frames=8):
        self._q = queue.Queue(maxsize=max_frames)
        self._raw = raw
        threading.Thread(target=self._run, daemon=True).start()

    def _offer(self, item):
        if item is None:
            self._q.put(item)
            return
        while True:
            try:
                self._q.put_nowait(item)
                return
            except queue.Full:
                try:
                    dropped = self._q.get_nowait()
                except queue.Empty:
                    continue
                if dropped is None:
                    self._q.put(None)
                    return

    def _run(self):
        framer = JpegFramer(max_frame_bytes=MAX_JPEG_BYTES)
        fd = self._raw.fileno()
        try:
            os.set_blocking(fd, True)
        except OSError:
            pass
        try:
            while True:
                try:
                    block = os.read(fd, 65536)
                except OSError:
                    block = b""
                if not block:
                    try:
                        framer.finish()
                    except Exception:
                        pass
                    self._offer(None)
                    return
                for frame in framer.feed(block):
                    self._offer(bytes(frame))
        except Exception:
            self._offer(None)

    def get(self, timeout=0.05):
        return self._q.get(timeout=timeout)



def check_cancel(cancel):
    if cancel.is_set():
        raise InterruptedError("playback cancelled")


def wait_io(readers, writers, deadline, cancel):
    while True:
        check_cancel(cancel)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("video operation deadline expired")
        ready = select.select(readers, writers, [], min(.05, remaining))
        if ready[0] or ready[1]:
            return ready[:2]


def json_exchange(client, request, deadline, cancel):
    encoded = json.dumps(request, separators=(",", ":")).encode() + b"\n"
    if len(encoded) > 4096:
        raise wire.ProtocolError("oversized JSON request")
    offset = 0
    while offset < len(encoded):
        wait_io([], [client], deadline, cancel)
        try:
            count = client.send(encoded[offset:])
        except BlockingIOError:
            continue
        if not count:
            raise EOFError("bridge closed during request")
        offset += count
    # Read only through the newline: an immediately following READY stays on socket.
    response = bytearray()
    while len(response) < 4096:
        wait_io([client], [], deadline, cancel)
        try:
            block = client.recv(1)
        except BlockingIOError:
            continue
        if not block:
            raise EOFError("bridge closed during JSON response")
        response.extend(block)
        if block == b"\n":
            answer = json.loads(response)
            validate_answer(answer, request)
            return answer
    raise wire.ProtocolError("oversized JSON response")


def validate_answer(answer, request):
    base = {"schemaVersion", "accepted", "op", "protocolVersion", "session", "epoch", "resultCode"}
    if not isinstance(answer, dict) or not base <= answer.keys():
        raise wire.ProtocolError("invalid bridge response")
    if (type(answer["accepted"]) is not bool or type(answer["schemaVersion"]) is not int
            or answer["schemaVersion"] != 1 or type(answer["protocolVersion"]) is not int
            or answer["protocolVersion"] != 1 or answer["op"] != request["op"]
            or answer["session"] != request["session"]):
        raise wire.ProtocolError("mismatched bridge response")
    wire.uint(answer["resultCode"], 32, maximum=15)
    if request["op"] != "videoOpen":
        raise wire.ProtocolError("JSON upgrade requires OPEN")
    expected_epoch = 1 if answer["accepted"] else 0
    if type(answer["epoch"]) is not int or answer["epoch"] != expected_epoch:
        raise wire.ProtocolError("mismatched bridge epoch")
    extras = set()
    if request["op"] == "videoOpen" and answer["accepted"]:
        wire.capability_bytes(answer.get("capability"))
        extras.add("capability")
    if "error" in answer:
        text = answer["error"]
        if not isinstance(text, str) or "\0" in text or len(text.encode()) > 252:
            raise wire.ProtocolError("invalid bridge diagnostic")
        extras.add("error")
    if set(answer) != base | extras or (answer["accepted"] != (answer["resultCode"] == 0)):
        raise wire.ProtocolError("invalid bridge result fields")


def connect_bridge(path, deadline, cancel):
    endpoint = Path(path).lstat()
    if (not stat.S_ISSOCK(endpoint.st_mode) or endpoint.st_uid != os.geteuid()
            or endpoint.st_mode & 0o077):
        raise RuntimeError("untrusted video bridge socket")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.setblocking(False)
    try:
        try:
            client.connect(str(path))
        except (BlockingIOError, InterruptedError):
            wait_io([], [client], deadline, cancel)
            error = client.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if error:
                raise OSError(error, "bridge connect failed")
        return client
    except BaseException:
        client.close()
        raise


class HostDiagnostics:
    """Fixed-size host observations; no source, owner, capability or native clock."""
    def __init__(self, clock=None):
        self.clock = clock or time.monotonic_ns
        self.started = self.clock()
        self.milestones = dict.fromkeys(("firstSourceJpegReady", "readyReceipt",
                                        "firstConsumedReceipt", "cancelRequested",
                                        "cancelProofReceipt"))
        self.parsed = self.sent = self.consumed = self.bytes_sent = 0
        self.queue_highwater = 0
        self.producer_wait = self.credit_wait = 0
        self.socket_buffers = None

    def mark(self, name):
        if self.milestones[name] is None:
            self.milestones[name] = self.clock()

    def snapshot(self):
        return dict(schemaVersion=1, clock="host-monotonic-nanoseconds",
                    startedMonotonicNs=self.started, hostElapsedNs=self.clock() - self.started,
                    milestones={name: dict(monotonicNs=value,
                                           reason="not observed" if value is None else None)
                                for name, value in self.milestones.items()},
                    framesParsed=self.parsed, framesSent=self.sent, framesConsumed=self.consumed,
                    streamBytesSent=self.bytes_sent,
                    queuedApplicationJpegBytesHighWater=self.queue_highwater,
                    queueAccounting="unconsumed JPEG reservations including host partial assembly; excludes copies, read block and kernel buffers",
                    producerWaitNs=self.producer_wait, creditWaitNs=self.credit_wait,
                    waitAccounting="host readiness/credit waits, including duplex receive servicing",
                    effectiveSocketBuffers=None if self.socket_buffers is None else
                    dict(sendBytes=self.socket_buffers[0], receiveBytes=self.socket_buffers[1]),
                    socketBuffersReason="not connected" if self.socket_buffers is None else None,
                    stageMeaning="CONSUMED is host receipt of submission/pacing acknowledgement; not pixel latency or native lateness")


class VideoStream:
    """One nonblocking duplex stream with one inbound/outbound record budget."""
    def __init__(self, client, session, capability, fps, cancel, diagnostics=None, on_progress=None):
        self.client = client
        client.setblocking(False)
        for option in (socket.SO_SNDBUF, socket.SO_RCVBUF):
            client.setsockopt(socket.SOL_SOCKET, option, 65576)
        try:
            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self.socket_buffers = tuple(client.getsockopt(socket.SOL_SOCKET, option)
                                    for option in (socket.SO_SNDBUF, socket.SO_RCVBUF))
        self.diagnostics = diagnostics or HostDiagnostics()
        self.diagnostics.socket_buffers = self.socket_buffers
        self.on_progress = on_progress
        self.reservations = {}
        self.state = wire.StreamState(session, fps.numerator, fps.denominator, capability=capability)
        self.cancel = cancel
        self.incoming = bytearray()
        self.record_deadline = None
        self.progress_timeout = max(30.0, 3 / float(fps))
        self.progress_deadline = time.monotonic() + self.progress_timeout
        self.drain_timeout = max(10.0, 2 / float(fps) + 5)

    def receive_available(self):
        target = wire.HEADER_SIZE
        if len(self.incoming) >= target:
            target += wire.decode_record_header(bytes(self.incoming[:target]))[1]
        try:
            block = self.client.recv(target - len(self.incoming))
        except BlockingIOError:
            return
        if not block:
            raise EOFError("video EOF before terminal result")
        if not self.incoming:
            self.record_deadline = time.monotonic() + 5
        self.incoming.extend(block)
        if len(self.incoming) >= wire.HEADER_SIZE:
            target = wire.HEADER_SIZE + wire.decode_record_header(bytes(self.incoming[:wire.HEADER_SIZE]))[1]
        if len(self.incoming) == target and target > wire.HEADER_SIZE:
            record = wire.decode_record(bytes(self.incoming), direction=wire.CONSUMER)
            self.state.accept(record, wire.CONSUMER)
            self.incoming.clear()
            self.record_deadline = None
            self.progress_deadline = time.monotonic() + self.progress_timeout
            if record.kind == wire.READY:
                self.diagnostics.mark("readyReceipt")
            elif record.kind == wire.CONSUMED:
                self.diagnostics.mark("firstConsumedReceipt")
                self.diagnostics.consumed += 1
                self.reservations.pop(struct.unpack('>Q', record.payload)[0], None)
            if self.on_progress is not None:
                self.on_progress()
            if record.kind == wire.ERROR:
                raise RuntimeError(f"video consumer failed with code {struct.unpack_from('>I', record.payload)[0]}")
            if record.kind == wire.CANCELLED:
                raise InterruptedError("video consumer cancelled")

    def wait(self, deadline, *, writable=False, source=None):
        deadline = min(deadline, self.record_deadline or deadline)
        readers = [self.client] + ([] if source is None else [source])
        readable, writers = wait_io(readers, [self.client] if writable else [], deadline, self.cancel)
        if self.client in readable:
            self.receive_available()
        return source in readable if source is not None else bool(writers)

    def send(self, kind, payload, deadline):
        record = wire.encode_record(kind, self.state.session, 1,
                                    self.state.sequences[wire.PRODUCER], payload)
        self.state.accept(wire.decode_record(record), wire.PRODUCER)
        offset = 0
        started = None
        while offset < len(record):
            if self.wait(min(deadline, started + 5 if started else deadline), writable=True):
                try:
                    count = self.client.send(memoryview(record)[offset:])
                except BlockingIOError:
                    continue
                if not count:
                    raise EOFError("video send closed")
                if started is None:
                    started = time.monotonic()
                offset += count
                self.diagnostics.bytes_sent += count
        if kind == wire.FRAME:
            _, total, fragment_offset = struct.unpack_from('>QII', payload)
            if fragment_offset + len(payload) - 16 == total:
                self.diagnostics.sent += 1

    def measured_wait(self, kind, *, source=None):
        started = self.diagnostics.clock()
        try:
            return self.wait(self.progress_deadline, source=source)
        finally:
            elapsed = self.diagnostics.clock() - started
            if kind == "credit":
                self.diagnostics.credit_wait += elapsed
            else:
                self.diagnostics.producer_wait += elapsed

    def attach(self, capability, deadline):
        self.send(wire.ATTACH, wire.capability_bytes(capability), deadline)
        while not self.state.ready:
            self.wait(deadline)

    def produce(self, source, encoder, loop=False, speaker=None, early_end=None):
        """Never request a fill-sized buffered read; pause the framer at each JPEG.

        `early_end()` is asked once the encoder exits cleanly: True means it lost an HTTP origin
        before the source's end, so the caller resumes from the playhead instead of finishing.
        """
        pump = source
        while True:
            check_cancel(self.cancel)
            wanted = take_seek_request()
            volume = take_volume_request()
            if volume is not None and not apply_encoder_volume(speaker, volume):
                raise VolumeRequested(volume)
            overlay = take_overlay_request()
            if overlay is not None:
                push_studio_alpha(overlay)
            if wanted is not None:
                raise SeekRequested(wanted)
            while self.state.received - self.state.consumed >= wire.WINDOW_FRAMES:
                wanted = take_seek_request()
                if wanted is not None:
                    raise SeekRequested(wanted)
                self.measured_wait("credit")
            try:
                item = pump.get(timeout=0.05)
            except queue.Empty:
                if encoder.poll() is not None:
                    item = None
                else:
                    continue
            if item is None:
                exit_deadline = time.monotonic() + 3
                while encoder.poll() is None:
                    check_cancel(self.cancel)
                    if time.monotonic() >= min(exit_deadline, self.progress_deadline):
                        raise TimeoutError("encoder exit deadline expired")
                    readable, _, _ = select.select([self.client], [], [], .05)
                    if readable:
                        self.receive_available()
                if encoder.returncode:
                    raise RuntimeError("ffmpeg encoder failed")
                if early_end is not None and early_end():
                    raise SourceEndedEarly()
                if loop:
                    raise SeekRequested(0.0)
                self.send(wire.EOS, struct.pack('>Q', self.state.received), self.progress_deadline)
                deadline = time.monotonic() + self.drain_timeout
                while not self.state.terminal:
                    self.wait(deadline)
                return self.state.received
            frame = item
            self.diagnostics.mark("firstSourceJpegReady")
            self.diagnostics.parsed += 1
            frame = align_jpeg_payload(frame)
            self.reservations[self.state.received] = len(frame)
            self.diagnostics.queue_highwater = max(self.diagnostics.queue_highwater,
                                                   sum(self.reservations.values()))
            if self.on_progress is not None:
                self.on_progress()
            # The generator remains paused with at most one short input block.
            index = self.state.received
            for offset in range(0, len(frame), FRAME_JPEG_CHUNK):
                fragment = frame[offset:offset + FRAME_JPEG_CHUNK]
                self.send(wire.FRAME, struct.pack('>QII', index, len(frame), offset) + fragment,
                          self.progress_deadline)
            del frame


def stop_encoder(encoder, *, harsh: bool = False):
    if encoder is None:
        return
    if encoder.poll() is None:
        if harsh:
            encoder.kill()
            try:
                encoder.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        else:
            encoder.terminate()
            try:
                encoder.wait(timeout=3)
            except subprocess.TimeoutExpired:
                encoder.kill()
                encoder.wait(timeout=2)
    for stream in (encoder.stdin, encoder.stdout, encoder.stderr):
        if stream:
            stream.close()
    if encoder.poll() is not None:
        try:
            _MEDIA_CHILDREN.remove(encoder)
        except ValueError:
            pass


def spawn_media(command, **kwargs):
    # SIGTERM must not unwind between Popen and registering its returned handle.
    _SOURCE_PROCESSES.spawning += 1
    try:
        if _SOURCE_PROCESSES.cancelled.is_set():
            raise InterruptedError("playback cancelled")
        child = subprocess.Popen(command, **kwargs)
        _MEDIA_CHILDREN.append(child)
    finally:
        _SOURCE_PROCESSES.spawning -= 1
    if _SOURCE_PROCESSES.cancelled.is_set():
        raise InterruptedError("playback cancelled")
    return child


def spawn_speaker(audio_cmd):
    """The host speaker. stdin is the live-volume channel; stderr is discarded.

    stderr used to be a PIPE nobody read, so once ffmpeg's warnings (reconnects, decode notices)
    filled the 16KB pipe buffer the speaker blocked on its next write and the sound stopped.
    """
    if not audio_cmd:
        return None
    return spawn_media(
        audio_cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, bufsize=0,
    )


def spawn_av(command, audio_cmd):
    encoder = spawn_media(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
    )
    return encoder, spawn_speaker(audio_cmd)


def initial_status():
    return dict(state=wire.OPENING, rendererReady=False, framesReceived=0,
                framesConsumed=0, eosTotal=None, terminalCode=0,
                cleanup="pending", cancelPhase="none")


def build_video_filters(args, fps, crop):
    """Build the native graph from resolved FPS/crop without source or device I/O."""
    if args.image_resolution != "native":
        raise ValueError("only native image resolution is supported")
    source_crop = f"crop={crop}," if crop != "none" else ""
    fit = getattr(args, "fit", "auto")
    if fit == "pad":
        spatial_filter = ("scale=960:540:force_original_aspect_ratio=decrease,"
                          "pad=960:540:(ow-iw)/2:(oh-ih)/2:black")
        source_crop = ""
    else:
        spatial_filter = "scale=960:540:force_original_aspect_ratio=increase,crop=960:540"
        if fit == "cover":
            source_crop = ""
    timeline_filter = (
        f"setpts=PTS/{args.playback_rate:.9g},"
        if args.playback_rate != 1.0 else ""
    )
    if args.interpolate:
        interpolation_options = {
            "fast": "me=epzs:mb_size=16:search_param=8",
            "quality": "me=hexbs:mb_size=8:search_param=32",
            "maximum": "me=umh:mb_size=8:search_param=64",
        }
        motion_options = ("mc_mode=obmc:me_mode=bilat:vsbmc=0:"
                          if args.interpolation_quality == "fast"
                          else "mc_mode=aobmc:me_mode=bidir:vsbmc=1:")
        rate_filter = timeline_filter + (
            f"minterpolate=fps={fps.numerator}/{fps.denominator}:mi_mode=mci:"
            f"{motion_options}scd=fdiff:"
            f"{interpolation_options[args.interpolation_quality]}"
        )
        return f"{source_crop}{spatial_filter},transpose=2,{rate_filter}"
    rate_filter = f"{timeline_filter}fps={fps.numerator}/{fps.denominator}"
    return f"{rate_filter},{source_crop}{spatial_filter},transpose=2"


def resolve_media_urls(source):
    """Video URL plus a separate audio URL when yt-dlp prints both."""
    if source.startswith(("http://", "https://")):
        probe = run(
            "yt-dlp", "--no-warnings", "-f",
            "bv*[vcodec^=avc][height<=720]+ba/b[vcodec^=avc][height<=720]/bv*[height<=540]+ba/b[height<=720]",
            "-g", source, capture=True,
        )
        urls = [line.strip() for line in probe.stdout.splitlines() if line.strip()]
        if not urls:
            raise RuntimeError("yt-dlp produced no stream URL")
        return urls[0], urls[1] if len(urls) > 1 else None
    return source, source


def source_has_audio(source):
    """True when ffprobe sees an audio stream. Missing tools mean no audio, not a crash."""
    try:
        result = run(
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=index", "-of", "csv=p=0", source,
            check=False, capture=True,
        )
    except (OSError, subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return bool((result.stdout or "").strip())


def audio_filter(args):
    """Host PCM. Large regular frames so AudioToolbox's two buffers do not underrun."""
    gain = clamp_volume(getattr(args, "volume", 1.0))
    return f"volume@vol={gain:.4f},aresample=48000,asetnsamples=n=8192:p=1"


def build_encoder_command(args, video, audio, filters):
    """JPEG only. Host audio is a second realtime ffmpeg so JPEG credit cannot underrun it."""
    command = ["ffmpeg", "-v", "error", "-nostdin"]
    _input_flags(command, video, args, realtime=True)
    if args.duration:
        command.extend(["-t", str(args.duration)])
    command.extend([
        "-map", "0:v:0",
        "-vf", filters,
        "-q:v", str(args.quality),
        "-pix_fmt", "yuvj420p",
        "-f", "image2pipe", "pipe:1",
        "-an",
    ])
    return command


def build_audio_command(args, audio):
    """Realtime AudioToolbox. Independent of the JPEG credit stall."""
    if not audio:
        return None
    command = ["ffmpeg", "-v", "error"]
    _input_flags(command, audio, args, realtime=True)
    if args.duration:
        command.extend(["-t", str(args.duration)])
    command.extend([
        "-vn",
        "-filter:a", audio_filter(args),
        "-ar", "48000",
        "-ac", "2",
        "-c:a", "pcm_s16le",
        "-f", "audiotoolbox", "dummy",
    ])
    return command


def _input_flags(command, url, args, realtime=False):
    command.extend(["-thread_queue_size", "512"])
    if str(url).startswith(("http://", "https://")):
        command.extend([
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_on_network_error", "1",
        ])
    if str(url).startswith(("http://", "https://")):
        # The default 2s cap gave up on an origin that stayed away longer: measured, a 20s outage
        # decoded 18s of a 60s stream and ffmpeg still exited 0. 30s per attempt and a 120s total
        # budget ride out a Wi-Fi roam; an outage longer than that resumes via `ended_early`.
        command.extend(["-reconnect_delay_max", "30", "-reconnect_delay_total_max", "120"])
    if realtime:
        command.append("-re")
    if args.start:
        command.extend(["-ss", str(args.start)])
    command.extend(["-i", url])



def main():
    parser = argparse.ArgumentParser(description="Stream owned persistent JPEG video to the D200 native DIVP display plane")
    parser.add_argument("input")
    parser.add_argument("--session", type=validated_session)
    parser.add_argument("--fps", default="source")
    parser.add_argument("--quality", type=int, default=5)
    parser.add_argument("--duration", type=float, default=0)
    parser.add_argument("--start", type=float, default=0)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--playback-rate", type=float, default=1.0)
    parser.add_argument("--interpolate", action="store_true")
    parser.add_argument("--interpolation-quality", choices=("fast", "quality", "maximum"), default="quality")
    parser.add_argument("--image-resolution", choices=("native", "half", "balanced", "full"), default="native")
    parser.add_argument("--spatial-filter", choices=("nearest", "bilinear", "sharp"), default="nearest")
    parser.add_argument("--studio-overlay", help=argparse.SUPPRESS)
    parser.add_argument("--hardware-scale", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--pause-stock-ui", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--crop", default="auto")
    parser.add_argument("--fit", choices=("auto", "pad", "cover"), default="auto")
    parser.add_argument("--no-audio", action="store_true")
    parser.add_argument("--volume", type=float, default=1.0)
    args = parser.parse_args()
    args.volume = clamp_volume(args.volume)
    if args.pause_stock_ui or args.studio_overlay or args.hardware_scale:
        raise SystemExit("stock zkgui remains authoritative; overlay/hardware-scale/pause-stock-ui are unsupported")
    if args.image_resolution != "native":
        raise SystemExit("only --image-resolution native is supported by the Studio-native video plane")
    if not 0 < args.playback_rate <= 16:
        raise SystemExit("playback rate must be in (0, 16]")
    if any(not math.isfinite(value) or value < 0 for value in (args.start, args.duration)):
        raise SystemExit("start and duration must be finite and non-negative")
    if args.crop not in ("auto", "none") and not re.fullmatch(r"[1-9]\d*:[1-9]\d*:\d+:\d+", args.crop):
        raise SystemExit("crop must be 'auto', 'none', or width:height:x:y")
    session = args.session or secrets.token_hex(16)
    requester = os.environ.get("D200_VIDEO_REQUEST_SESSION")
    if requester is not None and validated_session(requester) != session:
        raise SystemExit("request session does not match player session")
    cancel = threading.Event()
    diagnostics = HostDiagnostics()
    finalizing = False
    client = encoder = speaker = stderr_thread = None

    def interrupt(_signum, _frame):
        cancel.set()
        _SOURCE_PROCESSES.cancelled.set()
        diagnostics.mark("cancelRequested")
        try:
            if speaker is not None and speaker.poll() is None:
                speaker.kill()
            if encoder is not None and encoder.poll() is None:
                encoder.kill()
        except OSError:
            pass
        if not finalizing and not _SOURCE_PROCESSES.spawning:
            raise InterruptedError("playback cancelled")

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, interrupt)
    if args.loop:
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    else:
        signal.signal(signal.SIGHUP, interrupt)
    if "GHOSTDECK_CHILD_LEASE" in os.environ:
        from ghostdeck.lifecycle import child_lease
        child_lease(lambda: os.kill(os.getpid(), signal.SIGTERM))
    endpoint = StopEndpoint("player", lambda: os.kill(os.getpid(), signal.SIGTERM))
    owner = endpoint.state()
    state = dict(schemaVersion=2, phase="active", pid=os.getpid(), control=owner,
                 source=args.input, fps=args.fps, quality=args.quality, crop=args.crop,
                 start=args.start, loop=args.loop, playbackRate=args.playback_rate,
                 interpolate=args.interpolate, interpolationQuality=args.interpolation_quality,
                 imageResolution=args.image_resolution, spatialFilter=args.spatial_filter,
                 presentation="divp-disp-native-studio",
                 video=dict(protocolVersion=1, session=session, epoch=0, capability=None,
                            ownerControl=owner, requestSession=requester, status=initial_status(),
                            startupResultObserved=requester is None, terminalRetainUntilMonotonicNs=None))
    claimed = False
    client = encoder = speaker = stderr_thread = None
    credentials = None
    open_rejected = False
    success = False
    last_publication = None

    def publish_diagnostics():
        nonlocal state, last_publication
        now = diagnostics.clock()
        if last_publication is None or now - last_publication >= 200_000_000:
            state["diagnostics"] = diagnostics.snapshot()
            state["playheadAt"] = time.time()
            state = publish_video_state(state, state_path=HOST_STATE)
            last_publication = now

    try:
        state["diagnostics"] = diagnostics.snapshot()
        state = publish_video_state(state, claim=True, state_path=HOST_STATE)
        claimed = True
        duration_box = [0.0]
        dur_thread = threading.Thread(
            target=lambda: duration_box.__setitem__(0, probe_duration(args.input)),
            daemon=True,
        )
        dur_thread.start()
        source = args.input
        audio = None
        if source.startswith(("http://", "https://")):
            video, audio = resolve_media_urls(source)
            source = video
        elif not args.no_audio:
            audio = source if source_has_audio(source) else None
        if args.no_audio:
            audio = None
        fps = parse_fps(args.fps, source)
        crop_box = [args.crop]

        def fill_crop():
            crop_box[0] = detect_crop(source) if args.crop == "auto" else args.crop

        crop_thread = threading.Thread(target=fill_crop, daemon=True)
        crop_thread.start()
        ensure_runtime_modules()
        deadline = time.monotonic() + 10
        client = connect_bridge(BRIDGE_SOCKET, deadline, cancel)
        open_request = dict(schemaVersion=1, op="videoOpen", session=session,
                            fpsNumerator=fps.numerator, fpsDenominator=fps.denominator)
        answer = json_exchange(client, open_request, deadline, cancel)
        if should_retry_unproven_open(answer):
            try:
                client.close()
            except OSError:
                pass
            time.sleep(OPEN_RETRY_WAIT)
            deadline = time.monotonic() + 10
            client = connect_bridge(BRIDGE_SOCKET, deadline, cancel)
            answer = json_exchange(client, open_request, deadline, cancel)
        if not answer["accepted"]:
            open_rejected = True
            detail = answer.get("error")
            raise RuntimeError(f"video OPEN failed with code {answer['resultCode']}"
                               + (f": {detail}" if detail else ""))
        credentials = dict(session=session, epoch=1, capability=answer["capability"])
        state["video"].update(epoch=1, capability=answer["capability"])
        crop_thread.join()
        dur_thread.join()
        crop = crop_box[0]
        state["duration"] = duration_box[0]
        state["crop"] = crop
        filters = build_video_filters(args, fps, crop)
        command = build_encoder_command(args, source, audio, filters)
        state["diagnostics"] = diagnostics.snapshot()
        state = publish_video_state(state, state_path=HOST_STATE)
        stream = VideoStream(client, session, answer["capability"], fps, cancel,
                             diagnostics=diagnostics, on_progress=publish_diagnostics)
        stream.attach(answer["capability"], deadline)
        state["video"]["status"].update(state=wire.STATE_READY, rendererReady=True)
        state["diagnostics"] = diagnostics.snapshot()
        state = publish_video_state(state, state_path=HOST_STATE)
        check_cancel(cancel)
        audio_cmd = build_audio_command(args, audio)
        encoder, speaker = spawn_av(command, audio_cmd)
        stderr_tail = bytearray()
        stderr_thread = threading.Thread(target=drain_bounded, args=(encoder.stderr, stderr_tail), daemon=True)
        stderr_thread.start()
        pump = FramePump(encoder.stdout)
        pending_http = [None]

        def prefetch_http():
            if not args.loop or not args.input.startswith(("http://", "https://")):
                return
            try:
                pending_http[0] = resolve_media_urls(args.input)
            except Exception:
                pending_http[0] = None

        threading.Thread(target=prefetch_http, daemon=True).start()
        def playhead_loop():
            while not cancel.is_set() and not finalizing:
                publish_diagnostics()
                cancel.wait(0.2)

        threading.Thread(target=playhead_loop, daemon=True).start()

        def playhead():
            return speaker_playhead(args.start, diagnostics.started, diagnostics.clock(), args.playback_rate)

        resumes = []

        def early_end():
            if not ended_early(args.input, state.get("duration"), playhead()):
                return False
            # A source that keeps dropping at once (a revoked video, no network at all) must end the
            # session rather than spin: at most 3 resumes in any 10 minutes.
            now = time.monotonic()
            resumes[:] = [at for at in resumes if now - at < 600]
            if len(resumes) >= MAX_RESUMES_PER_10_MIN:
                return False
            resumes.append(now)
            return True

        while True:
            try:
                total = stream.produce(pump, encoder, loop=args.loop, speaker=speaker, early_end=early_end)
                break
            except SourceEndedEarly:
                # Same recovery as a seek to where the picture stopped, and the seek path already
                # re-resolves yt-dlp, so an expired signed URL is replaced as well.
                resume = playhead()
                emit_diagnostic(sys.stderr, dict(event="hostSourceResumed", atSeconds=round(resume, 3),
                                                 durationSeconds=state.get("duration")))
                pending_http[0] = None
                seek = SeekRequested(resume)
                args.start = seek.start
                state["start"] = seek.start
                diagnostics.started = diagnostics.clock()
                diagnostics.milestones["firstConsumedReceipt"] = None
                state["playheadAt"] = time.time()
                stop_encoder(speaker, harsh=True)
                stop_encoder(encoder, harsh=True)
                video, audio = resolve_media_urls(args.input)
                source = video
                if args.no_audio:
                    audio = None
                args.volume = CURRENT_VOLUME
                command = build_encoder_command(args, source, audio, filters)
                encoder, speaker = spawn_av(command, build_audio_command(args, audio))
                stderr_tail = bytearray()
                stderr_thread = threading.Thread(
                    target=drain_bounded, args=(encoder.stderr, stderr_tail), daemon=True,
                )
                stderr_thread.start()
                pump = FramePump(encoder.stdout)
                publish_diagnostics()
            except VolumeRequested as vol:
                args.volume = clamp_volume(vol.gain)
                apply_encoder_volume(None, args.volume)
                stop_encoder(speaker, harsh=True)
                speaker = None
                if args.volume > 0 and audio:
                    saved_start = args.start
                    args.start = speaker_playhead(
                        saved_start, diagnostics.started, diagnostics.clock(), args.playback_rate,
                    )
                    speaker = spawn_speaker(build_audio_command(args, audio))
                    args.start = saved_start
            except SeekRequested as seek:
                args.start = seek.start
                state["start"] = seek.start
                diagnostics.started = diagnostics.clock()
                diagnostics.milestones["firstConsumedReceipt"] = None
                state["playheadAt"] = time.time()
                stop_encoder(speaker, harsh=True)
                stop_encoder(encoder, harsh=True)
                if args.input.startswith(("http://", "https://")):
                    grabbed = pending_http[0]
                    pending_http[0] = None
                    if grabbed:
                        video, audio = grabbed
                    else:
                        video, audio = resolve_media_urls(args.input)
                    source = video
                    if args.no_audio:
                        audio = None
                    threading.Thread(target=prefetch_http, daemon=True).start()
                args.volume = CURRENT_VOLUME
                command = build_encoder_command(args, source, audio, filters)
                audio_cmd = build_audio_command(args, audio)
                encoder, speaker = spawn_av(command, audio_cmd)
                stderr_tail = bytearray()
                stderr_thread = threading.Thread(
                    target=drain_bounded, args=(encoder.stderr, stderr_tail), daemon=True,
                )
                stderr_thread.start()
                pump = FramePump(encoder.stdout)
                publish_diagnostics()
        check_cancel(cancel)
        answer = video_bridge_request(dict(schemaVersion=1, op="videoStatus", **credentials), 5,
                                      socket_path=BRIDGE_SOCKET)
        if not answer["accepted"] or answer["status"]["state"] != wire.STATE_DONE or answer["status"]["cleanup"] != "proven":
            raise RuntimeError("video completion cleanup is unproven")
        state["video"]["status"] = answer["status"]
        success = True
    finally:
        finalizing = True
        cancel.set()
        try:
            if speaker is not None and speaker.poll() is None:
                speaker.kill()
        except OSError:
            pass
        # Interrupt the data lane and begin encoder termination before control cancellation.
        if client is not None:
            client.close()
        cleanup_error = None
        encoder_errors = []

        def cleanup_encoder():
            try:
                _SOURCE_PROCESSES.close()
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
                encoder_errors.append(error)
            try:
                stop_encoder(speaker, harsh=True)
            except (OSError, subprocess.TimeoutExpired) as error:
                encoder_errors.append(error)
            for child in tuple(_MEDIA_CHILDREN):
                try:
                    stop_encoder(child)
                except (OSError, subprocess.TimeoutExpired) as error:
                    encoder_errors.append(error)

        encoder_cleanup = threading.Thread(target=cleanup_encoder, daemon=True)
        encoder_cleanup.start()
        try:
            if claimed and not success:
                if credentials is not None:
                    try:
                        diagnostics.mark("cancelRequested")
                        answer = video_bridge_request(dict(schemaVersion=1, op="videoCancel", reason=13 if cancel.is_set() else 11,
                                                           **credentials), 35, socket_path=BRIDGE_SOCKET)
                        status = answer.get("status")
                        if status is None:
                            raise RuntimeError("matching cancellation proof unavailable")
                        state["video"]["status"] = status
                        if status["cleanup"] == "proven" and status["state"] in (wire.STATE_DONE, wire.STATE_CANCELLED, wire.FAILED):
                            diagnostics.mark("cancelProofReceipt")
                        if status["state"] not in (wire.STATE_DONE, wire.STATE_CANCELLED, wire.FAILED):
                            status.update(state=wire.FAILED, terminalCode=wire.CLEANUP_FAILED,
                                          cleanup="unproven", cancelPhase="unproven")
                    except (OSError, ValueError, RuntimeError, EOFError):
                        state["video"]["status"].update(state=wire.FAILED, terminalCode=wire.CLEANUP_FAILED,
                                                         cleanup="unproven", cancelPhase="unproven")
                else:
                    # An interrupted OPEN can have reserved a native owner without returning credentials.
                    state["video"]["status"].update(state=wire.FAILED, terminalCode=wire.SOURCE_FAILURE,
                                                     cleanup="unproven" if client is not None and not open_rejected else "proven")
        finally:
            try:
                encoder_cleanup.join(timeout=5.5)
                if encoder_cleanup.is_alive():
                    raise subprocess.TimeoutExpired("encoder cleanup", 5.5)
                if encoder_errors:
                    raise encoder_errors[0]
                if stderr_thread is not None:
                    stderr_thread.join(timeout=1)
                    if stderr_thread.is_alive():
                        raise subprocess.TimeoutExpired("encoder stderr drain", 1)
            except (OSError, subprocess.TimeoutExpired) as error:
                cleanup_error = error
                state["video"]["status"].update(state=wire.FAILED, terminalCode=wire.CLEANUP_FAILED, cleanup="unproven")
            try:
                if claimed:
                    state.update(phase="terminal", control=None)
                    state["diagnostics"] = diagnostics.snapshot()
                    publish_video_state(state, state_path=HOST_STATE)
                    emit_diagnostic(sys.stdout, dict(event="hostVideoTerminal", status=state["video"]["status"],
                                                     diagnostics=state["diagnostics"]))
            finally:
                endpoint.close()
            if cleanup_error is not None:
                raise RuntimeError("encoder cleanup failed") from cleanup_error


def cli():
    try:
        main()
    except (KeyboardInterrupt, InterruptedError):
        raise SystemExit(130) from None
    except SystemExit:
        raise
    except Exception as error:
        # C-122: `main()` has a `finally` but no handler, so a failing dependency
        # (a present-but-broken ffprobe, a bad source, a rejected video OPEN) used to
        # reach the user as a raw traceback. One bounded line names the failure and
        # the exit status stays non-zero; the `finally` cleanup has already run.
        detail = " ".join(str(error).split())[:300]
        print(f"{type(error).__name__}: {detail}" if detail else type(error).__name__,
              file=sys.stderr, flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    cli()

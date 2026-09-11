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
import time

from d200_jpeg import JpegFramer
from d200_process_control import StopEndpoint, emit_diagnostic, publish_video_state, video_bridge_request
import d200_video_stream as wire

import shutil as _shutil
ADB = _shutil.which("adb") or "adb"
SERIAL = __import__("os").environ.get("GHOSTDECK_SERIAL") or ""
MAX_JPEG_BYTES = 1024 * 1024
HOST_STATE = Path("/tmp/d200-color-host.json")
BRIDGE_SOCKET = Path("/tmp/d200-adb-bridge.sock")
# Keep FRAME records under 12KiB; 14387-byte records stalled at upHave=12288.
FRAME_JPEG_CHUNK = 12288 - wire.HEADER_SIZE - 16
def align_jpeg_payload(frame):
    remainder = len(frame) % FRAME_JPEG_CHUNK
    if remainder:
        frame += b'\x00' * (FRAME_JPEG_CHUNK - remainder)
    return frame


def run(*arguments, check=True, capture=False):
    return subprocess.run((ADB, "-s", SERIAL, *arguments[1:]) if arguments and arguments[0] == ADB
                          else arguments, check=check, capture_output=capture, text=capture,
                          timeout=90)


def validated_session(value):
    if not re.fullmatch(r"[0-9a-f]{32}", value):
        raise argparse.ArgumentTypeError("session must be 32 lowercase hexadecimal characters")
    return value


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
    result = run("ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                 "stream=avg_frame_rate,r_frame_rate", "-of", "json", source, capture=True)
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
        result = run("ffmpeg", "-hide_banner", "-ss", f"{start:.3f}", "-i", source,
                     "-t", f"{sample:.3f}",
                     "-vf", "fps=2,cropdetect=8:2:0", "-f", "null", "-",
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


def parse_fps(value, source):
    if value == "source":
        fps = probe_source_fps(source)
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

    def produce(self, source, encoder):
        """Never request a fill-sized buffered read; pause the framer at each JPEG."""
        framer = JpegFramer(max_frame_bytes=MAX_JPEG_BYTES)
        pending = iter(())
        fd = source.fileno()
        os.set_blocking(fd, False)
        while True:
            check_cancel(self.cancel)
            while self.state.received - self.state.consumed >= wire.WINDOW_FRAMES:
                self.measured_wait("credit")
            try:
                frame = next(pending)
            except StopIteration:
                self.diagnostics.queue_highwater = max(self.diagnostics.queue_highwater,
                                                       sum(self.reservations.values()) + len(framer.data))
                if not self.measured_wait("producer", source=fd):
                    continue
                try:
                    block = os.read(fd, 65536)
                except BlockingIOError:
                    continue
                if not block:
                    framer.finish()
                    # EOF alone is not successful encoder completion.
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
                    self.send(wire.EOS, struct.pack('>Q', self.state.received), self.progress_deadline)
                    deadline = time.monotonic() + self.drain_timeout
                    while not self.state.terminal:
                        self.wait(deadline)
                    return self.state.received
                pending = framer.feed(block)
                continue
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


def stop_encoder(encoder):
    if encoder is None:
        return
    if encoder.poll() is None:
        encoder.terminate()
        try:
            encoder.wait(timeout=3)
        except subprocess.TimeoutExpired:
            encoder.kill()
            encoder.wait(timeout=2)
    for stream in (encoder.stdout, encoder.stderr):
        if stream:
            stream.close()


def initial_status():
    return dict(state=wire.OPENING, rendererReady=False, framesReceived=0,
                framesConsumed=0, eosTotal=None, terminalCode=0,
                cleanup="pending", cancelPhase="none")


def build_video_filters(args, fps, crop):
    """Build the native graph from resolved FPS/crop without source or device I/O."""
    if args.image_resolution != "native":
        raise ValueError("only native image resolution is supported")
    source_crop = f"crop={crop}," if crop != "none" else ""
    if crop == "none":
        spatial_filter = ("scale=960:540:force_original_aspect_ratio=decrease,"
                          "pad=960:540:(ow-iw)/2:(oh-ih)/2:black")
    else:
        spatial_filter = "scale=960:540:force_original_aspect_ratio=increase,crop=960:540"
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
    args = parser.parse_args()
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

    def interrupt(_signum, _frame):
        cancel.set()
        diagnostics.mark("cancelRequested")
        if not finalizing:
            raise InterruptedError("playback cancelled")

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, interrupt)
    if args.loop:
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    else:
        signal.signal(signal.SIGHUP, interrupt)
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
    client = encoder = stderr_thread = None
    credentials = None
    open_rejected = False
    success = False
    last_publication = None

    def publish_diagnostics():
        nonlocal state, last_publication
        now = diagnostics.clock()
        # Fixed-size snapshots at most once per second, plus lifecycle publications.
        if last_publication is None or now - last_publication >= 1_000_000_000:
            state["diagnostics"] = diagnostics.snapshot()
            state = publish_video_state(state, state_path=HOST_STATE)
            last_publication = now

    try:
        state["diagnostics"] = diagnostics.snapshot()
        state = publish_video_state(state, claim=True, state_path=HOST_STATE)
        claimed = True
        source = args.input
        if source.startswith(("http://", "https://")):
            probe = run("yt-dlp", "--no-warnings", "-f", "bv*[height<=1080]+ba/b[height<=1080]", "-g", source, capture=True)
            urls = [line.strip() for line in probe.stdout.splitlines() if line.strip()]
            if not urls:
                raise RuntimeError("yt-dlp produced no stream URL")
            source = urls[0]
        fps = parse_fps(args.fps, source)
        crop = ("none" if args.crop == "auto" and args.input.startswith(("http://", "https://"))
                else detect_crop(source) if args.crop == "auto" else args.crop)
        filters = build_video_filters(args, fps, crop)
        command = ["ffmpeg", "-v", "error"]
        if args.loop:
            command += ["-stream_loop", "-1"]
        if args.start:
            command += ["-ss", str(args.start)]
        command += ["-i", source]
        if args.duration:
            command += ["-t", str(args.duration)]
        command += ["-vf", filters, "-q:v", str(args.quality),
                    "-pix_fmt", "yuvj420p", "-f", "image2pipe", "-"]
        ensure_runtime_modules()
        deadline = time.monotonic() + 10
        client = connect_bridge(BRIDGE_SOCKET, deadline, cancel)
        answer = json_exchange(client, dict(schemaVersion=1, op="videoOpen", session=session,
                               fpsNumerator=fps.numerator, fpsDenominator=fps.denominator), deadline, cancel)
        if not answer["accepted"]:
            open_rejected = True
            raise RuntimeError(f"video OPEN failed with code {answer['resultCode']}")
        credentials = dict(session=session, epoch=1, capability=answer["capability"])
        state["video"].update(epoch=1, capability=answer["capability"])
        state["diagnostics"] = diagnostics.snapshot()
        state = publish_video_state(state, state_path=HOST_STATE)
        stream = VideoStream(client, session, answer["capability"], fps, cancel,
                             diagnostics=diagnostics, on_progress=publish_diagnostics)
        stream.attach(answer["capability"], deadline)
        state["video"]["status"].update(state=wire.STATE_READY, rendererReady=True)
        state["diagnostics"] = diagnostics.snapshot()
        state = publish_video_state(state, state_path=HOST_STATE)
        check_cancel(cancel)
        encoder = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        stderr_tail = bytearray()
        stderr_thread = threading.Thread(target=drain_bounded, args=(encoder.stderr, stderr_tail), daemon=True)
        stderr_thread.start()
        total = stream.produce(encoder.stdout, encoder)
        check_cancel(cancel)
        answer = video_bridge_request(dict(schemaVersion=1, op="videoStatus", **credentials), 5,
                                      socket_path=BRIDGE_SOCKET)
        if not answer["accepted"] or answer["status"]["state"] != wire.STATE_DONE or answer["status"]["cleanup"] != "proven":
            raise RuntimeError("video completion cleanup is unproven")
        state["video"]["status"] = answer["status"]
        success = True
    finally:
        finalizing = True
        # Interrupt the data lane and begin encoder termination before control cancellation.
        if client is not None:
            client.close()
        cleanup_error = None
        encoder_errors = []

        def cleanup_encoder():
            try:
                stop_encoder(encoder)
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


if __name__ == "__main__":
    cli()

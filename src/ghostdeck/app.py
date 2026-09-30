"""Host remote for the D200. A native window that runs the CLI; not a player."""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import Request, urlopen

from ghostdeck import studio

_SHIM_UP = re.compile(r"(?:^|\s)shim=up(?:\s|$)")
# The fields `status` prints. Parsed by name so a new field cannot be mistaken for a value.
# `transport` is present only when the deck is attached but its adb transport cannot run a command.
_STATUS_FIELDS = ("usb", "transport", "shim", "copy", "playing")
_USB_TEXT = {
    "adb": "덱 ADB",
    "hid": "덱 HID",
    "none": "덱 없음",
    "unknown": "덱 알 수 없음",
}
_YT_HOSTS = {
    "youtu.be",
    "youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtube-nocookie.com",
}
_MEDIA_SUFFIXES = (".mp4", ".m4v", ".webm", ".mkv", ".mov", ".m3u8", ".mpd")
HOST_STATE = Path("/tmp/d200-color-host.json")
SEEK_PATH = Path("/tmp/d200-color-seek")
VOLUME_PATH = Path("/tmp/d200-color-volume")
OVERLAY_PATH = Path("/tmp/d200-color-overlay")
PLAYLIST_PATH = Path.home() / ".ghostdeck" / "playlist.json"
PLAYER_PATH = Path.home() / ".ghostdeck" / "player.json"
_REPEAT_MODES = ("off", "all", "one")
_FIT_MODES = ("auto", "pad", "cover")
_CROP_MODES = ("auto", "none")


@dataclass(frozen=True)
class CommandResult:
    argv: list[str]
    code: int
    stdout: str
    stderr: str

    @property
    def detail(self) -> str:
        return message_head(self.stderr or self.stdout) or f"exit {self.code}"


def run_cli(argv: list[str]) -> CommandResult:
    """Run `ghostdeck` as a child. HID enumerate in this process crashes the window.

    Measured: macOS 27 EXC_BREAKPOINT (`__CFCheckCFInfoPACSignature` /
    `IOHIDDeviceScheduleWithRunLoop`) when the 2s status poll called
    `hid.enumerate()` on a worker thread inside the WKWebView process
    (python3.13-2026-09-16-163849.ips). A child has its own runloop.
    """
    root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    previous = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(root / "src") if not previous else str(root / "src") + os.pathsep + previous
    timeout = {"studio": 180.0, "play": 30.0, "bridge": 90.0, "stop": 60.0, "reconnect": 180.0}.get(argv[0] if argv else "", 20.0)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "ghostdeck", *argv],
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return CommandResult(list(argv), 1, "", "timed out")
    except OSError as error:
        return CommandResult(list(argv), 1, "", f"{type(error).__name__}: {error}")
    return CommandResult(list(argv), int(proc.returncode), proc.stdout or "", proc.stderr or "")


def message_head(text: str) -> str:
    """The headline of the last message a CLI child printed, or empty.

    A multi-line error indents its continuation lines (`d200-color-agent is not built.` is followed
    by indented download/install steps), so the last line alone was an indented URL with no reason
    attached. Warnings printed earlier sit on their own unindented lines, so the last unindented
    line is the start of the final message either way.
    """
    lines = [line for line in (text or "").splitlines() if line.strip()]
    for line in reversed(lines):
        if not line[:1].isspace():
            return line.strip()
    return lines[-1].strip() if lines else ""


def studio_installed() -> bool:
    """Whether `studio` can work on this host at all. A directory check: no HID, no subprocess."""
    return studio.ORIGINAL.is_dir()


def shim_is_up(status_stdout: str) -> bool:
    return bool(_SHIM_UP.search(status_stdout))


def parse_status_fields(status_stdout: str) -> dict[str, str]:
    """`usb=adb shim=up copy=yes playing=no` as a dict, ignoring anything unrecognised.

    The CLI prints one `key=value` line and owns that format; the window only reads it. Only the last
    line is considered, because that is where `print` puts the summary if anything else has written to
    stdout, and unknown keys are dropped rather than guessed at, so a future field cannot be rendered
    as one of these.
    """
    lines = (status_stdout or "").strip().splitlines()
    if not lines:
        return {}
    fields = {}
    for token in lines[-1].split():
        key, separator, value = token.partition("=")
        if separator and key in _STATUS_FIELDS:
            fields[key] = value
    return fields


def status_text(status_stdout: str, *, has_picture=None, has_studio: bool = True) -> str:
    """A one-line summary for the window, in Korean.

    The CLI's raw line is `usb=adb shim=up copy=yes playing=no`. USB stays, then the hidshim
    key surface, then play. Official Studio is named only when it is missing, because that is why
    there are no keys. `has_picture=False` is opening: pid live is not 재생 중.
    """
    fields = parse_status_fields(status_stdout)
    if not fields:
        return "상태를 읽지 못했습니다"
    if fields.get("transport"):
        # Attached but adb cannot run a command on it. Keys and play state are moot, and nothing on
        # the host recovers it, so the physical remedy is the whole message.
        return f"덱 응답 없음 ({fields['transport']}) · 덱을 뽑았다 다시 꽂으십시오"
    piece = [_USB_TEXT.get(fields.get("usb", ""), f"덱 {fields.get('usb')}")]
    if fields.get("shim") == "up":
        piece.append("키 연결됨")
    else:
        piece.append("키 없음" if has_studio else "Studio 없음")
    if fields.get("playing") == "yes":
        piece.append("재생 중" if has_picture is not False else "여는 중")
    else:
        piece.append("멈춤")
    if fields.get("usb", "").startswith("none"):
        piece.append("연결을 누르십시오")
    return " · ".join(piece)


def status_dot_color(status_stdout: str, *, has_picture=None, has_studio: bool = True):
    """An `NSColor` for the status dot, or None when AppKit is not loadable.

    Colour is the part that is read without looking: green only while the deck is actually playing,
    red when there is no usable deck, amber when the deck is there but the keys are down and
    `studio` can bring them up, and grey when it is simply idle and ready. A host without the
    official app has no keys to bring up, so `shim=down` is its normal state, not amber.
    """
    fields = parse_status_fields(status_stdout)
    if not fields:
        return None
    try:
        from AppKit import NSColor
    except ImportError:
        return None
    if (
        fields.get("usb", "").startswith("none")
        or fields.get("usb") == "unknown"
        or fields.get("transport")
    ):
        return NSColor.systemRedColor()
    if fields.get("playing") == "yes" and has_picture is not False:
        return NSColor.systemGreenColor()
    if fields.get("shim") == "down" and has_studio:
        return NSColor.systemOrangeColor()
    return NSColor.secondaryLabelColor()


def recovery_action(status_stdout: str, *, has_studio: bool = True) -> str:
    """The one recovery button this state calls for: "reconnect", "studio", or "" for none.

    `연결` finds a deck that is not on USB. `스튜디오` brings the keys up on a deck that is there.
    A wedged transport and a missing Python backend have no button that fixes them, so nothing is
    offered rather than a button that cannot help.
    """
    fields = parse_status_fields(status_stdout)
    if not fields or fields.get("transport"):
        return ""
    usb = fields.get("usb", "")
    if usb == "none":
        return "reconnect"
    if usb in ("adb", "hid") and fields.get("shim") == "down" and has_studio:
        return "studio"
    return ""

def bridge_down(detail: str) -> bool:
    """True when `play` refused because the bridge is not up, so `studio` is the remedy.

    `shim=up` only reports the copy process, and the copy outlives its bridge: `studio` starts both,
    but a bridge that dies (or a foreign listener on the endpoint) leaves the copy running. The
    window gated its one recovery step on `shim`, so in that state it ran `play` directly, `play`
    refused, and every later press failed the same way until a human ran `ghostdeck studio` by hand.
    `status` cannot answer this instead: it must stay usable while the bridge is down (T19), so it
    never consults the bridge. The refusal text is therefore the signal, matched on the phrase
    `studio` exports so a reword cannot silently break recovery.
    """
    return studio.BRIDGE_DOWN in (detail or "")


def read_pasteboard(run=subprocess.run) -> str:
    """Safari/Chrome copy lives on the macOS pasteboard."""
    try:
        result = run(["/usr/bin/pbpaste"], capture_output=True, text=True, check=False)
    except OSError:
        return ""
    return (result.stdout or "").strip()


def looks_like_source(text: str) -> bool:
    text = text.strip()
    if not text:
        return False
    return "://" in text or text.startswith("/")


def media_path_candidate(text: str) -> str:
    """A local file path that play can take, or empty. Existence is the CLI's job."""
    text = (text or "").strip().strip('"')
    if not text:
        return ""
    if text.startswith("file:"):
        text = unquote(urlparse(text).path)
    if not text.startswith("/"):
        return ""
    path = text.split("?")[0]
    if Path(path).suffix.lower() in _MEDIA_SUFFIXES:
        return path
    return ""


def play_should_loop(source: str) -> bool:
    """A file on disk loops (the CLI default). A URL is one watch."""
    return bool(media_path_candidate(source))


def dropped_play_source(filenames) -> str:
    """The first dropped path that looks like media."""
    for name in filenames or []:
        candidate = media_path_candidate(str(name))
        if candidate:
            return candidate
    return ""


def playlist_entry(
    source: str, title: str = "", channel: str = "", duration: float = 0.0,
) -> dict:
    entry = {
        "source": (source or "").strip(),
        "title": (title or "").strip(),
        "channel": (channel or "").strip(),
    }
    length = parse_duration(duration)
    if length > 0:
        entry["duration"] = round(length, 3)
    return entry


def playlist_source(item) -> str:
    if isinstance(item, dict):
        return str(item.get("source") or "").strip()
    return str(item or "").strip()


def playlist_identity(source: str) -> str:
    """Canonical playable identity. Watch URLs collapse to watch?v=."""
    source = (source or "").strip()
    return youtube_watch_url(source) or source



def playlist_normalize(items) -> list[dict]:
    out = []
    seen: dict[str, int] = {}
    for item in items or []:
        if isinstance(item, dict):
            source = str(item.get("source") or "").strip()
            title = str(item.get("title") or "").strip()
            channel = str(item.get("channel") or "").strip()
            duration = parse_duration(item.get("duration"))
        else:
            source = str(item).strip()
            title = ""
            channel = ""
            duration = 0.0
        if not source:
            continue
        key = playlist_identity(source)
        if key in seen:
            existing = out[seen[key]]
            if title and not existing.get("title"):
                existing["title"] = title
            if channel and not existing.get("channel"):
                existing["channel"] = channel
            if duration > 0 and not existing.get("duration"):
                existing["duration"] = round(duration, 3)
            continue
        seen[key] = len(out)
        out.append(playlist_entry(key, title, channel, duration))
    return out


def playlist_label(item) -> str:
    """Channel · title when known. Never a raw watch URL."""
    if not isinstance(item, dict):
        item = playlist_entry(str(item or ""))
    title = str(item.get("title") or "").strip()
    channel = str(item.get("channel") or "").strip()
    if title and channel:
        return f"{channel} · {title}"
    if title:
        return title
    source = playlist_source(item)
    if not source:
        return ""
    local = media_path_candidate(source)
    if local:
        return Path(local).stem
    watch = youtube_watch_url(source)
    if watch:
        vid = parse_qs(urlparse(watch).query).get("v", [""])[0]
        return f"YouTube · {vid}" if vid else watch
    return source if len(source) <= 48 else source[:45] + "..."


def playlist_add(items, source: str, title: str = "", channel: str = "", duration: float = 0.0) -> list[dict]:
    """Append a playable source. A source already in the queue is not added again."""
    source = playlist_identity((source or "").strip())
    out = playlist_normalize(items)
    if not source:
        return out
    title = (title or "").strip()
    channel = (channel or "").strip()
    length = parse_duration(duration)
    for i, item in enumerate(out):
        if playlist_identity(playlist_source(item)) == source:
            merged = dict(item)
            if title and not merged.get("title"):
                merged["title"] = title
            if channel and not merged.get("channel"):
                merged["channel"] = channel
            if length > 0 and not merged.get("duration"):
                merged["duration"] = round(length, 3)
            out[i] = merged
            return out
    return out + [playlist_entry(source, title, channel, length)]


def playlist_extend(items, entries) -> list[dict[str, str]]:
    """Append many sources, skipping anything already queued."""
    out = playlist_normalize(items)
    seen = {playlist_identity(playlist_source(item)) for item in out}
    for entry in playlist_normalize(entries):
        src = playlist_identity(playlist_source(entry))
        if not src or src in seen:
            continue
        out.append(entry)
        seen.add(src)
    return out



def playlist_title(item) -> str:
    """The primary line: the video title, never a URL."""
    if not isinstance(item, dict):
        item = playlist_entry(str(item or ""))
    title = str(item.get("title") or "").strip()
    if title:
        return title
    source = playlist_source(item)
    local = media_path_candidate(source)
    if local:
        return Path(local).stem
    watch = youtube_watch_url(source)
    if watch:
        vid = parse_qs(urlparse(watch).query).get("v", [""])[0]
        return vid or "YouTube"
    return playlist_label(item)


def playlist_subtitle(item) -> str:
    """The secondary line: channel, or empty."""
    if not isinstance(item, dict):
        item = playlist_entry(str(item or ""))
    return str(item.get("channel") or "").strip()


def playlist_meta(item) -> str:
    """The row's second line: channel and length, whichever are known."""
    if not isinstance(item, dict):
        item = playlist_entry(str(item or ""))
    parts = []
    channel = playlist_subtitle(item)
    if channel:
        parts.append(channel)
    length = parse_duration(item.get("duration"))
    if length > 0:
        parts.append(format_clock(length))
    if not parts and media_path_candidate(playlist_source(item)):
        parts.append("로컬 파일")
    return " · ".join(parts)


def youtube_video_id(source: str) -> str:
    """The 11-character video id of a YouTube watch source, or empty."""
    watch = youtube_watch_url((source or "").strip())
    if not watch:
        return ""
    vid = parse_qs(urlparse(watch).query).get("v", [""])[0]
    return vid if re.fullmatch(r"[A-Za-z0-9_-]{11}", vid or "") else ""


def thumbnail_url(source: str) -> str:
    """A small still for a YouTube source (mqdefault: 320x180, always present), or empty."""
    vid = youtube_video_id(source)
    return f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg" if vid else ""


def thumbnail_cache_path(source: str, root: Path | None = None) -> Path:
    """Where a source's still lives on disk. One file per source identity, so no duplicates."""
    base = (root if root is not None else Path.home() / ".ghostdeck" / "thumbs")
    key = hashlib.sha1(playlist_identity(source).encode("utf-8")).hexdigest()[:20]
    return base / f"{key}.jpg"


def fetch_thumbnail(source: str, root: Path | None = None, fetch=None, probe=None) -> Path | None:
    """The cached still for `source`, fetching it once. None when there is no still to show.

    YouTube: the i.ytimg.com still. A local file: one ffmpeg frame 10% in. Never HID, never the deck.
    """
    path = thumbnail_cache_path(source, root)
    if path.is_file() and path.stat().st_size > 0:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    url = thumbnail_url(source)
    try:
        if url:
            opener = urlopen if fetch is None else fetch
            with opener(Request(url, headers={"User-Agent": "ghostdeck/0.1"}), timeout=5) as resp:
                data = resp.read(512 * 1024)
            if not data:
                return None
            tmp.write_bytes(data)
        else:
            local = media_path_candidate(source)
            if not local or not Path(local).is_file():
                return None
            length = source_duration(local, probe=probe)
            runner = subprocess.run if probe is None else probe
            runner(
                ["ffmpeg", "-v", "error", "-y", "-ss", f"{max(0.0, length * 0.1):.2f}", "-i", local,
                 "-frames:v", "1", "-vf", "scale=320:-2", "-f", "mjpeg", str(tmp)],
                capture_output=True, timeout=15, check=False,
            )
            if not tmp.is_file() or tmp.stat().st_size == 0:
                return None
        os.replace(tmp, path)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    finally:
        tmp.unlink(missing_ok=True)
    return path


def playlist_move(items, src: int, dst: int) -> list[dict[str, str]]:
    """Move the row at `src` so it lands at `dst`. Out of range is a no-op."""
    out = playlist_normalize(items)
    if not (0 <= src < len(out)):
        return out
    dst = max(0, min(len(out) - 1, dst))
    if src == dst:
        return out
    item = out.pop(src)
    out.insert(dst, item)
    return out



def playlist_remove(items, index: int) -> list[dict[str, str]]:
    out = playlist_normalize(items)
    if index < 0 or index >= len(out):
        return out
    del out[index]
    return out


def playlist_next(items, current: str, *, repeat: str = "off", shuffle: bool = False, rng=None) -> str:
    """The next source to play. Empty means stop."""
    sources = [playlist_identity(playlist_source(item)) for item in playlist_normalize(items)]
    current = playlist_identity((current or "").strip())
    if not sources:
        return ""
    if repeat == "one" and current in sources:
        return current
    if shuffle:
        pick = rng.choice if rng is not None else random.choice
        pool = [src for src in sources if src != current]
        if pool:
            return pick(pool)
        return sources[0] if repeat == "all" else ""
    if current not in sources:
        return sources[0]
    nxt = sources.index(current) + 1
    if nxt < len(sources):
        return sources[nxt]
    if repeat == "all":
        return sources[0]
    return ""


def playlist_prev(items, current: str, *, repeat: str = "off", shuffle: bool = False, rng=None) -> str:
    """The previous source. Shuffle picks another track."""
    sources = [playlist_identity(playlist_source(item)) for item in playlist_normalize(items)]
    current = playlist_identity((current or "").strip())
    if not sources:
        return ""
    if shuffle:
        return playlist_next(
            items, current, repeat="all" if repeat != "off" else "off", shuffle=True, rng=rng
        )
    if current not in sources:
        return sources[-1]
    idx = sources.index(current)
    if idx > 0:
        return sources[idx - 1]
    if repeat == "all":
        return sources[-1]
    return sources[0]


def playlist_advance(items, current: str) -> str:
    """The next queued source after `current`, or empty at the end."""
    return playlist_next(items, current, repeat="off", shuffle=False)


def queue_play_source(items, selected, now: str = "", seen: str = "") -> str:
    """What ▶ plays: the selected row, else the live/last source, else the queue head."""
    items = playlist_normalize(items)
    try:
        row = int(selected)
    except (TypeError, ValueError):
        row = -1
    if 0 <= row < len(items):
        return playlist_source(items[row])
    now = playlist_identity(now)
    if now:
        return now
    seen = playlist_identity(seen)
    if seen:
        return seen
    if items:
        return playlist_source(items[0])
    return ""




def playlist_should_loop(source: str, items, repeat: str = "off") -> bool:
    """Whether the player process itself loops. Queue wrap is `playlist_next`."""
    if repeat == "one":
        return True
    n = len(playlist_normalize(items))
    if repeat == "all" and n <= 1:
        return True
    return False


def repeat_label(repeat: str) -> str:
    return {"all": "전체", "one": "한곡"}.get(repeat, "반복")


def play_fit(fit: str) -> str:
    fit = (fit or "auto").strip()
    return fit if fit in _FIT_MODES else "auto"


def play_crop_choice(crop: str) -> str:
    crop = (crop or "auto").strip()
    return crop if crop in _CROP_MODES else "auto"


def play_crop_argv(source: str, crop: str) -> str:
    """auto letterbox detect is a local-file job. HTTP waits on ffmpeg cropdetect."""
    chosen = play_crop_choice(crop)
    if chosen != "auto":
        return chosen
    src = source or ""
    if youtube_watch_url(src) or src.startswith(("http://", "https://")):
        return "none"
    return "auto"


def playlist_click_row(clicked, selected, count) -> int:
    """Double-click uses clickedRow. Empty-selection tables deselect on the second click."""
    try:
        n = int(count)
    except (TypeError, ValueError):
        n = 0
    for raw in (clicked, selected):
        try:
            row = int(raw)
        except (TypeError, ValueError):
            continue
        if 0 <= row < n:
            return row
    return -1

def playlist_playing(item, now: str, seen: str = "") -> bool:
    ident = playlist_identity(playlist_source(item) if isinstance(item, dict) else str(item or ""))
    if not ident:
        return False
    live = playlist_identity(now) or playlist_identity(seen)
    return ident == live


def fit_label(fit: str) -> str:
    """What the frame does on the deck. It applies from the next play; the tooltip says so."""
    return {"pad": "화면: 맞춤", "cover": "화면: 채움"}.get(play_fit(fit), "화면: 자동")


def crop_label(crop: str) -> str:
    return "여백: 유지" if play_crop_choice(crop) == "none" else "여백: 자동"

QUEUE_ROW_H = 52


@dataclass(frozen=True)
class Layout:
    """Where each region sits in a content view of `width` x `height` (AppKit: y grows up).

    Top: the toolbar. Under it, the Now Playing card spans the full width: that is the thing being
    controlled, so it leads. Below the card, the browser takes every spare pixel of width and the
    queue keeps a fixed column, so resizing makes the page bigger instead of stretching buttons.
    """

    width: float
    height: float
    pad: float = 16
    gap: float = 12
    toolbar_h: float = 36
    card_h: float = 156
    footer_h: float = 30
    title_h: float = 28
    side_w: float = 360

    @property
    def toolbar_y(self) -> float:
        return self.height - self.title_h - self.toolbar_h

    @property
    def card(self) -> tuple[float, float, float, float]:
        y = self.toolbar_y - self.gap - self.card_h
        return (self.pad, y, self.width - self.pad * 2, self.card_h)

    @property
    def body_y(self) -> float:
        return self.footer_h

    @property
    def body_h(self) -> float:
        return max(0.0, self.card[1] - self.gap - self.body_y)

    @property
    def queue(self) -> tuple[float, float, float, float]:
        x = self.width - self.pad - self.side_w
        return (x, self.body_y, self.side_w, self.body_h)

    @property
    def web(self) -> tuple[float, float, float, float]:
        w = max(0.0, self.queue[0] - self.gap - self.pad)
        return (self.pad, self.body_y, w, self.body_h)


# The narrowest the window may get. Two constraints, the wider wins: browser (mobile page needs
# ~320pt) + queue column, and the card's own row (thumbnail + transport + mute/volume/settings =
# 756pt). Below this the controls would overlap, so the window refuses to shrink further.
LAYOUT_MIN = (760, 640)
LAYOUT_DEFAULT = (1000, 860)


def now_state_label(has_picture: bool, has_source: bool, busy: bool) -> str:
    """The small caps line above the title: what the deck is doing right now."""
    if has_picture:
        return "● 덱에서 재생 중"
    if has_source and busy:
        return "여는 중…"
    if has_source:
        return "멈춤"
    return "대기 중"


def seek_step(key: str, shift: bool = False) -> float:
    """Seconds a key moves the deck's playhead. 0 for keys that do not seek."""
    step = {"left": -5.0, "right": 5.0, "j": -10.0, "l": 10.0}.get(key, 0.0)
    return step * (6 if shift and key in ("left", "right") else 1)


def clamp_seek(position: float, delta: float, duration: float) -> float:
    """The new playhead after a relative seek, kept inside [0, duration - 1]."""
    target = max(0.0, float(position) + float(delta))
    if duration > 0:
        target = min(target, max(0.0, float(duration) - 1.0))
    return target


def volume_step(volume: float, key: str) -> float:
    """Up/down arrows change the deck's gain by 10%."""
    delta = {"up": 0.1, "down": -0.1}.get(key, 0.0)
    return clamp_volume(round(float(volume) + delta, 2))

# CLI refusal fragment (lower-cased) -> the Korean line the window shows. First match wins, so the
# more specific phrase comes first. Each line names the next thing to do, not the internal cause.
_FAILURE_NOTES = (
    ("open failed", "덱이 아직 이전 영상을 안 놓았습니다."),
    ("busy with another video", "덱이 아직 이전 영상을 안 놓았습니다."),
    ("has not proven", "덱이 아직 이전 영상을 안 놓았습니다."),
    ("before it started", "재생을 시작하지 못했습니다."),
    ("nothing is playing", "재생을 시작하지 못했습니다."),
    ("transport is", "덱이 응답하지 않습니다. 덱을 뽑았다 다시 꽂으십시오."),
    ("no d200 on usb", "덱이 USB에 없습니다. 케이블을 꽂고 연결을 누르십시오."),
    ("not enumerating through adb", "덱이 ADB로 전환되지 않았습니다. 연결을 누르십시오."),
    ("not in adb after switch", "덱이 ADB로 전환되지 않았습니다. 연결을 누르십시오."),
    ("did not enumerate through adb", "덱이 ADB로 전환되지 않았습니다. 연결을 누르십시오."),
    ("no adb device reachable", "덱이 보이지 않습니다. 케이블을 꽂고 연결을 누르십시오."),
    (studio.STUDIO_MISSING.lower(), "Ulanzi Studio가 없습니다. 브리지를 누르면 키 없이 재생됩니다."),
    ("back in hid mode", "브리지가 덱을 놓쳤습니다. 스튜디오를 누르면 새로 띄웁니다."),
    (studio.BRIDGE_DOWN.lower(), "브리지가 꺼져 있습니다. 스튜디오를 누르십시오."),
    (f"no live {studio.BRIDGE.name} of ours", "다른 프로세스가 브리지 소켓을 잡고 있습니다."),
    ("bridge socket did not come up", "브리지를 띄우지 못했습니다. 연결을 누른 다음 다시 시도하십시오."),
    ("did not stay running", "Studio 복사본이 바로 꺼졌습니다. 스튜디오를 다시 누르십시오."),
    ("d200-color-agent is not built", "덱 에이전트가 없습니다. README의 디바이스 에이전트 설치를 따르십시오."),
    ("ffmpeg not on path", "ffmpeg가 없습니다. brew install ffmpeg 후 다시 시도하십시오."),
    ("ffprobe not on path", "ffprobe가 없습니다. brew install ffmpeg 후 다시 시도하십시오."),
    ("yt-dlp not on path", "yt-dlp가 없습니다. brew install yt-dlp 후 다시 시도하십시오."),
    ("is not installed (pip install", "파이썬 패키지가 없습니다. pip install -e \".[device]\" 후 다시 여십시오."),
    ("source is not a file", "재생할 파일을 찾지 못했습니다."),
    ("did not release within", "덱이 영상을 놓지 않았습니다. 정지를 한 번 더 누르십시오."),
    ("timed out", "응답이 너무 늦습니다. 연결을 누른 다음 다시 시도하십시오."),
)


def failure_note(detail: str, *, fallback: str = "실패했습니다.") -> str:
    """Korean note for a failed CLI step. Known refusals are mapped; anything else passes through.

    An unknown message is shown as-is (without a `RuntimeError:` prefix) rather than replaced with a
    generic line: a reason the table does not know is still more useful than no reason.
    """
    text = (detail or "").strip()
    if text.lower().startswith("runtimeerror:"):
        text = text.split(":", 1)[-1].strip()
    lower = text.lower()
    for needle, note in _FAILURE_NOTES:
        if needle in lower:
            return note
    return text or fallback


def play_failure_note(detail: str) -> str:
    """Korean note for a failed play."""
    return failure_note(detail, fallback="재생을 시작하지 못했습니다.")


def play_success_note(has_picture: bool) -> str:
    if has_picture:
        return "덱에서 재생 중입니다. 창이 멈추거나 끊겨도 덱은 계속 재생됩니다."
    return "첫 프레임을 기다리는 중입니다."


# `d200_video_stream` states: DONE is 6, reached only when the deck consumed every frame up to EOS.
_VIDEO_STATE_DONE = 6


def deck_finished(path: Path = HOST_STATE) -> str:
    """The source whose session ended because the video ran out, or empty.

    Only a natural end counts: the player published `phase=terminal` with the deck's own DONE status
    and proven cleanup. A player that was killed (a switch started from anywhere, a stop) or that
    failed leaves a different status, so its disappearance is not "the track ended".
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return ""
    if not isinstance(data, dict) or str(data.get("phase") or "") != "terminal":
        return ""
    video = data.get("video") if isinstance(data.get("video"), dict) else {}
    status = video.get("status") if isinstance(video.get("status"), dict) else {}
    if status.get("state") != _VIDEO_STATE_DONE or status.get("cleanup") != "proven":
        return ""
    return str(data.get("source") or "").strip()


def should_auto_next(
    *,
    playing: bool,
    finished: bool,
    was_playing: bool,
    saw_picture: bool,
    user_stopped: bool,
    busy: bool,
) -> bool:
    """Advance only after the track we watched play reached its own end.

    `finished` is `deck_finished()` naming that track. Without it, "no player and no session" also
    matched the gap inside a switch, so the window started the next track over the one just chosen
    and the new player was killed (measured: 2 of 12 external switches while the window was open).
    """
    if user_stopped or busy or playing or not finished:
        return False
    return was_playing and saw_picture


def opening_session(*, playing: bool, has_picture: bool, session_active: bool = False) -> bool:
    return (playing or session_active) and not has_picture


def seek_note(seconds, live: bool) -> str:
    clock = format_clock(play_offset(seconds))
    return f"{clock}부터 다시 재생합니다."


def clamp_volume(value) -> float:
    """Host speaker gain in [0, 1]. Junk becomes 1."""
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


def audio_gain(volume, muted: bool) -> float:
    """Linear gain sent to the player. Mute is 0 without losing the slider."""
    return 0.0 if muted else clamp_volume(volume)


def player_prefs_load(path: Path = PLAYER_PATH) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    repeat = str(data.get("repeat") or "off")
    if repeat not in _REPEAT_MODES:
        repeat = "off"
    return {
        "repeat": repeat,
        "shuffle": bool(data.get("shuffle")),
        "volume": clamp_volume(data["volume"]) if "volume" in data else 1.0,
        "muted": bool(data.get("muted")),
        "overlay": clamp_volume(data["overlay"]) if "overlay" in data else 1.0,
        "fit": play_fit(str(data.get("fit") or "auto")),
        "crop": play_crop_choice(str(data.get("crop") or "auto")),
    }


def player_prefs_save(path: Path, prefs) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    repeat = str((prefs or {}).get("repeat") or "off")
    if repeat not in _REPEAT_MODES:
        repeat = "off"
    payload = {
        "repeat": repeat,
        "shuffle": bool((prefs or {}).get("shuffle")),
        "volume": clamp_volume((prefs or {}).get("volume", 1.0)),
        "muted": bool((prefs or {}).get("muted")),
        "overlay": clamp_volume((prefs or {}).get("overlay", 1.0)),
        "fit": play_fit(str((prefs or {}).get("fit") or "auto")),
        "crop": play_crop_choice(str((prefs or {}).get("crop") or "auto")),
    }
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def playlist_index(items, source: str) -> int:
    """The queue row holding `source`, or -1."""
    source = playlist_identity((source or "").strip())
    if not source:
        return -1
    for index, item in enumerate(playlist_normalize(items)):
        if playlist_identity(playlist_source(item)) == source:
            return index
    return -1


def playlist_find(items, source: str) -> dict[str, str]:
    source = playlist_identity((source or "").strip())
    for item in playlist_normalize(items):
        if playlist_identity(playlist_source(item)) == source:
            return item
    return playlist_entry(source)


def playlist_load(path: Path) -> list[dict[str, str]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return []
    if not isinstance(raw, list):
        return []
    return playlist_normalize(raw)


def playlist_save(path: Path, items) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(playlist_normalize(items), indent=2, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8")


def source_identity(source: str, fetch=None, probe=None) -> tuple[str, str]:
    """(title, channel) for a source. Never HID. Empty on failure."""
    source = (source or "").strip()
    watch = youtube_watch_url(source)
    if watch:
        return _youtube_oembed(watch, fetch=fetch)
    local = media_path_candidate(source)
    if local:
        return _file_identity(local, probe=probe)
    return "", ""


def _youtube_oembed(watch: str, fetch=None) -> tuple[str, str]:
    opener = urlopen if fetch is None else fetch
    url = "https://www.youtube.com/oembed?format=json&url=" + quote(watch, safe="")
    try:
        req = Request(url, headers={"User-Agent": "ghostdeck/0.1"})
        with opener(req, timeout=5) as resp:
            raw = resp.read()
        data = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
    except (OSError, json.JSONDecodeError, UnicodeError, ValueError, TypeError):
        return "", ""
    if not isinstance(data, dict):
        return "", ""
    return str(data.get("title") or "").strip(), str(data.get("author_name") or "").strip()


def _file_identity(path: str, probe=None) -> tuple[str, str]:
    runner = subprocess.run if probe is None else probe
    try:
        result = runner(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format_tags=title,artist,album_artist",
                "-of", "json", path,
            ],
            capture_output=True, text=True, timeout=5, check=False,
        )
        tags = ((json.loads(result.stdout or "{}").get("format") or {}).get("tags") or {})
        title = str(tags.get("title") or "").strip()
        channel = str(tags.get("artist") or tags.get("album_artist") or "").strip()
        if title:
            return title, channel
    except (OSError, json.JSONDecodeError, ValueError, TypeError, AttributeError):
        pass
    return Path(path).stem, ""


def deck_now_playing(path: Path = HOST_STATE) -> str:
    """The source of an active session, or empty. No HID, no subprocess."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    if str(data.get("phase") or "") != "active":
        return ""
    src = data.get("source")
    return str(src).strip() if src else ""


def deck_session_active(path: Path = HOST_STATE) -> bool:
    """True when the player last published an active session. No HID."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return False
    return isinstance(data, dict) and str(data.get("phase") or "") == "active"


def deck_has_picture(path: Path = HOST_STATE) -> bool:
    """True only after the player has sent a frame. Claimed-but-opening is not playing."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return False
    if not isinstance(data, dict) or str(data.get("phase") or "") != "active":
        return False
    try:
        wall = float(data.get("playheadAt") or 0.0)
    except (TypeError, ValueError):
        wall = 0.0
    if wall <= 0 or (time.time() - wall) > 2.5:
        return False
    diag = data.get("diagnostics") if isinstance(data.get("diagnostics"), dict) else {}
    try:
        if int(diag.get("framesSent") or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    milestones = diag.get("milestones")
    if isinstance(milestones, dict):
        rec = milestones.get("firstConsumedReceipt")
        if isinstance(rec, dict) and rec.get("monotonicNs") is not None:
            return True
    return False


def format_clock(seconds: float) -> str:
    """m:ss or h:mm:ss. Junk is 0:00."""
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        value = 0.0
    if not math.isfinite(value) or value < 0:
        value = 0.0
    total = int(value)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def wrap_playhead(pos: float, duration: float, looping: bool) -> float:
    """Keep the bar on the timeline. Looping wraps; otherwise it sticks at the end."""
    if not math.isfinite(pos) or pos < 0:
        pos = 0.0
    if duration <= 0:
        return pos
    if looping:
        return pos % duration
    return duration if pos > duration else pos

def deck_playhead(path: Path = HOST_STATE) -> tuple[str, float, bool]:
    """(source, seconds, active). Seconds is --start plus time since the first consumed frame."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return "", 0.0, False
    if not isinstance(data, dict):
        return "", 0.0, False
    source = str(data.get("source") or "").strip()
    start = play_offset(data.get("start"))
    try:
        rate = float(data.get("playbackRate") or 1.0)
    except (TypeError, ValueError):
        rate = 1.0
    if not math.isfinite(rate) or rate <= 0:
        rate = 1.0
    active = str(data.get("phase") or "") == "active"
    try:
        published_at = float(data.get("playheadAt") or 0.0)
    except (TypeError, ValueError):
        published_at = 0.0
    if active and published_at > 0 and (time.time() - published_at) > 2.5:
        active = False
    diag = data.get("diagnostics") if isinstance(data.get("diagnostics"), dict) else {}
    started = diag.get("startedMonotonicNs")
    elapsed = diag.get("hostElapsedNs")
    first = None
    milestones = diag.get("milestones")
    if isinstance(milestones, dict):
        rec = milestones.get("firstConsumedReceipt")
        if isinstance(rec, dict):
            first = rec.get("monotonicNs")
    pos = start
    try:
        if (
            isinstance(started, (int, float))
            and isinstance(elapsed, (int, float))
            and isinstance(first, (int, float))
        ):
            published = float(started) + float(elapsed)
            if published >= float(first):
                pos = start + (published - float(first)) / 1e9 * rate
    except (TypeError, ValueError):
        pos = start
    if active and first is not None:
        try:
            wall = float(data.get("playheadAt") or 0.0)
        except (TypeError, ValueError):
            wall = 0.0
        if wall > 0:
            pos += max(0.0, time.time() - wall) * rate
    pos = wrap_playhead(pos, parse_duration(data.get("duration")), bool(data.get("loop")))
    return source, pos, active


def parse_duration(raw) -> float:
    """A media length in seconds. Junk is 0."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value) or value <= 0:
        return 0.0
    return value


def source_duration(source: str, probe=None) -> float:
    """Length of the playable source. Never HID. Never the page's video tag."""
    source = (source or "").strip()
    if not source:
        return 0.0
    runner = subprocess.run if probe is None else probe
    watch = youtube_watch_url(source)
    local = media_path_candidate(source)
    try:
        if watch:
            result = runner(
                ["yt-dlp", "--no-warnings", "--skip-download", "-O", "%(duration)s", watch],
                capture_output=True, text=True, timeout=20, check=False,
            )
            raw = (result.stdout or "").strip()
        elif local:
            result = runner(
                [
                    "ffprobe", "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "csv=p=0", local,
                ],
                capture_output=True, text=True, timeout=8, check=False,
            )
            raw = (result.stdout or "").strip()
        else:
            return 0.0
    except (OSError, subprocess.TimeoutExpired):
        return 0.0
    return parse_duration(raw)


def deck_crop(path: Path = HOST_STATE) -> str:
    """Detected letterbox crop from the player receipt. Empty if unknown."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    crop = str(data.get("crop") or "").strip()
    parts = crop.split(":")
    if len(parts) != 4:
        return ""
    try:
        width, height, _x, _y = (int(part) for part in parts)
    except ValueError:
        return ""
    return crop if width > 0 and height > 0 else ""


def request_live_seek(seconds, path: Path = SEEK_PATH) -> bool:
    """Ask the running player to jump. True if the request was written."""
    try:
        path.write_text(f"{play_offset(seconds):.3f}\n", encoding="utf-8")
    except OSError:
        return False
    return True


def request_live_volume(gain, path: Path = VOLUME_PATH) -> bool:
    """Ask the running player to set host gain. True if the request was written."""
    try:
        path.write_text(f"{clamp_volume(gain):.4f}\n", encoding="utf-8")
    except OSError:
        return False
    return True


def _push_studio_alpha(gain: float) -> None:
    n = int(round(clamp_volume(gain) * 255))
    try:
        from ghostdeck import adb
        binary = adb.adb_bin()
    except (FileNotFoundError, OSError):
        return
    try:
        subprocess.run(
            [binary, "shell", f"echo {n} >/tmp/d200-studio-alpha"],
            timeout=5,
            check=False,
            capture_output=True,
        )
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
        return


def request_live_overlay(gain, path: Path = OVERLAY_PATH) -> bool:
    """Ask the running player, and the deck preload, to set Studio button opacity."""
    value = clamp_volume(gain)
    try:
        path.write_text(f"{value:.4f}\n", encoding="utf-8")
    except OSError:
        return False
    threading.Thread(target=_push_studio_alpha, args=(value,), daemon=True).start()
    return True


def deck_duration(path: Path = HOST_STATE) -> float:
    """Duration the player published for the current source. 0 if unknown."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return 0.0
    if not isinstance(data, dict):
        return 0.0
    return parse_duration(data.get("duration"))


def should_retry_pending(pending: str, pending_start: float, seen: str, played_start: float) -> bool:
    """True when a queued play is a different source or a real seek on the same one."""
    if not pending:
        return False
    if pending != seen:
        return True
    return abs(play_offset(pending_start) - play_offset(played_start)) > 0.2






def play_request(
    field: str,
    page_href: str,
    page_media: str = "",
    pasteboard: str = "",
    start: float = 0.0,
) -> tuple[str, float]:
    """What 재생 sends.

    The URL field is not navigation-only: a local file typed or opened there wins, otherwise
    the page's video, otherwise a copied path/URL. YouTube's homepage is not a video.
    """
    field = (field or "").strip()
    local = media_path_candidate(field)
    if local:
        return local, 0.0
    page = youtube_watch_url(page_href) or playable_source(page_href, page_media)
    if page:
        return page, play_offset(start)
    local = media_path_candidate(pasteboard)
    if local:
        return local, 0.0
    watch = youtube_watch_url(field) or playable_source(field)
    if watch:
        return watch, 0.0
    watch = youtube_watch_url(pasteboard) or playable_source(pasteboard)
    if watch:
        return watch, 0.0
    return "", 0.0



def resolve_source(field: str, pasteboard: str = "") -> str:
    """The field wins. An empty field plays a copied file path or URL."""
    field = field.strip()
    if field:
        return field
    pasteboard = pasteboard.strip()
    return pasteboard if looks_like_source(pasteboard) else ""


def youtube_watch_url(href: str) -> str:
    """A playable youtube.com/watch?v= URL, or empty if this page is not a video."""
    href = href.strip()
    if not href:
        return ""
    parsed = urlparse(href)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in _YT_HOSTS:
        return ""
    if host == "youtu.be":
        vid = parsed.path.lstrip("/").split("/")[0]
        return f"https://www.youtube.com/watch?v={vid}" if vid else ""
    qs = parse_qs(parsed.query)
    if qs.get("v") and qs["v"][0]:
        return f"https://www.youtube.com/watch?v={qs['v'][0]}"
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] in ("shorts", "embed", "live", "v"):
        return f"https://www.youtube.com/watch?v={parts[1]}"
    return ""


def youtube_playlist_page(href: str) -> str:
    """A /playlist?list= URL. Watch pages with &list= stay a single video."""
    href = (href or "").strip()
    if not href:
        return ""
    parsed = urlparse(href)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in _YT_HOSTS and not host.endswith(".youtube.com"):
        return ""
    pid = (parse_qs(parsed.query).get("list") or [""])[0].strip()
    if not pid:
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    if parts[:1] != ["playlist"]:
        return ""
    if not pid.startswith(("PL", "UU", "OL", "FL")):
        return ""
    return f"https://www.youtube.com/playlist?list={pid}"


def youtube_playlist_entries(href: str, run=None, limit: int = 200) -> list[dict[str, str]]:
    """Watch URLs for a playlist page, via yt-dlp. Empty on failure."""
    page = youtube_playlist_page(href)
    if not page:
        return []
    runner = subprocess.run if run is None else run
    try:
        result = runner(
            ["yt-dlp", "--flat-playlist", "--no-warnings", "-J", page],
            capture_output=True, text=True, timeout=90, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    try:
        data = json.loads(result.stdout or "")
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []
    out: list[dict[str, str]] = []
    for entry in data.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        vid = str(entry.get("id") or "").strip()
        if not vid or vid.startswith("http"):
            watch = youtube_watch_url(str(entry.get("url") or entry.get("id") or ""))
            vid = (parse_qs(urlparse(watch).query).get("v") or [""])[0] if watch else ""
        if not vid or "/" in vid or " " in vid:
            continue
        out.append(
            playlist_entry(
                f"https://www.youtube.com/watch?v={vid}",
                str(entry.get("title") or ""),
                str(entry.get("uploader") or entry.get("channel") or ""),
            )
        )
        if len(out) >= limit:
            break
    return out


def playable_source(href: str, media_src: str = "") -> str:
    """Page or media URL to send to play. YouTube stays a watch URL so ads do not restart."""
    href = (href or "").strip()
    watch = youtube_watch_url(href)
    if watch:
        return watch
    src = (media_src or "").strip()
    if src.startswith("blob:") or "googlevideo.com" in src:
        src = ""
    if src.startswith("http://") or src.startswith("https://"):
        path = urlparse(src).path.lower()
        if path.endswith(_MEDIA_SUFFIXES):
            return src
    host = urlparse(href).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host in _YT_HOSTS or host.endswith(".youtube.com"):
        return ""
    return href


def should_start_play(seen: str, href: str, media_src: str = "") -> str:
    """Empty if this is the same video (YouTube ads fire play again on the same watch URL)."""
    source = playable_source(href, media_src)
    if not source or source == seen:
        return ""
    return source

def play_offset(seconds) -> float:
    """A --start value. Junk becomes 0."""
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value) or value < 0:
        return 0.0
    return value

def parse_watch_payload(raw) -> tuple[str, float]:
    """url and currentTime from the page, or (raw, 0) if it is just a URL."""
    if isinstance(raw, dict):
        return str(raw.get("url") or ""), play_offset(raw.get("t"))
    text = str(raw or "").strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return text, 0.0
        if isinstance(data, dict):
            return str(data.get("url") or ""), play_offset(data.get("t"))
    return text, 0.0

def is_google_login_host(host: str) -> bool:
    """accounts.google.* is a desktop WebAuthn page; mobile YouTube asks for Bluetooth instead."""
    host = (host or "").lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host == "accounts.youtube.com" or host == "accounts.google.com" or host.startswith("accounts.google.")

def page_follow_action(seen_watch: str, href: str) -> tuple[str, str]:
    """Browse freely. Deck playback is only the queue and the play button."""
    return seen_watch, ""

def ensure_store_id(path: Path, mint) -> str:
    """One UUID for WKWebsiteDataStore so YouTube login and cache survive relaunch."""
    if path.is_file():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    value = str(mint()).strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    return value


class DeckRemote:
    """The play button's contract: studio first if the shim is down, then play.

    Studio is the keys. Video sits behind it through the zkgui color-key
    (`D200_VIDEO_UNDER_STUDIO`); play must not quit the copy to show a picture.
    """

    def __init__(self, run=run_cli):
        self._run = run

    def status(self) -> CommandResult:
        return self._run(["status"])

    def stop(self) -> CommandResult:
        return self._run(["stop"])

    def reconnect(self) -> CommandResult:
        return self._run(["reconnect"])

    def studio(self) -> CommandResult:
        return self._run(["studio"])

    def bridge(self) -> CommandResult:
        return self._run(["bridge"])

    def _bring_up(self, results: list[CommandResult]) -> bool:
        """`studio`, or the bridge alone when this host has no Studio to start. True if either is up.

        Without the official app `studio` can never succeed, but video does not need it: the bridge
        is the transport and Studio is only the keys. Stopping at that refusal left a Studio-less
        host with no way to play from the window at all.
        """
        results.append(self._run(["studio"]))
        if results[-1].code == 0:
            return True
        if studio.STUDIO_MISSING not in results[-1].detail:
            return False
        results.append(self._run(["bridge"]))
        return results[-1].code == 0

    def play(self, source: str, pasteboard: str = "", start: float = 0.0, loop: bool = True, crop: str = "auto", volume: float = 1.0, fit: str = "auto") -> list[CommandResult]:
        source = resolve_source(source, pasteboard)
        if not source:
            return [CommandResult(["play"], 2, "", "유튜브에서 영상을 열거나 파일을 연 다음 재생을 누르십시오")]
        results: list[CommandResult] = []
        resume = start > 0 or (crop and crop != "auto")
        if not resume:
            st = self._run(["status"])
            results.append(st)
            if not shim_is_up(st.stdout) and not self._bring_up(results):
                return results
        argv = ["play", source]
        start = play_offset(start)
        if start > 0:
            argv.extend(["--start", f"{start:.3f}"])
        if crop and crop != "auto":
            argv.extend(["--crop", crop])
        if not loop:
            argv.append("--no-loop")
        gain = audio_gain(volume, False)
        if gain != 1.0:
            argv.extend(["--volume", f"{gain:.4f}"])
        fit = play_fit(fit)
        if fit != "auto":
            argv.extend(["--fit", fit])
        played = self._run(argv)
        results.append(played)
        # `shim=up` said the copy was running, so `studio` was skipped -- but the bridge it needs was
        # gone, and `play` refused. Start it now and retry once: this is the same recovery the
        # shim-down branch already does, reached from the failure instead of from `status`.
        if played.code != 0 and bridge_down(played.detail) and self._bring_up(results):
            results.append(self._run(argv))
        return results


def _busy_call(remote: DeckRemote, op: str, source: str, pasteboard: str, done, start=0.0, loop=True, crop="auto", volume=1.0, fit="auto") -> None:
    try:
        if op == "status":
            results = [remote.status()]
        elif op == "stop":
            results = [remote.stop()]
        elif op == "reconnect":
            results = [remote.reconnect()]
        elif op == "studio":
            results = [remote.studio()]
        elif op == "bridge":
            results = [remote.bridge()]
        else:
            results = remote.play(source, pasteboard, start=start, loop=loop, crop=crop, volume=volume, fit=fit)
        done(results, None)
    except Exception as error:
        done([], error)


def main() -> int:
    try:
        import objc
        from AppKit import (
            NSApp,
            NSAppearance,
            NSImage,
            NSImageView,
            NSTableCellView,
            NSVisualEffectView,
            NSWindowStyleMaskFullSizeContentView,
            NSApplication,
            NSBackingStoreBuffered,
            NSBezelStyleRounded,
            NSButton,
            NSColor,
            NSDragOperationCopy,
            NSDragOperationMove,
            NSEventModifierFlagCommand,
            NSEventModifierFlagShift,
            NSFilenamesPboardType,
            NSFont,
            NSMakeRect,
            NSMenu,
            NSMenuItem,
            NSObject,
            NSOpenPanel,
            NSProgressIndicator,
            NSScrollView,
            NSSlider,
            NSTableColumn,
            NSTableView,
            NSBezelBorder,
            NSEvent,
            NSEventMaskKeyDown,
            NSEventModifierFlagControl,
            NSEventModifierFlagOption,
            NSText,
            NSTextField,
            NSView,
            NSViewHeightSizable,
            NSViewMaxYMargin,
            NSViewMinYMargin,
            NSViewMinXMargin,
            NSViewWidthSizable,
            NSWindow,
            NSWindowStyleMaskClosable,
            NSWindowStyleMaskMiniaturizable,
            NSWindowStyleMaskResizable,
            NSWindowStyleMaskTitled,
        )
        from Foundation import NSIndexSet, NSURL, NSURLRequest, NSTimer
        from PyObjCTools import AppHelper
        from WebKit import (
            WKUserContentController,
            WKUserScript,
            WKWebView,
            WKWebViewConfiguration,
            WKWebsiteDataStore,
        )
    except ImportError:
        print(
            "ghostdeck gui needs pyobjc-framework-Cocoa and pyobjc-framework-WebKit on macOS",
            file=sys.stderr,
        )
        return 2
    # `NSColor.CGColor()` hands back an opaque CGColorRef that PyObjC wraps with a warning each
    # time. The layer only stores the pointer, so the warning is noise, and the 0.1s playhead redraw
    # repaints pills: it printed ~2 lines a second into the launcher's log for as long as the window
    # was open.
    import warnings

    warnings.filterwarnings("ignore", category=objc.ObjCPointerWarning)

    remote = DeckRemote()
    hook_js = """
(function(){
  if (window.__ghostdeckHooked) return;
  window.__ghostdeckHooked = true;
  function ytId(href){
    try {
      var u = new URL(href, location.href);
      if (u.searchParams.get('v')) return u.searchParams.get('v');
      var parts = u.pathname.split('/').filter(Boolean);
      if (parts[0] && ['shorts','embed','live','v'].indexOf(parts[0]) >= 0) return parts[1] || '';
    } catch (e) {}
    return '';
  }
  function playlistUrl(){
    try {
      var u = new URL(location.href);
      if (u.pathname.indexOf('/playlist') === 0) {
        var list = u.searchParams.get('list') || '';
        if (list) return 'https://www.youtube.com/playlist?list=' + list;
      }
    } catch (e) {}
    return '';
  }
  function watchUrl(){
    var pl = playlistUrl();
    if (pl) return pl;
    var id = ytId(location.href);
    if (!id) {
      var el = document.querySelector('[video-id]');
      if (el) id = el.getAttribute('video-id') || '';
    }
    if (!id) {
      var canon = document.querySelector('link[rel="canonical"]');
      if (canon) id = ytId(canon.href);
    }
    return id ? ('https://www.youtube.com/watch?v=' + id) : String(location.href);
  }
  window.__ghostdeckWatch = watchUrl;
  window.__ghostdeckNow = function(){
    var v = document.querySelector('video');
    return JSON.stringify({
      url: watchUrl(),
      t: (v && isFinite(v.currentTime)) ? v.currentTime : 0
    });
  };
  function post(type){
    try {
      var v = document.querySelector('video');
      window.webkit.messageHandlers.ghostdeck.postMessage({
        type: type,
        url: watchUrl(),
        src: (v && v.currentSrc) ? String(v.currentSrc) : '',
        t: (v && isFinite(v.currentTime)) ? v.currentTime : 0
      });
    } catch (e) {}
  }
  function silence(v){
    try { v.muted = true; v.volume = 0; v.pause(); } catch (e) {}
  }
  function hook(v){
    if (v.__ghostdeck) return;
    v.__ghostdeck = true;
    silence(v);
    v.addEventListener('play', function(){ silence(v); }, true);
    v.addEventListener('volumechange', function(){ if (!v.muted || v.volume) silence(v); }, true);
  }
  function scan(){ document.querySelectorAll('video,audio').forEach(hook); }
  function mountQueue(){
    var pl = playlistUrl();
    var id = ytId(location.href) || (!pl && ytId(watchUrl()));
    var btn = document.getElementById('ghostdeck-queue');
    var label = pl ? '＋ 재생목록' : '＋ 대기열';
    if (!id && !pl) { if (btn) btn.remove(); return; }
    if (btn) { btn.textContent = label; return; }
    btn = document.createElement('button');
    btn.id = 'ghostdeck-queue';
    btn.type = 'button';
    btn.textContent = label;
    btn.setAttribute('aria-label', '대기열에 넣기');
    btn.style.cssText = 'position:fixed;right:14px;bottom:80px;z-index:2147483647;padding:9px 14px;border:0;border-radius:999px;background:#C8FF47;color:#111;font:700 12px/1.1 -apple-system,BlinkMacSystemFont,sans-serif;letter-spacing:.02em;cursor:pointer;box-shadow:0 8px 24px rgba(200,255,71,.28);';
    btn.addEventListener('click', function(e){
      e.preventDefault();
      e.stopPropagation();
      post('queue');
      btn.textContent = '넣음';
      setTimeout(function(){ if (btn) btn.textContent = playlistUrl() ? '＋ 재생목록' : '＋ 대기열'; }, 1200);
    }, true);
    document.documentElement.appendChild(btn);
  }
  scan();
  mountQueue();
  var mountSoon = false;
  function requestMount(){
    if (mountSoon) return;
    mountSoon = true;
    setTimeout(function(){ mountSoon = false; scan(); mountQueue(); }, 800);
  }
  new MutationObserver(requestMount).observe(document.documentElement, {childList:true, subtree:true});
  var last = watchUrl();
  setInterval(function(){
    var now = watchUrl();
    if (now !== last){
      last = now;
      post('nav');
      mountQueue();
    }
  }, 400);
})();
"""
    def _rgb(r, g, b, a=1.0):
        return NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, a)

    INK = _rgb(0.035, 0.035, 0.04)
    CARD = _rgb(0.12, 0.12, 0.135)
    LIME = _rgb(0.784, 1.0, 0.278)
    GHOST = _rgb(1, 1, 1, 0.52)
    HAIR = _rgb(1, 1, 1, 0.12)
    SNOW = NSColor.whiteColor()

    def _label(frame, text, size=12, bold=False, color=None, mask=0):
        lab = NSTextField.alloc().initWithFrame_(frame)
        lab.setEditable_(False)
        lab.setBezeled_(False)
        lab.setDrawsBackground_(False)
        lab.setFont_(NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size))
        lab.setTextColor_(color or SNOW)
        lab.setStringValue_(text)
        lab.setAutoresizingMask_(mask)
        return lab

    def _pill(btn, fill, ink):
        btn.setBordered_(False)
        btn.setWantsLayer_(True)
        btn.layer().setCornerRadius_(9.0)
        btn.layer().setBackgroundColor_(fill.CGColor())
        if ink is not None:
            btn.setContentTintColor_(ink)
        return btn

    def _symbol(name):
        try:
            return NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
        except Exception:
            return None

    def _icon(btn, name, ink=SNOW):
        img = _symbol(name)
        if img is not None:
            btn.setImage_(img)
            btn.setTitle_("")
            btn.setContentTintColor_(ink)
        return btn

    def _describe(view, text):
        """Tooltip and VoiceOver label. Icon-only buttons have no other name."""
        if view is not None:
            view.setToolTip_(text)
            view.setAccessibilityLabel_(text)
        return view


    def _gui_href(ctrl) -> str:
        url = ctrl.web.URL()
        return str(url.absoluteString()) if url is not None else ""

    def _gui_set_busy(ctrl, on: bool) -> None:
        ctrl.busy = on
        for name in ("file_btn", "reconnect_btn", "bridge_btn"):
            button = getattr(ctrl, name, None)
            if button is not None:
                button.setEnabled_(not on)
        button = getattr(ctrl, "studio_btn", None)
        if button is not None:
            # Without the official app `studio` can only fail, so it is never offered.
            button.setEnabled_(not on and studio_installed())
        # studio/reconnect can take minutes; the dot turns into a spinner so the window does not
        # look frozen while a CLI child works.
        spinner = getattr(ctrl, "spinner", None)
        dot = getattr(ctrl, "dot", None)
        if spinner is not None:
            if on:
                spinner.startAnimation_(None)
            else:
                spinner.stopAnimation_(None)
        if dot is not None:
            dot.setHidden_(on)

    def _gui_recovery_draw(ctrl, action: str) -> None:
        """Light up only the recovery button this state calls for."""
        for name, key in (("reconnect_btn", "reconnect"), ("studio_btn", "studio")):
            button = getattr(ctrl, name, None)
            if button is None:
                continue
            on = action == key
            _pill(button, LIME if on else CARD, INK if on else SNOW)

    def _gui_install_menu(ctrl) -> None:
        """The menu bar. Without one, ⌘Q/⌘C/⌘V/⌘A had nothing to route through.

        Edit and window items keep a nil target so AppKit sends them down the responder chain to the
        field editor or the key window. Shortcuts that a text field also uses (⌘←, Space, Esc) are
        deliberately not bound: a menu key equivalent beats the field editor, so they would stop
        working while typing a URL.
        """

        def item(menu, title, action, key="", mods=None, target=ctrl):
            entry = menu.addItemWithTitle_action_keyEquivalent_(title, action, key)
            if mods is not None:
                entry.setKeyEquivalentModifierMask_(mods)
            if target is not None:
                entry.setTarget_(target)
            return entry

        def submenu(bar, title):
            # `init` leaves the title as "NSMenuItem"; name the holder like its menu.
            holder = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
            bar.addItem_(holder)
            menu = NSMenu.alloc().initWithTitle_(title)
            holder.setSubmenu_(menu)
            return menu

        bar = NSMenu.alloc().init()
        app_menu = submenu(bar, "ghostdeck")
        item(app_menu, "ghostdeck 숨기기", "hide:", "h", target=None)
        app_menu.addItem_(NSMenuItem.separatorItem())
        item(app_menu, "ghostdeck 종료", "terminate:", "q", target=None)

        file_menu = submenu(bar, "파일")
        item(file_menu, "파일 열기…", "openFile:", "o")
        item(file_menu, "페이지를 대기열에 추가", "addToPlaylist:")

        edit_menu = submenu(bar, "편집")
        item(edit_menu, "실행 취소", "undo:", "z", target=None)
        item(edit_menu, "다시 실행", "redo:", "z", NSEventModifierFlagCommand | NSEventModifierFlagShift, None)
        edit_menu.addItem_(NSMenuItem.separatorItem())
        item(edit_menu, "오려두기", "cut:", "x", target=None)
        item(edit_menu, "복사하기", "copy:", "c", target=None)
        item(edit_menu, "붙여넣기", "paste:", "v", target=None)
        item(edit_menu, "모두 선택", "selectAll:", "a", target=None)

        view_menu = submenu(bar, "보기")
        item(view_menu, "새로 고침", "reload:", "r")
        item(view_menu, "주소 입력", "focusUrl:", "l")
        view_menu.addItem_(NSMenuItem.separatorItem())
        item(view_menu, "뒤로", "back:", "[")
        item(view_menu, "앞으로", "forward:", "]")

        play_menu = submenu(bar, "재생")
        item(play_menu, "재생 / 일시정지 (↩)", "play:")
        item(play_menu, "정지", "stop:", ".")
        play_menu.addItem_(NSMenuItem.separatorItem())
        item(play_menu, "이전 곡", "prevTrack:")
        item(play_menu, "다음 곡", "nextTrack:")
        item(play_menu, "셔플", "toggleShuffle:")
        item(play_menu, "반복 모드 바꾸기", "cycleRepeat:")
        item(play_menu, "음소거", "toggleMute:")

        deck_menu = submenu(bar, "덱")
        item(deck_menu, "덱 다시 연결", "reconnect:")
        item(deck_menu, "스튜디오 켜기 (키 + 영상)", "studio:")
        item(deck_menu, "브리지만 켜기 (영상만)", "bridge:")
        deck_menu.addItem_(NSMenuItem.separatorItem())
        item(deck_menu, "덱 설정 보기/숨기기", "toggleSettings:", ",")

        window_menu = submenu(bar, "윈도우")
        item(window_menu, "최소화", "performMiniaturize:", "m", target=None)
        item(window_menu, "닫기", "performClose:", "w", target=None)

        NSApp.setMainMenu_(bar)
        NSApp.setWindowsMenu_(window_menu)

    def _gui_note_failure(ctrl, note: str, full: str) -> None:
        """The short Korean line in the strip; the whole CLI message on hover."""
        ctrl.note.setStringValue_(note)
        full = (full or "").strip()
        ctrl.note.setToolTip_(full if full and full != note else None)

    def _gui_apply(ctrl, results, error, epoch=None) -> None:
        if epoch is not None and epoch != getattr(ctrl, "epoch", 0):
            return
        _gui_set_busy(ctrl, False)
        pending = getattr(ctrl, "pending_source", "")
        pending_start = getattr(ctrl, "pending_start", 0.0)
        ctrl.pending_source = ""
        ctrl.pending_start = 0.0
        ctrl.note.setToolTip_(None)
        if error is not None:
            ctrl.seen_watch = ""
            raw = f"{type(error).__name__}: {error}"
            _gui_note_failure(ctrl, play_failure_note(raw), raw)
            if should_retry_pending(
                pending, pending_start, getattr(ctrl, "seen_watch", ""), getattr(ctrl, "played_start", 0.0)
            ):
                _gui_kick(ctrl, "play", pending, start=pending_start)
            return
        for item in results:
            if item.argv[:1] == ["status"] and item.stdout.strip():
                picture = deck_has_picture()
                has = studio_installed()
                ctrl.status.setStringValue_(status_text(item.stdout, has_picture=picture, has_studio=has))
                ctrl.status.setToolTip_(item.stderr.strip() or None)
                dot = getattr(ctrl, "dot", None)
                colour = status_dot_color(item.stdout, has_picture=picture, has_studio=has)
                if dot is not None and colour is not None:
                    dot.setTextColor_(colour)
                _gui_recovery_draw(ctrl, recovery_action(item.stdout, has_studio=has))
                _gui_sync_deck(ctrl, parse_status_fields(item.stdout).get("playing") == "yes")
        last = results[-1] if results else None
        if last is None:
            if should_retry_pending(
                pending, pending_start, getattr(ctrl, "seen_watch", ""), getattr(ctrl, "played_start", 0.0)
            ):
                _gui_kick(ctrl, "play", pending, start=pending_start)
            return
        if last.code != 0:
            ctrl.seen_watch = ""
            if last.argv[:1] == ["play"]:
                note = play_failure_note(last.detail)
            else:
                note = failure_note(last.detail)
            _gui_note_failure(ctrl, note, last.stderr or last.stdout)
        elif last.argv[:1] == ["stop"]:
            ctrl.seen_watch = ""
            ctrl.note.setStringValue_("멈췄습니다.")
        elif last.argv[:1] == ["play"]:
            if len(last.argv) > 1:
                ctrl.seen_watch = last.argv[1]
                _gui_playlist_put(ctrl, last.argv[1])
            ctrl.note.setStringValue_(play_success_note(deck_has_picture()))
        elif last.argv[:1] == ["studio"]:
            ctrl.note.setStringValue_("스튜디오를 켰습니다. 이제 재생할 수 있습니다.")
        elif last.argv[:1] == ["bridge"]:
            ctrl.note.setStringValue_("브리지를 켰습니다. 키 없이 재생할 수 있습니다.")
        elif last.argv[:1] == ["reconnect"]:
            ctrl.note.setStringValue_("연결했습니다. 재생할 수 있습니다.")
        if should_retry_pending(
            pending, pending_start, getattr(ctrl, "seen_watch", ""), getattr(ctrl, "played_start", 0.0)
        ):
            _gui_kick(ctrl, "play", pending, start=pending_start)

    def _gui_kick(ctrl, op: str, source: str, start: float = 0.0, loop: bool = False, crop: str = "auto") -> None:
        if op in ("reconnect", "studio", "bridge"):
            ctrl.epoch = getattr(ctrl, "epoch", 0) + 1
            epoch = ctrl.epoch
            if not ctrl.busy:
                _gui_set_busy(ctrl, True)
            ctrl.note.setStringValue_(
                {"reconnect": "연결 다시 잡는 중…", "studio": "스튜디오 켜는 중…", "bridge": "브리지 켜는 중…"}.get(op, "준비 중…")
            )
            threading.Thread(
                target=_busy_call,
                args=(
                    remote,
                    op,
                    "",
                    "",
                    lambda r, e: AppHelper.callAfter(lambda: _gui_apply(ctrl, r, e, epoch)),
                    0.0,
                    False,
                    "auto",
                ),
                daemon=True,
            ).start()
            return
        if op == "stop":
            ctrl.epoch = getattr(ctrl, "epoch", 0) + 1
            ctrl.pending_source = ""
            ctrl.pending_start = 0.0
            ctrl.user_stopped = True
            epoch = ctrl.epoch
            if not ctrl.busy:
                _gui_set_busy(ctrl, True)
            ctrl.note.setStringValue_("멈추는 중…")
            threading.Thread(
                target=_busy_call,
                args=(
                    remote,
                    "stop",
                    "",
                    "",
                    lambda r, e: AppHelper.callAfter(lambda: _gui_apply(ctrl, r, e, epoch)),
                    0.0,
                    False,
                    "auto",
                ),
                daemon=True,
            ).start()
            return
        if ctrl.busy and op == "play":
            ctrl.pending_source = source
            ctrl.pending_start = play_offset(start)
            ctrl.note.setStringValue_("지금 작업이 끝나면 재생합니다.")
            return
        if ctrl.busy and op != "status":
            return
        if op != "status":
            if op == "play":
                ctrl.seen_watch = source
                ctrl.played_start = play_offset(start)
                ctrl.saw_picture = False
            _gui_set_busy(ctrl, True)
            ctrl.note.setStringValue_("재생 준비 중…" if op == "play" else "멈추는 중…")
        pasteboard = read_pasteboard() if op == "play" else ""
        if op == "play":
            pref = play_crop_choice(getattr(ctrl, "crop", "auto"))
            if crop == "auto":
                crop = pref
            crop = play_crop_argv(source, crop)
        thread = threading.Thread(
            target=_busy_call,
            args=(
                remote,
                op,
                source,
                pasteboard,
                lambda r, e, epoch=getattr(ctrl, "epoch", 0): AppHelper.callAfter(
                    lambda: _gui_apply(ctrl, r, e, epoch)
                ),
                play_offset(start),
                playlist_should_loop(
                    source,
                    getattr(ctrl, "playlist", []),
                    getattr(ctrl, "repeat", "off"),
                ) if op == "play" else loop,
                crop,
                audio_gain(getattr(ctrl, "volume", 1.0), getattr(ctrl, "muted", False)),
                play_fit(getattr(ctrl, "fit", "auto")),
            ),
            daemon=True,
        )
        thread.start()

    def _gui_follow(ctrl, href: str, start: float = 0.0) -> None:
        _seen, action = page_follow_action(getattr(ctrl, "seen_watch", ""), href)
        if not action:
            return
        if action == "stop":
            _gui_kick(ctrl, "stop", "")
            return
        _gui_kick(ctrl, "play", action, start=start)

    def _gui_playlist_put(ctrl, source: str) -> None:
        source = (source or "").strip()
        if not source:
            return
        ctrl.playlist = playlist_add(getattr(ctrl, "playlist", []), source)
        playlist_save(PLAYLIST_PATH, ctrl.playlist)
        _gui_playlist_draw(ctrl)
        found = playlist_find(ctrl.playlist, source)
        if found.get("title") and found.get("duration"):
            return

        def fill():
            title, channel = source_identity(source) if not found.get("title") else ("", "")
            length = source_duration(source) if not found.get("duration") else 0.0

            def apply():
                ctrl.playlist = playlist_add(getattr(ctrl, "playlist", []), source, title, channel, length)
                playlist_save(PLAYLIST_PATH, ctrl.playlist)
                _gui_playlist_draw(ctrl)

            AppHelper.callAfter(apply)

        threading.Thread(target=fill, daemon=True).start()

    def _gui_backfill_meta(ctrl) -> None:
        """Fill missing lengths for rows saved before durations were stored. One worker, in order."""
        missing = [playlist_source(item) for item in getattr(ctrl, "playlist", []) if not item.get("duration")]
        if not missing:
            return

        def fill():
            for source in missing:
                length = source_duration(source)
                if length <= 0:
                    continue

                def apply(source=source, length=length):
                    ctrl.playlist = playlist_add(getattr(ctrl, "playlist", []), source, duration=length)
                    playlist_save(PLAYLIST_PATH, ctrl.playlist)
                    _gui_playlist_draw(ctrl)

                AppHelper.callAfter(apply)

        threading.Thread(target=fill, daemon=True).start()

    _THUMBS: dict = {}
    _THUMB_PENDING: set = set()

    def _thumb_image(ctrl, source: str):
        """The NSImage for a source's still, or None while it loads (the view redraws on arrival)."""
        key = playlist_identity(source)
        if not key:
            return None
        if key in _THUMBS:
            return _THUMBS[key]
        if key in _THUMB_PENDING:
            return None
        _THUMB_PENDING.add(key)

        def load():
            path = fetch_thumbnail(key)

            def apply():
                _THUMB_PENDING.discard(key)
                image = NSImage.alloc().initWithContentsOfFile_(str(path)) if path else None
                _THUMBS[key] = image
                table = getattr(ctrl, "playlist_table", None)
                if table is not None:
                    table.reloadData()
                _gui_now_draw(ctrl)

            AppHelper.callAfter(apply)

        threading.Thread(target=load, daemon=True).start()
        return None


    def _gui_queue_href(ctrl, href: str) -> None:
        href = (href or "").strip()
        if not href:
            return
        if youtube_playlist_page(href):
            ctrl.note.setStringValue_("재생목록을 읽는 중…")

            def fill_list():
                entries = youtube_playlist_entries(href)

                def apply():
                    if not entries:
                        ctrl.note.setStringValue_("재생목록을 읽지 못했습니다.")
                        return
                    ctrl.playlist = playlist_extend(getattr(ctrl, "playlist", []), entries)
                    playlist_save(PLAYLIST_PATH, ctrl.playlist)
                    _gui_playlist_draw(ctrl)
                    ctrl.note.setStringValue_(f"재생목록 {len(entries)}곡을 넣었습니다.")

                AppHelper.callAfter(apply)

            threading.Thread(target=fill_list, daemon=True).start()
            return
        source = youtube_watch_url(href) or playable_source(href)
        if not source:
            ctrl.note.setStringValue_("이 페이지에서 영상을 찾지 못했습니다.")
            return
        _gui_playlist_put(ctrl, source)
        ctrl.note.setStringValue_("대기열에 넣었습니다.")


    def _gui_card_row(ctrl) -> None:
        """Fit mute + volume between the repeat button and the settings button.

        Autoresizing alone cannot do this: a fixed-width slider pinned right slides into the
        transport buttons as the window narrows (measured at 760pt: over shuffle and repeat), and a
        stretchy one becomes absurdly long when it widens. So the slider takes what is free, 60-160pt.
        """
        repeat = getattr(ctrl, "repeat_btn", None)
        gear = getattr(ctrl, "settings_btn", None)
        volume = getattr(ctrl, "volume_bar", None)
        mute = getattr(ctrl, "mute_btn", None)
        if None in (repeat, gear, volume, mute):
            return
        left = repeat.frame().origin.x + repeat.frame().size.width + 16
        right = gear.frame().origin.x - 12
        width = max(60.0, min(160.0, right - left - 32 - 6))
        vf = volume.frame()
        vf.origin.x, vf.size.width = right - width, width
        volume.setFrame_(vf)
        mf = mute.frame()
        mf.origin.x = vf.origin.x - 6 - mf.size.width
        mute.setFrame_(mf)

    def _gui_settings_draw(ctrl, open_=None) -> None:
        """Show or hide the deck-settings drawer at the top of the queue column.

        Studio, bridge, fit, crop and the overlay slider are set once and forgotten, so they stay
        folded away and the queue gets the room. Opening the drawer pushes the list down.
        """
        if open_ is not None:
            ctrl.settings_open = bool(open_)
        on = bool(getattr(ctrl, "settings_open", False))
        for item in getattr(ctrl, "settings_views", []):
            item.setHidden_(not on)
        scroll = getattr(ctrl, "queue_scroll", None)
        head = getattr(ctrl, "queue_head", None)
        if scroll is not None and head is not None:
            # Measured from the header's current frame, not a launch-time constant: the header rides
            # the column's top edge as the window resizes, and the list must end under it (or under
            # the drawer) at every size.
            frame = scroll.frame()
            top = head.frame().origin.y - 10
            ceiling = top - (getattr(ctrl, "settings_h", 0) if on else 0)
            frame.size.height = max(60.0, ceiling - frame.origin.y)
            scroll.setFrame_(frame)
        button = getattr(ctrl, "settings_btn", None)
        if button is not None:
            _pill(button, LIME if on else CARD, INK if on else SNOW)
            _icon(button, "slider.horizontal.3", INK if on else SNOW)

    def _gui_playlist_draw(ctrl) -> None:
        table = getattr(ctrl, "playlist_table", None)
        items = getattr(ctrl, "playlist", [])
        if table is not None:
            selected = int(table.selectedRow())
            table.reloadData()
            if 0 <= selected < len(items):
                table.selectRowIndexes_byExtendingSelection_(
                    NSIndexSet.indexSetWithIndex_(selected), False
                )
        heading = getattr(ctrl, "queue_head", None)
        if heading is not None:
            n = len(items)
            heading.setStringValue_(f"대기열  {n}" if n else "대기열")
        empty = getattr(ctrl, "queue_empty", None)
        if empty is not None:
            empty.setHidden_(bool(items))
        _gui_now_draw(ctrl)
        _gui_playhead_draw(ctrl)
        _gui_mode_draw(ctrl)

    def _gui_now_draw(ctrl) -> None:
        source, _pos, active = deck_playhead()
        if not active:
            source = getattr(ctrl, "seen_watch", "") if getattr(ctrl, "busy", False) else ""
        source = playlist_identity(source)
        items = getattr(ctrl, "playlist", [])
        found = playlist_find(items, source) if source else playlist_entry("")
        now_head = getattr(ctrl, "now_head", None)
        if now_head is not None:
            now_head.setStringValue_(now_state_label(deck_has_picture(), bool(source), bool(getattr(ctrl, "busy", False))))
        title = getattr(ctrl, "now_title", None)
        if title is not None:
            title.setStringValue_(playlist_title(found) if source else "재생 중인 영상이 없습니다")
        channel = getattr(ctrl, "now_channel", None)
        if channel is not None:
            channel.setStringValue_(
                (playlist_subtitle(found) or " ") if source
                else "유튜브에서 영상을 열고 ▶ 를 누르거나, 파일을 끌어다 놓으십시오"
            )
        art = getattr(ctrl, "now_art", None)
        if art is not None:
            image = _thumb_image(ctrl, source) if source else None
            art.setImage_(image)
            glyph = getattr(ctrl, "now_glyph", None)
            if glyph is not None:
                glyph.setHidden_(image is not None)
        if getattr(ctrl, "shown_now", None) != source:
            ctrl.shown_now = source
            table = getattr(ctrl, "playlist_table", None)
            if table is not None:
                table.reloadData()
                # The selection follows the deck, so ▶ after ■ resumes the track that was playing
                # instead of whatever row happened to be selected. A source that ends (stop, end of
                # track) leaves the selection where it was, which is that same track.
                row = playlist_index(items, source)
                if row >= 0:
                    table.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(row), False)
                    table.scrollRowToVisible_(row)
        play_btn = getattr(ctrl, "play_btn", None)
        if play_btn is not None:
            _icon(play_btn, "pause.fill" if deck_has_picture() else "play.fill", INK)
            _pill(play_btn, LIME, INK)

    def _gui_mode_draw(ctrl) -> None:
        shuffle_btn = getattr(ctrl, "shuffle_btn", None)
        if shuffle_btn is not None:
            on = bool(getattr(ctrl, "shuffle", False))
            _icon(shuffle_btn, "shuffle", INK if on else SNOW)
            _pill(shuffle_btn, LIME if on else CARD, INK if on else SNOW)
            _describe(shuffle_btn, "셔플: 켬" if on else "셔플: 꺼짐")
        repeat_btn = getattr(ctrl, "repeat_btn", None)
        if repeat_btn is not None:
            mode = getattr(ctrl, "repeat", "off")
            _icon(repeat_btn, "repeat.1" if mode == "one" else "repeat", INK if mode != "off" else SNOW)
            _pill(repeat_btn, LIME if mode != "off" else CARD, INK if mode != "off" else SNOW)
            _describe(repeat_btn, {"all": "반복: 전체", "one": "반복: 한 곡"}.get(mode, "반복: 끔"))
        mute_btn = getattr(ctrl, "mute_btn", None)
        if mute_btn is not None:
            muted = bool(getattr(ctrl, "muted", False))
            _icon(mute_btn, "speaker.slash.fill" if muted else "speaker.wave.2.fill", INK if muted else SNOW)
            _describe(mute_btn, "음소거 풀기" if muted else "음소거")
            _pill(mute_btn, LIME if muted else CARD, INK if muted else SNOW)
        bar = getattr(ctrl, "volume_bar", None)
        if bar is not None and not getattr(ctrl, "voluming", False):
            bar.setDoubleValue_(clamp_volume(getattr(ctrl, "volume", 1.0)))
        fit_btn = getattr(ctrl, "fit_btn", None)
        if fit_btn is not None:
            mode = play_fit(getattr(ctrl, "fit", "auto"))
            fit_btn.setTitle_(fit_label(mode))
            _pill(fit_btn, LIME if mode != "auto" else CARD, INK if mode != "auto" else SNOW)
        crop_btn = getattr(ctrl, "crop_btn", None)
        if crop_btn is not None:
            mode = play_crop_choice(getattr(ctrl, "crop", "auto"))
            crop_btn.setTitle_(crop_label(mode))
            _pill(crop_btn, LIME if mode != "auto" else CARD, INK if mode != "auto" else SNOW)


    def _save_prefs(ctrl) -> None:
        player_prefs_save(
            PLAYER_PATH,
            {
                "repeat": getattr(ctrl, "repeat", "off"),
                "shuffle": bool(getattr(ctrl, "shuffle", False)),
                "volume": clamp_volume(getattr(ctrl, "volume", 1.0)),
                "muted": bool(getattr(ctrl, "muted", False)),
                "overlay": clamp_volume(getattr(ctrl, "overlay", 1.0)),
                "fit": play_fit(getattr(ctrl, "fit", "auto")),
                "crop": play_crop_choice(getattr(ctrl, "crop", "auto")),
            },
        )


    def _commit_audio(ctrl) -> None:
        _save_prefs(ctrl)
        request_live_volume(audio_gain(getattr(ctrl, "volume", 1.0), getattr(ctrl, "muted", False)))
        _gui_mode_draw(ctrl)

    def _commit_overlay(ctrl) -> None:
        _save_prefs(ctrl)
        request_live_overlay(clamp_volume(getattr(ctrl, "overlay", 1.0)))

    def _gui_playhead_draw(ctrl) -> None:
        bar = getattr(ctrl, "seek_bar", None)
        elapsed = getattr(ctrl, "elapsed_lab", None)
        remain = getattr(ctrl, "remain_lab", None)
        if getattr(ctrl, "seeking", False):
            if elapsed is not None and bar is not None:
                elapsed.setStringValue_(format_clock(bar.doubleValue()))
            return
        _gui_now_draw(ctrl)
        source, pos, _active = deck_playhead()
        host_len = deck_duration()
        if host_len > 0 and source:
            ctrl.duration_for = source
            ctrl.media_duration = host_len
        duration = float(getattr(ctrl, "media_duration", 0.0) or 0.0)
        if source and source != getattr(ctrl, "duration_for", ""):
            ctrl.duration_for = source
            ctrl.media_duration = 0.0
            duration = 0.0
            if not getattr(ctrl, "duration_busy", False):
                ctrl.duration_busy = True

                def fill():
                    value = source_duration(source)

                    def apply():
                        ctrl.duration_busy = False
                        if getattr(ctrl, "duration_for", "") == source:
                            ctrl.media_duration = value
                        _gui_playhead_draw(ctrl)

                    AppHelper.callAfter(apply)

                threading.Thread(target=fill, daemon=True).start()
        duration = float(getattr(ctrl, "media_duration", 0.0) or 0.0)
        now = time.monotonic()
        hold = getattr(ctrl, "hold_pos", None)
        hold_until = float(getattr(ctrl, "hold_until", 0.0) or 0.0)
        if hold is not None and now < hold_until and abs(pos - hold) > 1.25:
            pos = hold
        else:
            ctrl.hold_pos = None
            if not deck_has_picture():
                ctrl.playhead_wall = None
                ctrl.playhead_shown = None
            else:
                wall = getattr(ctrl, "playhead_wall", None)
                shown = getattr(ctrl, "playhead_shown", None)
                if wall is not None and shown is not None:
                    guessed = shown + (now - wall)
                    if abs(guessed - pos) < 2.5:
                        pos = max(pos, guessed)
                ctrl.playhead_wall = now
                ctrl.playhead_shown = pos
        pos = wrap_playhead(
            pos,
            duration,
            playlist_should_loop(
                source,
                getattr(ctrl, "playlist", []),
                getattr(ctrl, "repeat", "off"),
            ),
        )
        if elapsed is not None:
            elapsed.setStringValue_(format_clock(pos) if source else "0:00")
        if remain is not None:
            remain.setStringValue_(format_clock(duration) if duration else "--:--")
        if bar is None:
            return
        if duration <= 0:
            return
        bar.setMaxValue_(duration)
        bar.setDoubleValue_(pos)
        bar.setEnabled_(True)

    def _gui_sync_deck(ctrl, playing: bool) -> None:
        now = playlist_identity(deck_now_playing())
        sources = [playlist_identity(playlist_source(item)) for item in getattr(ctrl, "playlist", [])]
        if now and now not in sources:
            _gui_playlist_put(ctrl, now)
        else:
            _gui_playlist_draw(ctrl)
        picture = deck_has_picture()
        if picture:
            ctrl.was_playing = True
            ctrl.saw_picture = True
            # The deck is the authority on what is playing: a track started from the CLI, the deck
            # or another window is the one whose end must advance the queue.
            if now and not getattr(ctrl, "busy", False):
                ctrl.seen_watch = now
            return
        from ghostdeck import play as playmod
        proc = playmod.playing()
        opening = (proc or deck_session_active()) and not getattr(ctrl, "saw_picture", False)
        if (opening or proc) and not picture:
            return
        watched = playlist_identity(getattr(ctrl, "seen_watch", "") or now)
        ended = playlist_identity(deck_finished())
        if should_auto_next(
            playing=proc,
            finished=bool(ended) and ended == watched,
            was_playing=getattr(ctrl, "was_playing", False),
            saw_picture=getattr(ctrl, "saw_picture", False),
            user_stopped=getattr(ctrl, "user_stopped", False),
            busy=bool(getattr(ctrl, "busy", False)),
        ):
            nxt = playlist_next(
                getattr(ctrl, "playlist", []),
                getattr(ctrl, "seen_watch", "") or now,
                repeat=getattr(ctrl, "repeat", "off"),
                shuffle=getattr(ctrl, "shuffle", False),
            )
            ctrl.was_playing = False
            ctrl.saw_picture = False
            if nxt:
                _gui_kick(ctrl, "play", nxt)
                return
        ctrl.was_playing = False


    class DropBar(NSView):
        """Toolbar/status strip accepts a dropped media file. Clicks go to the controls on top."""

        def draggingEntered_(self, _info):
            return NSDragOperationCopy

        def draggingUpdated_(self, _info):
            return NSDragOperationCopy

        def prepareForDragOperation_(self, _info):
            return True

        def performDragOperation_(self, info):
            names = info.draggingPasteboard().propertyListForType_(NSFilenamesPboardType) or []
            source = dropped_play_source(list(names))
            if not source:
                return False
            ctrl = self.ctrl
            ctrl.url_field.setStringValue_(source)
            _gui_kick(ctrl, "play", source)
            return True

        def hitTest_(self, _point):
            return None

    class SeekSlider(NSSlider):
        def mouseDown_(self, event):
            ctrl = self.ctrl
            ctrl.seeking = True
            objc.super(SeekSlider, self).mouseDown_(event)
            ctrl.hold_pos = play_offset(self.doubleValue())
            ctrl.hold_until = time.monotonic() + 2.0
            ctrl.seeking = False
            elapsed = getattr(ctrl, "elapsed_lab", None)
            if elapsed is not None:
                elapsed.setStringValue_(format_clock(self.doubleValue()))

        def mouseDragged_(self, event):
            objc.super(SeekSlider, self).mouseDragged_(event)
            ctrl = self.ctrl
            elapsed = getattr(ctrl, "elapsed_lab", None)
            if elapsed is not None:
                elapsed.setStringValue_(format_clock(self.doubleValue()))

    class QueueDrop(NSView):
        """The queue pane enqueues a drop. It does not start playback."""

        def draggingEntered_(self, _info):
            return NSDragOperationCopy

        def draggingUpdated_(self, _info):
            return NSDragOperationCopy

        def prepareForDragOperation_(self, _info):
            return True

        def performDragOperation_(self, info):
            names = info.draggingPasteboard().propertyListForType_(NSFilenamesPboardType) or []
            added = False
            for name in names:
                source = media_path_candidate(str(name))
                if source:
                    _gui_playlist_put(self.ctrl, source)
                    added = True
            if added:
                self.ctrl.note.setStringValue_("대기열에 넣었습니다.")
            return added

    class Controller(NSObject):
        def init(self):
            self = objc.super(Controller, self).init()
            self.busy = False
            self.pending_source = ""
            self.pending_start = 0.0
            self.seen_watch = ""
            self.playlist = playlist_load(PLAYLIST_PATH)
            prefs = player_prefs_load()
            self.repeat = prefs["repeat"]
            self.shuffle = prefs["shuffle"]
            self.volume = prefs["volume"]
            self.muted = prefs["muted"]
            self.overlay = prefs.get("overlay", 1.0)
            self.fit = prefs.get("fit", "auto")
            self.crop = prefs.get("crop", "auto")
            self.voluming = False
            self.was_playing = False
            self.user_stopped = False
            self.epoch = 0
            self.resume_pos = 0.0
            self.resume_source = ""
            self.media_duration = 0.0
            self.duration_for = ""
            self.duration_busy = False
            self.seeking = False
            self.played_start = 0.0
            # Toolbar, then a full-width Now Playing card, then browser | queue. See `Layout`.
            L = Layout(*LAYOUT_DEFAULT)
            W, H = L.width, L.height
            PAD = L.pad
            TOOL_Y = L.toolbar_y
            CX, CY, CW, CH = L.card
            QX, QY, QW, QH = L.queue
            WX, WY, WW, WH = L.web
            # Autoresizing, named by what each group should do when the window changes size.
            top_left = NSViewMinYMargin
            top_wide = NSViewWidthSizable | NSViewMinYMargin
            top_right = NSViewMinXMargin | NSViewMinYMargin
            right_col = NSViewMinXMargin | NSViewHeightSizable
            self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, W, H),
                NSWindowStyleMaskTitled
                | NSWindowStyleMaskClosable
                | NSWindowStyleMaskMiniaturizable
                | NSWindowStyleMaskResizable
                | NSWindowStyleMaskFullSizeContentView,
                NSBackingStoreBuffered,
                False,
            )
            self.window.setTitle_("ghostdeck")
            self.window.setTitlebarAppearsTransparent_(True)
            self.window.setTitleVisibility_(1)
            self.window.setReleasedWhenClosed_(False)
            self.window.setContentMinSize_(LAYOUT_MIN)
            self.window.setContentSize_((W, H))
            self.window.setBackgroundColor_(INK)
            try:
                self.window.setAppearance_(NSAppearance.appearanceNamed_("NSAppearanceNameDarkAqua"))
            except Exception:
                pass
            self.window.center()
            view = self.window.contentView()
            view.setWantsLayer_(False)
            drop = DropBar.alloc().initWithFrame_(view.bounds())
            drop.ctrl = self
            drop.registerForDraggedTypes_([NSFilenamesPboardType])
            drop.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
            view.addSubview_(drop)

            # ---- browser -------------------------------------------------------------------
            config = WKWebViewConfiguration.alloc().init()
            from Foundation import NSUUID
            from WebKit import WKWebsiteDataStore

            uid = NSUUID.alloc().initWithUUIDString_(
                ensure_store_id(
                    Path.home() / ".ghostdeck" / "webkit-store-id",
                    lambda: str(NSUUID.UUID().UUIDString()),
                )
            )
            config.setWebsiteDataStore_(WKWebsiteDataStore.dataStoreForIdentifier_(uid))
            try:
                config.setMediaTypesRequiringUserActionForPlayback_(3)
            except Exception:
                pass
            config.preferences().setJavaScriptCanOpenWindowsAutomatically_(True)
            ucc = WKUserContentController.alloc().init()
            ucc.addScriptMessageHandler_name_(self, "ghostdeck")
            ucc.addUserScript_(
                WKUserScript.alloc().initWithSource_injectionTime_forMainFrameOnly_(
                    hook_js, 0, False
                )
            )
            config.setUserContentController_(ucc)
            self.ucc = ucc
            self.popups = []
            prefs = config.defaultWebpagePreferences()
            if prefs is not None:
                prefs.setPreferredContentMode_(0)

            shell = NSView.alloc().initWithFrame_(NSMakeRect(WX, WY, WW, WH))
            shell.setWantsLayer_(True)
            shell.layer().setCornerRadius_(10.0)
            shell.layer().setMasksToBounds_(True)
            shell.layer().setBorderWidth_(1.0)
            shell.layer().setBorderColor_(HAIR.CGColor())
            # The browser takes all the spare width: resizing the window makes the page bigger.
            shell.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
            view.addSubview_(shell)
            self.web = WKWebView.alloc().initWithFrame_configuration_(
                NSMakeRect(0, 0, WW, WH),
                config,
            )
            self.web.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
            self.web.setUIDelegate_(self)
            self.web.setNavigationDelegate_(self)
            shell.addSubview_(self.web)
            self.web.loadRequest_(
                NSURLRequest.requestWithURL_(NSURL.URLWithString_("https://www.youtube.com"))
            )

            # ---- toolbar -------------------------------------------------------------------
            def bar_btn(x, w, title, symbol, action, tip):
                btn = NSButton.alloc().initWithFrame_(NSMakeRect(x, TOOL_Y + 4, w, 28))
                btn.setTitle_(title)
                btn.setFont_(NSFont.boldSystemFontOfSize_(11))
                _pill(btn, CARD, SNOW)
                if symbol:
                    _icon(btn, symbol, SNOW)
                    if title:
                        btn.setTitle_(title)
                        btn.setImagePosition_(2)  # NSImageLeft: icon then label
                btn.setTarget_(self)
                btn.setAction_(action)
                btn.setAutoresizingMask_(top_left)
                _describe(btn, tip)
                view.addSubview_(btn)
                return btn

            # Shortcuts live in the menu bar (`_gui_install_menu`), not on the buttons.
            bar_btn(PAD, 28, "", "chevron.left", "back:", "뒤로 (⌘[)")
            bar_btn(PAD + 34, 28, "", "chevron.right", "forward:", "앞으로 (⌘])")
            bar_btn(PAD + 68, 28, "", "arrow.clockwise", "reload:", "새로 고침 (⌘R)")
            url_x = PAD + 104
            self.file_btn = bar_btn(W - PAD - 72, 72, " 파일", "folder", "openFile:",
                                    "영상 파일을 골라 덱에서 재생 (⌘O)")
            self.file_btn.setAutoresizingMask_(top_right)
            self.url_field = NSTextField.alloc().initWithFrame_(
                NSMakeRect(url_x, TOOL_Y + 4, W - PAD - 72 - 8 - url_x, 28)
            )
            self.url_field.setStringValue_("https://www.youtube.com")
            self.url_field.setBezeled_(False)
            self.url_field.setBordered_(False)
            self.url_field.setDrawsBackground_(True)
            self.url_field.setBackgroundColor_(CARD)
            self.url_field.setTextColor_(SNOW)
            self.url_field.setFont_(NSFont.systemFontOfSize_(12))
            self.url_field.setWantsLayer_(True)
            self.url_field.layer().setCornerRadius_(8.0)
            self.url_field.setFocusRingType_(1)
            self.url_field.setTarget_(self)
            self.url_field.setAction_("go:")
            try:
                self.url_field.setPlaceholderString_("주소, 유튜브 링크 또는 영상 파일 경로")
            except Exception:
                pass
            _describe(self.url_field, "주소 또는 영상 파일 경로 (⌘L)")
            self.url_field.setAutoresizingMask_(top_wide)
            view.addSubview_(self.url_field)

            # ---- Now Playing card ----------------------------------------------------------
            card = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(CX, CY, CW, CH))
            card.setMaterial_(7)
            card.setBlendingMode_(0)
            card.setState_(1)
            card.setWantsLayer_(True)
            card.layer().setCornerRadius_(12.0)
            card.layer().setMasksToBounds_(True)
            card.setAutoresizingMask_(top_wide)
            view.addSubview_(card)

            # Thumbnail on the left, 16:9 and as tall as the card allows.
            IN = 16
            art_h = CH - IN * 2
            art_w = round(art_h * 16 / 9)
            AX, AY = CX + IN, CY + IN
            self.now_art = NSImageView.alloc().initWithFrame_(NSMakeRect(AX, AY, art_w, art_h))
            self.now_art.setImageScaling_(3)  # NSImageScaleProportionallyUpOrDown
            self.now_art.setWantsLayer_(True)
            self.now_art.layer().setCornerRadius_(8.0)
            self.now_art.layer().setMasksToBounds_(True)
            self.now_art.layer().setBackgroundColor_(CARD.CGColor())
            self.now_art.setAutoresizingMask_(top_left)
            _describe(self.now_art, "재생 중인 영상")
            view.addSubview_(self.now_art)
            # Placeholder glyph, centred on the (empty) thumbnail.
            self.now_glyph = NSImageView.alloc().initWithFrame_(
                NSMakeRect(AX + art_w / 2 - 16, AY + art_h / 2 - 16, 32, 32)
            )
            glyph = _symbol("play.rectangle")
            if glyph is not None:
                self.now_glyph.setImage_(glyph)
                self.now_glyph.setContentTintColor_(GHOST)
            self.now_glyph.setAutoresizingMask_(top_left)
            view.addSubview_(self.now_glyph)

            # Right column, top to bottom: state · title · channel, seek row, transport row.
            # Stacked from the card's top edge down, each row's height and gap spelled out, so the
            # rows cannot collide: 14 + 2 + 22 + 2 + 16 = 56 of text, 6 gap, 18 seek, 8 gap, 32 buttons.
            TX = AX + art_w + 18                 # text/controls column
            TR = CX + CW - IN                    # its right edge (tracks the card)
            text_w = TR - TX
            HEAD_Y = CY + CH - IN - 14
            TITLE_Y = HEAD_Y - 2 - 22
            CHAN_Y = TITLE_Y - 2 - 16
            SEEK_Y = CHAN_Y - 6 - 18
            ROW_Y = SEEK_Y - 8 - 32
            self.now_head = _label(NSMakeRect(TX, HEAD_Y, text_w, 14), "대기 중", 10, True, LIME, top_wide)
            view.addSubview_(self.now_head)
            self.now_title = _label(
                NSMakeRect(TX, TITLE_Y, text_w, 22),
                "재생 중인 영상이 없습니다", 16, True, SNOW, top_wide,
            )
            self.now_title.cell().setLineBreakMode_(4)  # NSLineBreakByTruncatingTail
            view.addSubview_(self.now_title)
            self.now_channel = _label(
                NSMakeRect(TX, CHAN_Y, text_w, 16),
                "유튜브에서 영상을 열고 ▶ 를 누르거나, 파일을 끌어다 놓으십시오", 12, False, GHOST, top_wide,
            )
            self.now_channel.cell().setLineBreakMode_(4)
            view.addSubview_(self.now_channel)

            # Seek row: elapsed | bar | total. The bar tracks the card's width.
            self.elapsed_lab = _label(NSMakeRect(TX, SEEK_Y + 1, 52, 16), "0:00", 11, False, GHOST, top_left)
            self.elapsed_lab.setFont_(NSFont.monospacedDigitSystemFontOfSize_weight_(11, 0))
            view.addSubview_(self.elapsed_lab)
            self.remain_lab = _label(NSMakeRect(TR - 52, SEEK_Y + 1, 52, 16), "--:--", 11, False, GHOST, top_right)
            self.remain_lab.setFont_(NSFont.monospacedDigitSystemFontOfSize_weight_(11, 0))
            self.remain_lab.setAlignment_(2)
            view.addSubview_(self.remain_lab)
            self.seek_bar = SeekSlider.alloc().initWithFrame_(
                NSMakeRect(TX + 56, SEEK_Y, text_w - 112, 18)
            )
            self.seek_bar.setMinValue_(0.0)
            self.seek_bar.setMaxValue_(1.0)
            self.seek_bar.setDoubleValue_(0.0)
            self.seek_bar.setContinuous_(True)
            self.seek_bar.setEnabled_(True)
            self.seek_bar.setTarget_(self)
            self.seek_bar.ctrl = self
            self.seek_bar.setAction_("seek:")
            self.seek_bar.setAutoresizingMask_(top_wide)
            _describe(self.seek_bar, "덱 재생 위치 (←/→ 5초, ⇧ 30초)")
            view.addSubview_(self.seek_bar)

            # Transport, left-aligned under the title. Play is the one big button.

            def tbtn(x, w, title, action, fill=CARD, ink=SNOW, symbol="", h=32):
                btn = NSButton.alloc().initWithFrame_(NSMakeRect(x, ROW_Y, w, h))
                btn.setTitle_(title)
                btn.setFont_(NSFont.boldSystemFontOfSize_(11))
                _pill(btn, fill, ink)
                if symbol:
                    _icon(btn, symbol, ink)
                btn.setTarget_(self)
                btn.setAction_(action)
                btn.setAutoresizingMask_(top_left)
                view.addSubview_(btn)
                return btn

            x = TX
            self.prev_btn = tbtn(x, 36, "⏮", "prevTrack:", symbol="backward.end.fill"); x += 36 + 6
            self.play_btn = tbtn(x, 56, "▶", "play:", LIME, INK, "play.fill"); x += 56 + 6
            self.play_btn.setKeyEquivalent_("\r")
            # Esc is not ■: it is the "cancel editing" key in the URL field and in web inputs,
            # and as a window key equivalent it stopped the deck from there. ⌘. is in the menu.
            self.stop_btn = tbtn(x, 36, "■", "stop:", symbol="stop.fill"); x += 36 + 6
            self.next_btn = tbtn(x, 36, "⏭", "nextTrack:", symbol="forward.end.fill"); x += 36 + 16
            self.shuffle_btn = tbtn(x, 36, "셔플", "toggleShuffle:", symbol="shuffle"); x += 36 + 6
            self.repeat_btn = tbtn(x, 36, "반복", "cycleRepeat:", symbol="repeat"); x += 36 + 16
            _describe(self.prev_btn, "이전 곡")
            _describe(self.play_btn, "덱에서 재생 / 일시정지 (↩ 또는 스페이스)")
            _describe(self.stop_btn, "덱 재생 멈춤 (⌘.)")
            _describe(self.next_btn, "다음 곡")
            _describe(self.shuffle_btn, "셔플 켜기/끄기")

            # Volume on the right of the transport row; the settings disclosure at the far right.
            self.settings_btn = NSButton.alloc().initWithFrame_(NSMakeRect(TR - 32, ROW_Y, 32, 32))
            _pill(self.settings_btn, CARD, SNOW)
            _icon(self.settings_btn, "slider.horizontal.3", SNOW)
            self.settings_btn.setTarget_(self)
            self.settings_btn.setAction_("toggleSettings:")
            self.settings_btn.setAutoresizingMask_(top_right)
            _describe(self.settings_btn, "덱 설정 (스튜디오 · 브리지 · 화면 · 여백 · 버튼 불투명도)")
            view.addSubview_(self.settings_btn)
            vol_w = max(80, min(160, TR - 32 - 12 - 32 - 6 - x))
            self.volume_bar = NSSlider.alloc().initWithFrame_(
                NSMakeRect(TR - 32 - 12 - vol_w, ROW_Y + 5, vol_w, 22)
            )
            self.volume_bar.setMinValue_(0.0)
            self.volume_bar.setMaxValue_(1.0)
            self.volume_bar.setDoubleValue_(self.volume)
            self.volume_bar.setContinuous_(True)
            self.volume_bar.setTarget_(self)
            self.volume_bar.setAction_("setVolume:")
            self.volume_bar.setAutoresizingMask_(top_right)
            _describe(self.volume_bar, "덱 소리 크기 (↑/↓)")
            view.addSubview_(self.volume_bar)
            self.mute_btn = tbtn(TR - 32 - 12 - vol_w - 6 - 32, 32, "음소거", "toggleMute:",
                                 symbol="speaker.wave.2.fill")
            self.mute_btn.setAutoresizingMask_(top_right)
            # Their final frames come from `_gui_card_row`, on every resize.

            # ---- queue column --------------------------------------------------------------
            frost = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(QX, QY, QW, QH))
            frost.setMaterial_(7)
            frost.setBlendingMode_(0)
            frost.setState_(1)
            frost.setWantsLayer_(True)
            frost.layer().setCornerRadius_(12.0)
            frost.layer().setMasksToBounds_(True)
            frost.setAutoresizingMask_(right_col)
            view.addSubview_(frost)
            pane = QueueDrop.alloc().initWithFrame_(frost.bounds())
            pane.ctrl = self
            pane.registerForDraggedTypes_([NSFilenamesPboardType])
            pane.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
            frost.addSubview_(pane)
            self.queue_pane = frost

            QIN = 14
            head_y = QY + QH - QIN - 16
            self.queue_head = _label(
                NSMakeRect(QX + QIN, head_y, QW - QIN * 2 - 64, 16),
                "대기열", 12, True, SNOW, top_right,
            )
            view.addSubview_(self.queue_head)

            def qbtn(x, y, w, title, action, symbol, tip, mask):
                btn = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, w, 28))
                btn.setTitle_(title)
                btn.setFont_(NSFont.boldSystemFontOfSize_(11))
                _pill(btn, CARD, SNOW)
                if symbol:
                    _icon(btn, symbol, SNOW)
                btn.setTarget_(self)
                btn.setAction_(action)
                btn.setAutoresizingMask_(mask)
                _describe(btn, tip)
                view.addSubview_(btn)
                return btn

            # "+" sits on the header line: adding is the main queue action.
            self.add_btn = qbtn(QX + QW - QIN - 60, head_y - 6, 60, " 추가", "addToPlaylist:", "plus",
                                "보고 있는 페이지를 대기열에 넣기", top_right)
            self.add_btn.setImagePosition_(2)

            # Settings drawer: rarely used deck controls, collapsed by default, at the queue's top.
            DRAWER_H = 104
            self.settings_h = DRAWER_H
            drawer_top = head_y - 14
            dy = drawer_top - 28
            half = (QW - QIN * 2 - 8) / 2
            self.studio_btn = qbtn(QX + QIN, dy, half, "스튜디오", "studio:", "",
                                   "Studio 복사본과 브리지를 켭니다. 키가 없을 때 누르십시오", top_right)
            self.bridge_btn = qbtn(QX + QIN + half + 8, dy, half, "브리지", "bridge:", "",
                                   "브리지만 켭니다. Studio 없이 영상만 보냅니다", top_right)
            dy -= 34
            self.fit_btn = qbtn(QX + QIN, dy, half, fit_label(self.fit), "cycleFit:", "",
                                "다음 재생부터: 자동 → 맞춤(레터박스) → 채움(잘라내기)", top_right)
            self.crop_btn = qbtn(QX + QIN + half + 8, dy, half, crop_label(self.crop), "cycleCrop:", "",
                                 "다음 재생부터: 검은 여백 자동 제거 / 원본 유지", top_right)
            dy -= 30
            self.overlay_lab = _label(
                NSMakeRect(QX + QIN, dy + 4, 118, 16), "Studio 버튼 불투명도", 10, False, GHOST, top_right,
            )
            view.addSubview_(self.overlay_lab)
            self.overlay_bar = NSSlider.alloc().initWithFrame_(
                NSMakeRect(QX + QIN + 122, dy + 1, QW - QIN * 2 - 122, 22)
            )
            self.overlay_bar.setMinValue_(0.0)
            self.overlay_bar.setMaxValue_(1.0)
            self.overlay_bar.setDoubleValue_(self.overlay)
            self.overlay_bar.setContinuous_(True)
            self.overlay_bar.setTarget_(self)
            self.overlay_bar.setAction_("setOverlay:")
            self.overlay_bar.setAutoresizingMask_(top_right)
            _describe(self.overlay_bar, "영상 위 Studio 버튼 불투명도 (오른쪽이 진함)")
            _describe(self.overlay_lab, "영상 위 Studio 버튼 불투명도")
            view.addSubview_(self.overlay_bar)
            self.settings_views = [
                self.studio_btn, self.bridge_btn, self.fit_btn, self.crop_btn,
                self.overlay_lab, self.overlay_bar,
            ]
            if not studio_installed():
                self.studio_btn.setEnabled_(False)
                _describe(self.studio_btn, "Ulanzi Studio가 설치되어 있지 않습니다. 브리지를 쓰십시오")

            # The list, and the row actions under it.
            BTN_H = 28
            table_y = QY + QIN + BTN_H + 8
            scroll = NSScrollView.alloc().initWithFrame_(
                NSMakeRect(QX + QIN - 6, table_y, QW - QIN * 2 + 12, head_y - 10 - table_y)
            )
            scroll.setHasVerticalScroller_(True)
            scroll.setAutohidesScrollers_(True)
            scroll.setBorderType_(0)
            scroll.setDrawsBackground_(False)
            scroll.setAutoresizingMask_(NSViewMinXMargin | NSViewHeightSizable)
            self.queue_scroll = scroll
            self.playlist_table = NSTableView.alloc().initWithFrame_(scroll.contentView().bounds())
            track_col = NSTableColumn.alloc().initWithIdentifier_("track")
            track_col.setWidth_(QW - QIN * 2 + 12)
            track_col.setEditable_(False)
            self.playlist_table.addTableColumn_(track_col)
            self.playlist_table.setHeaderView_(None)
            self.playlist_table.setRowHeight_(QUEUE_ROW_H)
            self.playlist_table.setIntercellSpacing_((0, 2))
            self.playlist_table.setUsesAlternatingRowBackgroundColors_(False)
            self.playlist_table.setBackgroundColor_(NSColor.clearColor())
            try:
                self.playlist_table.setStyle_(4)  # NSTableViewStylePlain
            except Exception:
                pass
            try:
                self.playlist_table.setAppearance_(
                    NSAppearance.appearanceNamed_("NSAppearanceNameDarkAqua")
                )
            except Exception:
                pass
            self.playlist_table.setDataSource_(self)
            self.playlist_table.setDelegate_(self)
            # Empty selection is allowed so nothing is selected until the user or the deck picks a
            # row; a forced row 0 made ▶ play the queue head instead of the track just stopped.
            self.playlist_table.setAllowsEmptySelection_(True)
            self.playlist_table.setAllowsMultipleSelection_(False)
            self.playlist_table.setTarget_(self)
            self.playlist_table.setDoubleAction_("playSelected:")
            self.playlist_table.registerForDraggedTypes_(["ghostdeck.playlist.row", NSFilenamesPboardType])
            self.playlist_table.setDraggingSourceOperationMask_forLocal_(NSDragOperationMove, True)
            menu = NSMenu.alloc().init()
            for title, action in (
                ("재생", "playSelected:"),
                ("정지", "stop:"),
                ("대기열에서 제거", "removeSelected:"),
                ("위로", "moveUp:"),
                ("아래로", "moveDown:"),
            ):
                item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, "")
                item.setTarget_(self)
                menu.addItem_(item)
            self.playlist_table.setMenu_(menu)
            scroll.setDocumentView_(self.playlist_table)
            view.addSubview_(scroll)
            self.queue_empty = _label(
                NSMakeRect(QX + QIN, QY + QH / 2 - 30, QW - QIN * 2, 48),
                "대기열이 비어 있습니다\n페이지의 ＋ 대기열 또는 여기로 파일을 끌어다 놓으십시오",
                11, False, GHOST, NSViewMinXMargin | NSViewMinYMargin | NSViewMaxYMargin,
            )
            self.queue_empty.setAlignment_(1)
            self.queue_empty.cell().setWraps_(True)
            view.addSubview_(self.queue_empty)

            third = (QW - QIN * 2 - 16) / 3
            bottom = NSViewMinXMargin | NSViewMaxYMargin
            self.up_btn = qbtn(QX + QIN, QY + QIN, third, "↑", "moveUp:", "chevron.up", "위로 (⌥↑)", bottom)
            self.down_btn = qbtn(QX + QIN + third + 8, QY + QIN, third, "↓", "moveDown:", "chevron.down",
                                 "아래로 (⌥↓)", bottom)
            self.del_btn = qbtn(QX + QIN + (third + 8) * 2, QY + QIN, third, "삭제", "removeSelected:", "trash",
                                "대기열에서 빼기 (⌫)", bottom)
            _gui_settings_draw(self, open_=False)
            _gui_playlist_draw(self)

            # ---- status footer -------------------------------------------------------------
            self.dot = _label(NSMakeRect(PAD, 7, 14, 16), "●", 11, False, LIME, NSViewMaxYMargin)
            view.addSubview_(self.dot)
            self.spinner = NSProgressIndicator.alloc().initWithFrame_(NSMakeRect(PAD, 8, 14, 14))
            self.spinner.setStyle_(1)  # NSProgressIndicatorStyleSpinning
            self.spinner.setControlSize_(1)  # NSControlSizeSmall
            self.spinner.setDisplayedWhenStopped_(False)
            self.spinner.setIndeterminate_(True)
            self.spinner.setAutoresizingMask_(NSViewMaxYMargin)
            view.addSubview_(self.spinner)
            self.status = _label(
                NSMakeRect(PAD + 18, 7, 300, 16), "상태 확인 중…", 11, False, GHOST, NSViewMaxYMargin,
            )
            view.addSubview_(self.status)
            self.reconnect_btn = NSButton.alloc().initWithFrame_(NSMakeRect(W - PAD - 76, 3, 76, 24))
            self.reconnect_btn.setTitle_("다시 연결")
            self.reconnect_btn.setFont_(NSFont.boldSystemFontOfSize_(11))
            _pill(self.reconnect_btn, CARD, SNOW)
            self.reconnect_btn.setTarget_(self)
            self.reconnect_btn.setAction_("reconnect:")
            _describe(self.reconnect_btn, "케이블을 다시 꽂은 뒤 덱을 다시 잡습니다")
            self.reconnect_btn.setAutoresizingMask_(NSViewMinXMargin | NSViewMaxYMargin)
            view.addSubview_(self.reconnect_btn)
            self.note = _label(
                NSMakeRect(PAD + 322, 7, W - PAD * 2 - 322 - 84, 16),
                "유튜브에서 영상을 열고 ▶ 를 누르십시오.",
                11, False, GHOST, NSViewWidthSizable | NSViewMaxYMargin,
            )
            self.note.cell().setLineBreakMode_(4)
            view.addSubview_(self.note)
            # The window, not the page, starts with the keyboard, so Space and the arrows reach the
            # player until the user clicks into the page or the address field.
            self.window.setInitialFirstResponder_(self.playlist_table)
            ctrl = self

            def on_key(event):
                if event.window() is not ctrl.window:
                    return event
                return None if ctrl.keyCommand_(event) else event

            self.key_monitor = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
                NSEventMaskKeyDown, on_key
            )
            _gui_backfill_meta(self)

            _gui_install_menu(self)
            # Remember size and place across launches -- restored only now. Every control above is
            # placed for LAYOUT_DEFAULT and follows later size changes through its autoresizing
            # mask; restoring the saved frame before they existed left them placed for 1000x860
            # inside a smaller view, so the toolbar and the card landed above the window's top edge.
            # The key lives in the shared `Python` defaults domain (no bundle id of our own).
            self.window.setFrameAutosaveName_("ghostdeck.main")
            _gui_card_row(self)
            _gui_settings_draw(self)
            self.window.makeKeyAndOrderFront_(None)
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                2.0, self, "poll:", None, True
            )
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                0.1, self, "tickPlayhead:", None, True
            )
            AppHelper.callAfter(lambda: _gui_kick(self, "status", ""))
            return self

        def validateMenuItem_(self, item):
            action = item.action()
            if action in ("reconnect:", "bridge:", "openFile:"):
                return not self.busy
            if action == "studio:":
                return not self.busy and studio_installed()
            if action == "back:":
                return bool(self.web.canGoBack())
            if action == "forward:":
                return bool(self.web.canGoForward())
            return True

        def back_(self, _sender):
            self.web.goBack_(None)

        def reconnect_(self, _sender):
            _gui_kick(self, "reconnect", "")

        def studio_(self, _sender):
            _gui_kick(self, "studio", "")

        def bridge_(self, _sender):
            _gui_kick(self, "bridge", "")

        def forward_(self, _sender):
            self.web.goForward_(None)

        def go_(self, _sender):
            raw = str(self.url_field.stringValue() or "").strip()
            local = media_path_candidate(raw)
            if local:
                _gui_kick(self, "play", local)
                return
            if not raw:
                return
            if "://" not in raw:
                raw = "https://" + raw
            url = NSURL.URLWithString_(raw)
            if url is None:
                return
            self.web.loadRequest_(NSURLRequest.requestWithURL_(url))

        def openFile_(self, _sender):
            panel = NSOpenPanel.openPanel()
            panel.setCanChooseFiles_(True)
            panel.setCanChooseDirectories_(False)
            panel.setAllowsMultipleSelection_(True)
            panel.setAllowedFileTypes_([suffix[1:] for suffix in _MEDIA_SUFFIXES])
            if panel.runModal() != 1:
                return
            sources = []
            for item in list(panel.URLs() or []):
                source = media_path_candidate(str(item.path()))
                if source:
                    sources.append(source)
            if not sources:
                self.note.setStringValue_("재생할 수 있는 영상이 아닙니다.")
                return
            for source in sources:
                _gui_playlist_put(self, source)
            self.url_field.setStringValue_(sources[0])
            _gui_kick(self, "play", sources[0])

        def reload_(self, _sender):
            self.web.reload_(None)

        def focusUrl_(self, _sender):
            self.window.makeFirstResponder_(self.url_field)

        def play_(self, _sender):
            if deck_has_picture():
                self.stop_(_sender)
                return
            from ghostdeck import play as playmod
            if opening_session(
                playing=playmod.playing(),
                has_picture=False,
                session_active=deck_session_active(),
            ):
                self.note.setStringValue_(play_success_note(False))
                return
            items = getattr(self, "playlist", [])
            row = -1
            table = getattr(self, "playlist_table", None)
            if table is not None:
                row = int(table.selectedRow())
            source = queue_play_source(
                items, row, deck_now_playing(),
                getattr(self, "seen_watch", "") or getattr(self, "resume_source", ""),
            )
            if source:
                start = 0.0
                crop = play_crop_choice(getattr(self, "crop", "auto"))
                if playlist_identity(source) == playlist_identity(getattr(self, "resume_source", "")):
                    start = play_offset(getattr(self, "resume_pos", 0.0))
                    if crop == "auto":
                        crop = deck_crop() or "auto"
                self.resume_pos = 0.0
                self.user_stopped = False
                _gui_kick(self, "play", source, start=start, crop=crop)
                return
            ctrl = self
            field = str(self.url_field.stringValue() or "")
            local = media_path_candidate(field)
            if local:
                _gui_kick(ctrl, "play", local)
                return

            def after(raw, _err):
                page, start = parse_watch_payload(raw)
                page = page or _gui_href(ctrl)
                source, off = play_request(field, page, "", read_pasteboard(), start)
                if source:
                    _gui_kick(ctrl, "play", source, start=off)
                    return
                ctrl.note.setStringValue_("이 페이지에서 영상을 찾지 못했습니다.")

            self.web.evaluateJavaScript_completionHandler_(
                "(window.__ghostdeckNow ? window.__ghostdeckNow() : window.location.href)",
                after,
            )

        def stop_(self, _sender):
            source, pos, active = deck_playhead()
            if active and source:
                self.resume_pos = pos
                self.resume_source = source
            self.user_stopped = True
            _gui_kick(self, "stop", "")
            self.web.evaluateJavaScript_completionHandler_(
                "document.querySelectorAll('video').forEach(function(v){v.pause()}); null",
                lambda *_a: None,
            )

        def poll_(self, _timer):
            if not self.busy:
                _gui_kick(self, "status", "")

        def tickPlayhead_(self, _timer):
            _gui_playhead_draw(self)

        def seek_(self, sender):
            duration = float(getattr(self, "media_duration", 0.0) or 0.0)
            if duration <= 0:
                return
            at = play_offset(sender.doubleValue())
            if at > duration:
                at = duration
            source = getattr(self, "seen_watch", "") or deck_now_playing()
            if not source:
                self.note.setStringValue_("재생 중인 영상이 없습니다.")
                return
            self.user_stopped = False
            self.hold_pos = at
            self.hold_until = time.monotonic() + 2.0
            if deck_has_picture() and request_live_seek(at):
                self.note.setStringValue_(seek_note(at, True))
                return
            crop = play_crop_choice(getattr(self, "crop", "auto"))
            if crop == "auto":
                crop = deck_crop() or "auto"
            _gui_kick(self, "play", source, start=at, crop=crop)
            self.note.setStringValue_(seek_note(at, False))

        def setVolume_(self, sender):
            self.volume = clamp_volume(sender.doubleValue())
            if self.volume > 0:
                self.muted = False
            _commit_audio(self)

        def toggleMute_(self, _sender):
            self.muted = not bool(getattr(self, "muted", False))
            _commit_audio(self)

        def setOverlay_(self, sender):
            self.overlay = clamp_volume(sender.doubleValue())
            _commit_overlay(self)

        def cycleFit_(self, _sender):
            modes = list(_FIT_MODES)
            cur = play_fit(getattr(self, "fit", "auto"))
            self.fit = modes[(modes.index(cur) + 1) % len(modes)]
            _save_prefs(self)
            _gui_mode_draw(self)

        def cycleCrop_(self, _sender):
            modes = list(_CROP_MODES)
            cur = play_crop_choice(getattr(self, "crop", "auto"))
            self.crop = modes[(modes.index(cur) + 1) % len(modes)]
            _save_prefs(self)
            _gui_mode_draw(self)

        def toggleSettings_(self, _sender):
            _gui_settings_draw(self, open_=not bool(getattr(self, "settings_open", False)))

        def seekBy_(self, delta):
            """Move the deck's playhead by `delta` seconds, through the same path as the bar."""
            duration = float(getattr(self, "media_duration", 0.0) or 0.0)
            _source, pos, active = deck_playhead()
            if not active or duration <= 0:
                return False
            self.seek_bar.setDoubleValue_(clamp_seek(pos, delta, duration))
            self.seek_(self.seek_bar)
            _gui_playhead_draw(self)
            return True

        def keyCommand_(self, event):
            """Player keys, active only when no text field or web input has the keyboard.

            Space plays/pauses, ←/→ seek 5s (⇧ 30s), J/L seek 10s, ↑/↓ set volume, M mutes, N/P
            change track. Returns True when the key was handled. A key typed into the URL field or
            into a web page's input is never taken: typing there must keep working.
            """
            responder = self.window.firstResponder()
            # The URL field's editor and anything inside the web view own their own keys.
            if responder is not None and responder.isKindOfClass_(NSText):
                return False
            if responder is not None and responder.isKindOfClass_(NSView) and responder.isDescendantOf_(self.web):
                return False
            flags = int(event.modifierFlags())
            if flags & (NSEventModifierFlagCommand | NSEventModifierFlagOption | NSEventModifierFlagControl):
                return False
            shift = bool(flags & NSEventModifierFlagShift)
            key = {123: "left", 124: "right", 125: "down", 126: "up", 49: "space"}.get(
                int(event.keyCode()), str(event.charactersIgnoringModifiers() or "").lower()
            )
            if key == "space":
                self.play_(None)
                return True
            step = seek_step(key, shift)
            if step:
                return self.seekBy_(step) or True
            if key in ("up", "down"):
                self.volume = volume_step(getattr(self, "volume", 1.0), key)
                if self.volume > 0:
                    self.muted = False
                _commit_audio(self)
                self.note.setStringValue_(f"소리 {int(round(self.volume * 100))}%")
                return True
            actions = {"m": self.toggleMute_, "n": self.nextTrack_, "p": self.prevTrack_}
            if key in actions:
                actions[key](None)
                return True
            return False

        def prevTrack_(self, _sender):
            now = deck_now_playing() or getattr(self, "seen_watch", "")
            nxt = playlist_prev(
                getattr(self, "playlist", []),
                now,
                repeat=getattr(self, "repeat", "off"),
                shuffle=getattr(self, "shuffle", False),
            )
            if not nxt:
                self.note.setStringValue_("이전 곡이 없습니다.")
                return
            self.user_stopped = False
            _gui_kick(self, "play", nxt)

        def nextTrack_(self, _sender):
            now = deck_now_playing() or getattr(self, "seen_watch", "")
            nxt = playlist_next(
                getattr(self, "playlist", []),
                now,
                repeat="all" if getattr(self, "repeat", "off") == "off" else getattr(self, "repeat", "off"),
                shuffle=getattr(self, "shuffle", False),
            )
            if not nxt:
                self.note.setStringValue_("다음 곡이 없습니다.")
                return
            self.user_stopped = False
            _gui_kick(self, "play", nxt)

        def toggleShuffle_(self, _sender):
            self.shuffle = not bool(getattr(self, "shuffle", False))
            _save_prefs(self)
            _gui_mode_draw(self)

        def cycleRepeat_(self, _sender):
            modes = list(_REPEAT_MODES)
            cur = getattr(self, "repeat", "off")
            self.repeat = modes[(modes.index(cur) + 1) % len(modes)] if cur in modes else "off"
            _save_prefs(self)
            _gui_mode_draw(self)

        def numberOfRowsInTableView_(self, _table):
            return len(getattr(self, "playlist", []))

        def tableView_viewForTableColumn_row_(self, table, _col, row):
            """A queue row: 16:9 still, bold title, channel · length. The playing row is lime."""
            items = getattr(self, "playlist", [])
            if row < 0 or row >= len(items):
                return None
            item = items[row]
            cell = table.makeViewWithIdentifier_owner_("queueRow", self)
            width = table.tableColumns()[0].width()
            if cell is None:
                cell = NSTableCellView.alloc().initWithFrame_(NSMakeRect(0, 0, width, QUEUE_ROW_H))
                cell.setIdentifier_("queueRow")
                art = NSImageView.alloc().initWithFrame_(NSMakeRect(6, 6, 71, 40))
                art.setImageScaling_(3)
                art.setWantsLayer_(True)
                art.layer().setCornerRadius_(5.0)
                art.layer().setMasksToBounds_(True)
                art.layer().setBackgroundColor_(CARD.CGColor())
                art.setTag_(1)
                cell.addSubview_(art)
                title = _label(NSMakeRect(86, 26, width - 92, 18), "", 12, True, SNOW, NSViewWidthSizable)
                title.cell().setLineBreakMode_(4)
                title.setTag_(2)
                cell.addSubview_(title)
                meta = _label(NSMakeRect(86, 8, width - 92, 16), "", 10, False, GHOST, NSViewWidthSizable)
                meta.cell().setLineBreakMode_(4)
                meta.setTag_(3)
                cell.addSubview_(meta)
            art, title, meta = (cell.viewWithTag_(tag) for tag in (1, 2, 3))
            on = playlist_playing(item, deck_now_playing(), getattr(self, "seen_watch", ""))
            title.setStringValue_(playlist_title(item))
            title.setTextColor_(LIME if on else SNOW)
            meta.setStringValue_(("▶ 재생 중 · " if on else "") + (playlist_meta(item) or " "))
            meta.setTextColor_(LIME if on else GHOST)
            art.setImage_(_thumb_image(self, playlist_source(item)))
            cell.setToolTip_(playlist_source(item))
            return cell

        def tableView_objectValueForTableColumn_row_(self, _table, _col, row):
            items = getattr(self, "playlist", [])
            if row < 0 or row >= len(items):
                return ""
            return playlist_label(items[row])

        def tableView_shouldEditTableColumn_row_(self, _table, _col, _row):
            return False

        def tableView_writeRowsWithIndexes_toPasteboard_(self, _table, indexes, pboard):
            row = int(indexes.firstIndex())
            pboard.declareTypes_owner_(["ghostdeck.playlist.row"], None)
            pboard.setString_forType_(str(row), "ghostdeck.playlist.row")
            return True

        def tableView_validateDrop_proposedRow_proposedDropOperation_(self, table, info, row, _op):
            types = list(info.draggingPasteboard().types() or [])
            if "ghostdeck.playlist.row" in types or NSFilenamesPboardType in types:
                table.setDropRow_dropOperation_(row, 1)
                return NSDragOperationMove if "ghostdeck.playlist.row" in types else NSDragOperationCopy
            return 0

        def tableView_acceptDrop_row_dropOperation_(self, _table, info, row, _op):
            pboard = info.draggingPasteboard()
            names = pboard.propertyListForType_(NSFilenamesPboardType) or []
            if names:
                for name in names:
                    source = media_path_candidate(str(name))
                    if source:
                        _gui_playlist_put(self, source)
                return True
            raw = pboard.stringForType_("ghostdeck.playlist.row")
            if raw is None or str(raw).strip() == "":
                return False
            src = int(str(raw).strip())
            dst = row
            if src < dst:
                dst -= 1
            self.playlist = playlist_move(getattr(self, "playlist", []), src, dst)
            playlist_save(PLAYLIST_PATH, self.playlist)
            _gui_playlist_draw(self)
            return True

        def moveUp_(self, _sender):
            row = int(self.playlist_table.selectedRow())
            if row <= 0:
                return
            self.playlist = playlist_move(getattr(self, "playlist", []), row, row - 1)
            playlist_save(PLAYLIST_PATH, self.playlist)
            _gui_playlist_draw(self)
            self.playlist_table.selectRowIndexes_byExtendingSelection_(
                NSIndexSet.indexSetWithIndex_(row - 1), False
            )

        def moveDown_(self, _sender):
            row = int(self.playlist_table.selectedRow())
            items = getattr(self, "playlist", [])
            if row < 0 or row >= len(items) - 1:
                return
            self.playlist = playlist_move(items, row, row + 1)
            playlist_save(PLAYLIST_PATH, self.playlist)
            _gui_playlist_draw(self)
            self.playlist_table.selectRowIndexes_byExtendingSelection_(
                NSIndexSet.indexSetWithIndex_(row + 1), False
            )

        def addToPlaylist_(self, _sender):
            ctrl = self

            def after(raw, _err):
                page, _start = parse_watch_payload(raw)
                page = page or _gui_href(ctrl)
                _gui_queue_href(ctrl, page)


            self.web.evaluateJavaScript_completionHandler_(
                "(window.__ghostdeckNow ? window.__ghostdeckNow() : window.location.href)",
                after,
            )

        def playSelected_(self, _sender):
            items = getattr(self, "playlist", [])
            table = self.playlist_table
            row = playlist_click_row(table.clickedRow(), table.selectedRow(), len(items))
            if row < 0:
                self.note.setStringValue_("재생할 항목을 고르십시오.")
                return
            source = playlist_source(items[row])
            from ghostdeck import play as playmod
            same = playlist_identity(source) == playlist_identity(deck_now_playing())
            if opening_session(
                playing=playmod.playing(),
                has_picture=deck_has_picture(),
                session_active=deck_session_active(),
            ) and same:
                self.note.setStringValue_(play_success_note(False))
                return
            self.user_stopped = False
            _gui_kick(self, "play", source)

        def removeSelected_(self, _sender):
            row = int(self.playlist_table.selectedRow())
            self.playlist = playlist_remove(getattr(self, "playlist", []), row)
            playlist_save(PLAYLIST_PATH, self.playlist)
            _gui_playlist_draw(self)

        def userContentController_didReceiveScriptMessage_(self, _ucc, message):
            body = message.body()
            kind = ""
            href = ""
            src = ""
            start = 0.0
            try:
                kind = str(body.objectForKey_("type") or "")
                href = str(body.objectForKey_("url") or "")
                src = str(body.objectForKey_("src") or "")
                start = play_offset(body.objectForKey_("t"))
            except Exception:
                if isinstance(body, dict):
                    kind = str(body.get("type") or "")
                    href = str(body.get("url") or "")
                    src = str(body.get("src") or "")
                    start = play_offset(body.get("t"))
            href = href or _gui_href(self)
            if kind == "queue":
                _gui_queue_href(self, href)
                return
            if kind == "play" or kind == "nav":
                return
            _gui_follow(self, href, start=start)

        def webView_didCommitNavigation_(self, webView, _nav):
            if webView is not self.web:
                return
            if media_path_candidate(str(self.url_field.stringValue() or "")):
                return
            url = webView.URL()
            if url is not None:
                self.url_field.setStringValue_(str(url.absoluteString()))

        def webView_decidePolicyForNavigationAction_decisionHandler_(self, _webView, _action, handler):
            handler(1)

        def webView_createWebViewWithConfiguration_forNavigationAction_windowFeatures_(
            self, _webView, configuration, navigationAction, _features
        ):
            page = configuration.defaultWebpagePreferences()
            if page is not None:
                page.setPreferredContentMode_(0)
            popup = WKWebView.alloc().initWithFrame_configuration_(
                NSMakeRect(0, 0, 560, 720),
                configuration,
            )
            popup.setUIDelegate_(self)
            popup.setNavigationDelegate_(self)
            win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, 560, 720),
                NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskResizable,
                NSBackingStoreBuffered,
                False,
            )
            win.setTitle_("ghostdeck — 덱 플레이어")
            win.contentView().addSubview_(popup)
            popup.setFrame_(win.contentView().bounds())
            popup.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
            win.center()
            win.makeKeyAndOrderFront_(None)
            NSApp.activateIgnoringOtherApps_(True)
            self.popups.append((win, popup))
            request = navigationAction.request() if navigationAction is not None else None
            if request is not None:
                popup.loadRequest_(request)
            return popup

        def webViewDidClose_(self, webView):
            kept = []
            for win, popup in self.popups:
                if popup is webView:
                    win.close()
                else:
                    kept.append((win, popup))
            self.popups = kept


        def windowDidResize_(self, _notification):
            _gui_card_row(self)
            _gui_settings_draw(self)

        def windowWillClose_(self, _notification):
            try:
                self.ucc.removeScriptMessageHandlerForName_("ghostdeck")
            except Exception:
                pass
            NSApp.terminate_(None)

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(0)
    controller = Controller.alloc().init()
    app.setDelegate_(controller)
    controller.window.setDelegate_(controller)
    app.activateIgnoringOtherApps_(True)
    AppHelper.runEventLoop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

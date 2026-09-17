"""Host remote for the D200. A native window that runs the CLI; not a player."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import Request, urlopen

from ghostdeck import studio

_SHIM_UP = re.compile(r"(?:^|\s)shim=up(?:\s|$)")
# The fields `status` prints. Parsed by name so a new field cannot be mistaken for a value.
_STATUS_FIELDS = ("usb", "shim", "copy", "playing")
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
PLAYLIST_PATH = Path.home() / ".ghostdeck" / "playlist.json"


@dataclass(frozen=True)
class CommandResult:
    argv: list[str]
    code: int
    stdout: str
    stderr: str

    @property
    def detail(self) -> str:
        text = (self.stderr or self.stdout).strip()
        if text:
            return text.splitlines()[-1]
        return f"exit {self.code}"


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
    timeout = {"studio": 180.0, "play": 30.0, "bridge": 90.0, "stop": 60.0}.get(argv[0] if argv else "", 20.0)
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


def status_text(status_stdout: str) -> str:
    """A one-line summary for the window, in Korean.

    The CLI's raw line is `usb=adb shim=up copy=yes playing=no`, which reads as a debug dump in a
    window someone is using to watch a video. The two facts that matter are where the deck is and
    whether it is playing; a bridge that is down is worth saying only when it is, because that is the
    state in which pressing 재생 will start it.
    """
    fields = parse_status_fields(status_stdout)
    if not fields:
        return "상태를 읽지 못했습니다"
    piece = [_USB_TEXT.get(fields.get("usb", ""), f"덱 {fields.get('usb')}")]
    piece.append("재생 중" if fields.get("playing") == "yes" else "멈춤")
    if fields.get("shim") == "down":
        piece.append("브리지 꺼짐")
    return " · ".join(piece)


def status_dot_color(status_stdout: str):
    """An `NSColor` for the status dot, or None when AppKit is not loadable.

    Colour is the part that is read without looking: green only while the deck is actually playing,
    red when there is no usable deck, amber when the deck is there but the bridge is down (the one
    state where pressing 재생 has something to do), and grey when it is simply idle and ready.
    """
    fields = parse_status_fields(status_stdout)
    if not fields:
        return None
    try:
        from AppKit import NSColor
    except ImportError:
        return None
    if fields.get("usb", "").startswith("none") or fields.get("usb") == "unknown":
        return NSColor.systemRedColor()
    if fields.get("playing") == "yes":
        return NSColor.systemGreenColor()
    if fields.get("shim") == "down":
        return NSColor.systemOrangeColor()
    return NSColor.secondaryLabelColor()

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


def playlist_entry(source: str, title: str = "", channel: str = "") -> dict[str, str]:
    return {
        "source": (source or "").strip(),
        "title": (title or "").strip(),
        "channel": (channel or "").strip(),
    }


def playlist_source(item) -> str:
    if isinstance(item, dict):
        return str(item.get("source") or "").strip()
    return str(item or "").strip()


def playlist_normalize(items) -> list[dict[str, str]]:
    out = []
    for item in items or []:
        if isinstance(item, dict):
            source = str(item.get("source") or "").strip()
            if source:
                out.append(playlist_entry(source, item.get("title") or "", item.get("channel") or ""))
        else:
            source = str(item).strip()
            if source:
                out.append(playlist_entry(source))
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


def playlist_add(items, source: str, title: str = "", channel: str = "") -> list[dict[str, str]]:
    """Append a playable source. Consecutive duplicates are ignored."""
    source = (source or "").strip()
    out = playlist_normalize(items)
    if not source:
        return out
    if out and playlist_source(out[-1]) == source:
        last = dict(out[-1])
        if title and not last.get("title"):
            last["title"] = title.strip()
        if channel and not last.get("channel"):
            last["channel"] = channel.strip()
        out[-1] = last
        return out
    return out + [playlist_entry(source, title, channel)]


def playlist_extend(items, entries) -> list[dict[str, str]]:
    """Append many sources, skipping anything already queued."""
    out = playlist_normalize(items)
    seen = {playlist_source(item) for item in out}
    for entry in playlist_normalize(entries):
        src = playlist_source(entry)
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


def playlist_advance(items, current: str) -> str:
    """The next queued source after `current`, or empty at the end."""
    items = playlist_normalize(items)
    current = (current or "").strip()
    sources = [playlist_source(item) for item in items]
    if not sources:
        return ""
    if current in sources:
        nxt = sources.index(current) + 1
        return sources[nxt] if nxt < len(sources) else ""
    return sources[0]


def playlist_should_loop(source: str, items) -> bool:
    """A queue plays through. A single file still loops (CLI default)."""
    items = playlist_normalize(items)
    if len(items) > 1:
        return False
    return play_should_loop(source)


def playlist_find(items, source: str) -> dict[str, str]:
    source = (source or "").strip()
    for item in playlist_normalize(items):
        if playlist_source(item) == source:
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
    """The source the player last published, or empty. No HID, no subprocess."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return ""
    if not isinstance(data, dict):
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
            now = float(started) + float(elapsed)
            if now >= float(first):
                pos = start + (now - float(first)) / 1e9 * rate
    except (TypeError, ValueError):
        pos = start
    if not math.isfinite(pos) or pos < 0:
        pos = 0.0
    return source, pos, active


def source_duration(source: str, probe=None) -> float:
    """Length in seconds. 0 if unknown. Never HID."""
    source = (source or "").strip()
    if not source:
        return 0.0
    runner = subprocess.run if probe is None else probe
    watch = youtube_watch_url(source)
    local = media_path_candidate(source)
    try:
        if watch:
            result = runner(
                ["yt-dlp", "--no-warnings", "--no-playlist", "-O", "duration", watch],
                capture_output=True, text=True, timeout=20, check=False,
            )
            lines = (result.stdout or "").strip().splitlines()
            raw = lines[-1] if lines else ""
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
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value) or value <= 0:
        return 0.0
    return value





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
    """When the page changes: (new_seen, play_url | 'stop' | ''). Same YouTube id is a no-op.

    Stop only if the current source is a YouTube watch. A local file playing while the
    window sits on youtube.com would otherwise look like "left the video" and halt the deck.
    """
    if youtube_playlist_page(href):
        return seen_watch, ""
    watch = youtube_watch_url(href)
    if watch == seen_watch:
        return seen_watch, ""
    if watch:
        return watch, watch
    if youtube_watch_url(seen_watch):
        return "", "stop"
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

    def play(self, source: str, pasteboard: str = "", start: float = 0.0, loop: bool = True) -> list[CommandResult]:
        source = resolve_source(source, pasteboard)
        if not source:
            return [CommandResult(["play"], 2, "", "유튜브에서 영상을 열거나 파일을 연 다음 재생을 누르십시오")]
        results: list[CommandResult] = []
        st = self._run(["status"])
        results.append(st)
        if not shim_is_up(st.stdout):
            results.append(self._run(["studio"]))
            if results[-1].code != 0:
                return results
        argv = ["play", source]
        start = play_offset(start)
        if start > 0:
            argv.extend(["--start", f"{start:.3f}"])
        if not loop:
            argv.append("--no-loop")
        played = self._run(argv)
        results.append(played)
        # `shim=up` said the copy was running, so `studio` was skipped -- but the bridge it needs was
        # gone, and `play` refused. Start it now and retry once: this is the same recovery the
        # shim-down branch already does, reached from the failure instead of from `status`.
        if played.code != 0 and bridge_down(played.detail):
            results.append(self._run(["studio"]))
            if results[-1].code == 0:
                results.append(self._run(argv))
        return results


def _busy_call(remote: DeckRemote, op: str, source: str, pasteboard: str, done, start=0.0, loop=True) -> None:
    try:
        if op == "status":
            results = [remote.status()]
        elif op == "stop":
            results = [remote.stop()]
        else:
            results = remote.play(source, pasteboard, start=start, loop=loop)
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
            NSFilenamesPboardType,
            NSFont,
            NSMakeRect,
            NSMenu,
            NSMenuItem,
            NSObject,
            NSOpenPanel,
            NSScrollView,
            NSSlider,
            NSTableColumn,
            NSTableView,
            NSBezelBorder,
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
  function hook(v){
    if (v.__ghostdeck) return;
    v.__ghostdeck = true;
    v.addEventListener('play', function(){ post('play'); });
  }
  function scan(){ document.querySelectorAll('video').forEach(hook); }
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
  new MutationObserver(function(){ scan(); mountQueue(); }).observe(document.documentElement, {childList:true, subtree:true});
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


    def _gui_href(ctrl) -> str:
        url = ctrl.web.URL()
        return str(url.absoluteString()) if url is not None else ""

    def _gui_set_busy(ctrl, on: bool) -> None:
        ctrl.busy = on
        # The window is a remote for a CLI that takes seconds per press (a `play` waits for the player
        # to survive its grace window). Without this the buttons looked inert and were re-pressable
        # while a command was still in flight, which the log showed as stacked players.
        for name in ("play_btn", "stop_btn", "file_btn"):
            button = getattr(ctrl, name, None)
            if button is not None:
                button.setEnabled_(not on)
        bar = getattr(ctrl, "seek_bar", None)
        if bar is not None:
            bar.setEnabled_(
                (not on) and float(getattr(ctrl, "media_duration", 0) or 0) > 0
            )

    def _gui_apply(ctrl, results, error) -> None:
        _gui_set_busy(ctrl, False)
        pending = getattr(ctrl, "pending_source", "")
        pending_start = getattr(ctrl, "pending_start", 0.0)
        ctrl.pending_source = ""
        ctrl.pending_start = 0.0
        if error is not None:
            ctrl.seen_watch = ""
            ctrl.note.setStringValue_(f"{type(error).__name__}: {error}")
            if pending and pending != getattr(ctrl, "seen_watch", ""):
                _gui_kick(ctrl, "play", pending, start=pending_start)
            return
        for item in results:
            if item.argv[:1] == ["status"] and item.stdout.strip():
                ctrl.status.setStringValue_(status_text(item.stdout))
                dot = getattr(ctrl, "dot", None)
                colour = status_dot_color(item.stdout)
                if dot is not None and colour is not None:
                    dot.setTextColor_(colour)
                _gui_sync_deck(ctrl, parse_status_fields(item.stdout).get("playing") == "yes")
        last = results[-1] if results else None
        if last is None:
            if pending and pending != getattr(ctrl, "seen_watch", ""):
                _gui_kick(ctrl, "play", pending, start=pending_start)
            return
        if last.code != 0:
            ctrl.seen_watch = ""
            ctrl.note.setStringValue_(last.detail)
        elif last.argv[:1] == ["stop"]:
            ctrl.seen_watch = ""
            ctrl.note.setStringValue_("멈췄습니다.")
        elif last.argv[:1] == ["play"]:
            if len(last.argv) > 1:
                ctrl.seen_watch = last.argv[1]
                _gui_playlist_put(ctrl, last.argv[1])
            ctrl.note.setStringValue_("덱에서 재생 중입니다. 창이 멈추거나 끊겨도 덱은 계속 재생됩니다.")
        elif last.argv[:1] == ["studio"]:
            ctrl.note.setStringValue_("브리지를 켰습니다. 이제 재생할 수 있습니다.")
        if pending and pending != getattr(ctrl, "seen_watch", ""):
            _gui_kick(ctrl, "play", pending, start=pending_start)

    def _gui_kick(ctrl, op: str, source: str, start: float = 0.0, loop: bool = False) -> None:
        if ctrl.busy and op == "play":
            ctrl.pending_source = source
            ctrl.pending_start = play_offset(start)
            return
        if ctrl.busy and op != "status":
            return
        if op != "status":
            if op == "play":
                ctrl.seen_watch = source
            _gui_set_busy(ctrl, True)
            ctrl.note.setStringValue_("재생 준비 중…" if op == "play" else "멈추는 중…")
        pasteboard = read_pasteboard() if op == "play" else ""
        thread = threading.Thread(
            target=_busy_call,
            args=(
                remote,
                op,
                source,
                pasteboard,
                lambda r, e: AppHelper.callAfter(lambda: _gui_apply(ctrl, r, e)),
                play_offset(start),
                playlist_should_loop(source, getattr(ctrl, "playlist", [])) if op == "play" else loop,
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
        if playlist_find(ctrl.playlist, source).get("title"):
            return

        def fill():
            title, channel = source_identity(source)

            def apply():
                ctrl.playlist = playlist_add(getattr(ctrl, "playlist", []), source, title, channel)
                playlist_save(PLAYLIST_PATH, ctrl.playlist)
                _gui_playlist_draw(ctrl)

            AppHelper.callAfter(apply)

        threading.Thread(target=fill, daemon=True).start()


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


    def _gui_playlist_draw(ctrl) -> None:
        table = getattr(ctrl, "playlist_table", None)
        if table is not None:
            table.reloadData()
        items = getattr(ctrl, "playlist", [])
        now = deck_now_playing()
        found = playlist_find(items, now) if now else playlist_entry("")
        title = getattr(ctrl, "now_title", None)
        channel = getattr(ctrl, "now_channel", None)
        if title is not None:
            title.setStringValue_(playlist_title(found) if now else "재생 중인 영상이 없습니다")
        if channel is not None:
            channel.setStringValue_(playlist_subtitle(found) if now else "페이지에서 추가하거나 파일을 놓으십시오")
        heading = getattr(ctrl, "queue_head", None)
        if heading is not None:
            n = len(items)
            heading.setStringValue_(
                f"대기열 · {n}곡" if n else "대기열 · 영상을 끌어다 놓으십시오"
            )
        field = getattr(ctrl, "now_field", None)
        if field is not None:
            field.setStringValue_(playlist_label(found) if now else "없음")
        _gui_playhead_draw(ctrl)

    def _gui_playhead_draw(ctrl) -> None:
        bar = getattr(ctrl, "seek_bar", None)
        elapsed = getattr(ctrl, "elapsed_lab", None)
        remain = getattr(ctrl, "remain_lab", None)
        source, pos, _active = deck_playhead()
        if source and source != getattr(ctrl, "duration_for", ""):
            ctrl.duration_for = source
            ctrl.media_duration = 0.0
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
        if duration > 0 and pos > duration:
            pos = duration
        if elapsed is not None:
            elapsed.setStringValue_(format_clock(pos) if source else "0:00")
        if remain is not None:
            remain.setStringValue_(format_clock(duration) if duration else "--:--")
        if bar is None:
            return
        if duration <= 0:
            bar.setEnabled_(False)
            bar.setDoubleValue_(0.0)
            return
        bar.setMaxValue_(duration)
        bar.setDoubleValue_(pos)
        bar.setEnabled_(not getattr(ctrl, "busy", False))

    def _gui_sync_deck(ctrl, playing: bool) -> None:
        now = deck_now_playing()
        sources = [playlist_source(item) for item in getattr(ctrl, "playlist", [])]
        if now and now not in sources:
            _gui_playlist_put(ctrl, now)
        else:
            _gui_playlist_draw(ctrl)
        was = getattr(ctrl, "was_playing", False)
        if playing:
            ctrl.was_playing = True
            return
        if was and not getattr(ctrl, "user_stopped", False) and not ctrl.busy:
            nxt = playlist_advance(
                getattr(ctrl, "playlist", []),
                getattr(ctrl, "seen_watch", "") or now,
            )
            ctrl.was_playing = False
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
            self.was_playing = False
            self.user_stopped = False
            self.media_duration = 0.0
            self.duration_for = ""
            self.duration_busy = False
            # Dark shell: phone column + frosted queue.
            PAD = 20
            PHONE_W = 392
            SIDE_W = 372
            SIDE_GAP = 18
            W = PAD + PHONE_W + SIDE_GAP + SIDE_W + PAD
            H = 820
            FOOT = 26
            TOP = 36
            BAR = 38
            GAP = 8
            PHONE_X, PHONE_Y = PAD, FOOT
            PHONE_H = H - FOOT - TOP - BAR - GAP
            CHROME_Y = PHONE_Y + PHONE_H + GAP
            QUEUE_H = PHONE_H + GAP + BAR
            SIDE_X = PAD + PHONE_W + SIDE_GAP
            stick_top = NSViewMinXMargin | NSViewWidthSizable | NSViewMinYMargin
            stick_bot = NSViewMinXMargin | NSViewMaxYMargin
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
            self.window.setMinSize_((760, 560))
            self.window.setContentSize_((W, H))
            self.window.setBackgroundColor_(INK)
            try:
                self.window.setAppearance_(NSAppearance.appearanceNamed_("NSAppearanceNameDarkAqua"))
            except Exception:
                pass
            self.window.center()
            view = self.window.contentView()
            view.setWantsLayer_(True)
            view.layer().setBackgroundColor_(INK.CGColor())
            drop = DropBar.alloc().initWithFrame_(view.bounds())
            drop.ctrl = self
            drop.registerForDraggedTypes_([NSFilenamesPboardType])
            drop.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
            view.addSubview_(drop)

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

            shell = NSView.alloc().initWithFrame_(NSMakeRect(PHONE_X, PHONE_Y, PHONE_W, PHONE_H))
            shell.setWantsLayer_(True)
            shell.layer().setCornerRadius_(28.0)
            shell.layer().setMasksToBounds_(True)
            shell.layer().setBorderWidth_(1.0)
            shell.layer().setBorderColor_(HAIR.CGColor())
            shell.setAutoresizingMask_(NSViewHeightSizable)
            view.addSubview_(shell)
            self.web = WKWebView.alloc().initWithFrame_configuration_(
                NSMakeRect(0, 0, PHONE_W, PHONE_H),
                config,
            )
            self.web.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
            self.web.setUIDelegate_(self)
            self.web.setNavigationDelegate_(self)
            shell.addSubview_(self.web)
            self.web.loadRequest_(
                NSURLRequest.requestWithURL_(NSURL.URLWithString_("https://www.youtube.com"))
            )
            notch = NSView.alloc().initWithFrame_(NSMakeRect((PHONE_W - 108) / 2, PHONE_H - 16, 108, 5))
            notch.setWantsLayer_(True)
            notch.layer().setCornerRadius_(2.5)
            notch.layer().setBackgroundColor_(_rgb(0, 0, 0, 0.45).CGColor())
            notch.setAutoresizingMask_(NSViewMinYMargin)
            shell.addSubview_(notch)
            home = NSView.alloc().initWithFrame_(NSMakeRect((PHONE_W - 118) / 2, 8, 118, 4))
            home.setWantsLayer_(True)
            home.layer().setCornerRadius_(2.0)
            home.layer().setBackgroundColor_(_rgb(1, 1, 1, 0.28).CGColor())
            shell.addSubview_(home)

            def nav_btn(x, title, symbol, action):
                btn = NSButton.alloc().initWithFrame_(NSMakeRect(PHONE_X + x, CHROME_Y + 5, 28, 28))
                img = _symbol(symbol)
                if img is not None:
                    btn.setImage_(img)
                    btn.setBordered_(False)
                    btn.setContentTintColor_(SNOW)
                else:
                    btn.setTitle_(title)
                    btn.setBordered_(False)
                    btn.setFont_(NSFont.systemFontOfSize_(15))
                _pill(btn, CARD, SNOW)
                btn.setTarget_(self)
                btn.setAction_(action)
                btn.setAutoresizingMask_(NSViewMinYMargin)
                view.addSubview_(btn)
                return btn

            back = nav_btn(8, "‹", "chevron.left", "back:")
            back.setKeyEquivalent_("[")
            back.setKeyEquivalentModifierMask_(NSEventModifierFlagCommand)
            fwd = nav_btn(42, "›", "chevron.right", "forward:")
            fwd.setKeyEquivalent_("]")
            fwd.setKeyEquivalentModifierMask_(NSEventModifierFlagCommand)
            self.file_btn = NSButton.alloc().initWithFrame_(
                NSMakeRect(PHONE_X + 76, CHROME_Y + 5, 44, 28)
            )
            self.file_btn.setTitle_("파일")
            self.file_btn.setFont_(NSFont.boldSystemFontOfSize_(11))
            _pill(self.file_btn, CARD, SNOW)
            self.file_btn.setTarget_(self)
            self.file_btn.setAction_("openFile:")
            self.file_btn.setKeyEquivalent_("o")
            self.file_btn.setKeyEquivalentModifierMask_(NSEventModifierFlagCommand)
            self.file_btn.setAutoresizingMask_(NSViewMinYMargin)
            view.addSubview_(self.file_btn)
            self.url_field = NSTextField.alloc().initWithFrame_(
                NSMakeRect(PHONE_X + 128, CHROME_Y + 5, PHONE_W - 136, 28)
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
            self.url_field.setAutoresizingMask_(NSViewMinYMargin)
            view.addSubview_(self.url_field)

            frost = NSVisualEffectView.alloc().initWithFrame_(
                NSMakeRect(SIDE_X, PHONE_Y, SIDE_W, QUEUE_H)
            )
            frost.setMaterial_(7)
            frost.setBlendingMode_(0)
            frost.setState_(1)
            frost.setWantsLayer_(True)
            frost.layer().setCornerRadius_(22.0)
            frost.layer().setMasksToBounds_(True)
            frost.setAutoresizingMask_(NSViewMinXMargin | NSViewWidthSizable | NSViewHeightSizable)
            view.addSubview_(frost)
            pane = QueueDrop.alloc().initWithFrame_(frost.bounds())
            pane.ctrl = self
            pane.registerForDraggedTypes_([NSFilenamesPboardType])
            pane.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
            frost.addSubview_(pane)

            heading = _label(
                NSMakeRect(SIDE_X + 18, PHONE_Y + QUEUE_H - 36, SIDE_W - 36, 14),
                "NOW", 10, True, LIME, stick_top,
            )
            view.addSubview_(heading)
            self.now_title = _label(
                NSMakeRect(SIDE_X + 18, PHONE_Y + QUEUE_H - 62, SIDE_W - 36, 22),
                "재생 중인 영상이 없습니다", 15, True, SNOW, stick_top,
            )
            view.addSubview_(self.now_title)
            self.now_channel = _label(
                NSMakeRect(SIDE_X + 18, PHONE_Y + QUEUE_H - 80, SIDE_W - 36, 16),
                "페이지에서 추가하거나 파일을 놓으십시오", 11, False, GHOST, stick_top,
            )
            view.addSubview_(self.now_channel)

            self.play_btn = NSButton.alloc().initWithFrame_(
                NSMakeRect(SIDE_X + 18, PHONE_Y + QUEUE_H - 120, 150, 32)
            )
            self.play_btn.setTitle_("▶   덱 재생")
            self.play_btn.setFont_(NSFont.boldSystemFontOfSize_(12))
            _pill(self.play_btn, LIME, INK)
            self.play_btn.setTarget_(self)
            self.play_btn.setAction_("play:")
            self.play_btn.setKeyEquivalent_("\r")
            self.play_btn.setAutoresizingMask_(stick_top)
            view.addSubview_(self.play_btn)
            self.stop_btn = NSButton.alloc().initWithFrame_(
                NSMakeRect(SIDE_X + 176, PHONE_Y + QUEUE_H - 120, 72, 32)
            )
            self.stop_btn.setTitle_("■  정지")
            self.stop_btn.setFont_(NSFont.boldSystemFontOfSize_(12))
            _pill(self.stop_btn, CARD, SNOW)
            self.stop_btn.setTarget_(self)
            self.stop_btn.setAction_("stop:")
            self.stop_btn.setKeyEquivalent_("\x1b")
            self.stop_btn.setAutoresizingMask_(stick_top)
            view.addSubview_(self.stop_btn)
            self.elapsed_lab = _label(
                NSMakeRect(SIDE_X + 18, PHONE_Y + QUEUE_H - 146, 64, 14),
                "0:00", 10, False, GHOST, stick_top,
            )
            view.addSubview_(self.elapsed_lab)
            self.remain_lab = _label(
                NSMakeRect(SIDE_X + SIDE_W - 82, PHONE_Y + QUEUE_H - 146, 64, 14),
                "--:--", 10, False, GHOST, stick_top,
            )
            self.remain_lab.setAlignment_(2)
            view.addSubview_(self.remain_lab)
            self.seek_bar = NSSlider.alloc().initWithFrame_(
                NSMakeRect(SIDE_X + 18, PHONE_Y + QUEUE_H - 168, SIDE_W - 36, 18)
            )
            self.seek_bar.setMinValue_(0.0)
            self.seek_bar.setMaxValue_(1.0)
            self.seek_bar.setDoubleValue_(0.0)
            self.seek_bar.setContinuous_(False)
            self.seek_bar.setEnabled_(False)
            self.seek_bar.setTarget_(self)
            self.seek_bar.setAction_("seek:")
            self.seek_bar.setAutoresizingMask_(stick_top)
            view.addSubview_(self.seek_bar)

            self.queue_head = _label(
                NSMakeRect(SIDE_X + 18, PHONE_Y + QUEUE_H - 188, SIDE_W - 36, 14),
                "대기열", 10, True, GHOST, stick_top,
            )
            view.addSubview_(self.queue_head)

            BTN_H = 28
            table_y = PHONE_Y + 46
            table_h = max(80, (PHONE_Y + QUEUE_H - 200) - table_y)
            scroll = NSScrollView.alloc().initWithFrame_(
                NSMakeRect(SIDE_X + 10, table_y, SIDE_W - 20, table_h)
            )
            scroll.setHasVerticalScroller_(True)
            scroll.setBorderType_(0)
            scroll.setDrawsBackground_(False)
            scroll.setAutoresizingMask_(NSViewMinXMargin | NSViewWidthSizable | NSViewHeightSizable)
            self.playlist_table = NSTableView.alloc().initWithFrame_(scroll.contentView().bounds())
            title_col = NSTableColumn.alloc().initWithIdentifier_("title")
            title_col.setWidth_(SIDE_W - 140)
            title_col.setEditable_(False)
            chan_col = NSTableColumn.alloc().initWithIdentifier_("channel")
            chan_col.setWidth_(100)
            chan_col.setEditable_(False)
            self.playlist_table.addTableColumn_(title_col)
            self.playlist_table.addTableColumn_(chan_col)
            self.playlist_table.setHeaderView_(None)
            self.playlist_table.setRowHeight_(40)
            self.playlist_table.setUsesAlternatingRowBackgroundColors_(False)
            self.playlist_table.setBackgroundColor_(NSColor.clearColor())
            try:
                self.playlist_table.setAppearance_(
                    NSAppearance.appearanceNamed_("NSAppearanceNameDarkAqua")
                )
            except Exception:
                pass
            self.playlist_table.setDataSource_(self)
            self.playlist_table.setDelegate_(self)
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

            def qbtn(x, w, title, action):
                btn = NSButton.alloc().initWithFrame_(NSMakeRect(x, PHONE_Y + 12, w, BTN_H))
                btn.setTitle_(title)
                btn.setFont_(NSFont.boldSystemFontOfSize_(11))
                _pill(btn, CARD, SNOW)
                btn.setTarget_(self)
                btn.setAction_(action)
                btn.setAutoresizingMask_(stick_bot)
                view.addSubview_(btn)
                return btn

            self.add_btn = qbtn(SIDE_X + 12, 72, "＋ 넣기", "addToPlaylist:")
            self.row_play_btn = qbtn(SIDE_X + 90, 52, "재생", "playSelected:")
            self.up_btn = qbtn(SIDE_X + 148, 32, "↑", "moveUp:")
            self.down_btn = qbtn(SIDE_X + 186, 32, "↓", "moveDown:")
            self.del_btn = qbtn(SIDE_X + SIDE_W - 64, 52, "삭제", "removeSelected:")
            _gui_playlist_draw(self)

            reload_btn = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 1, 1))
            reload_btn.setKeyEquivalent_("r")
            reload_btn.setKeyEquivalentModifierMask_(NSEventModifierFlagCommand)
            reload_btn.setTarget_(self)
            reload_btn.setAction_("reload:")
            view.addSubview_(reload_btn)
            focus_btn = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 1, 1))
            focus_btn.setKeyEquivalent_("l")
            focus_btn.setKeyEquivalentModifierMask_(NSEventModifierFlagCommand)
            focus_btn.setTarget_(self)
            focus_btn.setAction_("focusUrl:")
            view.addSubview_(focus_btn)

            self.dot = _label(NSMakeRect(PAD, 6, 14, 16), "●", 10, False, LIME, NSViewMaxYMargin)
            view.addSubview_(self.dot)
            self.status = _label(
                NSMakeRect(PAD + 16, 6, 280, 16), "상태 확인 중…", 10, False, GHOST,
                NSViewMaxYMargin,
            )
            view.addSubview_(self.status)
            self.note = _label(
                NSMakeRect(PAD + 300, 6, W - PAD - 310, 16),
                "유튜브를 열고 덱 재생. 대기열은 오른쪽.",
                10, False, GHOST, NSViewWidthSizable | NSViewMaxYMargin,
            )
            view.addSubview_(self.note)

            self.window.makeKeyAndOrderFront_(None)
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                2.0, self, "poll:", None, True
            )
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                0.4, self, "tickPlayhead:", None, True
            )
            AppHelper.callAfter(lambda: _gui_kick(self, "status", ""))
            return self

        def back_(self, _sender):
            self.web.goBack_(None)

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
            """Play the field's file, or the video on this page, on the deck.

            A local path in the URL field wins so 파일 / a drop / a typed path actually play.
            Otherwise resume the page (정지 leaves it paused) and read the watch URL.
            """
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
                "document.querySelectorAll('video').forEach(function(v){ if (v.paused) v.play().catch(function(){}); });"
                " (window.__ghostdeckNow ? window.__ghostdeckNow() : window.location.href)",
                after,
            )

        def stop_(self, _sender):
            """Stop the deck player AND the page's video, so the stop actually sticks.

            Clearing `seen_watch` alone was not enough: the page kept playing, its `play` listener
            fired again, and `should_start_play("")` treats an unknown seen-id as a NEW video -- so the
            deck restarted on the next event and 정지 looked broken (observed: `playing=no` for a
            moment, then `playing=yes` again with the page's own YouTube id). Pausing the page first
            is what makes the stop hold; the deck stop runs after it, so no `play` can slip between.
            """
            self.user_stopped = True
            self.seen_watch = ""
            self.web.evaluateJavaScript_completionHandler_(
                "document.querySelectorAll('video').forEach(function(v){v.pause()}); null",
                lambda _result, _error: _gui_kick(self, "stop", ""),
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
            source = deck_now_playing() or getattr(self, "seen_watch", "")
            if not source:
                self.note.setStringValue_("재생 중인 영상이 없습니다.")
                return
            self.user_stopped = False
            _gui_kick(self, "play", source, start=at)
            self.note.setStringValue_(f"{format_clock(at)}부터 재생합니다.")

        def numberOfRowsInTableView_(self, _table):
            return len(getattr(self, "playlist", []))

        def tableView_objectValueForTableColumn_row_(self, _table, col, row):
            items = getattr(self, "playlist", [])
            if row < 0 or row >= len(items):
                return ""
            item = items[row]
            ident = str(col.identifier()) if col is not None else "title"
            if ident == "channel":
                return playlist_subtitle(item)
            title = playlist_title(item)
            return ("▶ " + title) if playlist_source(item) == deck_now_playing() else title

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
            row = int(self.playlist_table.selectedRow())
            items = getattr(self, "playlist", [])
            if row < 0 or row >= len(items):
                self.note.setStringValue_("재생할 항목을 고르십시오.")
                return
            source = playlist_source(items[row])
            if source and source == deck_now_playing() and deck_session_active():
                self.user_stopped = True
                _gui_kick(self, "stop", "")
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
            if kind == "play":
                source = should_start_play(getattr(self, "seen_watch", ""), href, src)
                if source:
                    _gui_kick(self, "play", source, start=start)
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

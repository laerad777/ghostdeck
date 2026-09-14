"""Host remote for the D200. A native window that runs the CLI; not a player."""

from __future__ import annotations

import io
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from ghostdeck import cli

_SHIM_UP = re.compile(r"(?:^|\s)shim=up(?:\s|$)")
_YT_HOSTS = {
    "youtu.be",
    "youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtube-nocookie.com",
}


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
    """Run `ghostdeck` in-process and capture the same stdout/stderr a terminal would show."""
    out = io.StringIO()
    err = io.StringIO()
    stdout, stderr = sys.stdout, sys.stderr
    try:
        sys.stdout, sys.stderr = out, err
        code = cli.main(list(argv))
    except SystemExit as error:
        raw = error.code
        code = 0 if raw is None else (raw if isinstance(raw, int) else 1)
    finally:
        sys.stdout, sys.stderr = stdout, stderr
    return CommandResult(list(argv), int(code), out.getvalue(), err.getvalue())


def shim_is_up(status_stdout: str) -> bool:
    return bool(_SHIM_UP.search(status_stdout))


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


class DeckRemote:
    """The play button's contract: studio first if the shim is down, then play."""

    def __init__(self, run=run_cli):
        self._run = run

    def status(self) -> CommandResult:
        return self._run(["status"])

    def stop(self) -> CommandResult:
        return self._run(["stop"])

    def play(self, source: str, pasteboard: str = "") -> list[CommandResult]:
        source = resolve_source(source, pasteboard)
        if not source:
            return [CommandResult(["play"], 2, "", "유튜브에서 영상을 연 다음 재생을 누르십시오")]
        results: list[CommandResult] = []
        st = self._run(["status"])
        results.append(st)
        if not shim_is_up(st.stdout):
            results.append(self._run(["studio"]))
            if results[-1].code != 0:
                return results
        results.append(self._run(["play", source]))
        return results


def _busy_call(remote: DeckRemote, op: str, source: str, pasteboard: str, done) -> None:
    try:
        if op == "status":
            results = [remote.status()]
        elif op == "stop":
            results = [remote.stop()]
        else:
            results = remote.play(source, pasteboard)
        done(results, None)
    except Exception as error:
        done([], error)


def main() -> int:
    try:
        import objc
        from AppKit import (
            NSApp,
            NSApplication,
            NSBackingStoreBuffered,
            NSBezelStyleRounded,
            NSButton,
            NSColor,
            NSFont,
            NSMakeRect,
            NSObject,
            NSOpenPanel,
            NSTextField,
            NSViewHeightSizable,
            NSViewMaxYMargin,
            NSViewWidthSizable,
            NSWindow,
            NSWindowStyleMaskClosable,
            NSWindowStyleMaskMiniaturizable,
            NSWindowStyleMaskResizable,
            NSWindowStyleMaskTitled,
        )
        from Foundation import NSURL, NSURLRequest, NSTimer
        from PyObjCTools import AppHelper
        from WebKit import WKWebView, WKWebViewConfiguration
    except ImportError:
        print(
            "ghostdeck gui needs pyobjc-framework-Cocoa and pyobjc-framework-WebKit on macOS",
            file=sys.stderr,
        )
        return 2

    remote = DeckRemote()
    pause_js = (
        "document.querySelectorAll('video,audio').forEach"
        "(function(v){v.pause();v.muted=true;})"
    )

    def _gui_href(ctrl) -> str:
        url = ctrl.web.URL()
        return str(url.absoluteString()) if url is not None else ""

    def _gui_set_busy(ctrl, on: bool) -> None:
        ctrl.busy = on
        ctrl.play_btn.setEnabled_(not on)
        ctrl.stop_btn.setEnabled_(not on)

    def _gui_apply(ctrl, results, error) -> None:
        _gui_set_busy(ctrl, False)
        if error is not None:
            ctrl.note.setStringValue_(f"{type(error).__name__}: {error}")
            return
        for item in results:
            if item.argv[:1] == ["status"] and item.stdout.strip():
                ctrl.status.setStringValue_(item.stdout.strip().splitlines()[-1])
        last = results[-1] if results else None
        if last is None:
            return
        if last.code != 0:
            ctrl.note.setStringValue_(last.detail)
        elif last.argv[:1] == ["stop"]:
            ctrl.note.setStringValue_("정지. Studio가 켜져 있으면 덱은 ADB입니다.")
        elif last.argv[:1] == ["play"]:
            ctrl.note.setStringValue_("덱에서 재생. 루프는 정지까지 계속됩니다.")
        elif last.argv[:1] == ["studio"]:
            ctrl.note.setStringValue_("Studio 브리지를 시작했습니다.")

    def _gui_kick(ctrl, op: str, source: str) -> None:
        if ctrl.busy and op != "status":
            return
        if op != "status":
            _gui_set_busy(ctrl, True)
            ctrl.note.setStringValue_("재생 준비…" if op == "play" else "정지…")
        pasteboard = read_pasteboard() if op == "play" else ""
        thread = threading.Thread(
            target=_busy_call,
            args=(
                remote,
                op,
                source,
                pasteboard,
                lambda r, e: AppHelper.callAfter(lambda: _gui_apply(ctrl, r, e)),
            ),
            daemon=True,
        )
        thread.start()

    class Controller(NSObject):
        def init(self):
            self = objc.super(Controller, self).init()
            self.busy = False
            self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, 1020, 740),
                NSWindowStyleMaskTitled
                | NSWindowStyleMaskClosable
                | NSWindowStyleMaskMiniaturizable
                | NSWindowStyleMaskResizable,
                NSBackingStoreBuffered,
                False,
            )
            self.window.setTitle_("ghostdeck")
            self.window.setReleasedWhenClosed_(False)
            self.window.setMinSize_((640, 480))
            self.window.center()
            view = self.window.contentView()

            config = WKWebViewConfiguration.alloc().init()
            self.web = WKWebView.alloc().initWithFrame_configuration_(
                NSMakeRect(0, 56, 1020, 684),
                config,
            )
            self.web.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
            view.addSubview_(self.web)
            self.web.loadRequest_(
                NSURLRequest.requestWithURL_(NSURL.URLWithString_("https://www.youtube.com"))
            )

            self.status = NSTextField.alloc().initWithFrame_(NSMakeRect(12, 32, 520, 18))
            self.status.setEditable_(False)
            self.status.setBezeled_(False)
            self.status.setDrawsBackground_(False)
            self.status.setFont_(NSFont.userFixedPitchFontOfSize_(11))
            self.status.setStringValue_("usb=? shim=? copy=? playing=?")
            self.status.setAutoresizingMask_(NSViewWidthSizable | NSViewMaxYMargin)
            view.addSubview_(self.status)

            play_btn = NSButton.alloc().initWithFrame_(NSMakeRect(540, 24, 110, 28))
            play_btn.setTitle_("이 영상 재생")
            play_btn.setBezelStyle_(NSBezelStyleRounded)
            play_btn.setTarget_(self)
            play_btn.setAction_("play:")
            play_btn.setAutoresizingMask_(NSViewMaxYMargin)
            view.addSubview_(play_btn)
            self.play_btn = play_btn

            stop_btn = NSButton.alloc().initWithFrame_(NSMakeRect(656, 24, 72, 28))
            stop_btn.setTitle_("정지")
            stop_btn.setBezelStyle_(NSBezelStyleRounded)
            stop_btn.setTarget_(self)
            stop_btn.setAction_("stop:")
            stop_btn.setAutoresizingMask_(NSViewMaxYMargin)
            view.addSubview_(stop_btn)
            self.stop_btn = stop_btn

            open_btn = NSButton.alloc().initWithFrame_(NSMakeRect(734, 24, 72, 28))
            open_btn.setTitle_("파일")
            open_btn.setBezelStyle_(NSBezelStyleRounded)
            open_btn.setTarget_(self)
            open_btn.setAction_("openFile:")
            open_btn.setAutoresizingMask_(NSViewMaxYMargin)
            view.addSubview_(open_btn)

            self.note = NSTextField.alloc().initWithFrame_(NSMakeRect(12, 8, 996, 16))
            self.note.setEditable_(False)
            self.note.setBezeled_(False)
            self.note.setDrawsBackground_(False)
            self.note.setFont_(NSFont.labelFontOfSize_(11))
            self.note.setTextColor_(NSColor.secondaryLabelColor())
            self.note.setStringValue_("영상을 열고 이 영상 재생. 정지 후 Studio가 켜져 있으면 덱은 ADB입니다.")
            self.note.setAutoresizingMask_(NSViewWidthSizable | NSViewMaxYMargin)
            view.addSubview_(self.note)

            self.window.makeKeyAndOrderFront_(None)
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                2.0, self, "poll:", None, True
            )
            AppHelper.callAfter(lambda: _gui_kick(self, "status", ""))
            return self

        def play_(self, _sender):
            ctrl = self

            def after(href, _err):
                page = href if isinstance(href, str) else _gui_href(ctrl)
                watch = youtube_watch_url(page or _gui_href(ctrl))
                if watch:
                    ctrl.web.evaluateJavaScript_completionHandler_(pause_js, None)
                    _gui_kick(ctrl, "play", watch)
                    return
                _gui_kick(ctrl, "play", "")

            self.web.evaluateJavaScript_completionHandler_("window.location.href", after)

        def stop_(self, _sender):
            _gui_kick(self, "stop", "")

        def poll_(self, _timer):
            if not self.busy:
                _gui_kick(self, "status", "")

        def openFile_(self, _sender):
            panel = NSOpenPanel.openPanel()
            panel.setCanChooseFiles_(True)
            panel.setCanChooseDirectories_(False)
            panel.setAllowedFileTypes_(["mp4", "mov", "mkv", "webm", "m4v"])
            if panel.runModal() != 1:
                return
            url = panel.URL()
            if url is None:
                return
            _gui_kick(self, "play", str(url.path()))

        def windowWillClose_(self, _notification):
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

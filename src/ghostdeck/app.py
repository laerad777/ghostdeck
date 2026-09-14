"""Host remote for the D200. A native window that runs the CLI; not a player."""

from __future__ import annotations

import io
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
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
        if path.endswith((".mp4", ".m4v", ".webm", ".mkv", ".mov", ".m3u8", ".mpd")):
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

def is_google_login_host(host: str) -> bool:
    """accounts.google.* is a desktop WebAuthn page; mobile YouTube asks for Bluetooth instead."""
    host = (host or "").lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host == "accounts.youtube.com" or host == "accounts.google.com" or host.startswith("accounts.google.")

def page_follow_action(seen_watch: str, href: str) -> tuple[str, str]:
    """When the page changes: (new_seen, play_url | 'stop' | ''). Same YouTube id is a no-op."""
    watch = youtube_watch_url(href)
    if watch == seen_watch:
        return seen_watch, ""
    if watch:
        return watch, watch
    if seen_watch:
        return "", "stop"
    return "", ""

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
            NSTextField,
            NSViewHeightSizable,
            NSViewMaxYMargin,
            NSViewMinXMargin,
            NSViewWidthSizable,
            NSWindow,
            NSWindowStyleMaskClosable,
            NSWindowStyleMaskMiniaturizable,
            NSWindowStyleMaskResizable,
            NSWindowStyleMaskTitled,
        )
        from Foundation import NSURL, NSURLRequest, NSTimer
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
  function watchUrl(){
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
  function post(type){
    try {
      var v = document.querySelector('video');
      window.webkit.messageHandlers.ghostdeck.postMessage({
        type: type,
        url: watchUrl(),
        src: (v && v.currentSrc) ? String(v.currentSrc) : ''
      });
    } catch (e) {}
  }
  function hook(v){
    if (v.__ghostdeck) return;
    v.__ghostdeck = true;
    v.addEventListener('play', function(){ post('play'); });
  }
  function scan(){ document.querySelectorAll('video').forEach(hook); }
  scan();
  new MutationObserver(scan).observe(document.documentElement, {childList:true, subtree:true});
  var last = watchUrl();
  setInterval(function(){
    var now = watchUrl();
    if (now !== last){
      last = now;
      post('nav');
    }
  }, 400);
})();
"""

    def _gui_href(ctrl) -> str:
        url = ctrl.web.URL()
        return str(url.absoluteString()) if url is not None else ""

    def _gui_set_busy(ctrl, on: bool) -> None:
        ctrl.busy = on

    def _gui_apply(ctrl, results, error) -> None:
        _gui_set_busy(ctrl, False)
        pending = getattr(ctrl, "pending_source", "")
        ctrl.pending_source = ""
        if error is not None:
            ctrl.seen_watch = ""
            ctrl.note.setStringValue_(f"{type(error).__name__}: {error}")
            if pending:
                _gui_kick(ctrl, "play", pending)
            return
        for item in results:
            if item.argv[:1] == ["status"] and item.stdout.strip():
                ctrl.status.setStringValue_(item.stdout.strip().splitlines()[-1])
        last = results[-1] if results else None
        if last is None:
            if pending:
                _gui_kick(ctrl, "play", pending)
            return
        if last.code != 0:
            ctrl.seen_watch = ""
            ctrl.note.setStringValue_(last.detail)
        elif last.argv[:1] == ["stop"]:
            ctrl.seen_watch = ""
            ctrl.note.setStringValue_("정지. Studio가 켜져 있으면 덱은 ADB입니다.")
        elif last.argv[:1] == ["play"]:
            if len(last.argv) > 1:
                ctrl.seen_watch = last.argv[1]
            ctrl.note.setStringValue_("덱에서 재생. 루프는 정지까지 계속됩니다.")
        elif last.argv[:1] == ["studio"]:
            ctrl.note.setStringValue_("Studio 브리지를 시작했습니다.")
        if pending:
            _gui_kick(ctrl, "play", pending)

    def _gui_kick(ctrl, op: str, source: str) -> None:
        if ctrl.busy and op == "play":
            ctrl.pending_source = source
            return
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

    def _gui_follow(ctrl, href: str) -> None:
        _seen, action = page_follow_action(getattr(ctrl, "seen_watch", ""), href)
        if not action:
            return
        if action == "stop":
            _gui_kick(ctrl, "stop", "")
            return
        _gui_kick(ctrl, "play", action)

    class Controller(NSObject):
        def init(self):
            self = objc.super(Controller, self).init()
            self.busy = False
            self.pending_source = ""
            self.seen_watch = ""
            self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, 420, 844),
                NSWindowStyleMaskTitled
                | NSWindowStyleMaskClosable
                | NSWindowStyleMaskMiniaturizable
                | NSWindowStyleMaskResizable,
                NSBackingStoreBuffered,
                False,
            )
            self.window.setTitle_("ghostdeck")
            self.window.setReleasedWhenClosed_(False)
            self.window.setMinSize_((320, 560))
            self.window.center()
            view = self.window.contentView()

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
            self.web = WKWebView.alloc().initWithFrame_configuration_(
                NSMakeRect(0, 80, 420, 764),
                config,
            )
            self.web.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
            self.web.setUIDelegate_(self)
            self.web.setNavigationDelegate_(self)
            view.addSubview_(self.web)
            self.web.loadRequest_(
                NSURLRequest.requestWithURL_(NSURL.URLWithString_("https://www.youtube.com"))
            )

            back = NSButton.alloc().initWithFrame_(NSMakeRect(8, 52, 28, 24))
            back.setTitle_("‹")
            back.setBezelStyle_(NSBezelStyleRounded)
            back.setTarget_(self)
            back.setAction_("back:")
            back.setAutoresizingMask_(NSViewMaxYMargin)
            view.addSubview_(back)

            fwd = NSButton.alloc().initWithFrame_(NSMakeRect(38, 52, 28, 24))
            fwd.setTitle_("›")
            fwd.setBezelStyle_(NSBezelStyleRounded)
            fwd.setTarget_(self)
            fwd.setAction_("forward:")
            fwd.setAutoresizingMask_(NSViewMaxYMargin)
            view.addSubview_(fwd)

            self.url_field = NSTextField.alloc().initWithFrame_(NSMakeRect(70, 52, 248, 24))
            self.url_field.setStringValue_("https://www.youtube.com")
            self.url_field.setTarget_(self)
            self.url_field.setAction_("go:")
            self.url_field.setAutoresizingMask_(NSViewWidthSizable | NSViewMaxYMargin)
            view.addSubview_(self.url_field)

            play_btn = NSButton.alloc().initWithFrame_(NSMakeRect(322, 52, 90, 24))
            play_btn.setTitle_("재생")
            play_btn.setBezelStyle_(NSBezelStyleRounded)
            play_btn.setTarget_(self)
            play_btn.setAction_("play:")
            play_btn.setAutoresizingMask_(NSViewMinXMargin | NSViewMaxYMargin)
            view.addSubview_(play_btn)

            self.status = NSTextField.alloc().initWithFrame_(NSMakeRect(8, 32, 404, 16))
            self.status.setEditable_(False)
            self.status.setBezeled_(False)
            self.status.setDrawsBackground_(False)
            self.status.setFont_(NSFont.userFixedPitchFontOfSize_(10))
            self.status.setStringValue_("usb=? shim=? copy=? playing=?")
            self.status.setAutoresizingMask_(NSViewWidthSizable | NSViewMaxYMargin)
            view.addSubview_(self.status)

            self.note = NSTextField.alloc().initWithFrame_(NSMakeRect(8, 4, 404, 24))
            self.note.setEditable_(False)
            self.note.setBezeled_(False)
            self.note.setDrawsBackground_(False)
            self.note.setFont_(NSFont.labelFontOfSize_(10))
            self.note.setTextColor_(NSColor.secondaryLabelColor())
            self.note.setStringValue_("아무 사이트. 영상 재생이면 덱도 재생. 광고는 무시합니다.")
            self.note.setAutoresizingMask_(NSViewWidthSizable | NSViewMaxYMargin)
            view.addSubview_(self.note)

            self.window.makeKeyAndOrderFront_(None)
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                2.0, self, "poll:", None, True
            )
            AppHelper.callAfter(lambda: _gui_kick(self, "status", ""))
            return self

        def back_(self, _sender):
            self.web.goBack_(None)

        def forward_(self, _sender):
            self.web.goForward_(None)

        def go_(self, _sender):
            raw = str(self.url_field.stringValue() or "").strip()
            if not raw:
                return
            if "://" not in raw:
                raw = "https://" + raw
            url = NSURL.URLWithString_(raw)
            if url is None:
                return
            self.web.loadRequest_(NSURLRequest.requestWithURL_(url))

        def play_(self, _sender):
            ctrl = self

            def after(href, _err):
                page = href if isinstance(href, str) else _gui_href(ctrl)
                source = should_start_play(getattr(ctrl, "seen_watch", ""), page)
                if source:
                    _gui_kick(ctrl, "play", source)
                    return
                watch = youtube_watch_url(page) or playable_source(page)
                if watch:
                    _gui_kick(ctrl, "play", watch)
                    return
                ctrl.note.setStringValue_("이 페이지에서 영상을 찾지 못했습니다.")

            self.web.evaluateJavaScript_completionHandler_(
                "window.__ghostdeckWatch ? window.__ghostdeckWatch() : window.location.href",
                after,
            )

        def poll_(self, _timer):
            if not self.busy:
                _gui_kick(self, "status", "")
            ctrl = self

            def after(href, _err):
                page = href if isinstance(href, str) else _gui_href(ctrl)
                _gui_follow(ctrl, page)

            self.web.evaluateJavaScript_completionHandler_(
                "window.__ghostdeckWatch ? window.__ghostdeckWatch() : window.location.href",
                after,
            )

        def userContentController_didReceiveScriptMessage_(self, _ucc, message):
            body = message.body()
            kind = ""
            href = ""
            src = ""
            try:
                kind = str(body.objectForKey_("type") or "")
                href = str(body.objectForKey_("url") or "")
                src = str(body.objectForKey_("src") or "")
            except Exception:
                if isinstance(body, dict):
                    kind = str(body.get("type") or "")
                    href = str(body.get("url") or "")
                    src = str(body.get("src") or "")
            href = href or _gui_href(self)
            if kind == "play":
                source = should_start_play(getattr(self, "seen_watch", ""), href, src)
                if source and not self.busy:
                    self.seen_watch = source
                    _gui_kick(self, "play", source)
                return
            _gui_follow(self, href)

        def webView_didCommitNavigation_(self, webView, _nav):
            if webView is not self.web:
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
            win.setTitle_("ghostdeck")
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

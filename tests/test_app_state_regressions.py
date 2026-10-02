"""Execute the GUI's shared callbacks without importing Cocoa or touching a deck."""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from ghostdeck import app


def gui_function(name, **overrides):
    """Compile the actual nested handler, supplying only its platform boundary fakes."""
    tree = ast.parse(Path(app.__file__).read_text())
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
    namespace = dict(vars(app), **overrides)
    exec(compile(ast.Module(body=[node], type_ignores=[]), app.__file__, "exec"), namespace)
    return namespace[name]


class Note:
    def setStringValue_(self, value):
        self.value = value


@pytest.mark.parametrize("source", ["/tmp/a?b.mp4", "/tmp/a#b.mp4", "/tmp/a%3Fb.mp4"])
def test_literal_local_paths_are_not_urls(source):
    assert app.media_path_candidate(source) == source
    assert app.dropped_play_source([source]) == source
    assert app.play_request(source, "") == (source, 0.0)


def test_file_url_decodes_only_url_path():
    assert app.media_path_candidate("file:///tmp/a%3Fb.mp4?download=1#part") == "/tmp/a?b.mp4"
    assert app.media_path_candidate("https://example.org/a.mp4") == ""


@pytest.mark.parametrize("backfill", [False, True])
@pytest.mark.parametrize("readd", [False, True])
def test_pending_metadata_cannot_restore_removed_row(backfill, readd):
    callbacks = []
    saves = []
    source = "/tmp/a.mp4"
    ctrl = SimpleNamespace(playlist=[app.playlist_entry(source)])
    class Thread:
        def __init__(self, target, **kwargs):
            self.target = target
        def start(self):
            self.target()
    handler = gui_function(
        "_gui_backfill_meta" if backfill else "_gui_playlist_put",
        threading=SimpleNamespace(Thread=Thread),
        AppHelper=SimpleNamespace(callAfter=callbacks.append),
        source_length=lambda source: (42, False),
        source_identity=lambda source: ("Old title", "Old channel"),
        playlist_save=lambda path, items: saves.append(list(items)),
        _gui_playlist_draw=lambda ctrl: None,
    )
    handler(ctrl) if backfill else handler(ctrl, source)
    assert callbacks
    app.playlist_remove_row(ctrl, 0)
    if readd:
        ctrl.playlist = app.playlist_add(ctrl.playlist, source, "New title")
        app.playlist_metadata_token(ctrl, source)
    before = list(ctrl.playlist)
    callbacks.pop()()
    assert ctrl.playlist == before


def test_metadata_survives_move_and_updates_only_matching_identity():
    ctrl = SimpleNamespace(playlist=app.playlist_normalize(["/tmp/a.mp4", "/tmp/b.mp4"]))
    token = app.playlist_metadata_token(ctrl, "/tmp/a.mp4")
    ctrl.playlist = app.playlist_move(ctrl.playlist, 0, 1)
    assert app.playlist_apply_metadata(ctrl, "/tmp/a.mp4", token, "Title", duration=12)
    assert ctrl.playlist[0]["source"] == "/tmp/b.mp4"
    assert ctrl.playlist[1]["title"] == "Title"
    assert not app.playlist_apply_metadata(ctrl, "/tmp/b.mp4", token, "Wrong")


@pytest.mark.parametrize("busy", [False, True])
def test_central_play_clears_stopped_even_when_queued(busy):
    launches = []
    ctrl = SimpleNamespace(busy=busy, user_stopped=True, note=Note(), playlist=["/tmp/a.mp4"])
    handler = gui_function(
        "_gui_kick", remote=None, read_pasteboard=lambda: "",
        deck_loop_state=lambda: ("/tmp/old.mp4", "old-session", True),
        _gui_set_busy=lambda ctrl, value: setattr(ctrl, "busy", value),
        _gui_notify=lambda ctrl, text: None,
        threading=SimpleNamespace(Thread=lambda **kw: SimpleNamespace(start=lambda: launches.append(kw))),
    )
    handler(ctrl, "play", "/tmp/a.mp4", start=7)
    assert ctrl.user_stopped is False
    if busy:
        assert ctrl.pending_source == "/tmp/a.mp4"
        assert ctrl.pending_start == 7
        assert not launches
    else:
        assert launches[0]["args"][6] is False
        assert ctrl.replacing_session == "old-session"


@pytest.mark.parametrize("mode,items,old_loop,new_loop", [
    ("off", ["/tmp/a.mp4"], True, False),
    ("one", ["/tmp/a.mp4", "/tmp/b.mp4"], False, True),
    ("all", ["/tmp/a.mp4", "/tmp/b.mp4"], True, False),
    ("all", ["/tmp/a.mp4"], False, True),
])
def test_repeat_and_queue_changes_restart_at_current_position(mode, items, old_loop, new_loop):
    ctrl = SimpleNamespace(playlist=items, repeat=mode, worker_source="/tmp/a.mp4", worker_loop=old_loop)
    calls = []
    def kick(ctrl, op, source, **kwargs):
        calls.append((op, source, kwargs))
        ctrl.worker_loop = app.playlist_should_loop(source, ctrl.playlist, ctrl.repeat)
        ctrl.replacing_session = "session"
    sync = gui_function("_gui_sync_loop", deck_playhead=lambda: ("/tmp/a.mp4", 13.5, True),
                        deck_crop=lambda: "100:100:0:0", _gui_kick=kick,
                        deck_has_picture=lambda: True,
                        deck_loop_state=lambda: ("/tmp/a.mp4", "session", old_loop))
    sync(ctrl)
    assert calls == [("play", "/tmp/a.mp4", {"start": 13.5, "crop": "100:100:0:0", "preserve_queue": True})]
    assert ctrl.worker_loop is new_loop
    sync(ctrl)
    assert len(calls) == 1


@pytest.mark.parametrize("blocked", ["busy", "user_stopped", "opening", "different_source"])
def test_loop_sync_defers_without_resurrecting_or_overriding_playback(blocked):
    ctrl = SimpleNamespace(playlist=["/tmp/a.mp4"], repeat="off", worker_source="/tmp/a.mp4", worker_loop=True)
    setattr(ctrl, blocked, True)
    state = ("/tmp/b.mp4" if blocked == "different_source" else "/tmp/a.mp4", 10, blocked != "opening")
    calls = []
    sync = gui_function("_gui_sync_loop", deck_playhead=lambda: state,
                        deck_loop_state=lambda: ("/tmp/a.mp4", "session", True),
                        deck_has_picture=lambda: blocked != "opening",
                        _gui_kick=lambda *a, **k: calls.append(a))
    sync(ctrl)
    assert calls == []


def test_loop_sync_adopts_existing_worker_and_same_source_replacement():
    ctrl = SimpleNamespace(playlist=["/tmp/a.mp4"], repeat="off", worker_loop=False)
    calls = []
    sync = gui_function("_gui_sync_loop", deck_playhead=lambda: ("/tmp/a.mp4", 9, True),
                        deck_loop_state=lambda: ("/tmp/a.mp4", "external-session", True),
                        deck_has_picture=lambda: True, deck_crop=lambda: "auto",
                        _gui_kick=lambda *a, **k: calls.append((a, k)))
    sync(ctrl)
    assert len(calls) == 1  # Published loop wins over absent/stale GUI launch state.


def test_deck_loop_state_rejects_missing_or_invalid_metadata(tmp_path):
    import json
    path = tmp_path / "state.json"
    assert app.deck_loop_state(path) == ("", "", None)
    for data in [[], {}, {"video": {}, "loop": "false"}]:
        path.write_text(json.dumps(data))
        assert app.deck_loop_state(path) == ("", "", None)
    path.write_text(json.dumps({"source": "/tmp/a.mp4", "video": {"session": "abc"}, "loop": False}))
    assert app.deck_loop_state(path) == ("/tmp/a.mp4", "abc", False)


def test_loop_sync_waits_for_replacement_then_uses_latest_preference():
    ctrl = SimpleNamespace(playlist=["/tmp/a.mp4"], repeat="one", replacing_session="old")
    state = ["/tmp/a.mp4", "old", False]
    calls = []
    def kick(ctrl, *args, **kwargs):
        calls.append((args, kwargs))
        ctrl.replacing_session = state[1]
    sync = gui_function("_gui_sync_loop", deck_loop_state=lambda: tuple(state),
                        deck_playhead=lambda: ("/tmp/a.mp4", 17, True),
                        deck_has_picture=lambda: True, deck_crop=lambda: "auto", _gui_kick=kick)
    sync(ctrl)
    assert not calls
    state[:] = ["/tmp/a.mp4", "replacement", True]
    ctrl.repeat = "off"  # A preference change during the previous launch must not be lost.
    sync(ctrl)
    assert len(calls) == 1
    sync(ctrl)
    assert len(calls) == 1
    state[:] = ["/tmp/a.mp4", "settled", False]
    sync(ctrl)
    assert len(calls) == 1


def test_loop_sync_does_not_restart_when_publication_changes_during_read():
    states = iter([("/tmp/a.mp4", "old", True), ("/tmp/a.mp4", "new", True)])
    ctrl = SimpleNamespace(playlist=["/tmp/a.mp4"], repeat="off")
    calls = []
    sync = gui_function("_gui_sync_loop", deck_loop_state=lambda: next(states),
                        deck_playhead=lambda: ("/tmp/a.mp4", 17, True),
                        deck_has_picture=lambda: True,
                        _gui_kick=lambda *a, **k: calls.append(a))
    sync(ctrl)
    assert not calls


def test_live_metadata_is_preserved_and_backfill_skips_known_live_rows():
    ctrl = SimpleNamespace(playlist=app.playlist_normalize(["https://example.org/live"]))
    token = app.playlist_metadata_token(ctrl, "https://example.org/live")
    assert app.playlist_apply_metadata(ctrl, "https://example.org/live", token, live=True)
    assert ctrl.playlist[0]["live"] is True
    backfill = gui_function("_gui_backfill_meta", threading=None)
    backfill(ctrl)  # No worker/probe is needed for a known live stream.


def test_undo_restores_a_new_lifetime_and_requests_fresh_metadata():
    import json
    source = "/tmp/a.mp4"
    entry = app.playlist_entry(source)
    ctrl = SimpleNamespace(playlist=[entry], removed_now={source},
                           window=SimpleNamespace(undoManager=lambda: None),
                           playlist_table=SimpleNamespace(
                               selectRowIndexes_byExtendingSelection_=lambda *a: None,
                               scrollRowToVisible_=lambda *a: None))
    old_token = app.playlist_metadata_token(ctrl, source)
    app.playlist_remove_row(ctrl, 0)
    requested = []
    def put(ctrl, source):
        requested.append(source)
        app.playlist_metadata_token(ctrl, source)
    restore = gui_function("restoreRow_", playlist_save=lambda *a: None,
                           _gui_playlist_put=put, _gui_notify=lambda *a: None,
                           NSIndexSet=SimpleNamespace(indexSetWithIndex_=lambda i: i))
    restore(ctrl, {"row": 0, "entry": json.dumps(entry)})
    assert requested == [source]
    assert ctrl.removed_now == set()
    assert not app.playlist_apply_metadata(ctrl, source, old_token, "Stale")
    new_token = app.playlist_metadata_token(ctrl, source)
    assert app.playlist_apply_metadata(ctrl, source, new_token, "Fresh", duration=42)
    assert ctrl.playlist[0]["title"] == "Fresh"


@pytest.mark.parametrize("operation,timeout", [("play", 180.0), ("status", 20.0), ("reconnect", 180.0)])
def test_gui_command_timeout_allows_play_recovery_and_cleanup(monkeypatch, operation, timeout):
    def run(argv, **kwargs):
        assert kwargs["timeout"] == timeout
        if operation == "play":
            # Two bridge readiness attempts + bridge startup + OPEN + graceful cleanup.
            assert kwargs["timeout"] > 2 * app.studio.BRIDGE_READY_TIMEOUT + app.studio.BRIDGE_WAIT + 20 + 5
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(app.subprocess, "run", run)
    def managed(argv, **kwargs):
        result = run(argv, **kwargs)
        return result.returncode, result.stdout, result.stderr
    monkeypatch.setattr(app.lifecycle, "run_command", managed)
    assert app.run_cli([operation]).code == 0


@pytest.mark.parametrize("items", [[], ["/tmp/b.mp4"]])
def test_repeat_all_does_not_loop_a_removed_source(items):
    assert not app.playlist_should_loop("/tmp/a.mp4", items, "all")


def test_internal_restart_success_does_not_readopt_removed_track():
    ctrl = SimpleNamespace(playlist=[app.playlist_entry("/tmp/b.mp4")], removed_now={"/tmp/a.mp4"},
                           epoch=0, note=SimpleNamespace(setToolTip_=lambda *a: None))
    puts = []
    apply = gui_function("_gui_apply", _gui_set_busy=lambda *a: None,
                         _gui_notify=lambda *a: None, deck_has_picture=lambda: True,
                         _gui_playlist_put=lambda *a: puts.append(a))
    apply(ctrl, [app.CommandResult(["play", "/tmp/a.mp4"], 0, "", "")], None, 0)
    assert not puts
    assert [app.playlist_source(item) for item in ctrl.playlist] == ["/tmp/b.mp4"]


@pytest.mark.parametrize("preserve_queue", [False, True])
def test_explicit_play_clears_removed_suppression_but_loop_restart_preserves_it(preserve_queue):
    ctrl = SimpleNamespace(busy=True, user_stopped=True, removed_now={"/tmp/a.mp4"})
    kick = gui_function("_gui_kick", _gui_notify=lambda *a: None)
    kick(ctrl, "play", "/tmp/a.mp4", preserve_queue=preserve_queue)
    assert ("/tmp/a.mp4" in ctrl.removed_now) is preserve_queue


@pytest.mark.parametrize("detail", [
    "timed out; startup cleanup is still pending; wait before retrying",
    "startup handoff was committed but is still unconfirmed; wait before retrying",
])
def test_pending_cleanup_never_prompts_or_retries_play(detail):
    notes, retries = [], []
    ctrl = SimpleNamespace(pending_source="/tmp/next.mp4", pending_start=0, seen_watch="/tmp/old.mp4",
                           note=SimpleNamespace(setToolTip_=lambda value: None))
    apply = gui_function("_gui_apply", _gui_set_busy=lambda *args: None,
                         _gui_note_failure=lambda ctrl, text, raw: notes.append(text),
                         _gui_kick=lambda *args, **kwargs: retries.append(args))
    apply(ctrl, [app.CommandResult(["play", "/tmp/old.mp4"], 75, "", detail)], None)
    assert not retries
    assert notes and "기다리십시오" in notes[0]
    assert "다시 연결" not in notes[0]
    assert "다시 연결" not in app.play_failure_note(detail)

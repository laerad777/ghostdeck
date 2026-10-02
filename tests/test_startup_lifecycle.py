"""Real temporary processes only: no sockets, USB, adb, Studio or vendor execution."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from ghostdeck import lifecycle

SRC = str(Path(lifecycle.__file__).resolve().parents[1])


def env(tmp_path):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return dict(os.environ, HOME=str(home), PYTHONPATH=SRC)


def script(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(textwrap.dedent(body))
    return path


def wait_file(path, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return path.read_text()
        time.sleep(.01)
    pytest.fail(f"missing {path}")


def child(tmp_path, *, restore=.1):
    return script(tmp_path, "child.py", f'''
        import os, signal, time
        from pathlib import Path
        from ghostdeck.lifecycle import child_lease
        stopped = False
        def stop(*args):
            global stopped
            stopped = True
        signal.signal(signal.SIGTERM, stop)
        child_lease(lambda: os.kill(os.getpid(), signal.SIGTERM))
        Path({str(tmp_path / 'child.pid')!r}).write_text(str(os.getpid()))
        while not stopped:
            time.sleep(.01)
        Path({str(tmp_path / 'restoring')!r}).touch()
        time.sleep({restore})
        Path({str(tmp_path / 'restored')!r}).touch()
    ''')


def command(tmp_path, child_path, *, work=20, cleanup=2):
    return script(tmp_path, "command.py", f'''
        import subprocess, sys, time
        from ghostdeck.lifecycle import Command, Cancelled
        try:
            with Command(cleanup_wait={cleanup}) as command:
                command.spawn([sys.executable, {str(child_path)!r}],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              start_new_session=True)
                deadline = time.monotonic() + {work}
                while time.monotonic() < deadline:
                    command.check()
                    time.sleep(.01)
        except (Cancelled, RuntimeError) as error:
            print(str(error), file=sys.stderr)
            sys.exit(1)
    ''')


def probe_gate(tmp_path):
    return subprocess.run([sys.executable, "-c", "from ghostdeck.lifecycle import Command\nwith Command(): pass"],
                          env=env(tmp_path), capture_output=True, text=True, timeout=5)


def test_timeout_cancels_owned_child_and_waits_for_restore(tmp_path):
    runner = command(tmp_path, child(tmp_path))
    code, _, err = lifecycle.run_command([sys.executable, str(runner)], env=env(tmp_path), timeout=.3)
    assert code != 0 and "timed out" in err
    assert (tmp_path / "restored").exists()
    assert probe_gate(tmp_path).returncode == 0


def test_parent_sigkill_cancels_child_and_retains_gate_through_restore(tmp_path):
    runner = command(tmp_path, child(tmp_path, restore=.6))
    proc = subprocess.Popen([sys.executable, str(runner)], env=env(tmp_path))
    try:
        wait_file(tmp_path / "child.pid")
        proc.kill()  # Only this fixture process, proving cleanup even without Python finally.
        proc.wait(timeout=5)
        wait_file(tmp_path / "restoring")
        assert probe_gate(tmp_path).returncode != 0
        wait_file(tmp_path / "restored")
        assert probe_gate(tmp_path).returncode == 0
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


def test_cleanup_timeout_remains_explicit_and_blocks_retry(tmp_path):
    runner = command(tmp_path, child(tmp_path, restore=.8), cleanup=.1)
    code, _, err = lifecycle.run_command([sys.executable, str(runner)], env=env(tmp_path), timeout=.3)
    assert code != 0 and "cleanup is still pending" in err
    assert probe_gate(tmp_path).returncode != 0
    wait_file(tmp_path / "restored")
    assert probe_gate(tmp_path).returncode == 0


def test_success_handoff_detaches_only_owned_child(tmp_path):
    runner = command(tmp_path, child(tmp_path), work=.2)
    code, _, err = lifecycle.run_command([sys.executable, str(runner)], env=env(tmp_path), timeout=2)
    pid = int(wait_file(tmp_path / "child.pid"))
    try:
        assert (code, err) == (0, "")
        assert not (tmp_path / "restoring").exists()
        assert probe_gate(tmp_path).returncode == 0
    finally:
        os.kill(pid, signal.SIGTERM)  # Exact fixture PID still alive; no process census.
        wait_file(tmp_path / "restored")


def test_gui_cancellation_event_wins_before_handoff(tmp_path):
    runner = command(tmp_path, child(tmp_path))
    cancel = threading.Event()
    def stop():
        wait_file(tmp_path / "child.pid")
        cancel.set()
    worker = threading.Thread(target=stop)
    worker.start()
    code, _, err = lifecycle.run_command([sys.executable, str(runner)], env=env(tmp_path), timeout=5, cancel=cancel)
    worker.join(timeout=5)
    assert code != 0 and "cancelled" in err
    assert (tmp_path / "restored").exists()


def test_failed_startup_does_not_touch_unowned_process(tmp_path):
    foreign = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"], env=env(tmp_path))
    try:
        runner = command(tmp_path, child(tmp_path))
        code, _, _ = lifecycle.run_command([sys.executable, str(runner)], env=env(tmp_path), timeout=.3)
        assert code != 0
        assert foreign.poll() is None
    finally:
        foreign.terminate()
        foreign.wait(timeout=5)


def test_stop_serializes_after_cancelled_startup():
    from ghostdeck.app import CommandResult, DeckRemote
    entered, exited = threading.Event(), threading.Event()
    calls = []
    remote = None
    def run(argv):
        calls.append(argv[0])
        if argv[0] == "play":
            entered.set()
            assert remote._cancel.wait(3)
            time.sleep(.05)
            exited.set()
            return CommandResult(argv, 1, "", "cancelled")
        if argv[0] == "stop":
            assert exited.is_set()
        return CommandResult(argv, 0, "shim=up", "")
    remote = DeckRemote(run)
    worker = threading.Thread(target=lambda: remote.play("fake.mp4"))
    worker.start()
    assert entered.wait(3)
    assert remote.stop().code == 0
    worker.join(timeout=3)
    assert not worker.is_alive()
    assert calls == ["status", "play", "stop"]


def test_two_children_share_one_commit_decision(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir(); second.mkdir()
    a, b = child(first), child(second)
    runner = script(tmp_path, "two.py", f'''
        import subprocess, sys, time
        from ghostdeck.lifecycle import Command
        with Command() as command:
            for path in ({str(a)!r}, {str(b)!r}):
                command.spawn([sys.executable, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(.2)
    ''')
    code, _, err = lifecycle.run_command([sys.executable, str(runner)], env=env(tmp_path), timeout=2)
    pids = [int(wait_file(path / "child.pid")) for path in (first, second)]
    try:
        assert (code, err) == (0, "")
        assert all(not (path / "restoring").exists() for path in (first, second))
        assert probe_gate(tmp_path).returncode == 0
    finally:
        for pid in pids:
            os.kill(pid, signal.SIGTERM)
        for path in (first, second):
            wait_file(path / "restored")


def source_registry():
    import importlib.util
    path = Path(__file__).resolve().parents[1] / "vendor" / "d200_subprocess.py"
    spec = importlib.util.spec_from_file_location("test_source_processes", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SourceProcesses()


def test_source_tool_leader_exit_does_not_orphan_helpers(tmp_path):
    registry = source_registry()
    marker = tmp_path / "helper-completed"
    helper = script(tmp_path, "helper.py", f'''
        import signal, time
        from pathlib import Path
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(1.5)
        Path({str(marker)!r}).touch()
    ''')
    tool = script(tmp_path, "tool.py", f'''
        import subprocess, sys
        subprocess.Popen([sys.executable, {str(helper)!r}], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("result", flush=True)
    ''')
    began = time.monotonic()
    result = registry.run([sys.executable, str(tool)], capture_output=True, text=True)
    assert result.stdout == "result\n"
    assert time.monotonic() - began < 1.5
    time.sleep(1.5)
    assert not marker.exists()


def test_source_close_races_running_probe_and_refuses_late_spawn(tmp_path):
    registry = source_registry()
    marker = tmp_path / "started"
    tool = script(tmp_path, "probe.py", f'''
        import time
        from pathlib import Path
        Path({str(marker)!r}).touch()
        time.sleep(10)
    ''')
    errors = []
    def probe():
        try:
            registry.run([sys.executable, str(tool)], capture_output=True)
        except (InterruptedError, RuntimeError):
            errors.append(True)
    thread = threading.Thread(target=probe)
    thread.start()
    wait_file(marker)
    registry.close()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert not registry.children
    with pytest.raises(InterruptedError):
        registry.run([sys.executable, str(tool)])


def test_source_missing_tool_preserves_oserror(tmp_path):
    registry = source_registry()
    with pytest.raises(FileNotFoundError) as error:
        registry.run([str(tmp_path / "missing")], capture_output=True)
    assert error.value.filename == str(tmp_path / "missing")
    assert not registry.children


def test_source_guard_survives_closed_result_reader_until_group_cleanup(tmp_path):
    guard = Path(__file__).resolve().parents[1] / "vendor" / "d200_subprocess.py"
    marker, armed = tmp_path / "survived", tmp_path / "armed"
    helper = script(tmp_path, "helper.py", f'''
        import signal, time
        from pathlib import Path
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        Path({str(armed)!r}).touch()
        time.sleep(.8)
        Path({str(marker)!r}).touch()
    ''')
    status, answer = os.pipe()
    lease, writer = os.pipe()
    proc = subprocess.Popen([sys.executable, str(guard), str(answer), str(lease), sys.executable, str(helper)],
                            pass_fds=(answer, lease), start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    os.close(answer); os.close(lease)
    try:
        wait_file(armed)
        os.close(status); status = None
        os.close(writer); writer = None
        proc.wait(timeout=3)
        assert proc.returncode == -signal.SIGKILL
        time.sleep(.8)
        assert not marker.exists()
    finally:
        if status is not None: os.close(status)
        if writer is not None: os.close(writer)
        proc.communicate(timeout=3)


def test_gui_quit_cancels_startup_and_refuses_new_commands():
    from ghostdeck.app import CommandResult, DeckRemote
    remote = DeckRemote(lambda argv: CommandResult(argv, 0, "", ""))
    remote.shutdown()
    assert remote.play("file.mp4")[0].code != 0
    assert remote.stop().code != 0


def test_cancelled_command_does_not_interrupt_restoration(tmp_path):
    runner = script(tmp_path, "restoration.py", f'''
        import time
        from pathlib import Path
        from ghostdeck.lifecycle import Command, Cancelled
        try:
            with Command():
                Path({str(tmp_path / 'entered')!r}).touch()
                time.sleep(.4)
                Path({str(tmp_path / 'restored')!r}).touch()
        except Cancelled:
            raise SystemExit(1)
    ''')
    cancel = threading.Event()
    worker = threading.Thread(target=lambda: (wait_file(tmp_path / "entered"), cancel.set()))
    worker.start()
    code, _, _ = lifecycle.run_command([sys.executable, str(runner)], env=env(tmp_path), timeout=5, cancel=cancel)
    worker.join(timeout=5)
    assert code != 0
    assert (tmp_path / "restored").exists()


def test_media_child_is_registered_before_second_spawn_failure(tmp_path, monkeypatch):
    import importlib.util
    vendor = Path(__file__).resolve().parents[1] / "vendor"
    monkeypatch.syspath_prepend(str(vendor))
    spec = importlib.util.spec_from_file_location("lifecycle_player_fixture", vendor / "d200-color-play.py")
    player = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(player)
    real_popen = subprocess.Popen
    count = 0
    def spawn(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("speaker failed")
        return real_popen(*args, **kwargs)
    monkeypatch.setattr(player.subprocess, "Popen", spawn)
    try:
        with pytest.raises(OSError, match="speaker failed"):
            player.spawn_av([sys.executable, "-c", "import time; time.sleep(10)"], ["bad-speaker"])
        assert len(player._MEDIA_CHILDREN) == 1
    finally:
        for child in tuple(player._MEDIA_CHILDREN):
            player.stop_encoder(child)
    assert all(child.poll() is not None for child in player._MEDIA_CHILDREN)


def test_media_cancellation_during_popen_keeps_child_registered(monkeypatch):
    import importlib.util
    vendor = Path(__file__).resolve().parents[1] / "vendor"
    monkeypatch.syspath_prepend(str(vendor))
    spec = importlib.util.spec_from_file_location("lifecycle_media_cancel_fixture", vendor / "d200-color-play.py")
    player = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(player)
    real_popen = subprocess.Popen
    def spawn(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        assert player._SOURCE_PROCESSES.spawning
        player._SOURCE_PROCESSES.cancelled.set()
        return child
    monkeypatch.setattr(player.subprocess, "Popen", spawn)
    try:
        with pytest.raises(InterruptedError):
            player.spawn_media([sys.executable, "-c", "import time; time.sleep(10)"])
        assert len(player._MEDIA_CHILDREN) == 1
    finally:
        for child in tuple(player._MEDIA_CHILDREN):
            player.stop_encoder(child)


def test_actual_cli_play_grace_cancellation_owns_detached_player(tmp_path):
    player = child(tmp_path, restore=.1)
    source = tmp_path / "movie.mp4"
    source.touch()
    runner = script(tmp_path, "cli-play.py", f'''
        from pathlib import Path
        from ghostdeck import cli, play, studio, state
        state.STATE_PATH = Path({str(tmp_path / 'home' / '.ghostdeck' / 'state.json')!r})
        play._require_tools = lambda source: None
        play.adb.require_adb = lambda: 'fake-adb'
        studio.require_bridge_or_start_it = lambda: None
        play.devicebuild.ensure = lambda: None
        play.usb.detect = lambda: {{'mode': 'adb', 'serial': 'fixture'}}
        play._kill_play = lambda: None
        play._signal_speakers = lambda: None
        play.abandon_host_session = lambda: None
        play.VENDOR_PLAY = Path({str(player)!r})
        play._HOST_STATE = Path({str(tmp_path / 'never-published')!r})
        raise SystemExit(cli.main(['play', {str(source)!r}]))
    ''')
    code, _, err = lifecycle.run_command([sys.executable, str(runner)], env=env(tmp_path), timeout=.3)
    assert code != 0 and "timed out" in err
    assert (tmp_path / "restored").exists()
    assert probe_gate(tmp_path).returncode == 0


def test_managed_bridge_spawn_returns_real_owned_child_not_detached_wrapper(tmp_path):
    bridge = child(tmp_path)
    runner = script(tmp_path, "cli-bridge.py", f'''
        import sys, time
        from pathlib import Path
        from ghostdeck import lifecycle, studio
        studio.BRIDGE = Path({str(bridge)!r})
        studio.adb.require_adb = lambda: 'fake-adb'
        try:
            with lifecycle.Command():
                with open({str(tmp_path / 'bridge.log')!r}, 'w') as log:
                    child = studio._spawn_bridge('fixture', log)
                while not Path({str(tmp_path / 'child.pid')!r}).exists():
                    time.sleep(.01)
                assert child.pid == int(Path({str(tmp_path / 'child.pid')!r}).read_text())
                while True:
                    lifecycle.check()
                    time.sleep(.01)
        except lifecycle.Cancelled:
            raise SystemExit(1)
    ''')
    code, _, err = lifecycle.run_command([sys.executable, str(runner)], env=env(tmp_path), timeout=.3)
    assert code != 0 and "timed out" in err
    assert (tmp_path / "restored").exists()


def test_stopped_media_handles_do_not_accumulate_across_seeks(monkeypatch):
    import importlib.util
    vendor = Path(__file__).resolve().parents[1] / "vendor"
    monkeypatch.syspath_prepend(str(vendor))
    spec = importlib.util.spec_from_file_location("lifecycle_media_prune_fixture", vendor / "d200-color-play.py")
    player = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(player)
    children = [player.spawn_media([sys.executable, "-c", "import time; time.sleep(10)"]) for _ in range(2)]
    try:
        for child in tuple(player._MEDIA_CHILDREN):
            player.stop_encoder(child)
        assert not player._MEDIA_CHILDREN
        assert all(child.poll() is not None for child in children)
    finally:
        for child in tuple(player._MEDIA_CHILDREN):
            player.stop_encoder(child)

"""Device-free safety regressions for bridge selection and graceful shutdown."""
import json
import signal
from types import SimpleNamespace

import pytest

from ghostdeck import studio


def test_hid_deck_never_selects_adb_phone(monkeypatch):
    found = iter([{'mode': 'hid', 'serial': 'DECK'}, {'mode': 'adb', 'serial': 'DECK'}])
    monkeypatch.setattr(studio.usb, 'detect', lambda: next(found))
    monkeypatch.setattr(studio.adb, 'serial_from_devices', lambda: pytest.fail('positional fallback'))
    switches = []
    monkeypatch.setattr(studio.usb, 'enable_adb', lambda: switches.append(True))
    assert studio._adb_serial() == 'DECK'
    assert switches == [True]


@pytest.mark.parametrize('found', [None, {'mode': 'none'}, {'mode': 'adb', 'serial': None}])
def test_unknown_bus_identity_never_selects_phone(monkeypatch, found):
    monkeypatch.setattr(studio.usb, 'detect', lambda: found)
    monkeypatch.setattr(studio.adb, 'serial_from_devices', lambda: pytest.fail('positional fallback'))
    assert studio._bus_serial() == ''


def test_shutdown_timeout_never_kills_or_replaces(monkeypatch, tmp_path):
    monkeypatch.setattr(studio, 'BRIDGE_STATE', tmp_path / 'bridge.json')
    monkeypatch.setattr(studio, '_owned_bridge_pid', lambda: 42)
    monkeypatch.setattr(studio, '_pid_argv', lambda pid: str(studio.BRIDGE))
    calls = []
    monkeypatch.setattr(studio.os, 'kill', lambda pid, sig: calls.append((pid, sig)))
    with pytest.raises(RuntimeError, match='cleanup is still pending'):
        studio._stop_our_bridge(timeout=0)
    assert calls == [(42, signal.SIGTERM)]


def test_disconnected_restoring_bridge_blocks_retry(monkeypatch, tmp_path):
    record = tmp_path / 'bridge.json'
    record.write_text(json.dumps({'pid': 42}))
    monkeypatch.setattr(studio, 'BRIDGE_STATE', record)
    monkeypatch.setattr(studio, '_owned_bridge_pid', lambda: None)
    monkeypatch.setattr(studio, '_pid_argv', lambda pid: f'python {studio.BRIDGE}')
    monkeypatch.setattr(studio.adb, 'restart_server', lambda: pytest.fail('unsafe restart'))
    monkeypatch.setattr(studio, '_spawn_bridge', lambda *a, **k: pytest.fail('replacement'))
    for operation in (studio.reconnect, studio._ensure_bridge):
        with pytest.raises(RuntimeError, match='cleanup is still pending'):
            operation()


def test_recycled_state_pid_is_not_authority(monkeypatch, tmp_path):
    record = tmp_path / 'bridge.json'
    record.write_text(json.dumps({'pid': 42}))
    monkeypatch.setattr(studio, 'BRIDGE_STATE', record)
    monkeypatch.setattr(studio, '_pid_argv', lambda pid: 'unrelated-process')
    studio._refuse_pending_bridge_cleanup()


def test_multiple_usb_matches_are_refused(monkeypatch):
    import sys
    from ghostdeck import usb
    core = SimpleNamespace(find=lambda **kwargs: [object(), object()])
    monkeypatch.setitem(sys.modules, 'usb', SimpleNamespace(core=core))
    monkeypatch.setitem(sys.modules, 'usb.core', core)
    monkeypatch.setattr(usb, '_importable', lambda name: True)
    with pytest.raises(RuntimeError, match='multiple D200'):
        usb._usb_find(0x18d1, 0xd002)


def test_two_hid_decks_refuse_before_open_or_write(monkeypatch):
    from ghostdeck import usb
    entries = [{'interface_number': 0, 'path': b'/first', 'serial_number': 'a'},
               {'interface_number': 0, 'path': b'/second', 'serial_number': 'b'}]
    monkeypatch.setattr(usb, '_hid_module', lambda: SimpleNamespace(enumerate=lambda *args: entries))
    for operation in (usb._hid_serial, lambda: usb._hid_iface0(timeout=0)):
        with pytest.raises(RuntimeError, match='multiple D200 HID'):
            operation()
    usb._require_single_hid([entries[0], entries[0], {'interface_number': 1, 'path': b'/other-interface'}])


def test_restoration_can_finish_after_old_eight_second_deadline(monkeypatch, tmp_path):
    monkeypatch.setattr(studio, 'BRIDGE_STATE', tmp_path / 'bridge.json')
    monkeypatch.setattr(studio, '_owned_bridge_pid', lambda: 42)
    clock = [0.0]
    monkeypatch.setattr(studio.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(studio.time, 'sleep', lambda delay: clock.__setitem__(0, clock[0] + delay))
    monkeypatch.setattr(studio, '_process_still_exists', lambda pid: clock[0] < 13)
    signals = []
    monkeypatch.setattr(studio.os, 'kill', lambda pid, sig: signals.append(sig))
    studio._stop_our_bridge()
    assert clock[0] >= 13
    assert signals == [signal.SIGTERM]


def test_reconnect_does_not_restart_adb_after_pending_cleanup(monkeypatch):
    monkeypatch.setattr(studio, '_refuse_pending_bridge_cleanup', lambda: None)
    monkeypatch.setattr(studio, 'running', lambda: False)
    monkeypatch.setattr(studio, '_quit_official', lambda: None)
    def pending():
        raise RuntimeError('bridge cleanup is still pending')
    monkeypatch.setattr(studio, '_stop_our_bridge', pending)
    monkeypatch.setattr(studio.adb, 'restart_server', lambda: pytest.fail('restarted before cleanup'))
    monkeypatch.setattr(studio, 'launch', lambda: pytest.fail('replacement'))
    with pytest.raises(RuntimeError, match='cleanup is still pending'):
        studio.reconnect()


def test_pending_cleanup_path_with_spaces_still_blocks(monkeypatch, tmp_path):
    record = tmp_path / 'bridge.json'
    record.write_text(json.dumps({'pid': 42}))
    monkeypatch.setattr(studio, 'BRIDGE_STATE', record)
    monkeypatch.setattr(studio, 'BRIDGE', tmp_path / 'my checkout' / 'vendor' / 'd200-local-bridge.py')
    monkeypatch.setattr(studio, '_pid_argv', lambda pid: f'python {studio.BRIDGE} --serial test')
    monkeypatch.setattr(studio, '_owned_bridge_pid', lambda: None)
    with pytest.raises(RuntimeError, match='cleanup is still pending'):
        studio._refuse_pending_bridge_cleanup()


def test_missing_record_cannot_hide_restoring_bridge(monkeypatch, tmp_path):
    monkeypatch.setattr(studio, 'BRIDGE_STATE', tmp_path / 'removed-by-tmp-cleaner')
    monkeypatch.setattr(studio, '_bridge_process_pids', lambda: {42})
    monkeypatch.setattr(studio, '_owned_bridge_pid', lambda: None)
    monkeypatch.setattr(studio.adb, 'restart_server', lambda: pytest.fail('unsafe restart'))
    monkeypatch.setattr(studio, '_spawn_bridge', lambda *a, **k: pytest.fail('replacement'))
    for operation in (studio.reconnect, studio._ensure_bridge):
        with pytest.raises(RuntimeError, match='cleanup is still pending'):
            operation()


def test_census_failure_refuses_to_assume_no_cleanup(monkeypatch):
    import subprocess
    def failed(*args, **kwargs):
        raise subprocess.TimeoutExpired('ps', 5)
    monkeypatch.setattr(studio.subprocess, 'check_output', failed)
    with pytest.raises(RuntimeError, match='cannot rule out pending bridge cleanup'):
        studio._refuse_pending_bridge_cleanup()


def test_live_socket_owner_with_missing_record_remains_reusable(monkeypatch, tmp_path):
    monkeypatch.setattr(studio, 'BRIDGE_STATE', tmp_path / 'missing')
    monkeypatch.setattr(studio, '_bridge_process_pids', lambda: {42})
    monkeypatch.setattr(studio, '_owned_bridge_pid', lambda: 42)
    studio._refuse_pending_bridge_cleanup()


def test_census_is_only_a_veto_and_handles_spaced_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(studio, 'BRIDGE', tmp_path / 'my checkout' / 'bridge.py')
    monkeypatch.setattr(studio, 'BRIDGE_STATE', tmp_path / 'missing')
    monkeypatch.setattr(studio.subprocess, 'check_output', lambda *a, **k: f'42 python {studio.BRIDGE} --serial X\n43 unrelated-process\n')
    monkeypatch.setattr(studio, '_owned_bridge_pid', lambda: None)
    monkeypatch.setattr(studio.os, 'kill', lambda *a: pytest.fail('census must never signal'))
    assert studio._bridge_process_pids() == {42}
    with pytest.raises(RuntimeError, match='cleanup is still pending'):
        studio._stop_our_bridge()

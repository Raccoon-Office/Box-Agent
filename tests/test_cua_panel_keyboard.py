import subprocess
from types import SimpleNamespace

import pytest
from box_agent.mcp_servers import cua_panel_keyboard as keyboard


@pytest.fixture
def native(monkeypatch):
    monkeypatch.setattr(keyboard.sys, 'platform', 'darwin')
    monkeypatch.setattr(keyboard, '_helper', lambda: '/test/helper')


def test_unvalidated_chord_stays_on_driver(native, monkeypatch):
    monkeypatch.setattr(keyboard, '_helper', lambda: pytest.fail('must not compile'))
    assert keyboard.deliver_panel_key(1, 2, 's', ['cmd'], timeout=1) is None


@pytest.mark.parametrize('output', ['garbage', '{}', 'null', '{"effect":"confirmed"}'])
def test_invalid_helper_result_cannot_trigger_replay(native, monkeypatch, output):
    monkeypatch.setattr(keyboard.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout=output))
    with pytest.raises(RuntimeError, match='outcome unknown'):
        keyboard._invoke(['1', '2', '36'], timeout=1)


def test_timeout_cannot_trigger_replay(native, monkeypatch):
    def run(*args, **kwargs):
        raise subprocess.TimeoutExpired('helper', 1)
    monkeypatch.setattr(keyboard.subprocess, 'run', run)
    with pytest.raises(RuntimeError, match='outcome unknown'):
        keyboard._invoke(['1', '2', '36'], timeout=1)


def test_explicit_refusal_allows_driver(native, monkeypatch):
    monkeypatch.setattr(keyboard.subprocess, 'run', lambda *a, **k: SimpleNamespace(
        returncode=0, stdout='{"effect":"not_sent","code":"no_unique_remote_panel"}'))
    assert keyboard._invoke(['1', '2', '36'], timeout=1)['effect'] == 'not_sent'


def test_launch_failure_did_not_deliver(native, monkeypatch):
    def run(*args, **kwargs):
        raise FileNotFoundError('helper')
    monkeypatch.setattr(keyboard.subprocess, 'run', run)
    assert keyboard._invoke(['1', '2', '36'], timeout=1)['effect'] == 'not_sent'


def test_open_shortcut_uses_native_menu_equivalent(native, monkeypatch):
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout='{"effect":"unverifiable","code":"menu_equivalent_invoked","ax_result":0}')
    monkeypatch.setattr(keyboard.subprocess, 'run', run)
    report = keyboard.deliver_panel_key(123, 456, 'o', ['cmd'], timeout=5)
    assert report['code'] == 'menu_equivalent_invoked'
    assert calls == [['/test/helper', '123', '456', '31']]


def test_modified_open_shortcut_keeps_driver_semantics(native, monkeypatch):
    monkeypatch.setattr(keyboard, '_helper', lambda: pytest.fail('must not compile'))
    assert keyboard.deliver_panel_key(1, 2, 'o', ['cmd', 'shift'], timeout=1) is None


def test_field_focus_accepts_only_helper_verified_focus(native, monkeypatch):
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout='{"effect":"confirmed","code":"panel_field_focus"}')
    monkeypatch.setattr(keyboard.subprocess, 'run', run)
    assert keyboard.focus_panel_field(12, 34, (10, 20, 30, 40), timeout=5)['effect'] == 'confirmed'
    assert calls == [['/test/helper', 'focus-field', '12', '34', '10', '20', '30', '40']]


def test_panel_receiver_lease_reuses_child_and_releases_on_close(native, monkeypatch):
    import sys
    real_popen = subprocess.Popen
    children = []
    def launch(args, **kwargs):
        child = real_popen([sys.executable, '-u', '-c',
                           'import sys; print(\'{"ready":true}\'); list(sys.stdin)'], **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(keyboard.subprocess, 'Popen', launch)
    session = keyboard.PanelReceiverLease()
    try:
        first = session.environment(12, 1, 12, 34)
        assert first['BOX_CUA_PANEL_LEASE_PID'] == str(children[0].pid)
        assert session.environment(12, 1, 12, 34) == first
        assert len(children) == 1
        second = session.environment(13, 1, 13, 35)
        assert children[0].poll() == 0
        assert second['BOX_CUA_PANEL_TOKEN'] != first['BOX_CUA_PANEL_TOKEN']
    finally:
        session.close()
    assert all(child.poll() == 0 for child in children)
    session.close()


def test_panel_resolves_before_lease_and_revalidates_at_delivery(native, monkeypatch):
    calls, leases, closed = [], [], []
    def invoke(args, **kwargs):
        calls.append(kwargs.get('env', {}))
        if len(calls) == 1:
            return {'effect': 'not_sent', 'code': 'panel_target_resolved',
                    'resolved_pid': 24, 'receiver_window_id': 56}
        return {'effect': 'unverifiable', 'code': 'panel_key_posted'}
    monkeypatch.setattr(keyboard, '_invoke', invoke)
    session = SimpleNamespace(host=SimpleNamespace(environment=lambda *a: {}), environment=lambda *args: leases.append(args) or {'lease': 'owned'},
                              close=lambda: closed.append(True))
    result = keyboard.deliver_panel_key(12, 34, 'g', ['cmd', 'shift'], timeout=2, session=session)
    assert result['code'] == 'panel_key_posted'
    assert calls[0]['BOX_CUA_RESOLVE_PANEL'] == '1'
    assert leases == [(12, 24, 56, 2, 34)]
    assert calls[1] == {'lease': 'owned'}
    assert not closed


def test_panel_target_changed_releases_before_fallback(native, monkeypatch):
    reports = iter([{'effect': 'not_sent', 'code': 'panel_target_resolved',
                     'resolved_pid': 24, 'receiver_window_id': 56},
                    {'effect': 'not_sent', 'code': 'panel_target_changed'}])
    monkeypatch.setattr(keyboard, '_invoke', lambda *a, **k: next(reports))
    closed = []
    session = SimpleNamespace(host=SimpleNamespace(environment=lambda *a: {}), environment=lambda *a: {}, close=lambda: closed.append(True))
    assert keyboard.deliver_panel_key(12, 34, 'return', [], timeout=2,
                                      session=session)['code'] == 'panel_target_changed'
    assert closed == [True]


def test_indexed_confirmation_checks_same_field_at_resolution_and_delivery(native, monkeypatch):
    calls = []
    def invoke(args, **kwargs):
        calls.append(kwargs['env'])
        return ({'effect': 'not_sent', 'code': 'panel_target_resolved',
                 'resolved_pid': 24, 'receiver_window_id': 56} if len(calls) == 1
                else {'effect': 'unverifiable', 'code': 'panel_key_posted'})
    monkeypatch.setattr(keyboard, '_invoke', invoke)
    session = SimpleNamespace(host=SimpleNamespace(environment=lambda *a: {}),
                              environment=lambda *a: {}, close=lambda: None)
    keyboard.deliver_panel_key(12, 34, 'return', [], timeout=2, session=session,
                               field_frame=(100, 200, 300, 40))
    assert calls[0]['BOX_CUA_PANEL_FIELD_FRAME'] == '[100, 200, 300, 40]'
    assert calls[1]['BOX_CUA_PANEL_FIELD_FRAME'] == calls[0]['BOX_CUA_PANEL_FIELD_FRAME']


def test_panel_session_keeps_both_processes_until_retarget_or_close(monkeypatch):
    children = []
    class Lease:
        def __init__(self):
            self.closed = 0
            children.append(self)
        def close(self):
            self.closed += 1
        def environment(self, pid, timeout, host, window):
            return {'pid': str(pid)}
    monkeypatch.setattr(keyboard, 'PanelReceiverLease', Lease)
    session = keyboard.PanelFocusSession()
    first = session.environment(12, 24, 56, 2, 34)
    counts = [c.closed for c in children]
    assert session.environment(12, 24, 56, 2, 34) == first
    assert [c.closed for c in children] == counts
    session.environment(12, 25, 57, 2, 34)
    assert [c.closed for c in children] == counts
    session.environment(13, 25, 57, 2, 35)
    assert [c.closed for c in children] == [n + 1 for n in counts]
    session.close()
    assert session.identity is None


def test_sheet_action_passes_observed_identity_and_value(native, monkeypatch):
    import json
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout='{"effect":"unverifiable","code":"sheet_action_invoked"}')
    monkeypatch.setattr(keyboard.subprocess, 'run', run)
    keyboard.perform_sheet_action(12, 34, (1, 2, 300, 200),
        {'role': 'AXTextField', 'frame': {'x': 3, 'y': 4, 'w': 100, 'h': 20},
         'value': 'old.docx', 'identifier': 'filename'}, 'set_value', value='new.docx', timeout=5)
    assert calls[0][:4] == ['/test/helper', 'sheet-action', '12', '34']
    request = json.loads(calls[0][4])
    assert request['old_value'] == 'old.docx' and request['value'] == 'new.docx'
    assert request['identifier'] == 'filename'
    assert request['sheet_frame'] == [1, 2, 300, 200]


def test_focused_editor_preserves_unicode_payload_and_window(native, monkeypatch):
    import json
    calls = []
    def invoke(args, **kwargs):
        calls.append((args, kwargs))
        return {'effect': 'unverifiable', 'code': 'focused_editor_posted'}
    monkeypatch.setattr(keyboard, '_invoke', invoke)
    payload = '中文_😀' * 200
    report = keyboard.deliver_focused_editor(12, 34, frame=(1, 2, 3, 4), text=payload, timeout=5)
    assert report['effect'] == 'unverifiable'
    args, kwargs = calls[0]
    assert args[:3] == ['focused-editor', '12', '34']
    assert json.loads(args[3]) == {'frame': [1, 2, 3, 4], 'text': payload}
    assert kwargs == {'timeout': 5}


def test_focused_editor_unsupported_navigation_stays_on_driver(native, monkeypatch):
    monkeypatch.setattr(keyboard, '_invoke', lambda *a, **k: pytest.fail('unsupported navigation'))
    assert keyboard.deliver_focused_editor(1, 2, frame=(0, 0, 1, 1), navigation='s', timeout=1) is None


@pytest.mark.parametrize('key,modifiers,keycode', [
    ('down', ['cmd', 'shift'], 125),
    ('end', ['cmd'], 119),
    ('z', ['cmd', 'shift'], 6),
])
def test_editor_selection_navigation_and_undo_preserve_modifiers(native, monkeypatch, key, modifiers, keycode):
    import json

    calls = []
    monkeypatch.setattr(keyboard, '_invoke', lambda args, **kwargs: calls.append(args) or
                        {'effect': 'unverifiable', 'code': 'focused_editor_posted'})
    keyboard.deliver_focused_editor(12, 34, frame=(1, 2, 3, 4), timeout=5,
                                   key=key, modifiers=modifiers)
    assert json.loads(calls[0][3]) == {
        'frame': [1, 2, 3, 4], 'keycode': keycode, 'modifiers': modifiers,
    }


@pytest.mark.parametrize('key,modifiers', [('s', ['cmd']), ('a', []), ('a', ['ctrl']), ('down', ['unknown'])])
def test_non_editor_shortcuts_stay_on_driver(native, monkeypatch, key, modifiers):
    monkeypatch.setattr(keyboard, '_invoke', lambda *a, **k: pytest.fail('unexpected native input'))
    assert keyboard.deliver_focused_editor(12, 34, frame=(1, 2, 3, 4), timeout=5,
                                          key=key, modifiers=modifiers) is None

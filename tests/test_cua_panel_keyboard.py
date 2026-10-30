"""Remote panel delivery is bounded and never replays uncertain input."""
from types import SimpleNamespace

import pytest

from box_agent.mcp_servers import cua_adapter as adapter
from box_agent.mcp_servers import cua_panel_keyboard as panel
from tests.test_cua_038 import observed
from tests.test_cua_adapter_port import no_native_input_in_fake_driver_tests
from tests.test_cua_upstream_attachments import popover_app


@pytest.mark.parametrize('effect', ['unverifiable', 'not_sent'])
def test_panel_route_only_falls_through_after_not_sent(monkeypatch, effect):
    driver, api, state = observed()
    calls = []
    def deliver(pid, window, key, modifiers, **kwargs):
        calls.append((pid, window, key, modifiers))
        return {'effect': effect, 'code': 'panel_key_posted' if effect != 'not_sent' else 'not_open_panel'}
    monkeypatch.setattr(adapter, 'deliver_panel_key', deliver)
    api.control('pressKey', 'app', {'key': 'cmd+shift+g'})
    assert calls == [(42, 7, 'g', ['cmd', 'shift'])]
    assert sum(n == 'hotkey' for n, _ in driver.calls) == (effect == 'not_sent')
    assert not any(n == 'bring_to_front' for n, _ in driver.calls)


def test_unknown_helper_outcome_closes_lease_without_driver_replay(monkeypatch):
    driver, api, state = observed()
    closed = []
    monkeypatch.setattr(api.panel_focus, 'close', lambda: closed.append(True))
    def fail(*args, **kwargs):
        raise RuntimeError('outcome unknown')
    monkeypatch.setattr(adapter, 'deliver_panel_key', fail)
    with pytest.raises(RuntimeError, match='unknown'):
        api.control('pressKey', 'app', {'key': 'return'})
    assert closed
    assert not any(n in {'press_key', 'hotkey', 'bring_to_front'} for n, _ in driver.calls)


@pytest.mark.parametrize('args', [
    {'key': 'return', 'target': 0},
    {'key': 'cmd+shift+g', 'deliveryMode': 'foreground'},
])
def test_explicit_target_and_foreground_do_not_enter_panel_route(monkeypatch, args):
    driver, api, state = observed()
    monkeypatch.setattr(adapter, 'deliver_panel_key', lambda *a, **k: pytest.fail('unexpected helper'))
    api.control('pressKey', 'app', args)
    assert any(n in {'press_key', 'hotkey'} for n, _ in driver.calls)


@pytest.mark.parametrize('key,modifiers', [('s', ['cmd']), ('a', ['cmd']), ('tab', [])])
def test_helper_leaves_menu_and_editor_keys_to_driver(monkeypatch, key, modifiers):
    monkeypatch.setattr(panel.sys, 'platform', 'darwin')
    monkeypatch.setattr(panel, '_invoke', lambda *a, **k: pytest.fail('unexpected input'))
    assert panel.deliver_panel_key(42, 7, key, modifiers, timeout=1) is None


@pytest.mark.parametrize('output,status', [('garbled', 0), ('{}', 0), ('{"effect":"not_sent"}', 1), ('{"effect":"partial"}', 0)])
def test_malformed_or_crashed_helper_is_unknown_not_safe_to_retry(monkeypatch, output, status):
    monkeypatch.setattr(panel, '_helper', lambda: '/fake/helper')
    monkeypatch.setattr(panel.subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout=output, returncode=status))
    with pytest.raises(RuntimeError, match='unknown'):
        panel._invoke(['42', '7', '36'], timeout=1)


def test_helper_timeout_is_unknown(monkeypatch):
    monkeypatch.setattr(panel, '_helper', lambda: '/fake/helper')
    def timeout(*a, **k):
        raise panel.subprocess.TimeoutExpired('helper', 1)
    monkeypatch.setattr(panel.subprocess, 'run', timeout)
    with pytest.raises(RuntimeError, match='unknown'):
        panel._invoke(['42', '7', '36'], timeout=1)


def test_adapter_close_releases_panel_session(monkeypatch):
    driver, api, state = observed()
    closed = []
    monkeypatch.setattr(api.panel_focus, 'close', lambda: closed.append(True))
    api.close()
    assert closed == [True]


@pytest.mark.parametrize('effect', ['not_sent', 'unverifiable'])
def test_addressed_path_return_preserves_ax_confirm_when_helper_cannot_prove_field(monkeypatch, popover_app, effect):
    api, driver, state, rows, root = popover_app
    original = driver.call
    def call(name, args):
        response = original(name, args)
        if name == 'get_window_state':
            for e in response['structuredContent']['elements']:
                if e.get('role') == 'AXTextField':
                    e.update(actions=['AXConfirm'], frame={'x': 230, 'y': 60, 'width': 200, 'height': 20})
        return response
    monkeypatch.setattr(driver, 'call', call)
    api.observe(state, 'getAXState')
    index = next(i for i, e in state['elements'].items() if e.get('role') == 'AXTextField')
    calls = []
    def deliver(*args, **kwargs):
        calls.append((args, kwargs))
        return {'effect': effect, 'code': 'panel_field_identity_unverified' if effect == 'not_sent' else 'panel_key_posted'}
    monkeypatch.setattr(adapter, 'deliver_panel_key', deliver)
    driver.calls.clear()
    api.control('pressKey', 'app', {'key': 'return', 'target': index})
    assert calls[0][0][:4] == (42, 80, 'return', [])
    assert calls[0][1]['field_frame'] == (230.0, 60.0, 200.0, 20.0)
    assert not any(n in {'press_key', 'hotkey', 'bring_to_front'} for n, _ in driver.calls)
    clicks = [p for n, p in driver.calls if n == 'click']
    assert len(clicks) == (effect == 'not_sent')
    if clicks:
        assert clicks[0]['action'] == 'confirm' and clicks[0]['window_id'] == 73
        assert clicks[0]['element_token'] == 'parent:2'



@pytest.mark.parametrize('code,panel_proven,escalates', [
    ('panel_focus_unavailable', True, True),
    ('panel_lease_unavailable', True, True),
    ('native_helper_permission_unavailable', True, True),
    ('panel_focus_unavailable', False, False),
    ('not_file_panel', True, False),
    ('focused_window_changed', True, False),
])
def test_helper_not_sent_routes_directly_to_verified_foreground(monkeypatch, code, panel_proven, escalates):
    driver, api, state = observed()
    monkeypatch.setattr(adapter, 'deliver_panel_key', lambda *a, **k: {'effect': 'not_sent', 'code': code})
    monkeypatch.setattr(api, 'current_file_panel', lambda *a: panel_proven)
    api.control('pressKey', 'app', {'key': 'cmd+shift+g'})
    posted = [p for n,p in driver.calls if n == 'hotkey']
    assert len(posted) == 1
    assert (posted[0].get('scope') == 'desktop') == escalates
    assert posted[0]['delivery_mode'] == ('foreground' if escalates else 'background')
    assert any(n == 'bring_to_front' for n,_ in driver.calls) == escalates


def test_open_menu_is_left_to_driver(monkeypatch):
    monkeypatch.setattr(panel.sys, 'platform', 'darwin')
    monkeypatch.setattr(panel, '_invoke', lambda *a, **k: pytest.fail('unexpected helper'))
    assert panel.deliver_panel_key(42, 7, 'o', ['cmd'], timeout=1) is None


def test_path_value_uses_driver_sheet_token(monkeypatch, popover_app):
    api, driver, state, rows, root = popover_app
    monkeypatch.setattr(panel, '_invoke', lambda *a, **k: pytest.fail('unexpected helper'))
    api.observe(state, 'getAXState')
    index = next(i for i, e in state['elements'].items() if e.get('role') == 'AXTextField')
    driver.calls.clear()
    api.control('setValue', 'app', {'target': index, 'value': '/tmp/coverage.docx'})
    writes = [p for n, p in driver.calls if n == 'set_value']
    assert len(writes) == 1
    assert writes[0]['element_token'] == 'parent:2'
    assert writes[0]['value'] == '/tmp/coverage.docx'
    assert not any(n in {'bring_to_front', 'hotkey', 'press_key'} for n, _ in driver.calls)


@pytest.mark.parametrize('identifier,proven', [('open-panel',True),('save-panel',True),('GoToWindow',True),('other',False)])
def test_file_panel_proof_uses_exact_ax_root(monkeypatch, identifier, proven):
    driver, api, state = observed()
    monkeypatch.setattr(api, 'call', lambda *a, **k: {
        'structuredContent': {'elements':[{'depth':0,'role':'AXWindow'}]},
        'content':[{'type':'text','text':f'- [0] AXWindow "Title" [id={identifier} actions=[raise]]'}]})
    assert api.current_file_panel(state, {'window_id':7}) == proven

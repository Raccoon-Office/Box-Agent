"""PR225 adaptor behavior retained independently of removed native input helpers."""
import pytest
from box_agent.mcp_servers import cua_adapter as adapter
from tests.test_cua_adapter_port import FakeDriver, result, no_native_input_in_fake_driver_tests
from tests.test_cua_upstream_attachments import popover_app


@pytest.mark.parametrize('method,args', [
    ('pressKey', {'key': 'cmd+shift+g'}),
    ('click', {'target': [100, 120]}),
])
def test_nonordinary_window_uses_observed_ax_proof_before_foreground_input(method, args):
    driver = FakeDriver()
    window = driver.responses['list_windows']['structuredContent']['windows'][0]
    window['layer'] = 8
    raw = driver.responses['get_window_state']['structuredContent']
    raw['elements'][0].update(role='AXWindow')
    raw.update(screenshot_width=800, screenshot_height=600, screenshot_frame_valid=True,
               window_bounds=window['bounds'],
               background_input={'exact_window': {'pid': 42, 'window_id': 7, 'status': 'matched'}})
    api = adapter.Adapter(driver)
    api.handles['app'] = state = {'pid': 42, 'app_active': True}
    api.observe(state, 'getAXStateAndScreenshot')
    driver.calls.clear()
    api.control(method, 'app', {**args, 'deliveryMode': 'foreground'})
    assert not any(n == 'bring_to_front' for n, _ in driver.calls)
    assert driver.calls[-1][1]['scope'] == 'desktop'
    assert driver.calls[-1][1]['delivery_mode'] == 'foreground'


@pytest.mark.parametrize('invalid', ['missing_ax', 'wrong_pid', 'moved', 'inactive'])
def test_nonordinary_window_without_live_ownership_proof_sends_no_input(invalid):
    driver = FakeDriver()
    window = driver.responses['list_windows']['structuredContent']['windows'][0]
    window['layer'] = 8
    driver.responses['get_window_state']['structuredContent']['elements'][0]['role'] = 'AXWindow'
    driver.responses['bring_to_front'] = {'isError': True, **result(code='bring_to_front_window_not_ordinary')}
    api = adapter.Adapter(driver)
    api.handles['app'] = state = {'pid': 42, 'app_active': True}
    api.observe(state, 'getAXState')
    observed = state['window_states'][7]
    if invalid == 'missing_ax':
        observed['raw_elements'] = []
    elif invalid == 'wrong_pid':
        observed['pid'] = 99
    elif invalid == 'moved':
        window['bounds'] = {**window['bounds'], 'x': 500}
    else:
        state['app_active'] = False
    driver.calls.clear()
    with pytest.raises((ValueError, adapter.ActionError)):
        api.control('pressKey', 'app', {'key': 'cmd+shift+g', 'deliveryMode': 'foreground'})
    assert not any(n in {'press_key', 'hotkey'} for n, _ in driver.calls)


def test_verified_sheet_foreground_click_uses_desktop_coordinates(popover_app):
    api, driver, state, rows, root = popover_app
    driver.tools['get_screen_size'] = {'inputSchema': {'properties': {}}}
    driver.responses['get_screen_size'] = result(scale_factor=2)
    api.observe(state, 'getAXStateAndScreenshot')
    driver.calls.clear()
    api.control('click', 'app', {'target': [20, 30], 'deliveryMode': 'foreground'})
    tool, args = driver.calls[-1]
    assert tool == 'click'
    assert args['scope'] == 'desktop' and args['delivery_mode'] == 'foreground'
    assert (args['x'], args['y']) == (496, 170)
    assert 'pid' not in args and 'window_id' not in args


@pytest.mark.parametrize('activated', [True, False])
def test_sheet_foreground_keys_require_driver_activation_and_live_proof(popover_app, monkeypatch, activated):
    api, driver, state, rows, root = popover_app
    state['bundle_id'] = 'com.apple.TextEdit'
    app = driver.responses['list_apps']['structuredContent']['apps'][0]
    app['active'] = False
    api.observe(state, 'getAXState')
    original = driver.call

    def call(name, args):
        response = original(name, args)
        if name == 'bring_to_front':
            assert args == {'pid': 42, 'window_id': 73}
            app['active'] = activated
        return response

    monkeypatch.setattr(driver, 'call', call)
    driver.calls.clear()
    if activated:
        api.control('pressKey', 'app', {'key': 'return', 'deliveryMode': 'foreground'})
        tool, args = driver.calls[-1]
        assert tool == 'press_key' and args['scope'] == 'desktop'
        assert args['key'] == 'return' and args['delivery_mode'] == 'foreground'
    else:
        with pytest.raises(ValueError, match='owning app is not confirmed foreground'):
            api.control('pressKey', 'app', {'key': 'return', 'deliveryMode': 'foreground'})
        assert not any(n == 'press_key' for n, _ in driver.calls)


@pytest.mark.parametrize('code', ['background_unavailable', 'same_pid_keyboard_ambiguity', 'off_space_or_ax_unresolved'])
def test_explicit_background_retains_pr225_verified_fallback(monkeypatch, code):
    driver = FakeDriver()
    api = adapter.Adapter(driver)
    api.handles['app'] = state = {'pid': 42}
    api.observe(state, 'getAXState')
    original = driver.call

    def call(name, args):
        response = original(name, args)
        if name == 'type_text' and args['delivery_mode'] == 'background':
            return {'isError': True, **result(code=code, effect='refused')}
        return response

    monkeypatch.setattr(driver, 'call', call)
    api.control('typeText', 'app', {'text': 'hello', 'target': 0, 'deliveryMode': 'background'})
    inputs = [a for n, a in driver.calls if n == 'type_text']
    assert [a['delivery_mode'] for a in inputs] == ['background', 'foreground']
    assert all((a['pid'], a['window_id'], a['element_token']) == (42, 7, 's1:1') for a in inputs)
    assert [a for n, a in driver.calls if n == 'bring_to_front'] == [{'pid': 42, 'window_id': 7}]


def test_foreground_focus_failure_prevents_input():
    driver = FakeDriver()
    api = adapter.Adapter(driver)
    api.handles['app'] = state = {'pid': 42}
    api.observe(state, 'getAXState')
    driver.responses['bring_to_front'] = result(exact_window_effect={'verified': False})
    with pytest.raises(ValueError, match='focus was not verified'):
        api.control('pressKey', 'app', {'key': 'cmd+o', 'deliveryMode': 'foreground'})
    assert not any(n in {'hotkey', 'press_key'} for n, _ in driver.calls)


@pytest.mark.parametrize('failure', ['inactive', 'blocked', 'focus', 'ambiguous'])
def test_attachment_failure_never_falls_back_to_activation(popover_app, failure):
    api, driver, state, rows, root = popover_app
    api.observe(state, 'getAXState')
    if failure == 'inactive':
        state['app_active'] = False
    elif failure == 'blocked':
        rows.append({**rows[0], 'window_id': 99, 'z_index': 9})
    elif failure == 'focus':
        state['attachment_focus_count'] = 0
    else:
        state['attachment_conflicts'] = {80}
    start = len(driver.calls)
    with pytest.raises(ValueError, match='AttachmentUnverified'):
        api.focus_window(state, rows[1], keyboard=True, observation_fresh=True)
    assert not any(name == 'bring_to_front' for name, _ in driver.calls[start:])


def test_explicit_false_focus_is_not_missing(popover_app, monkeypatch):
    api, driver, state, rows, root = popover_app
    original = driver.call
    def call(name, arguments):
        response = original(name, arguments)
        if name == 'get_window_state':
            for element in response['structuredContent']['elements']:
                if 'focused' in element:
                    element['focused'] = False
        return response
    monkeypatch.setattr(driver, 'call', call)
    api.observe(state, 'getAXState')
    assert state['attachment_focus_reported'] and state['attachment_focus_count'] == 0
    start = len(driver.calls)
    with pytest.raises(ValueError, match='focused-control evidence conflicts'):
        api.control('pressKey', 'app', {'key': 'cmd+a'})
    assert not any(name in {'bring_to_front', 'hotkey'} for name, _ in driver.calls[start:])


def test_foreground_activation_invalidates_app_inventory(monkeypatch):
    driver = FakeDriver()
    api = adapter.Adapter(driver)
    state = {'pid': 42, 'bundle_id': 'com.apple.TextEdit'}
    api.handles['app'] = state
    api.observe(state, 'getAXState')
    original = driver.call

    def call(name, args):
        response = original(name, args)
        if name == 'bring_to_front':
            driver.responses['list_apps']['structuredContent']['apps'][0]['pid'] = 99
        return response

    monkeypatch.setattr(driver, 'call', call)
    start = len(driver.calls)
    with pytest.raises(ValueError, match='ApplicationChanged'):
        api.control('pressKey', 'app', {'key': 'cmd+a', 'deliveryMode': 'foreground'})
    calls = driver.calls[start:]
    # Activation may replace the process. A preflight inventory (however many
    # reads it needs) must not satisfy the identity check after activation.
    activation = next(i for i, (name, _) in enumerate(calls) if name == 'bring_to_front')
    assert any(name == 'list_apps' for name, _ in calls[activation + 1:])
    assert not any(name in {'hotkey', 'press_key', 'type_text', 'clipboard_write'}
                   for name, _ in calls)


@pytest.mark.parametrize('fresh,frontmost', [(False, True), (True, False)])
def test_missing_focus_still_requires_fresh_frontmost_target(popover_app, fresh, frontmost):
    api, driver, state, rows, root = popover_app
    api.observe(state, 'getAXState')
    state.update(attachment_focus_reported=False, attachment_focus_count=0, attachment_focused=set())
    if not frontmost:
        rows.append({**rows[1], 'window_id': 99, 'z_index': 9})
        state['attachments'][99] = [{'source': api.frame(rows[1]), 'surface': api.frame(rows[2])}]
    start = len(driver.calls)
    with pytest.raises(ValueError, match='AttachmentUnverified'):
        api.focus_window(state, rows[1], keyboard=True, observation_fresh=fresh)
    assert not any(name == 'bring_to_front' for name, _ in driver.calls[start:])

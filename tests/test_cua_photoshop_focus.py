"""Keyboard preflight must not dismiss a live custom editor or replay input."""

import pytest
from box_agent.mcp_servers import cua_adapter as adapter
from tests.test_cua_adapter_port import (
    FakeDriver,
    result,
    no_native_input_in_fake_driver_tests,
)


def setup(monkeypatch, active=True):
    driver = FakeDriver()
    driver.responses['list_apps']['structuredContent']['apps'][0]['active'] = active
    monkeypatch.setattr(adapter, 'native_focused_window_id', lambda *a, **k: 7)
    api = adapter.Adapter(driver)
    state = {'pid': 42, 'bundle_id': 'com.apple.TextEdit'}
    api.handles['app'] = state
    api.observe(state, 'getAXState')
    driver.calls.clear()
    return api, state, driver


@pytest.mark.parametrize('change', ['toolbar', 'target', 'unstable'])
def test_keyboard_inventory_transition_rereads_before_any_paste(monkeypatch, change):
    api, state, driver = setup(monkeypatch)
    original = driver.call
    reads = 0

    def call(name, args):
        nonlocal reads
        response = original(name, args)
        if name == 'list_windows':
            reads += 1
            if reads >= 2:
                rows = response['structuredContent']['windows']
                if change == 'target':
                    rows[0]['bounds']['x'] = 20
                else:
                    rows.append(
                        {
                            'window_id': 8,
                            'pid': 42,
                            'title': 'Context bar',
                            'is_on_screen': True,
                            'layer': 8,
                            'bounds': {
                                'x': reads if change == 'unstable' else 10,
                                'y': 10,
                                'width': 100,
                                'height': 30,
                            },
                        }
                    )
        return response

    monkeypatch.setattr(driver, 'call', call)
    if change == 'toolbar':
        api.control('paste', 'app', {'text': 'AMAZING', 'deliveryMode': 'background'})
        assert sum(n == 'clipboard_write' for n, _ in driver.calls) == 1
        assert sum(n == 'hotkey' for n, _ in driver.calls) == 1
        assert sum(n == 'get_window_state' for n, _ in driver.calls) == 2
    else:
        with pytest.raises(ValueError, match='WindowChanged'):
            api.control(
                'paste', 'app', {'text': 'AMAZING', 'deliveryMode': 'background'}
            )
        assert not any(n in {'clipboard_write', 'hotkey'} for n, _ in driver.calls)
        assert sum(n == 'get_window_state' for n, _ in driver.calls) <= 2


@pytest.mark.parametrize(
    'role,tool',
    [('AXTextField', 'press_key'), ('AXTextArea', 'press_key'), ('AXButton', 'hotkey')],
)
def test_addressed_editing_shortcut_preserves_field_semantics(monkeypatch, role, tool):
    api, state, driver = setup(monkeypatch, active=False)
    driver.responses['get_window_state']['structuredContent']['elements'][0]['role'] = (
        role
    )
    api.observe(state, 'getAXState')
    driver.calls.clear()
    api.control('pressKey', 'app', {'target': 0, 'key': 'cmd+a'})
    inputs = [(n, a) for n, a in driver.calls if n in {'press_key', 'hotkey'}]
    assert len(inputs) == 1 and inputs[0][0] == tool
    assert inputs[0][1]['element_token'] == 's1:1'
    if tool == 'press_key':
        assert inputs[0][1]['key'] == 'a'
        assert inputs[0][1]['modifiers'] == ['cmd']


def test_addressed_paste_writes_clipboard_once_and_sends_field_key(monkeypatch):
    api, state, driver = setup(monkeypatch, active=False)
    driver.responses['get_window_state']['structuredContent']['elements'][0]['role'] = (
        'AXTextField'
    )
    api.observe(state, 'getAXState')
    driver.calls.clear()
    api.control('paste', 'app', {'target': 0, 'text': 'AMAZING'})
    inputs = [
        (n, a)
        for n, a in driver.calls
        if n in {'clipboard_write', 'press_key', 'hotkey'}
    ]
    assert [n for n, _ in inputs] == ['clipboard_write', 'press_key']
    assert inputs[1][1]['element_token'] == 's1:1'
    assert inputs[1][1]['modifiers'] == ['cmd']
    assert inputs[1][1]['key'] == 'v'


@pytest.mark.parametrize('active,focused', [(True, 7), (False, 7), (True, None)])
def test_foreground_keyboard_only_preserves_fresh_exact_focus(
    monkeypatch, active, focused
):
    api, state, driver = setup(monkeypatch)
    # Poison the per-action hint: the decision must use a new inventory.
    api.action_app_inventory = {
        'apps': [
            {
                'pid': 42,
                'running': True,
                'bundle_id': 'com.apple.TextEdit',
                'active': True,
            }
        ]
    }
    driver.responses['list_apps']['structuredContent']['apps'][0]['active'] = active
    monkeypatch.setattr(adapter, 'native_focused_window_id', lambda *a, **k: focused)
    api.focus_window(state, state['window'], keyboard=True, activate=True)
    assert any(n == 'list_apps' for n, _ in driver.calls)
    assert any(n == 'bring_to_front' for n, _ in driver.calls) == (
        not active or focused != 7
    )


def test_foreground_keyboard_rejects_moved_window_before_input(monkeypatch):
    api, state, driver = setup(monkeypatch)
    driver.responses['list_windows']['structuredContent']['windows'][0]['bounds'][
        'x'
    ] += 20
    with pytest.raises(ValueError, match='WindowChanged'):
        api.focus_window(state, state['window'], keyboard=True, activate=True)
    assert not any(
        n in {'bring_to_front', 'press_key', 'hotkey'} for n, _ in driver.calls
    )


@pytest.mark.parametrize('wrong_owner', [False, True])
def test_floating_menu_is_shown_without_rebinding_main_window(monkeypatch, wrong_owner):
    from box_agent.mcp_servers.cua_server import CuaService

    api, state, driver = setup(monkeypatch)
    driver.responses['list_windows']['structuredContent']['windows'].append(
        {
            'window_id': 8,
            'pid': 42,
            'layer': 8,
            'is_on_screen': True,
            'bounds': {'x': 40, 'y': 40, 'width': 300, 'height': 400},
            'title': 'Font menu',
        }
    )
    original = driver.call

    def call(name, args):
        response = original(name, args)
        if name == 'get_window_state' and args['window_id'] == 8:
            response['structuredContent'].update(
                pid=999 if wrong_owner else 42, window_id=8
            )
            response['structuredContent'].update(
                screenshot_frame_valid=True,
                screenshot_width=600,
                screenshot_height=800,
                window_bounds={'x': 40, 'y': 40, 'width': 300, 'height': 400},
            )
            assert args['include_accessibility_tree'] is False
        return response

    monkeypatch.setattr(driver, 'call', call)
    svc = CuaService(lambda timeout: driver)
    try:
        content, error = svc.call('get_app', {'app': 'TextEdit'})
        assert not error
        content, error = svc.call('get_state', {'app': 'TextEdit', 'screenshot': True})
        if wrong_owner:
            assert error
            assert 'another window' in str(content)
        else:
            assert not error
            assert sum(c['type'] == 'image' for c in content) == 2
            assert 'Additional floating window 8' in str(content)
            handle = svc.apps['TextEdit']
            assert svc.adapter.handles[handle]['window']['window_id'] == 7
            assert svc.adapter.handles[handle]['elements'][0]['element_token'] == 's1:1'
            assert svc.adapter.handles[handle]['floating_images'][8]['size'] == (
                600,
                800,
            )
    finally:
        svc.close()


@pytest.mark.parametrize(
    'failure',
    [
        None,
        'moved',
        'inactive',
        'obscured',
        'reordered',
        'stale',
        'bounds',
        'background',
    ],
)
def test_floating_click_uses_own_pixels_and_rejects_unsafe_targets(
    monkeypatch, failure
):
    api, state, driver = setup(monkeypatch)
    popup = {
        'window_id': 8,
        'pid': 42,
        'layer': 8,
        'is_on_screen': True,
        'z_index': 10,
        'bounds': {'x': 40, 'y': 50, 'width': 300, 'height': 400},
    }
    # Real Photoshop: the layer-0 document can report a larger z_index
    # than its layer-8 font palette. Window level must take precedence.
    driver.responses['list_windows']['structuredContent']['windows'][0]['z_index'] = 403
    driver.responses['list_windows']['structuredContent']['windows'].append(popup)
    import copy

    state['floating_images'] = {8: {'window': copy.deepcopy(popup), 'size': (600, 800)}}
    api.screen_ratio = 2
    args = {'windowId': 8, 'target': [100, 200], 'deliveryMode': 'foreground'}
    if failure == 'moved':
        popup['bounds']['x'] += 1
    elif failure == 'inactive':
        driver.responses['list_apps']['structuredContent']['apps'][0]['active'] = False
    elif failure in {'obscured', 'reordered'}:
        driver.responses['list_windows']['structuredContent']['windows'].append(
            {
                **copy.deepcopy(popup),
                'window_id': 9,
                'z_index': 11 if failure == 'obscured' else 5,
            }
        )
        if failure == 'reordered':
            popup['z_index'] = 1
    elif failure == 'stale':
        api.invalidate()
    elif failure == 'bounds':
        args['target'] = [600, 0]
    elif failure == 'background':
        args['deliveryMode'] = 'background'
    if failure:
        with pytest.raises(ValueError):
            api.control('clickFloating', 'app', args)
        assert not any(n in {'click', 'bring_to_front'} for n, _ in driver.calls)
    else:
        api.control('clickFloating', 'app', args)
        calls = [(n, a) for n, a in driver.calls if n in {'click', 'bring_to_front'}]
        assert len(calls) == 1 and calls[0][0] == 'click'
        assert calls[0][1]['x'] == 180 and calls[0][1]['y'] == 300
        assert calls[0][1]['scope'] == 'desktop'
        assert 'floating_images' not in state


@pytest.mark.parametrize(
    'method,args,expected',
    [
        ('pressKey', {'key': 'escape'}, 'press_key'),
        ('pressKey', {'key': 'cmd+a'}, 'hotkey'),
        ('paste', {'text': 'DIALOG VERIFIED'}, 'hotkey'),
        ('typeText', {'text': 'DIALOG VERIFIED'}, 'type_text'),
        ('click', {'target': [100, 50]}, 'click'),
    ],
)
def test_empty_ax_dialog_foreground_input_keeps_exact_window(
    monkeypatch, method, args, expected
):
    api, state, driver = setup(monkeypatch)
    # Historical PS New Layer: CG window exists, AX lookup for it is empty.
    state.update(unresolved_windows={7}, elements={}, attachment_ax={7: []})
    state.update(image_frame=api.frame(state['window']), image_size=(800, 600))
    driver.responses['clipboard_write'] = result(supported=True, written_type='text')
    api.control(method, 'app', {**args, 'deliveryMode': 'foreground'})
    sent = [
        (n, a)
        for n, a in driver.calls
        if n in {'hotkey', 'press_key', 'click', 'type_text'}
    ]
    assert len(sent) == 1
    name, params = sent[0]
    assert name == expected
    assert params['pid'] == 42 and params['window_id'] == 7
    assert params['delivery_mode'] == 'foreground' and 'scope' not in params
    assert not any(n == 'bring_to_front' for n, _ in driver.calls)


@pytest.mark.parametrize(
    'failure',
    [
        'inactive',
        'wrong_focus',
        'missing_focus',
        'moved',
        'conflict',
        'background',
        'focus_changes_after_clipboard',
    ],
)
def test_empty_ax_dialog_rejects_unproven_or_background_input(monkeypatch, failure):
    api, state, driver = setup(monkeypatch)
    state.update(unresolved_windows={7}, elements={}, attachment_ax={7: []})
    mode = 'foreground'
    if failure == 'inactive':
        driver.responses['list_apps']['structuredContent']['apps'][0]['active'] = False
    elif failure == 'wrong_focus':
        monkeypatch.setattr(adapter, 'native_focused_window_id', lambda *a, **k: 99)
    elif failure == 'missing_focus':
        monkeypatch.setattr(adapter, 'native_focused_window_id', lambda *a, **k: None)
    elif failure == 'moved':
        driver.responses['list_windows']['structuredContent']['windows'][0]['bounds'][
            'x'
        ] += 10
    elif failure == 'conflict':
        state['attachment_conflicts'] = {7}
    elif failure == 'background':
        mode = 'background'
        driver.responses['get_window_state']['structuredContent']['elements'] = []
    elif failure == 'focus_changes_after_clipboard':
        original = driver.call

        def call(name, args):
            response = original(name, args)
            if name == 'clipboard_write':
                monkeypatch.setattr(
                    adapter, 'native_focused_window_id', lambda *a, **k: 99
                )
            return response

        monkeypatch.setattr(driver, 'call', call)
    with pytest.raises(ValueError):
        api.control('paste', 'app', {'text': 'NOT SENT', 'deliveryMode': mode})
    assert not any(
        n in {'hotkey', 'press_key', 'click', 'type_text'} for n, _ in driver.calls
    )
    if failure != 'focus_changes_after_clipboard':
        assert not any(n == 'clipboard_write' for n, _ in driver.calls)


@pytest.mark.parametrize('geometry_valid', [True, False])
def test_empty_ax_dialog_returns_capture_only_frame_for_pixel_input(
    monkeypatch, geometry_valid
):
    api, state, driver = setup(monkeypatch)
    original = driver.call

    def call(name, args):
        response = original(name, args)
        if name == 'get_window_state':
            response['structuredContent'].update(
                pid=42,
                window_id=7,
                elements=[],
                degraded_reason='ax_window_unresolved',
                screenshot_frame_valid=geometry_valid,
                screenshot_width=800,
                screenshot_height=600,
                window_bounds=state['window']['bounds'],
            )
            if args.get('include_accessibility_tree') is False:
                response['content'][0]['data'] = 'capture-only-frame'
        return response

    monkeypatch.setattr(driver, 'call', call)
    observation = api.observe(state, 'getAXStateAndScreenshot')
    assert observation['image']['data'] == 'capture-only-frame'
    assert 'delivery_mode=foreground' in observation['state']
    assert (
        7 in state['unresolved_windows']
    )  # capture-only must not fabricate AX evidence
    if geometry_valid:
        api.control('click', 'app', {'target': [100, 50], 'deliveryMode': 'foreground'})
        click = next(args for name, args in driver.calls if name == 'click')
        assert click['pid'] == 42 and click['window_id'] == 7
    else:
        assert 'observation-only' in observation['state']
        with pytest.raises(ValueError, match='current screenshot'):
            api.control(
                'click', 'app', {'target': [100, 50], 'deliveryMode': 'foreground'}
            )
        assert not any(name == 'click' for name, _ in driver.calls)

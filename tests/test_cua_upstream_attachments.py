"""Native attachment regressions ported from mac-cua dev/codex_v1.0 a62bc9f.
Box-Agent retains screenshot-pixel coordinates and background-first delivery.
"""
import copy
import io
import json
import pytest
from box_agent.mcp_servers import cua_adapter as adapter
from tests.test_cua_adapter_port import FakeDriver, result, no_native_input_in_fake_driver_tests

@pytest.fixture
def popover_app(monkeypatch):
    driver = FakeDriver()
    api = adapter.Adapter(driver)
    state = {'pid': 42, 'app_active': True}
    api.handles['app'] = state
    rows = driver.responses['list_windows']['structuredContent']['windows']
    rows[0].update(window_id=73, z_index=1)
    bounds = {'x': 228, 'y': 55, 'width': 282, 'height': 181}
    rows.append({**rows[0], 'window_id': 80, 'title': '', 'z_index': 2, 'bounds': bounds})
    root = {'role': 'AXPopover', 'depth': 1, 'frame': dict(bounds), 'element_token': 'parent:0'}
    report = dict(code='bring_to_front_exact_window_unverified', pid=42, window_id=80,
                  exact_window_effect={'verified': False, 'target_visible_ordinary': True},
                  observed={'focused_window_id': 73, 'frontmost_ordinary_window_id': 80,
                            'front_process_matches_target': True, 'frontmost_pid': 42,
                            'workspace_frontmost_pid': 42})
    driver.responses['bring_to_front'] = {'isError': True, **result(**report)}
    original = driver.call

    def call(name, args):
        response = original(name, args)
        if name == 'get_window_state':
            elements = [] if args['window_id'] == 80 else [
                {'role': 'AXWindow', 'label': 'Calendar', 'depth': 0}, root,
                {'role': 'AXButton', 'label': 'New Event', 'depth': 2,
                 'element_token': 'parent:1', 'actions': ['AXPress']},
                {'role': 'AXTextField', 'label': 'New Quick Event', 'depth': 2,
                 'element_token': 'parent:2', 'focused': True},
                {'role': 'AXButton', 'label': 'Background button', 'depth': 1}]
            response['structuredContent'].update(
                pid=42, window_id=args['window_id'], elements=elements,
                screenshot_width=282, screenshot_height=181,
                window_bounds=bounds, screenshot_frame_valid=True,
                background_input={'exact_window': {'pid': 42, 'window_id': 80, 'status': 'ax_unresolved'}})
        return response

    monkeypatch.setattr(driver, 'call', call)
    return api, driver, state, rows, root


def test_recursive_ax_attachments_track_focus_and_detect_moved_parent():
    driver = FakeDriver()
    api = adapter.Adapter(driver)
    rows = [{'window_id': wid, 'pid': 42, 'z_index': i, 'is_on_screen': True,
             'bounds': {'x': i * 10, 'y': i * 10, 'width': 100, 'height': 100}}
            for i, wid in enumerate([28, 40, 39])]
    driver.responses['list_windows'] = result(windows=rows)
    state = {'pid': 42, 'app_active': True, 'attachment_ax': {28: [
        {'role': 'AXWindow', 'depth': 0},
        {'role': 'AXPopover', 'depth': 1, 'frame': rows[1]['bounds']},
        {'role': 'AXSheet', 'depth': 2, 'frame': rows[2]['bounds']},
        {'role': 'AXTextField', 'depth': 3, 'focused': True},
    ]}}
    api.build_attachments(state, rows)
    assert api.attachment_chain(state, 39, rows) == [39, 40, 28]
    assert state['attachment_focused'] == {39}
    rows[0]['bounds'] = {**rows[0]['bounds'], 'x': 900}
    with pytest.raises(ValueError, match='WindowChanged'):
        api.attachment_chain(state, 39, rows)


@pytest.mark.parametrize('failure', ['cycle', 'depth', 'ambiguous'])
def test_attachment_recursion_rejects_invalid_graphs(failure):
    api = adapter.Adapter(FakeDriver())
    rows = [{'window_id': wid, 'bounds': {'x': wid, 'y': 0, 'width': 10, 'height': 10}}
            for wid in range(18)]
    graph = {wid: [{'surface': api.frame(rows[wid]), 'source': api.frame(rows[wid + 1])}]
             for wid in range(17)}
    if failure == 'cycle':
        graph[1] = [{'surface': api.frame(rows[1]), 'source': api.frame(rows[0])}]
    elif failure == 'ambiguous':
        graph[0].append({'surface': api.frame(rows[0]), 'source': api.frame(rows[2])})
    with pytest.raises(ValueError, match='AttachmentUnverified'):
        api.attachment_chain({'attachments': graph}, 0, rows)


def test_relationship_observation_rejects_changed_parent(popover_app, monkeypatch):
    api, driver, state, rows, root = popover_app
    original = driver.call
    def call(name, args):
        response = original(name, args)
        if name == 'get_window_state' and args['window_id'] == 73:
            rows[0]['bounds'] = {**rows[0]['bounds'], 'x': 999}
        return response
    monkeypatch.setattr(driver, 'call', call)
    with pytest.raises(ValueError, match='WindowChanged'):
        api.observe(state, 'getAXState')
    assert not state['attachments'] and not state['fresh']
    assert not any(name == 'bring_to_front' for name, _ in driver.calls)


def test_indexed_child_skips_activation_when_parent_has_window_focus(popover_app):
    api, driver, state, rows, root = popover_app
    rows[0]['is_focused'] = True
    api.observe(state, 'getAXState')
    assert state['window']['window_id'] == 73
    assert state['element_surfaces'][2]['window_id'] == 80
    start = len(driver.calls)
    api.control('click', 'app', {'target': 2})
    assert driver.calls[-1][1]['window_id'] == 73
    assert not any(name == 'bring_to_front' for name, _ in driver.calls[start:])


def test_ambiguous_token_surface_is_rejected(popover_app):
    api, driver, state, rows, root = popover_app
    api.observe(state, 'getAXState')
    state['attachment_tokens'][(73, 'parent:1')] = {73, 80}
    start = len(driver.calls)
    with pytest.raises(ValueError, match='multiple surfaces'):
        api.control('click', 'app', {'target': 2})
    assert not any(name in {'bring_to_front', 'click'} for name, _ in driver.calls[start:])


def test_calendar_geometry_only_relationship():
    # Geometry-only reproduction: no labels, text values, tokens or screenshots.
    api = adapter.Adapter(FakeDriver())
    rows = [{'window_id': 28, 'pid': 42, 'bounds': {'x': 200, 'y': 25, 'width': 935, 'height': 598}},
            {'window_id': 47, 'pid': 42, 'bounds': {'x': 602, 'y': 146, 'width': 315, 'height': 397}}]
    state = {'pid': 42, 'attachment_ax': {28: [
        {'role': 'AXWindow', 'depth': 0, 'frame': {'x': 200, 'y': 25, 'w': 935, 'h': 598}},
        {'role': 'AXPopover', 'depth': 1, 'frame': {'x': 602, 'y': 146, 'w': 315, 'h': 397}},
    ]}}
    api.build_attachments(state, rows)
    assert api.attachment_chain(state, 47, rows) == [47, 28]


@pytest.mark.parametrize('method,args', [
    ('click', {'target': 2}),
    ('pressKey', {'key': 'cmd+a'}),
    ('paste', {'text': '/tmp/example.docx'}),
    ('pressKey', {'key': 'return'}),
])
def test_attached_input_enumerates_apps_once_without_activation(popover_app, method, args):
    api, driver, state, rows, root = popover_app
    state['bundle_id'] = 'com.apple.TextEdit'
    driver.responses['list_apps']['structuredContent']['apps'][0]['active'] = True
    api.observe(state, 'getAXState')
    start = len(driver.calls)
    api.control(method, 'app', {**args, 'deliveryMode': 'foreground'})
    calls = driver.calls[start:]
    assert sum(name == 'list_apps' for name, _ in calls) == 2
    assert not any(name == 'bring_to_front' for name, _ in calls)
    assert calls[-1][0] in {'click', 'hotkey', 'press_key'}
    assert api.action_app_inventory is None


def test_next_input_rechecks_process_identity_after_success_and_error(popover_app):
    api, driver, state, rows, root = popover_app
    state['bundle_id'] = 'com.apple.TextEdit'
    driver.responses['list_apps']['structuredContent']['apps'][0]['active'] = True
    api.observe(state, 'getAXState')
    api.control('pressKey', 'app', {'key': 'cmd+a', 'deliveryMode': 'foreground'})
    app = driver.responses['list_apps']['structuredContent']['apps'][0]
    app['pid'] = 99
    start = len(driver.calls)
    with pytest.raises(ValueError, match='ApplicationChanged'):
        api.control('paste', 'app', {'text': 'never pasted', 'deliveryMode': 'foreground'})
    assert not any(name in {'clipboard_write', 'hotkey'} for name, _ in driver.calls[start:])
    assert api.action_app_inventory is None
    app['pid'] = 42
    api.control('pressKey', 'app', {'key': 'return', 'deliveryMode': 'foreground'})
    assert driver.calls[-1][0] == 'press_key'


def test_paste_still_detects_window_movement_after_clipboard_write(popover_app, monkeypatch):
    api, driver, state, rows, root = popover_app
    state['bundle_id'] = 'com.apple.TextEdit'
    driver.responses['list_apps']['structuredContent']['apps'][0]['active'] = True
    api.observe(state, 'getAXState')
    original = driver.call

    def call(name, args):
        response = original(name, args)
        if name == 'clipboard_write':
            rows[1]['bounds'] = {**rows[1]['bounds'], 'x': 999}
        return response

    monkeypatch.setattr(driver, 'call', call)
    start = len(driver.calls)
    with pytest.raises(ValueError, match='WindowChanged'):
        api.control('paste', 'app', {'text': 'not delivered', 'deliveryMode': 'foreground'})
    assert not any(name == 'hotkey' for name, _ in driver.calls[start:])


@pytest.mark.parametrize('key', ['cmd+a', 'Tab', 'Return', 'Escape'])
@pytest.mark.parametrize('method,args', [('paste', {'text': 'Review'}), ('typeText', {'text': 'Review'})])
def test_keyboard_sequence_refreshes_focus_without_key_whitelist(popover_app, key, method, args):
    api, driver, state, rows, root = popover_app
    api.observe(state, 'getAXState')
    displayed = copy.deepcopy(state['window_states'])
    api.handles['other'] = {'fresh': True}
    api.control('pressKey', 'app', {'key': key})
    assert not state['fresh'] and not api.handles['other']['fresh']
    start = len(driver.calls)
    api.control(method, 'app', args)
    calls = driver.calls[start:]
    reads = [i for i, (name, _) in enumerate(calls) if name == 'get_window_state']
    delivery = next(i for i, (name, _) in enumerate(calls) if name in {'clipboard_write', 'type_text'})
    assert reads and max(reads) < delivery
    assert not state['fresh'] and state['window_states'] == displayed
    assert not any(name == 'bring_to_front' for name, _ in calls)
    with pytest.raises(ValueError, match='StaleElement'):
        api.control('click', 'app', {'target': 2})


@pytest.mark.parametrize('change', ['focus', 'same_surface_control', 'closed', 'moved', 'new_window',
                                   'during_read', 'ax_error', 'timeout'])
def test_keyboard_refresh_checks_current_state_before_paste(popover_app, monkeypatch, change):
    api, driver, state, rows, root = popover_app
    api.observe(state, 'getAXState')
    api.control('pressKey', 'app', {'key': 'cmd+a'})
    original = driver.call

    def call(name, args):
        response = original(name, args)
        if name == 'get_window_state' and args['window_id'] == 73:
            if change == 'timeout':
                raise TimeoutError('AX refresh timed out')
            if change == 'ax_error':
                return {'isError': True, 'content': [{'type': 'text', 'text': 'AX unavailable'}]}
            if change == 'during_read':
                rows[1]['bounds'] = {**rows[1]['bounds'], 'x': 300}
            if change in {'focus', 'same_surface_control'}:
                elements = response['structuredContent']['elements']
                elements[3]['focused'] = False
                elements[2 if change == 'same_surface_control' else 4]['focused'] = True
        return response

    monkeypatch.setattr(driver, 'call', call)
    if change == 'closed':
        rows.pop()
    elif change == 'moved':
        rows[0]['bounds'] = {**rows[0]['bounds'], 'x': 100}
    elif change == 'new_window':
        rows.append({**rows[1], 'window_id': 99, 'z_index': 9})
    elif change == 'inactive':
        state['app_active'] = False
    start = len(driver.calls)
    if change in {'focus', 'same_surface_control'}:
        api.control('paste', 'app', {'text': 'Review'})
        assert driver.calls[-1][0] == 'hotkey'
    else:
        with pytest.raises((ValueError, TimeoutError)):
            api.control('paste', 'app', {'text': 'Review'})
        assert not any(n in {'clipboard_write', 'hotkey', 'bring_to_front'} for n, _ in driver.calls[start:])
    assert not state['fresh']


@pytest.mark.parametrize('scale', [1, 2])
@pytest.mark.parametrize('failure', [None, 'covered', 'moved', 'outside'])
def test_attached_pixel_validation_uses_capture_geometry(popover_app, scale, failure):
    api, driver, state, rows, root = popover_app
    api.observe(state, 'getAXStateAndScreenshot')
    state['image_size'] = (282 * scale, 181 * scale)
    if failure == 'covered':
        rows.append({**rows[1], 'window_id': 99, 'z_index': 4})
    elif failure == 'moved':
        rows[0]['bounds'] = {**rows[0]['bounds'], 'x': 999}
    point = {'x': (2000 if failure == 'outside' else 10) * scale, 'y': 20 * scale}
    if failure:
        with pytest.raises(ValueError):
            api.verify_attached_point(state, rows[1], point, rows)
    else:
        api.verify_attached_point(state, rows[1], point, rows)
    assert not any(name in {'click', 'bring_to_front'} for name, _ in driver.calls)


def test_background_popover_keyboard_does_not_require_frontmost_app(popover_app):
    api, driver, state, rows, root = popover_app
    api.observe(state, 'getAXState')
    state['app_active'] = False
    api.control('pressKey', 'app', {'key': 'cmd+a'})
    assert driver.calls[-1][0] == 'hotkey'
    assert driver.calls[-1][1]['delivery_mode'] == 'background'
    assert not any(name == 'bring_to_front' for name, _ in driver.calls)


def test_set_value_does_not_enter_foreground_activity_check(popover_app):
    api, driver, state, rows, root = popover_app
    state['bundle_id'] = 'com.apple.TextEdit'
    driver.responses['list_apps']['structuredContent']['apps'][0]['active'] = False
    api.observe(state, 'getAXState')
    target = next(i for i, e in state['elements'].items() if e.get('role') == 'AXTextField')
    start = len(driver.calls)
    api.control('setValue', 'app', {'target': target, 'value': '/tmp/example.docx'})
    assert driver.calls[-1][0] == 'set_value'
    assert driver.calls[-1][1]['value'] == '/tmp/example.docx'
    assert state['app_active'] is False
    assert not any(name == 'bring_to_front' for name, _ in driver.calls[start:])


def test_screenshot_only_preserves_current_element_tokens(popover_app):
    api, driver, state, rows, root = popover_app
    api.observe(state, 'getAXState')
    token = api.target(state, 2)
    start = len(driver.calls)
    api.observe(state, 'getScreenshot')
    assert api.target(state, 2) == token
    assert not any(name == 'get_window_state' and args.get('include_accessibility_tree')
                   for name, args in driver.calls[start:])

@pytest.mark.parametrize('observation', ['getAXState', 'getScreenshot', 'getAXStateAndScreenshot'])
def test_unknown_surface_remains_observable_but_input_is_blocked(monkeypatch, observation):
    driver = FakeDriver()
    api = adapter.Adapter(driver)
    state = {'pid': 42, 'app_active': True}
    api.handles['app'] = state
    rows = driver.responses['list_windows']['structuredContent']['windows']
    rows[0]['z_index'] = 1
    rows.append({**rows[0], 'window_id': 39, 'z_index': 2,
                 'bounds': {'x': 10, 'y': 10, 'width': 20, 'height': 20}})
    original = driver.call

    def call(name, args):
        response = original(name, args)
        if name == 'get_window_state' and args['window_id'] == 39:
            response['structuredContent'].update(elements=[], degraded_reason='ax_window_unresolved')
        return response

    monkeypatch.setattr(driver, 'call', call)
    api.observe(state, observation)
    with pytest.raises(ValueError, match='no proven AX parent'):
        api.control('pressKey', 'app', {'key': 'Escape'})
    assert not any(name in {'bring_to_front', 'press_key'} for name, _ in driver.calls)


@pytest.mark.parametrize('role', ['AXSheet', 'AXPopover', 'AXMenu', 'AXWindow'])
def test_nested_surface_token_keeps_source_and_skips_activation(popover_app, role):
    api, driver, state, rows, root = popover_app
    root['role'] = role
    api.observe(state, 'getAXState')
    target = next(i for i, e in state['elements'].items() if e.get('element_token') == 'parent:1')
    assert state['element_surfaces'][target]['window_id'] == 80
    start = len(driver.calls)
    api.control('click', 'app', {'target': target})
    assert driver.calls[-1][1]['window_id'] == 73
    assert driver.calls[-1][1]['element_token'] == 'parent:1'
    assert not any(name == 'bring_to_front' for name, _ in driver.calls[start:])




def test_attached_field_return_confirms_ax_element_without_keyboard(popover_app, monkeypatch):
    api, driver, state, rows, root = popover_app
    original = driver.call
    def call(name, args):
        response = original(name, args)
        if name == 'get_window_state':
            for element in response['structuredContent']['elements']:
                if element.get('element_token') == 'parent:2':
                    element['actions'] = ['AXConfirm']
        return response
    monkeypatch.setattr(driver, 'call', call)
    api.observe(state, 'getAXState')
    target = next(i for i, e in state['elements'].items() if e.get('element_token') == 'parent:2')
    assert state['element_windows'][target]['window_id'] == 73
    assert state['element_surfaces'][target]['window_id'] == 80
    driver.calls.clear()
    api.control('pressKey', 'app', {'key': 'return', 'target': target})
    actions = [(n, a) for n, a in driver.calls if n in {'click', 'press_key', 'hotkey', 'bring_to_front'}]
    assert actions == [('click', {'pid': 42, 'window_id': 73, 'element_token': 'parent:2',
                                 'action': 'confirm', 'button': 'left', 'delivery_mode': 'background',
                                 'session': api.session})]


@pytest.mark.parametrize('method,args', [
    ('pressKey', {'key': 'return'}),  # No AXConfirm advertised.
    ('pressKey', {'key': 'cmd+a'}),
    ('typeText', {'text': 'must not type into parent'}),
    ('paste', {'text': 'must not change clipboard'}),
    ('pressKey', {'key': 'return', 'deliveryMode': 'foreground'}),
])
def test_attached_keyboard_never_uses_source_window_as_receiver(popover_app, method, args):
    api, driver, state, rows, root = popover_app
    api.observe(state, 'getAXState')
    target = next(i for i, e in state['elements'].items() if e.get('element_token') == 'parent:2')
    driver.calls.clear()
    with pytest.raises(ValueError, match='AttachmentKeyboardUnsupported'):
        api.control(method, 'app', {**args, 'target': target})
    assert not any(n in {'press_key', 'hotkey', 'type_text', 'clipboard_write', 'bring_to_front', 'click'}
                   for n, _ in driver.calls)


@pytest.mark.parametrize('failure', ['moved', 'refused'])
def test_attached_confirmation_never_retries_as_foreground(popover_app, failure):
    api, driver, state, rows, root = popover_app
    api.observe(state, 'getAXState')
    target = next(i for i, e in state['elements'].items() if e.get('element_token') == 'parent:2')
    state['elements'][target]['actions'] = ['AXConfirm']
    if failure == 'moved':
        rows[0]['bounds'] = {**rows[0]['bounds'], 'x': 999}
    else:
        driver.responses['click'] = {'isError': True, **result(effect='refused', code='background_unavailable')}
    driver.calls.clear()
    with pytest.raises((ValueError, adapter.ActionError)):
        api.control('pressKey', 'app', {'key': 'return', 'target': target})
    sent = [a for n, a in driver.calls if n == 'click']
    assert len(sent) == (0 if failure == 'moved' else 1)
    assert all(a['delivery_mode'] == 'background' for a in sent)
    assert not any(n in {'press_key', 'hotkey', 'bring_to_front'} for n, _ in driver.calls)


def test_mcp_path_assignment_then_return_never_dispatches_hid(popover_app, monkeypatch):
    from box_agent.mcp_servers.cua_server import CuaService
    api, driver, state, rows, root = popover_app
    original = driver.call
    field = {'value': ''}
    def call(name, args):
        response = original(name, args)
        if name == 'set_value':
            field['value'] = args['value']
        if name == 'get_window_state':
            for element in response['structuredContent']['elements']:
                if element.get('element_token') == 'parent:2':
                    element.update(actions=['AXConfirm'], value=field['value'])
        return response
    monkeypatch.setattr(driver, 'call', call)
    svc = CuaService(lambda timeout: driver)
    svc.adapter = api
    svc.apps['Word'] = 'app'
    try:
        assert not svc.call_result('get_state', {'app': 'Word'}).isError
        target = next(i for i, e in state['elements'].items() if e.get('element_token') == 'parent:2')
        assert not svc.call_result('set_value', {'app': 'Word', 'element': target, 'value': '/tmp/proof.docx'}).isError
        assert not svc.call_result('get_state', {'app': 'Word'}).isError
        target = next(i for i, e in state['elements'].items() if e.get('value') == '/tmp/proof.docx')
        response = svc.call_result('press_key', {'app': 'Word', 'element': target, 'key': 'return'})
        assert not response.isError
        assert response.structuredContent['requires_requery']
        sent = [(n, a) for n, a in driver.calls if n in {'set_value', 'click', 'press_key', 'hotkey', 'bring_to_front'}]
        assert [n for n, _ in sent] == ['set_value', 'click']
        assert sent[-1][1]['action'] == 'confirm'
        assert sent[-1][1]['delivery_mode'] == 'background'
    finally:
        svc.close()

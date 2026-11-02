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


def test_recursive_ax_attachments_skip_activation_and_require_focused_control():
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
    assert api.focus_window(state, rows[2], keyboard=True, observation_fresh=True) == rows[2]
    assert not any(name == 'bring_to_front' for name, _ in driver.calls)
    with pytest.raises(ValueError, match='focused-control'):
        api.focus_window(state, rows[2], keyboard=True, observation_fresh=False)
    state['attachment_focus_count'] = 2
    with pytest.raises(ValueError, match='focused-control'):
        api.focus_window(state, rows[2], keyboard=True, observation_fresh=True)
    state['attachment_focus_count'] = 1
    state['attachment_focused'] = {28}
    with pytest.raises(ValueError, match='focused-control'):
        api.focus_window(state, rows[2], keyboard=True, observation_fresh=True)
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
    assert sum(name == 'list_apps' for name, _ in calls) == 1
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
    assert sum(name == 'list_apps' for name, _ in calls) == 2
    assert not any(name == 'hotkey' for name, _ in calls)


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
    if change == 'same_surface_control':
        api.control('paste', 'app', {'text': 'Review'})
        assert driver.calls[-1][0] == 'press_key'
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
    assert driver.calls[-1][0] == 'press_key'
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



@pytest.mark.parametrize('activated', [True, False])
def test_inactive_host_requires_verified_activation_for_foreground_input(popover_app, monkeypatch, activated):
    api, driver, state, rows, root = popover_app
    api.observe(state, 'getAXState')
    state['app_active'] = False
    original = driver.call

    def call(name, args):
        if name == 'bring_to_front':
            driver.calls.append((name, args))
            state['app_active'] = activated
            return result(activated=activated)
        return original(name, args)

    monkeypatch.setattr(driver, 'call', call)
    start = len(driver.calls)
    if activated:
        assert api.focus_window(state, rows[1], keyboard=True,
                                observation_fresh=True, activate=True) == rows[1]
    else:
        with pytest.raises(ValueError, match='owning app is not confirmed foreground'):
            api.focus_window(state, rows[1], keyboard=True,
                             observation_fresh=True, activate=True)
    calls = [name for name, _ in driver.calls[start:]]
    assert 'bring_to_front' in calls
    assert 'get_window_state' in calls


def test_native_sheet_can_resolve_through_native_parent(popover_app, monkeypatch):
    api, driver, state, rows, root = popover_app
    root['role'] = 'AXSheet'
    native = [{**row, 'pid': 42, 'native_inventory': True} for row in rows]
    driver.responses['list_windows'] = result(windows=[])
    monkeypatch.setattr(adapter, 'native_window_inventory', lambda pid: native)
    observed = api.interaction_windows(state, verify_native=True)
    assert {row['window_id'] for row in observed} == {73, 80}
    assert all(row['native_verified'] is True for row in observed)


def test_foreground_pixel_fallback_recaptures_after_activation(popover_app, monkeypatch):
    api, driver, state, rows, root = popover_app
    original = driver.call
    def call(name, args):
        if name == 'bring_to_front':
            driver.calls.append((name, args))
            state['app_active'] = True
            return result(activated=True)
        response = original(name, args)
        if name == 'get_window_state':
            for field in response['structuredContent']['elements']:
                if field.get('role') == 'AXTextField':
                    field['frame'] = {'x': 240, 'y': 70, 'w': 120, 'h': 25}
        if name == 'click' and args.get('delivery_mode') == 'background':
            return {'isError': True, **result(code='background_unavailable', effect='not_sent')}
        return response
    monkeypatch.setattr(driver, 'call', call)
    api.observe(state, 'getAXState')
    state['app_active'] = False
    target = next(i for i, e in state['elements'].items() if e.get('role') == 'AXTextField')
    start = len(driver.calls)
    api.control('click', 'app', {'target': target})
    calls = driver.calls[start:]
    activation = next(i for i, (name, _) in enumerate(calls) if name == 'bring_to_front')
    assert any(name == 'get_window_state' and args.get('include_screenshot')
               for name, args in calls[activation + 1:])
    assert calls[-1][0] == 'click' and calls[-1][1]['scope'] == 'desktop'


@pytest.mark.parametrize('change', ['same', 'moved', 'closed'])
def test_sheet_validation_survives_driver_inventory_transition(popover_app, monkeypatch, change):
    api, driver, state, rows, root = popover_app
    root['role'] = 'AXSheet'
    native = [copy.deepcopy(row) for row in rows]
    for row in native:
        row.update(pid=42, native_inventory=True)
    monkeypatch.setattr(adapter, 'native_window_inventory', lambda pid: copy.deepcopy(native))
    driver.responses['list_windows'] = result(windows=[rows[0]])
    observed = api.interaction_windows(state, verify_native=True)
    assert api.current_window(observed)['native_verified'] is True
    key = api.native_window_key(rows[1])
    assert state['native_window_validation'][key] is True

    # Inactive apps expose the same sheet as a layer-0 Driver window.
    driver.responses['list_windows'] = result(windows=copy.deepcopy(rows))
    api.interaction_windows(state)
    assert state['native_window_validation'][key] is True

    # Clicking activates the app; the sheet is supplemental again.
    driver.responses['list_windows'] = result(windows=[rows[0]])
    if change == 'moved':
        native[1]['bounds']['x'] += 1
    elif change == 'closed':
        native.pop()
    start = len(driver.calls)
    observed = api.interaction_windows(state)
    if change == 'same':
        assert api.current_window(observed)['window_id'] == 80
        assert api.current_window(observed)['native_verified'] is True
    else:
        assert key not in state['native_window_validation']
        if change == 'moved':
            with pytest.raises(ValueError, match='new native surface'):
                api.current_window(observed)
    assert not any(name == 'get_window_state' for name, _ in driver.calls[start:])

"""Observation cost regressions; no native GUI input."""

from collections import Counter

import pytest

from box_agent.mcp_servers import cua_adapter as adapter
from tests.test_cua_adapter_port import (
    FakeDriver,
    result,
    no_native_input_in_fake_driver_tests,
)


def setup_panel(monkeypatch, truncated=False):
    driver = FakeDriver()
    driver.tools['get_window_state']['inputSchema']['properties']['timeout_ms'] = {}
    api = adapter.Adapter(driver)
    api.handles['app'] = state = {'pid': 42, 'bundle_id': 'com.apple.TextEdit'}
    bounds = {'x': 100, 'y': 100, 'width': 200, 'height': 100}
    rows = [
        {
            'window_id': i,
            'pid': 42,
            'title': str(i),
            'is_on_screen': True,
            'z_index': i,
            'bounds': {'x': 0, 'y': 0, 'width': 800, 'height': 600},
        }
        for i in range(1, 9)
    ]
    rows.append({**rows[0], 'window_id': 9, 'z_index': 9, 'bounds': bounds})
    driver.responses['list_windows'] = result(windows=rows)
    original = driver.call
    reads = Counter()

    def call(name, args):
        response = original(name, args)
        if name == 'get_window_state':
            wid = args['window_id']
            reads[wid] += 1
            elements = (
                []
                if wid == 9
                else [{'role': 'AXWindow', 'depth': 0, 'label': 'Document'}]
            )
            if wid == 1 and (not truncated or reads[wid] > 1):
                elements += [
                    {'role': 'AXSheet', 'depth': 1, 'label': 'Save', 'frame': bounds},
                    {
                        'role': 'AXButton',
                        'depth': 2,
                        'label': 'Save',
                        'element_token': f'fresh-{reads[wid]}',
                        'actions': ['AXPress'],
                    },
                ]
            response = result(
                pid=42,
                window_id=wid,
                elements=elements,
                elements_complete=not (wid == 1 and truncated),
                truncation_reason='timeout'
                if wid == 1 and truncated and reads[wid] == 1
                else None,
                truncated=wid == 1 and truncated and reads[wid] == 1,
            )
        return response

    monkeypatch.setattr(driver, 'call', call)
    return api, state, driver, reads


@pytest.mark.parametrize('truncated', [False, True])
def test_sheet_search_does_not_reread_complete_unrelated_documents(
    monkeypatch, truncated
):
    api, state, driver, reads = setup_panel(monkeypatch, truncated)
    observation = api.observe(state, 'getAXState')
    assert 'Attached sheet:' in observation['state']
    assert all(reads[i] == 1 for i in range(2, 9))
    assert reads[1] == 1 + int(truncated)
    assert state['elements'][1]['element_token'] == f'fresh-{reads[1]}'
    counts = Counter(n for n, _ in driver.calls)
    assert counts['get_window_state'] == 9 + int(truncated)
    assert counts['list_apps'] == 1
    assert counts['list_windows'] == 2
    assert api.action_app_inventory is None
    driver.responses['list_apps']['structuredContent']['apps'][0]['pid'] = 43
    with pytest.raises(ValueError, match='ApplicationChanged'):
        api.observe(state, 'getAXState')
    assert api.action_app_inventory is None


def test_observation_still_detects_window_movement(monkeypatch):
    api, state, driver, _ = setup_panel(monkeypatch)
    original = driver.call
    calls = 0

    def moving(name, args):
        nonlocal calls
        response = original(name, args)
        if name == 'list_windows':
            calls += 1
            if calls % 2 == 0:
                response['structuredContent']['windows'][-1]['bounds']['x'] += 1
        return response

    monkeypatch.setattr(driver, 'call', moving)
    with pytest.raises(ValueError, match='WindowChanged'):
        api.observe(state, 'getAXState')
    assert state['fresh'] is False
    assert api.action_app_inventory is None


def test_incomplete_projection_remains_a_fallback_candidate(monkeypatch):
    api, state, driver, reads = setup_panel(monkeypatch, truncated=True)
    original = driver.call

    def projection(name, args):
        response = original(name, args)
        if name == 'get_window_state':
            response['structuredContent']['elements_complete'] = False
        return response

    monkeypatch.setattr(driver, 'call', projection)
    observation = api.observe(state, 'getAXState')
    assert 'Attached sheet:' in observation['state']
    assert reads[1] == 2
    assert all(reads[i] == 1 for i in range(2, 9))


def test_timed_out_parent_read_retries_once_with_fresh_tokens(monkeypatch):
    api, state, driver, reads = setup_panel(monkeypatch, truncated=True)
    driver.tools['get_window_state']['inputSchema']['properties']['timeout_ms'] = {}
    original = driver.call

    def timed_out(name, args):
        response = original(name, args)
        if name == 'get_window_state' and args['window_id'] == 1:
            raw = response['structuredContent']
            if reads[1] == 1:
                raw.update(truncated=True, truncation_reason='timeout')
            else:
                assert args.get('timeout_ms') == 5000
                raw.update(truncated=False)
        return response

    monkeypatch.setattr(driver, 'call', timed_out)
    api.observe(state, 'getAXState')
    assert reads[1] == 2
    assert state['elements'][1]['element_token'] == 'fresh-2'


def test_repeated_sheet_observation_reads_only_target_and_known_parent(monkeypatch):
    api, state, driver, reads = setup_panel(monkeypatch)
    first = api.observe(state, 'getAXState', True)
    driver.calls.clear()
    second = api.observe(state, 'getAXState', True)
    assert first['state'] == second['state']
    assert [a['window_id'] for n, a in driver.calls if n == 'get_window_state'] == [
        9,
        1,
    ]
    assert state['elements'][1]['element_token'] == 'fresh-2'


def test_missing_known_sheet_source_expands_search(monkeypatch):
    api, state, driver, reads = setup_panel(monkeypatch)
    api.observe(state, 'getAXState')
    original = driver.call
    old_elements = state['window_states'][1]['raw_elements']

    def relocated(name, args):
        response = original(name, args)
        if name == 'get_window_state':
            if args['window_id'] == 1:
                response['structuredContent']['elements'] = []
            if args['window_id'] == 8:
                response['structuredContent']['elements'] = [
                    {**e, 'element_token': 'new-parent'} for e in old_elements
                ]
        return response

    monkeypatch.setattr(driver, 'call', relocated)
    driver.calls.clear()
    observation = api.observe(state, 'getAXState')
    assert 'Attached sheet:' in observation['state']
    assert [a['window_id'] for n, a in driver.calls if n == 'get_window_state'] == [
        9,
        1,
        8,
        7,
        6,
        5,
        4,
        3,
        2,
        8,
    ]
    assert state['element_windows'][1]['window_id'] == 8
    assert state['elements'][1]['element_token'] == 'new-parent'


def test_unresolved_target_stays_marked_without_global_scan(monkeypatch):
    api, state, driver, reads = setup_panel(monkeypatch)
    original = driver.call

    def unresolved(name, args):
        response = original(name, args)
        if name == 'get_window_state' and args['window_id'] == 9:
            response['structuredContent']['degraded_reason'] = 'ax_window_unresolved'
        return response

    monkeypatch.setattr(driver, 'call', unresolved)
    api.observe(state, 'getAXState')
    assert 9 in state['unresolved_windows']
    assert api.attachment_chain(
        state, 9, driver.responses['list_windows']['structuredContent']['windows']
    ) == [9, 1]


def test_observation_requeries_focus_switch_without_frame_change(monkeypatch):
    api, state, driver, reads = setup_panel(monkeypatch)
    original = driver.call

    def focus_changed(name, args):
        response = original(name, args)
        if name == 'list_windows' and reads:
            response['structuredContent']['windows'][0]['is_focused'] = True
        return response

    monkeypatch.setattr(driver, 'call', focus_changed)
    api.observe(state, 'getAXState')
    assert state['fresh'] and state['window']['window_id'] == 1
    assert not any(
        n in {'click', 'bring_to_front', 'type_text'} for n, _ in driver.calls
    )


def test_unaddressed_input_refreshes_only_known_attachment_chain(monkeypatch):
    api, state, driver, reads = setup_panel(monkeypatch)
    api.observe(state, 'getAXState')
    driver.calls.clear()
    api.refresh_keyboard_focus(state, state['window'])
    assert [a['window_id'] for n, a in driver.calls if n == 'get_window_state'] == [
        9,
        1,
    ]
    assert api.attachment_chain(
        state, 9, driver.responses['list_windows']['structuredContent']['windows']
    ) == [9, 1]


def test_binding_running_app_uses_one_inventory_and_refreshes_next_binding():
    driver = FakeDriver()
    api = adapter.Adapter(driver)
    first = api.control('getApp', args={'app': 'TextEdit'})
    assert sum(n == 'list_apps' for n, _ in driver.calls) == 1
    assert api.action_app_inventory is None
    driver.responses['list_apps']['structuredContent']['apps'][0]['pid'] = 43
    driver.responses['list_windows']['structuredContent']['windows'][0]['pid'] = 43
    driver.calls.clear()
    second = api.control('getApp', args={'app': 'TextEdit'})
    assert api.handles[first['handle']]['pid'] == 42
    assert api.handles[second['handle']]['pid'] == 43
    assert sum(n == 'list_apps' for n, _ in driver.calls) == 1


def test_known_competing_parents_are_rechecked_not_silently_forgotten(monkeypatch):
    api, state, driver, reads = setup_panel(monkeypatch)
    api.observe(state, 'getAXState')
    rows = driver.responses['list_windows']['structuredContent']['windows']
    state['attachments'][9].append(
        {
            'surface': api.frame(rows[-1]),
            'source': api.frame(rows[7]),
            'role': 'AXSheet',
        }
    )
    elements = state['window_states'][1]['raw_elements']
    original = driver.call

    def competing(name, args):
        response = original(name, args)
        if name == 'get_window_state' and args['window_id'] == 8:
            response['structuredContent']['elements'] = elements
        return response

    monkeypatch.setattr(driver, 'call', competing)
    driver.calls.clear()
    api.observe(state, 'getAXState')
    with pytest.raises(ValueError, match='multiple AX parents'):
        api.control('click', 'app', {'target': 1})
    assert not any(n == 'click' for n, _ in driver.calls)
    # No unrelated document is queried in the conflict-specific refresh.
    assert {a['window_id'] for n, a in driver.calls if n == 'get_window_state'} == {
        1,
        8,
        9,
    }


@pytest.mark.parametrize(
    'method,args,window_reads',
    [
        ('typeText', {'text': 'x'}, 2),
        ('pressKey', {'key': 'cmd+a'}, 2),
        ('paste', {'text': 'x'}, 3),
    ],
)
def test_input_reuses_immediate_focus_check_but_rechecks_after_clipboard(
    monkeypatch, method, args, window_reads
):
    api, state, driver, reads = setup_panel(monkeypatch)
    api.observe(state, 'getAXState')
    driver.calls.clear()
    api.control(method, 'app', {**args, 'deliveryMode': 'background'})
    assert sum(n == 'list_windows' for n, _ in driver.calls) == window_reads
    assert sum(n == 'list_apps' for n, _ in driver.calls) == 1
    assert [a['window_id'] for n, a in driver.calls if n == 'get_window_state'] == [
        9,
        1,
    ]


def test_known_slow_parent_skips_duplicate_short_timeout_next_observation(monkeypatch):
    api, state, driver, reads = setup_panel(monkeypatch, truncated=True)
    api.observe(state, 'getAXState')
    driver.calls.clear()
    api.observe(state, 'getAXState')
    parent_reads = [
        a for n, a in driver.calls if n == 'get_window_state' and a['window_id'] == 1
    ]
    assert len(parent_reads) == 1
    assert parent_reads[0]['timeout_ms'] == 5000
    assert state['elements'][1]['element_token'] == 'fresh-3'


def test_cold_sheet_discovery_detects_competing_parent_before_input(monkeypatch):
    api, state, driver, reads = setup_panel(monkeypatch)
    original = driver.call
    bounds = driver.responses['list_windows']['structuredContent']['windows'][-1][
        'bounds'
    ]

    def competing(name, args):
        response = original(name, args)
        if name == 'get_window_state' and args['window_id'] == 8:
            response['structuredContent']['elements'] = [
                {'role': 'AXWindow', 'depth': 0},
                {'role': 'AXSheet', 'depth': 1, 'frame': bounds},
                {
                    'role': 'AXButton',
                    'depth': 2,
                    'element_token': 'competing',
                    'actions': ['AXPress'],
                },
            ]
        return response

    monkeypatch.setattr(driver, 'call', competing)
    api.observe(state, 'getAXState')
    with pytest.raises(ValueError, match='multiple AX parents'):
        api.control('click', 'app', {'target': 1})
    assert not any(n == 'click' for n, _ in driver.calls)


def test_timeout_hint_resets_when_source_frame_changes(monkeypatch):
    api, state, driver, reads = setup_panel(monkeypatch, truncated=True)
    api.observe(state, 'getAXState')
    driver.responses['list_windows']['structuredContent']['windows'][0]['bounds'][
        'x'
    ] += 10
    driver.calls.clear()
    api.observe(state, 'getAXState')
    parent_reads = [
        a for n, a in driver.calls if n == 'get_window_state' and a['window_id'] == 1
    ]
    assert len(parent_reads) == 1
    assert 'timeout_ms' not in parent_reads[0]


def test_long_budget_timeout_does_not_repeat_the_long_read(monkeypatch):
    api, state, driver, reads = setup_panel(monkeypatch, truncated=True)
    api.observe(state, 'getAXState')
    original = driver.call

    def timed_out(name, args):
        response = original(name, args)
        if name == 'get_window_state' and args['window_id'] == 1:
            response['structuredContent'].update(
                truncated=True, truncation_reason='timeout'
            )
        return response

    monkeypatch.setattr(driver, 'call', timed_out)
    driver.calls.clear()
    api.observe(state, 'getAXState')
    parent_reads = [
        a for n, a in driver.calls if n == 'get_window_state' and a['window_id'] == 1
    ]
    assert len(parent_reads) == 1 and parent_reads[0]['timeout_ms'] == 5000


def test_first_discovery_refresh_returns_controls_not_internal_no_change(monkeypatch):
    api, state, driver, reads = setup_panel(monkeypatch)
    original = driver.call
    bounds = driver.responses['list_windows']['structuredContent']['windows'][-1][
        'bounds'
    ]

    def first_source(name, args):
        response = original(name, args)
        if name == 'get_window_state' and args['window_id'] == 1:
            response['structuredContent']['elements'] = []
        if name == 'get_window_state' and args['window_id'] == 8:
            response['structuredContent']['elements'] = [
                {'role': 'AXWindow', 'depth': 0},
                {'role': 'AXSheet', 'depth': 1, 'frame': bounds},
                {
                    'role': 'AXButton',
                    'depth': 2,
                    'label': 'Save',
                    'element_token': f'fresh-{reads[8]}',
                },
            ]
        return response

    monkeypatch.setattr(driver, 'call', first_source)
    observation = api.observe(state, 'getAXState')
    assert 'button "Save"' in observation['state']
    assert 'no change' not in observation['state']
    assert state['elements'][1]['element_token'] == 'fresh-2'


def test_closed_window_observation_retries_read_with_new_id_without_input(monkeypatch):
    api, state, driver, reads = setup_panel(monkeypatch)
    original = driver.call
    failed = False

    def closed(name, args):
        nonlocal failed
        response = original(name, args)
        if name == 'get_window_state' and not failed:
            failed = True
            driver.responses['list_windows']['structuredContent']['windows'] = [
                {
                    'pid': 42,
                    'window_id': 40,
                    'title': 'New document',
                    'is_on_screen': True,
                    'bounds': {'x': 0, 'y': 0, 'width': 100, 'height': 100},
                }
            ]
            return {'isError': True, **result(code='window_id_not_found')}
        return response

    monkeypatch.setattr(driver, 'call', closed)
    events = []
    api.event_sink = events.append
    observation = api.observe(state, 'getAXState')
    assert state['window']['window_id'] == 40 and state['fresh']
    assert 'New document' in observation['state']
    assert sum(e.get('phase') == 'observation_retry' for e in events) == 1
    assert not any(
        n in {'click', 'press_key', 'hotkey', 'bring_to_front'} for n, _ in driver.calls
    )


@pytest.mark.parametrize('code', ['window_id_not_found', 'stale_element_token'])
def test_observation_recovery_is_bounded_and_excludes_other_errors(monkeypatch, code):
    api, state, driver, reads = setup_panel(monkeypatch)
    original = driver.call

    def fail(name, args):
        response = original(name, args)
        return (
            {'isError': True, **result(code=code)}
            if name == 'get_window_state'
            else response
        )

    monkeypatch.setattr(driver, 'call', fail)
    with pytest.raises(adapter.ActionError):
        api.observe(state, 'getAXState')
    assert sum(n == 'get_window_state' for n, _ in driver.calls) == (
        2 if code == 'window_id_not_found' else 1
    )
    assert not state['fresh']


def test_observation_does_not_restart_after_original_deadline_expires(monkeypatch):
    import time

    api, state, driver, reads = setup_panel(monkeypatch)
    original = driver.call

    def expired(name, args):
        response = original(name, args)
        if name == 'get_window_state':
            api.deadline = time.monotonic() - 1
            return {'isError': True, **result(code='window_id_not_found')}
        return response

    monkeypatch.setattr(driver, 'call', expired)
    with pytest.raises(adapter.ActionError):
        api.observe(state, 'getAXState')
    assert sum(n == 'get_window_state' for n, _ in driver.calls) == 1


def test_known_unresolved_sheet_reads_fresh_parent_without_empty_child_probe(
    monkeypatch,
):
    api, state, driver, reads = setup_panel(monkeypatch)
    original = driver.call

    def unresolved(name, args):
        response = original(name, args)
        if name == 'get_window_state' and args['window_id'] == 9:
            response['structuredContent']['degraded_reason'] = 'ax_window_unresolved'
        return response

    monkeypatch.setattr(driver, 'call', unresolved)
    first = api.observe(state, 'getAXState', True)
    driver.calls.clear()
    second = api.observe(state, 'getAXState', True)
    assert first['state'] == second['state']
    assert [a['window_id'] for n, a in driver.calls if n == 'get_window_state'] == [1]
    assert state['elements'][1]['element_token'] == 'fresh-2'
    assert state['element_windows'][1]['window_id'] == 1
    assert state['element_surfaces'][1]['window_id'] == 9


def test_parent_first_does_not_skip_child_when_previous_subtree_disappears(monkeypatch):
    api, state, driver, reads = setup_panel(monkeypatch)
    original = driver.call

    def unresolved(name, args):
        response = original(name, args)
        if name == 'get_window_state' and args['window_id'] == 9:
            response['structuredContent']['degraded_reason'] = 'ax_window_unresolved'
        return response

    monkeypatch.setattr(driver, 'call', unresolved)
    api.observe(state, 'getAXState')

    def changed(name, args):
        response = original(name, args)
        if name == 'get_window_state':
            if args['window_id'] == 1:
                response['structuredContent']['elements'] = []
            if args['window_id'] == 9:
                response['structuredContent']['elements'] = [
                    {
                        'role': 'AXButton',
                        'label': 'Direct',
                        'element_token': 'direct-new',
                    }
                ]
        return response

    monkeypatch.setattr(driver, 'call', changed)
    driver.calls.clear()
    observed = api.observe(state, 'getAXState')
    assert 'Direct' in observed['state']
    assert [a['window_id'] for n, a in driver.calls if n == 'get_window_state'] == [
        1,
        9,
    ]
    assert state['elements'][0]['element_token'] == 'direct-new'


def test_explicit_depth_is_scoped_to_one_observation_and_not_silently_reused():
    driver = FakeDriver()
    driver.tools['get_window_state']['inputSchema']['properties']['max_depth'] = {}
    api = adapter.Adapter(driver)
    state = {'pid': 42}
    shallow = api.observe(state, 'getAXState', max_depth=3)
    assert 'AX scope: max_depth=3' in shallow['state']
    assert [a['max_depth'] for n, a in driver.calls if n == 'get_window_state'] == [3]
    assert api.observation_max_depth is None
    driver.calls.clear()
    full = api.observe(state, 'getAXState')
    assert 'AX scope' not in full['state'] and 'no change' not in full['state']
    assert all('max_depth' not in a for n, a in driver.calls if n == 'get_window_state')


@pytest.mark.parametrize('depth', [0, -1, True, 1.5, '3'])
def test_invalid_depth_is_rejected_without_reading(depth):
    driver = FakeDriver()
    api = adapter.Adapter(driver)
    with pytest.raises(ValueError, match='positive integer'):
        api.observe({'pid': 42}, 'getAXState', max_depth=depth)
    assert not driver.calls


def test_depth_requires_driver_capability_and_ax_observation():
    driver = FakeDriver()
    api = adapter.Adapter(driver)
    with pytest.raises(ValueError, match='UnsupportedCapability'):
        api.observe({'pid': 42}, 'getAXState', max_depth=3)
    with pytest.raises(ValueError, match='requires AX observation'):
        api.observe({'pid': 42}, 'getScreenshot', max_depth=3)
    assert not driver.calls


def test_shallow_binding_does_not_require_an_initial_full_tree():
    driver = FakeDriver()
    driver.tools['get_window_state']['inputSchema']['properties']['max_depth'] = {}
    api = adapter.Adapter(driver)
    observed = api.control('getApp', args={'app': 'TextEdit', 'maxDepth': 3})
    assert 'AX scope: max_depth=3' in observed['state']
    assert [a.get('max_depth') for n, a in driver.calls if n == 'get_window_state'] == [
        3
    ]
    driver.calls.clear()
    with pytest.raises(ValueError, match='positive integer'):
        api.control('getApp', args={'app': 'New App', 'maxDepth': 0})
    assert not driver.calls


@pytest.mark.parametrize('code', ['window_id_not_found', 'stale_element_token'])
def test_explicit_depth_survives_read_retry_and_is_restored_on_error(monkeypatch, code):
    driver = FakeDriver()
    driver.tools['get_window_state']['inputSchema']['properties']['max_depth'] = {}
    api = adapter.Adapter(driver)
    original = driver.call

    def fail(name, args):
        response = original(name, args)
        return (
            {'isError': True, **result(code=code)}
            if name == 'get_window_state'
            else response
        )

    monkeypatch.setattr(driver, 'call', fail)
    with pytest.raises(adapter.ActionError):
        api.observe({'pid': 42}, 'getAXState', max_depth=3)
    reads = [a for n, a in driver.calls if n == 'get_window_state']
    assert len(reads) == (2 if code == 'window_id_not_found' else 1)
    assert all(a['max_depth'] == 3 for a in reads)
    assert api.observation_max_depth is None
    assert api.action_app_inventory is None


def test_explicit_depth_is_kept_for_timeout_retry(monkeypatch):
    api, state, driver, reads = setup_panel(monkeypatch, truncated=True)
    driver.tools['get_window_state']['inputSchema']['properties']['max_depth'] = {}
    observed = api.observe(state, 'getAXState', max_depth=3)
    assert 'Attached sheet:' in observed['state']
    calls = [a for n, a in driver.calls if n == 'get_window_state']
    assert all(a['max_depth'] == 3 for a in calls)
    assert any(a.get('timeout_ms') == 5000 for a in calls)
    assert api.observation_max_depth is None


def test_missing_known_parent_is_not_read_twice_during_same_discovery(monkeypatch):
    api, state, driver, reads = setup_panel(monkeypatch)
    original = driver.call
    moved = False

    def panel(name, args):
        response = original(name, args)
        if name == 'get_window_state':
            raw = response['structuredContent']
            if args['window_id'] == 9:
                raw['degraded_reason'] = 'ax_window_unresolved'
            if moved and args['window_id'] == 1:
                raw['elements'] = []
            if moved and args['window_id'] == 8:
                raw['elements'] = [
                    {'role': 'AXWindow', 'depth': 0},
                    {
                        'role': 'AXSheet',
                        'depth': 1,
                        'frame': driver.responses['list_windows']['structuredContent'][
                            'windows'
                        ][-1]['bounds'],
                    },
                    {
                        'role': 'AXButton',
                        'depth': 2,
                        'label': 'New parent',
                        'element_token': f'new-{reads[8]}',
                        'actions': ['AXPress'],
                    },
                ]
        return response

    monkeypatch.setattr(driver, 'call', panel)
    api.observe(state, 'getAXState')
    moved = True
    driver.calls.clear()
    reads.clear()
    observed = api.observe(state, 'getAXState')
    assert 'New parent' in observed['state']
    assert reads[1] == 1
    assert reads[8] == 2  # Discovery then fresh tokens after competing reads.
    assert state['elements'][1]['element_token'] == 'new-2'
    assert state['fresh']
    assert sum(n == 'list_windows' for n, _ in driver.calls) == 2
    assert not any(n in {'click', 'hotkey', 'type_text'} for n, _ in driver.calls)


@pytest.mark.parametrize('supports_depth', [False, True])
@pytest.mark.parametrize(
    'identifier,expected',
    [
        ('open-panel', True),
        ('save-panel', True),
        ('GoToWindow', True),
        ('document', False),
    ],
)
def test_file_panel_identity_reads_only_root_when_supported(
    supports_depth, identifier, expected
):
    driver = FakeDriver()
    if supports_depth:
        driver.tools['get_window_state']['inputSchema']['properties']['max_depth'] = {}
    driver.responses['get_window_state'] = {
        'structuredContent': {'elements': [{'depth': 0, 'role': 'AXWindow'}]},
        'content': [
            {
                'type': 'text',
                'text': f'- [0] AXWindow "Title" [id={identifier} actions=[raise]]',
            }
        ],
    }
    api = adapter.Adapter(driver)
    assert api.current_file_panel({'pid': 42}, {'window_id': 7}) is expected
    reads = [args for name, args in driver.calls if name == 'get_window_state']
    assert len(reads) == 1
    assert reads[0].get('max_depth') == (1 if supports_depth else None)
    assert reads[0]['full_output'] is True
    assert reads[0]['include_screenshot'] is False
    assert api.observation_max_depth is None


@pytest.mark.parametrize('next_depth', [None, 4])
def test_changing_depth_rechecks_parent_ambiguity_before_input(monkeypatch, next_depth):
    api, state, driver, reads = setup_panel(monkeypatch)
    driver.tools['get_window_state']['inputSchema']['properties']['max_depth'] = {}
    original = driver.call
    bounds = driver.responses['list_windows']['structuredContent']['windows'][-1][
        'bounds'
    ]

    def scoped(name, args):
        response = original(name, args)
        if name == 'get_window_state':
            raw = response['structuredContent']
            if args['window_id'] == 9:
                raw['degraded_reason'] = 'ax_window_unresolved'
            if args['window_id'] == 8 and args.get('max_depth') != 3:
                raw['elements'] = [
                    {'role': 'AXWindow', 'depth': 0},
                    {'role': 'AXSheet', 'depth': 1, 'frame': bounds},
                    {
                        'role': 'AXButton',
                        'depth': 2,
                        'label': 'Competing',
                        'element_token': 'competing',
                        'actions': ['AXPress'],
                    },
                ]
        return response

    monkeypatch.setattr(driver, 'call', scoped)
    api.observe(state, 'getAXState', max_depth=3)
    driver.calls.clear()
    api.observe(state, 'getAXState', max_depth=next_depth)
    query_ids = [a['window_id'] for n, a in driver.calls if n == 'get_window_state']
    assert query_ids[0] == 9
    assert 8 in query_ids and 1 in query_ids
    with pytest.raises(ValueError, match='multiple AX parents'):
        api.control('click', 'app', {'target': 1})
    assert not any(n == 'click' for n, _ in driver.calls)


def test_launch_wait_samples_fresh_pid_and_handles_process_replacement(monkeypatch):
    from types import SimpleNamespace

    driver = FakeDriver()
    api = adapter.Adapter(driver)
    bundle = 'com.apple.TextEdit'
    api.action_app_inventory = {
        'apps': [{'bundle_id': bundle, 'running': True, 'pid': 1}]
    }
    samples = iter([None, 41, 42, 42])
    now = [0.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(
        adapter, 'time', SimpleNamespace(monotonic=lambda: now[0], sleep=sleep)
    )
    original = driver.call

    def polling(name, args):
        response = original(name, args)
        if name == 'list_apps':
            pid = next(samples)
            response = result(
                apps=[]
                if pid is None
                else [{'bundle_id': bundle, 'running': True, 'pid': pid}]
            )
        return response

    monkeypatch.setattr(driver, 'call', polling)
    assert api.stable_launched_app(bundle, deadline=2)['pid'] == 42
    assert sum(n == 'list_apps' for n, _ in driver.calls) == 4
    assert sleeps == [adapter.APP_LAUNCH_POLL_SECONDS] * 3


def test_launch_wait_for_absent_process_uses_original_deadline_without_relaunch(
    monkeypatch,
):
    from types import SimpleNamespace

    driver = FakeDriver()
    api = adapter.Adapter(driver)
    driver.responses['list_apps'] = result(apps=[])
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr(
        adapter, 'time', SimpleNamespace(monotonic=lambda: now[0], sleep=sleep)
    )
    with pytest.raises(ValueError, match='ApplicationLaunchUnstable'):
        api.stable_launched_app('com.apple.exposelauncher', deadline=0.6)
    assert now[0] == 0.6
    assert sum(n == 'list_apps' for n, _ in driver.calls) == 4
    assert not any(n == 'launch_app' for n, _ in driver.calls)


@pytest.mark.parametrize('has_controls', [False, True])
def test_automatic_shallow_recovery_reports_scope_even_when_driver_says_complete(
    monkeypatch, has_controls
):
    api, state, driver, reads = setup_panel(monkeypatch)
    driver.tools['get_window_state']['inputSchema']['properties']['max_depth'] = {}
    original = driver.call

    def shallow(name, args):
        response = original(name, args)
        if name == 'get_window_state' and args['window_id'] == 1:
            raw = response['structuredContent']
            if args.get('max_depth') != 3:
                raw.update(elements=[], truncated=True, truncation_reason='timeout')
            else:
                raw.update(truncated=False, truncation_reason=None)
                if not has_controls:
                    raw['elements'] = []
        return response

    monkeypatch.setattr(driver, 'call', shallow)
    observed = api.observe(state, 'getAXState')
    assert 'AX scope: max_depth=3 after full traversal timed out' in observed['state']
    assert 'deeper controls may be omitted' in observed['state']
    assert ('Attached sheet:' in observed['state']) is has_controls
    assert reads[1] == 3
    assert api.observation_max_depth is None
    if has_controls:
        monkeypatch.setattr(driver, 'call', original)
        full = api.observe(state, 'getAXState')
        assert 'AX scope:' not in full['state']
        assert 'no change' not in full['state']


def test_row_compaction_preserves_tokens_order_and_selection_without_mutating_source():
    import copy

    tree = [
        {'role': 'AXWindow', 'depth': 0},
        {'role': 'AXRow', 'depth': 1, 'selected': True, 'focused': True},
        {'role': 'AXCell', 'depth': 2, 'element_token': 'cell'},
        {'role': 'AXTextField', 'depth': 3, 'element_token': 'field'},
        {'role': 'AXButton', 'depth': 1, 'element_token': 'next'},
    ]
    before = copy.deepcopy(tree)
    assert adapter.compact_rows(tree) == [
        tree[0],
        {**tree[2], 'depth': 1, 'selected': True, 'focused': True},
        {**tree[3], 'depth': 2},
        tree[4],
    ]
    assert tree == before


@pytest.mark.parametrize(
    'row,children',
    [
        ({'label': 'Named'}, [{'role': 'AXCell', 'element_token': 'a'}]),
        ({}, [{'role': 'AXCell'}]),
        ({}, [{'role': 'AXButton', 'element_token': 'a'}]),
        (
            {},
            [
                {'role': 'AXCell', 'element_token': 'a'},
                {'role': 'AXCell', 'element_token': 'b'},
            ],
        ),
    ],
)
def test_row_compaction_retains_meaningful_or_unaddressable_wrappers(row, children):
    tree = [
        {'role': 'AXRow', 'depth': 0, **row},
        *[{'depth': 1, **child} for child in children],
    ]
    assert adapter.compact_rows(tree) == tree


def test_row_compaction_respects_missing_depth_levels_and_nested_row_boundaries():
    tree = [
        {'role': 'AXRow', 'depth': 0},
        {'role': 'AXGroup', 'depth': 2},
        {'role': 'AXRow', 'depth': 3},
        {'role': 'AXCell', 'depth': 4, 'element_token': 'nested'},
        {'role': 'AXCell', 'depth': 1, 'element_token': 'outer'},
        {'role': 'AXButton', 'depth': 0, 'element_token': 'outside'},
    ]
    # The outer row collapses its entire subtree once, as before; nested rows
    # are retained rather than independently compressed inside that subtree.
    assert adapter.compact_rows(tree) == [
        *[{**e, 'depth': e['depth'] - 1} for e in tree[1:5]],
        tree[5],
    ]


def test_attachment_geometry_is_only_parsed_for_surfaces(monkeypatch):
    api = adapter.Adapter(FakeDriver())
    rows = [
        {
            'window_id': 1,
            'pid': 42,
            'bounds': {'x': 0, 'y': 0, 'width': 800, 'height': 600},
        },
        {
            'window_id': 2,
            'pid': 42,
            'bounds': {'x': 10, 'y': 20, 'width': 100, 'height': 100},
        },
    ]
    state = {
        'pid': 42,
        'attachment_ax': {
            1: [
                {'role': 'AXWindow', 'depth': 0, 'frame': rows[0]['bounds']},
                {'role': 'AXSheet', 'depth': 1, 'frame': rows[1]['bounds']},
                *[
                    {
                        'role': 'AXButton',
                        'depth': 2,
                        'element_token': f'b{i}',
                        'frame': {'ordinary_control': True},
                        'focused': i == 0,
                    }
                    for i in range(100)
                ],
            ]
        },
    }
    original = adapter.rectangle
    calls = []

    def rectangle(value):
        assert not isinstance(value, dict) or 'ordinary_control' not in value
        calls.append(value)
        return original(value)

    monkeypatch.setattr(adapter, 'rectangle', rectangle)
    api.build_attachments(state, rows)
    assert len(calls) == 4  # Two inventory rows and two AX surfaces.
    assert api.attachment_chain(state, 2, rows) == [2, 1]
    assert state['attachment_focused'] == {2}
    assert len(state['attachment_tokens']) == 100
    assert all(owners == {2} for owners in state['attachment_tokens'].values())


@pytest.mark.parametrize('first_focus', [7, None])
def test_occluded_window_reuses_only_successful_focus_proof(monkeypatch, first_focus):
    driver = FakeDriver()
    row = driver.responses['list_windows']['structuredContent']['windows'][0]
    row.update(is_on_screen=False, on_current_space=True)
    reads = []

    def focus(pid, **kwargs):
        reads.append((pid, kwargs))
        return first_focus if kwargs.get('require_nonminimized') else 7

    monkeypatch.setattr(adapter, 'native_focused_window_id', focus)
    api = adapter.Adapter(driver)
    rows = api.interaction_windows({'pid': 42})
    if first_focus is not None:
        assert len(reads) == 1
        assert rows[0]['window_id'] == 7 and rows[0]['is_focused'] is True
    else:
        assert len(reads) == 2
        assert rows == []  # Unknown/minimized focus never admits an offscreen window.
    before = len(reads)
    api.interaction_windows({'pid': 42})
    assert len(reads) == before * 2  # No cache crosses window enumerations.


@pytest.mark.parametrize('focused', [True, False, None])
@pytest.mark.parametrize('native_focus', [False, True])
def test_keyboard_refresh_reuses_explicit_depth_only_with_live_focus(
    monkeypatch, focused, native_focus
):
    api, state, driver, _ = setup_panel(monkeypatch)
    driver.tools['get_window_state']['inputSchema']['properties']['max_depth'] = {}
    original = driver.call

    def call(name, args):
        response = original(name, args)
        if name == 'get_window_state' and args['window_id'] == 1:
            field = {'role': 'AXTextField', 'depth': 2}
            if focused is not None:
                field['focused'] = focused
            response['structuredContent']['elements'].append(field)
        return response

    monkeypatch.setattr(driver, 'call', call)
    if native_focus:
        monkeypatch.setattr(adapter, 'native_focused_window_id', lambda pid, **kw: 9)
    api.observe(state, 'getAXState', max_depth=3)
    driver.calls.clear()
    if focused is False:
        with pytest.raises(ValueError, match='focused-control evidence conflicts'):
            api.refresh_keyboard_focus(state, state['window'])
    else:
        api.refresh_keyboard_focus(state, state['window'])
    reads = [a for n, a in driver.calls if n == 'get_window_state']
    assert [a.get('max_depth') for a in reads] == (
        [3, 3]
        if focused is True or (focused is None and native_focus)
        else [3, 3, None, None]
    )
    assert not any(
        n in {'press_key', 'hotkey', 'bring_to_front'} for n, _ in driver.calls
    )


def test_scoped_keyboard_refresh_accepts_fresh_native_surface_focus(monkeypatch):
    api, state, driver, _ = setup_panel(monkeypatch)
    driver.tools['get_window_state']['inputSchema']['properties']['max_depth'] = {}
    monkeypatch.setattr(adapter, 'native_focused_window_id', lambda pid, **kw: 9)
    api.observe(state, 'getAXState', max_depth=3)
    driver.calls.clear()
    api.refresh_keyboard_focus(state, state['window'])
    assert [a.get('max_depth') for n, a in driver.calls if n == 'get_window_state'] == [
        3,
        3,
    ]


def test_keyboard_refresh_reads_newly_resolved_child_before_input(monkeypatch):
    api, state, driver, _ = setup_panel(monkeypatch)
    driver.tools['get_window_state']['inputSchema']['properties']['max_depth'] = {}
    api.observe(state, 'getAXState', max_depth=3)
    state['unresolved_windows'] = {9}
    monkeypatch.setattr(adapter, 'native_focused_window_id', lambda pid, **kw: 9)
    original = driver.call

    def resolved_child(name, args):
        response = original(name, args)
        if name == 'get_window_state':
            elements = response['structuredContent']['elements']
            if args['window_id'] == 9:
                # The previously unresolved child now reports explicit negative
                # focus. A parent-only refresh would silently miss this change.
                elements.extend(
                    [
                        {'role': 'AXWindow', 'depth': 0},
                        {'role': 'AXTextField', 'depth': 1, 'focused': False},
                    ]
                )
        return response

    monkeypatch.setattr(driver, 'call', resolved_child)
    driver.calls.clear()
    with pytest.raises(ValueError, match='focused-control evidence conflicts'):
        api.refresh_keyboard_focus(state, state['window'])
    assert any(n == 'get_window_state' and a['window_id'] == 9 for n, a in driver.calls)
    assert not any(
        n in {'press_key', 'hotkey', 'type_text', 'bring_to_front'}
        for n, _ in driver.calls
    )

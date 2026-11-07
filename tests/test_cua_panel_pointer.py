"""Parent-image coordinates must reach only the proven remote panel."""

import pytest
from box_agent.mcp_servers import cua_adapter as adapter
from tests.test_cua_adapter_port import no_native_input_in_fake_driver_tests
from tests.test_cua_upstream_attachments import popover_app


@pytest.fixture
def panel_image(popover_app, monkeypatch):
    api, driver, state, rows, root = popover_app
    root.update(role='AXSheet', label='GoToWindow')
    original = driver.call

    def call(name, args):
        response = original(name, args)
        if name == 'get_window_state' and args['window_id'] == 73:
            response['structuredContent'].update(
                window_bounds=rows[0]['bounds'],
                screenshot_width=1600,
                screenshot_height=1200,
                background_input={
                    'exact_window': {'pid': 42, 'window_id': 73, 'status': 'matched'}
                },
            )
        return response

    monkeypatch.setattr(driver, 'call', call)
    monkeypatch.setattr(
        adapter,
        'resolve_panel_geometry',
        lambda *a, **k: {
            'effect': 'confirmed',
            'read_only': True,
            'frontmost_pid': 42,
            'pid': 42,
            'window_id': 80,
            'parent_window_id': 73,
            'sheet_frame': adapter.rectangle(rows[1]['bounds']),
            'parent_frame': adapter.rectangle(rows[0]['bounds']),
        },
    )
    monkeypatch.setattr(
        adapter,
        'focus_file_panel',
        lambda pid, wid, **k: {
            'effect': 'confirmed',
            'pid': pid,
            'window_id': wid,
            'focused_window_id': wid,
            'frontmost_pid': pid,
        },
    )
    monkeypatch.setattr(
        adapter,
        'verify_file_panel_front',
        lambda pid, wid, **k: {
            'effect': 'confirmed',
            'read_only': True,
            'pid': pid,
            'window_id': wid,
            'focused_window_id': wid,
            'frontmost_pid': pid,
            'frame': adapter.rectangle(rows[1]['bounds']),
        },
    )
    api.observe(state, 'getAXStateAndScreenshot')
    driver.calls.clear()
    return api, driver, state, rows


def test_parent_capture_keeps_parent_geometry_and_panel_identity(panel_image):
    api, driver, state, rows = panel_image
    assert state['image_source_window'] == api.frame(rows[0])
    assert state['image_frame'] == api.frame(rows[1])
    assert state['image_size'] == (1600, 1200)
    assert state['image_bounds'] == adapter.rectangle(rows[0]['bounds'])


def test_overhanging_sheet_preserves_parent_coordinates_and_warns(
    panel_image, monkeypatch
):
    api, driver, state, rows = panel_image
    original = adapter.resolve_panel_geometry
    monkeypatch.setattr(
        adapter,
        'resolve_panel_geometry',
        lambda *a, **k: {**original(*a, **k), 'sheet_contained_in_parent': False},
    )
    output = api.observe(state, 'getAXStateAndScreenshot')
    assert output['image']
    assert 'outer edges may be clipped' in output['state']
    assert state['image_bounds'] == adapter.rectangle(rows[0]['bounds'])
    assert state['image_size'] == (1600, 1200)
    assert state['image_source_window'] == api.frame(rows[0])


def test_panel_coordinate_click_uses_verified_foreground_route(
    panel_image, monkeypatch
):
    api, driver, state, rows = panel_image
    monkeypatch.setattr(api, 'focus_window', lambda *a, **k: rows[1])
    monkeypatch.setattr(api, 'desktop_ratio', lambda: 2)
    api.control('click', 'app', {'target': [500, 160]})
    clicks = [a for n, a in driver.calls if n == 'click']
    assert len(clicks) == 1
    assert clicks[0]['scope'] == 'desktop'
    assert clicks[0]['delivery_mode'] == 'foreground'
    assert (clicks[0]['x'], clicks[0]['y']) == (500, 160)


def test_explicit_background_refuses_without_activation(panel_image):
    api, driver, state, rows = panel_image
    with pytest.raises(adapter.ActionError) as error:
        api.control(
            'click', 'app', {'target': [500, 160], 'deliveryMode': 'background'}
        )
    assert error.value.result['structuredContent']['code'] == 'background_unavailable'
    assert not any(n in {'click', 'bring_to_front'} for n, _ in driver.calls)


def test_changed_native_geometry_blocks_input(panel_image, monkeypatch):
    api, driver, state, rows = panel_image
    monkeypatch.setattr(
        adapter, 'resolve_panel_geometry', lambda *a, **k: {'effect': 'not_sent'}
    )
    with pytest.raises(ValueError, match='native panel geometry'):
        api.control('click', 'app', {'target': [500, 160]})
    assert not any(n in {'click', 'bring_to_front'} for n, _ in driver.calls)


def test_moved_parent_blocks_click_before_delivery(panel_image):
    api, driver, state, rows = panel_image
    rows[0]['bounds'] = {**rows[0]['bounds'], 'x': 20}
    with pytest.raises(ValueError):
        api.control('click', 'app', {'target': [500, 160]})
    assert not any(n in {'click', 'bring_to_front'} for n, _ in driver.calls)


def test_unverified_foreground_ownership_blocks_global_input(panel_image, monkeypatch):
    api, driver, state, rows = panel_image
    original = adapter.resolve_panel_geometry
    monkeypatch.setattr(
        adapter,
        'resolve_panel_geometry',
        lambda *a, **k: {**original(*a, **k), 'frontmost_pid': 999},
    )
    with pytest.raises(ValueError, match='not foreground'):
        api.control(
            'click', 'app', {'target': [500, 160], 'deliveryMode': 'foreground'}
        )
    assert not any(n == 'click' for n, _ in driver.calls)


def test_warm_goto_capture_skips_discarded_child_screenshot(panel_image):
    api, driver, state, rows = panel_image
    # The real Driver marks AX-unresolved children explicitly.
    state['unresolved_windows'] = {80}
    output = api.observe(state, 'getAXStateAndScreenshot')
    reads = [a for n, a in driver.calls if n == 'get_window_state']
    assert [a['window_id'] for a in reads] == [73, 73]
    assert [a['include_screenshot'] for a in reads] == [False, True]
    assert (
        output['image'] and 'Screenshot includes the parent window' in output['state']
    )
    assert state['image_source_window'] == api.frame(rows[0])
    assert state['image_frame'] == api.frame(rows[1])
    assert state['fresh'] is True


def test_parent_capture_hint_does_not_skip_changed_surface(panel_image, monkeypatch):
    api, driver, state, rows = panel_image
    state['unresolved_windows'] = {80}
    original = driver.call

    def changed(name, args):
        response = original(name, args)
        if name == 'get_window_state':
            for element in response['structuredContent'].get('elements', []):
                if element.get('label') == 'GoToWindow':
                    element['label'] = 'OtherSheet'
        return response

    monkeypatch.setattr(driver, 'call', changed)
    api.observe(state, 'getAXStateAndScreenshot')
    reads = [a for n, a in driver.calls if n == 'get_window_state']
    assert any(a['window_id'] == 80 and a['include_screenshot'] for a in reads)
    assert 'image_source_window' not in state


@pytest.mark.parametrize('layer', [0, 8])
@pytest.mark.parametrize(
    'outcome', ['verified', 'wrong_focus', 'wrong_pid', 'still_blocked', 'moved_parent']
)
def test_blocked_file_sheet_restores_exact_surface_before_keyboard(
    panel_image, monkeypatch, layer, outcome
):
    api, driver, state, rows = panel_image
    rows[1]['layer'] = layer
    rows[1]['is_focused'] = True  # Live W01 AX focus remained on GoTo.
    rows[0]['z_index'] = 3  # Owning app is already active, but document blocks GoTo.
    calls = []

    def focus(pid, wid, **kwargs):
        calls.append((pid, wid))
        if outcome != 'still_blocked':
            rows[0]['z_index'] = 1
        if outcome == 'moved_parent':
            rows[0]['bounds'] = {**rows[0]['bounds'], 'x': 99}
        return {
            'effect': 'confirmed',
            'pid': pid if outcome != 'wrong_pid' else 999,
            'window_id': wid,
            'focused_window_id': wid if outcome != 'wrong_focus' else 73,
            'frontmost_pid': pid,
        }

    monkeypatch.setattr(adapter, 'focus_file_panel', focus)
    if outcome == 'still_blocked':
        monkeypatch.setattr(
            adapter,
            'verify_file_panel_front',
            lambda *a, **k: {'effect': 'not_sent', 'code': 'file_panel_obstructed'},
        )
    if outcome == 'verified':
        assert (
            api.focus_window(
                state, rows[1], keyboard=True, observation_fresh=True, activate=True
            )['window_id']
            == 80
        )
    else:
        with pytest.raises(ValueError):
            api.focus_window(
                state, rows[1], keyboard=True, observation_fresh=True, activate=True
            )
    assert calls == [(42, 80)]
    assert not any(
        n in {'bring_to_front', 'press_key', 'hotkey', 'click'} for n, _ in driver.calls
    )


def test_blocked_sheet_without_foreground_request_is_not_raised(
    panel_image, monkeypatch
):
    api, driver, state, rows = panel_image
    rows[0]['z_index'] = 3

    def forbidden(*a, **k):
        pytest.fail('read-only verification must not raise a panel')

    monkeypatch.setattr(adapter, 'focus_file_panel', forbidden)
    monkeypatch.setattr(
        adapter, 'verify_file_panel_front', lambda *a, **k: {'effect': 'not_sent'}
    )
    with pytest.raises(ValueError, match='another surface blocks'):
        api.focus_window(state, rows[1], keyboard=True, observation_fresh=True)
    assert not any(
        n in {'bring_to_front', 'press_key', 'hotkey', 'click'} for n, _ in driver.calls
    )


def test_unverified_native_panel_focus_blocks_coordinate_click(
    panel_image, monkeypatch
):
    api, driver, state, rows = panel_image
    monkeypatch.setattr(
        adapter, 'focus_file_panel', lambda *a, **k: {'effect': 'unverifiable'}
    )
    with pytest.raises(ValueError, match='native panel focus not verified'):
        api.control(
            'click', 'app', {'target': [500, 160], 'deliveryMode': 'foreground'}
        )
    assert not any(n == 'click' for n, _ in driver.calls)


@pytest.mark.parametrize(
    'failure',
    [
        None,
        'pid',
        'window_id',
        'focused_window_id',
        'frontmost_pid',
        'frame',
        'read_only',
        'effect',
    ],
)
def test_stale_driver_order_requires_independent_exact_native_proof(
    panel_image, monkeypatch, failure
):
    api, driver, state, rows = panel_image
    rows[1]['is_focused'] = True
    rows[0]['z_index'] = 3
    report = {
        'effect': 'confirmed',
        'read_only': True,
        'pid': 42,
        'window_id': 80,
        'focused_window_id': 80,
        'frontmost_pid': 42,
        'frame': adapter.rectangle(rows[1]['bounds']),
    }
    if failure:
        report[failure] = {
            'frame': [0, 0, 1, 1],
            'read_only': False,
            'effect': 'not_sent',
        }.get(failure, 999)
    monkeypatch.setattr(adapter, 'verify_file_panel_front', lambda *a, **k: report)
    if failure:
        with pytest.raises(ValueError, match='another surface blocks'):
            api.focus_window(state, rows[1], keyboard=True, observation_fresh=True)
    else:
        assert (
            api.focus_window(state, rows[1], keyboard=True, observation_fresh=True)[
                'window_id'
            ]
            == 80
        )
    assert not any(
        n in {'bring_to_front', 'press_key', 'hotkey', 'click'} for n, _ in driver.calls
    )

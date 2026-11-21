from types import SimpleNamespace

import pytest

from box_agent._vendor.mac_cua.cua_adapter.cua_actions import ActionExecutor
from box_agent._vendor.mac_cua.cua_adapter.cua_driver import ActionError, data


@pytest.fixture
def activation():
    report = dict(code='bring_to_front_exact_window_unverified', status='partial',
                  pid=7, window_id=11, process_activated=True,
                  exact_window_effect=dict(focused=True, front_in_process_on_display=False))
    state = dict(pid=7)
    calls = []
    error = ActionError(dict(structuredContent=report))

    def call(name, args):
        calls.append(name)
        raise error

    def refresh(state, window):
        calls.append('refresh')
        state.update(app_active=True, active_window_id=11, ax_focused_window_id=11)

    executor = ActionExecutor(
        SimpleNamespace(record=lambda event: calls.append(event['phase'])),
        SimpleNamespace(clear_action_app_inventory=lambda: calls.append('clear'),
                        refresh_keyboard_focus=refresh),
        None, call, None, None, action_error=ActionError, result_data=data,
        clear_image=None, menu_entries=None, rectangle=None, top_window=None)
    return executor, state, report, calls


def test_z_order_only_refusal_requires_fresh_semantic_focus(activation):
    executor, state, _, calls = activation
    assert executor.activate_keyboard_target(state, {'window_id': 11}) == {'activated': True}
    assert calls == ['bring_to_front', 'clear', 'refresh', 'keyboard_activation_reconciled']
    from box_agent._vendor.mac_cua.cua_adapter.cua_evidence import validate_phase, FORMAT_VERSION
    validate_phase(dict(phase='keyboard_activation_reconciled', backend='native',
                        window_id=11, activation_report=activation[2]), FORMAT_VERSION)


@pytest.mark.parametrize('field,value', [
    ('pid', 8), ('window_id', 12), ('status', 'failed'),
    ('code', 'other'), ('process_activated', False),
    ('exact_window_effect', {'focused': False, 'front_in_process_on_display': False}),
    ('exact_window_effect', {'focused': True}),
])
def test_other_activation_failures_propagate_without_retry(activation, field, value):
    executor, state, report, calls = activation
    report[field] = value
    with pytest.raises(ActionError):
        executor.activate_keyboard_target(state, {'window_id': 11})
    assert calls == ['bring_to_front']


@pytest.mark.parametrize('field,value', [
    ('app_active', False), ('active_window_id', 12), ('ax_focused_window_id', 12),
])
def test_focus_change_during_activation_refuses_input(activation, field, value):
    executor, state, _, calls = activation
    original = executor.windows.refresh_keyboard_focus

    def refresh(state, window):
        original(state, window)
        state[field] = value

    executor.windows.refresh_keyboard_focus = refresh
    with pytest.raises(ActionError):
        executor.activate_keyboard_target(state, {'window_id': 11})
    assert 'keyboard_activation_reconciled' not in calls


def test_window_or_attachment_change_is_not_hidden(activation):
    executor, state, _, calls = activation

    def changed(state, window):
        raise ValueError('WindowChanged')

    executor.windows.refresh_keyboard_focus = changed
    with pytest.raises(ValueError, match='WindowChanged'):
        executor.activate_keyboard_target(state, {'window_id': 11})
    assert calls == ['bring_to_front', 'clear']

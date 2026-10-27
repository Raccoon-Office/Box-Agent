"""CuaDriver 0.38 boundaries: exact targets, full reads, and no unsafe replay."""

import pytest

from box_agent.mcp_servers import cua_adapter as adapter
from tests.test_cua_adapter_port import FakeDriver, result, no_native_input_in_fake_driver_tests


def observed():
    driver = FakeDriver()
    api = adapter.Adapter(driver)
    api.handles['app'] = state = {'pid': 42}
    api.observe(state, 'getAXStateAndScreenshot')
    return driver, api, state


def test_internal_reads_keep_elements_and_full_metadata():
    driver, api, state = observed()
    assert state['elements'][0]['element_token'] == 's1:1'
    api.control('typeText', 'app', {'text': 'hello'})
    api.observe(state, 'getScreenshot')
    reads = [args for tool, args in driver.calls if tool == 'get_window_state']
    assert len(reads) >= 3
    assert all(args['full_output'] is True for args in reads)


def test_closed_app_window_inventory_is_empty():
    driver, api, state = observed()
    driver.responses['list_windows']['structuredContent']['windows'] = []
    assert api.control('listWindows', 'app') == []


def test_incompatible_read_schema_stops_before_action():
    driver = FakeDriver()
    del driver.tools['get_window_state']['inputSchema']['properties']['full_output']
    api = adapter.Adapter(driver)
    with pytest.raises(ValueError, match='incompatible driver schema'):
        api.observe({'pid': 42}, 'getAXState')
    assert not any(n in {'click', 'press_key', 'hotkey'} for n, _ in driver.calls)


@pytest.mark.parametrize('mode', ['background', 'foreground'])
@pytest.mark.parametrize('method,args,tool', [
    ('pressKey', {'key': 'cmd+o', 'target': 0}, 'hotkey'),
    ('pressKey', {'key': 'escape', 'target': 0}, 'press_key'),
    ('paste', {'text': 'hello', 'target': 0}, 'hotkey'),
    ('typeText', {'text': 'line\nnext\tend', 'target': 0}, 'type_text'),
    ('click', {'target': 0}, 'click'),
    ('drag', {'from': [1, 1], 'to': [2, 2]}, 'drag'),
])
def test_input_retains_target_and_requested_mode(mode, method, args, tool):
    driver, api, state = observed()
    driver.calls.clear()
    api.control(method, 'app', {**args, 'deliveryMode': mode})
    name, sent = driver.calls[-1]
    assert name == tool
    assert sent['pid'] == 42 and sent['window_id'] == 7
    assert sent['delivery_mode'] == mode and 'scope' not in sent
    if 'target' in args:
        assert sent['element_token'] == 's1:1'
    assert not any(n == 'bring_to_front' for n, _ in driver.calls)
    if method == 'typeText':
        assert sent['text'] == args['text']


def test_collection_item_without_axpress_stays_semantic():
    driver, api, state = observed()
    driver.responses['get_window_state']['structuredContent']['elements'][0].update(
        role='AXRow', actions=[], selected=False)
    api.observe(state, 'getAXState')
    driver.calls.clear()
    api.control('click', 'app', {'target': 0})
    assert driver.calls[-1][0] == 'click'
    assert driver.calls[-1][1]['element_token'] == 's1:1'
    assert 'x' not in driver.calls[-1][1]
    assert not any(n == 'get_window_state' for n, _ in driver.calls)


def test_driver_all_layer_inventory_is_authoritative():
    driver, api, state = observed()
    base = driver.responses['list_windows']['structuredContent']['windows'][0]
    base['z_index'] = 1
    driver.responses['list_windows']['structuredContent']['windows'].append(
        {**base, 'window_id': 8, 'layer': 8, 'z_index': 2, 'title': 'Panel'})
    assert api.windows(state)['window_id'] == 8
    # A filtered/closed window must not be resurrected from a second inventory.
    driver.responses['list_windows']['structuredContent']['windows'].pop()
    assert api.windows(state)['window_id'] == 7


@pytest.mark.parametrize('effect', [None, 'unverifiable', 'partial', 'confirmed'])
def test_uncertain_background_failure_never_replays(effect):
    driver, api, state = observed()
    driver.responses['hotkey'] = {'isError': True, **result(code='background_unavailable', effect=effect)}
    with pytest.raises(adapter.ActionError):
        api.control('pressKey', 'app', {'key': 'cmd+shift+g'})
    sent = [a for n, a in driver.calls if n == 'hotkey']
    assert len(sent) == 1 and sent[0]['delivery_mode'] == 'background'


@pytest.mark.parametrize('code', ['window_not_key', 'target_focus_unconfirmed', 'same_pid_keyboard_ambiguity', 'off_space_or_ax_unresolved'])
def test_focus_or_window_refusal_never_escalates(monkeypatch, code):
    driver, api, state = observed()
    original = driver.call
    def call(tool, args):
        response = original(tool, args)
        if tool == 'hotkey' and args['delivery_mode'] == 'background':
            return {'isError': True, **result(code=code, effect='refused')}
        return response
    monkeypatch.setattr(driver, 'call', call)
    with pytest.raises(adapter.ActionError):
        api.control('pressKey', 'app', {'key': 'cmd+a', 'target': 0})
    sent = [a for n, a in driver.calls if n == 'hotkey']
    assert [a['delivery_mode'] for a in sent] == ['background']
    assert all((a['pid'], a['window_id'], a['element_token']) == (42, 7, 's1:1') for a in sent)


def test_driver_success_sends_panel_chord_once():
    driver, api, state = observed()
    api.control('pressKey', 'app', {'key': 'cmd+shift+g'})
    assert sum(n == 'hotkey' for n, _ in driver.calls) == 1


@pytest.mark.parametrize('key,tool', [('cmd+shift+g', 'hotkey'), ('return', 'press_key'), ('escape', 'press_key')])
def test_panel_key_refusal_falls_back_to_driver_with_same_target(monkeypatch, key, tool):
    driver, api, state = observed()
    original = driver.call
    def call(name, args):
        response = original(name, args)
        if name == tool and args['delivery_mode'] == 'background':
            return {'isError': True, **result(code='background_unavailable', effect='refused')}
        return response
    monkeypatch.setattr(driver, 'call', call)
    api.control('pressKey', 'app', {'key': key, 'target': 0})
    sent = [args for name, args in driver.calls if name == tool]
    assert [args['delivery_mode'] for args in sent] == ['background', 'foreground']
    assert all((args['pid'], args['window_id'], args['element_token']) == (42, 7, 's1:1') for args in sent)




def test_action_result_keeps_partial_delivery_details():
    driver, api, state = observed()
    report = {'effect': 'partial', 'delivered_chars': 3, 'path': 'key_events', 'hint': 'Observe before continuing.'}
    driver.responses['type_text'] = result(**report)
    with pytest.raises(adapter.ActionError) as caught:
        api.control('typeText', 'app', {'text': 'abcdef'})
    assert caught.value.result['structuredContent'] == report
    assert sum(n == 'type_text' for n, _ in driver.calls) == 1


@pytest.mark.parametrize('recommendation', [None, 'px', 'page'])
def test_background_request_and_nonforeground_advice_never_activate(monkeypatch, recommendation):
    driver, api, state = observed()
    original = driver.call
    report = {'code': 'background_unavailable', 'effect': 'refused'}
    if recommendation:
        report['escalation'] = {'recommended': recommendation, 'reason': 'Use the addressed background route.'}

    def call(name, args):
        response = original(name, args)
        if name == 'type_text' and args['delivery_mode'] == 'background':
            return {'isError': True, **result(**report)}
        return response

    monkeypatch.setattr(driver, 'call', call)
    request = {'text': 'hello', 'target': 0}
    if recommendation is None:
        request['deliveryMode'] = 'background'
    with pytest.raises(adapter.ActionError) as caught:
        api.control('typeText', 'app', request)
    assert caught.value.result['structuredContent'] == report
    sent = [args for name, args in driver.calls if name == 'type_text']
    assert len(sent) == 1 and sent[0]['delivery_mode'] == 'background'


def test_success_preserves_driver_route_advice_and_observation_text():
    driver, api, state = observed()
    advice = {'recommended': 'page', 'reason': 'AX readback does not confirm DOM input.'}
    driver.responses['type_text'] = {
        **result(effect='unverifiable', path='ax', escalation=advice),
        'content': [{'type': 'text', 'text': 'Input is unverified; observe the page before retrying.'}],
    }
    report = api.control('typeText', 'app', {'text': 'hello', 'target': 0})
    assert report['escalation'] == advice
    assert report['messages'] == ['Input is unverified; observe the page before retrying.']

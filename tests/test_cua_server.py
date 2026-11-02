"""Box-Agent MCP tool layer over the ported adapter (fake driver; no GUI/model).

Re-expresses the mac-cua JS REPL contracts from tests/test_cua_adapter.py as
direct tool calls, plus the cua-driver 0.32 deltas.
"""
import asyncio
import copy
import threading

import pytest

from box_agent.mcp_servers import cua_adapter as adapter
from box_agent.mcp_servers import cua_server

from tests.test_cua_adapter_port import (
    FakeDriver, PNG, result, no_native_input_in_fake_driver_tests,
)


@pytest.fixture
def service():
    drivers = []

    def factory(timeout):
        driver = FakeDriver()
        driver.tools['get_window_state']['inputSchema']['properties']['max_image_dimension'] = {}
        driver.tools['get_screen_size'] = {'name': 'get_screen_size', 'inputSchema': {
            'type': 'object', 'properties': {'session': {}}}}
        driver.responses['get_screen_size'] = result(width=1512, height=982, scale_factor=2.0)
        drivers.append(driver)
        return driver

    svc = cua_server.CuaService(factory)
    svc.drivers = drivers
    yield svc
    svc.close()


def ok(service, name, **args):
    content, is_error = service.call(name, args)
    assert not is_error, content
    return content


def texts(content):
    return '\n'.join(c['text'] for c in content if c['type'] == 'text')


def test_get_app_observation_diff_and_silent_actions(service):
    initial = ok(service, 'get_app', app='TextEdit')
    assert [c['type'] for c in initial] == ['text']
    assert texts(initial).startswith('Window:') and '0 button "OK"' in texts(initial)
    combined = ok(service, 'get_state', app='TextEdit', screenshot=True, disable_diffing=True)
    assert [c['type'] for c in combined] == ['text', 'image']
    assert combined[1] == {'type': 'image', 'mimeType': 'image/png', 'data': PNG}
    only_image = ok(service, 'get_state', app='TextEdit', screenshot=True, ax=False)
    assert [c['type'] for c in only_image] == ['image']
    driver = service.drivers[0]
    driver.responses['get_window_state']['structuredContent']['elements'][0]['label'] = 'Changed'
    assert '~ 0 button "Changed"' in texts(ok(service, 'get_state', app='TextEdit'))
    assert 'no change' in texts(ok(service, 'get_state', app='TextEdit'))
    before = len(driver.calls)
    assert texts(ok(service, 'click', app='TextEdit', target=0)) == 'Done (driver effect: unverifiable). Observe to verify.'
    ok(service, 'type_text', app='TextEdit', text='hello')
    ok(service, 'press_key', app='TextEdit', key='cmd+s')
    sent = [n for n, _ in driver.calls[before:]]
    assert sent.count('get_window_state') == 2 and sent[-1] == 'press_key'
    assert driver.calls[-1][1]['key'] == 's'
    assert driver.calls[-1][1]['modifiers'] == ['cmd']


def test_unknown_app_reference_and_stale_ids_send_no_input(service):
    content, is_error = service.call('click', {'app': 'Word', 'target': 0})
    assert is_error and 'get_app' in texts(content)
    ok(service, 'get_app', app='TextEdit')
    ok(service, 'click', app='TextEdit', target=0)
    driver = service.drivers[0]
    before = len(driver.calls)
    content, is_error = service.call('click', {'app': 'TextEdit', 'target': 0})
    assert is_error and 'StaleElement' in texts(content)
    assert not any(n == 'click' for n, _ in driver.calls[before:])
    content, is_error = service.call('click', {'app': 'TextEdit', 'target': [12, 34]})
    assert is_error and 'Coordinates require a current screenshot' in texts(content)


@pytest.mark.parametrize('code,uncertain', [(-25204, True), (-25205, True), (-25206, False)])
def test_ax_press_uncertainty_requires_observation_without_foreground_replay(service, code, uncertain):
    ok(service, 'get_app', app='TextEdit')
    driver = service.drivers[0]
    driver.responses['click'] = {
        'isError': True, 'content': [{'type': 'text', 'text':
            f'AX action failed: AXUIElementPerformAction(AXPress) returned {code}'}]}
    content, is_error = service.call('click', {'app': 'TextEdit', 'target': 0})
    assert is_error is not uncertain
    if uncertain:
        assert texts(content) == 'Done (driver effect: unverifiable). Observe to verify.'
    assert sum(name == 'click' for name, _ in driver.calls) == 1
    assert not any(name == 'bring_to_front' for name, _ in driver.calls)
    content, is_error = service.call('click', {'app': 'TextEdit', 'target': 0})
    assert is_error and 'StaleElement' in texts(content)


def test_failed_effects_are_errors_and_never_replayed(service):
    ok(service, 'get_app', app='TextEdit')
    driver = service.drivers[0]
    driver.responses['type_text'] = result(effect='partial')
    content, is_error = service.call('type_text', {'app': 'TextEdit', 'text': 'x'})
    assert is_error and 'partial' in texts(content)
    assert sum(n == 'type_text' for n, _ in driver.calls) == 1


def test_transport_failure_resets_references(service):
    ok(service, 'get_app', app='TextEdit')
    driver = service.drivers[0]

    def boom(name, args):
        driver.calls.append((name, args))
        raise TimeoutError('Driver timed out; outcome unknown.')

    driver.call = boom
    content, is_error = service.call('press_key', {'app': 'TextEdit', 'key': 'Return'})
    assert is_error and 'do not replay' in texts(content)
    assert service.adapter is None and driver.closed
    content, is_error = service.call('get_state', {'app': 'TextEdit'})
    assert is_error and 'get_app' in texts(content)
    ok(service, 'get_app', app='TextEdit')
    assert len(service.drivers) == 2


def test_paste_writes_clipboard_once_then_cmd_v(service):
    ok(service, 'get_app', app='TextEdit')
    driver = service.drivers[0]
    driver.calls.clear()
    ok(service, 'paste', app='TextEdit', text='/Users/me/报告.docx')
    actions = [(n, a) for n, a in driver.calls if n in {'bring_to_front', 'clipboard_write', 'hotkey', 'press_key'}]
    assert [n for n, _ in actions] == ['clipboard_write', 'press_key']
    assert actions[0][1]['text'] == '/Users/me/报告.docx'
    assert actions[-1][1] == {'pid': 42, 'window_id': 7, 'key': 'v', 'modifiers': ['cmd'],
                             'delivery_mode': 'background', 'session': service.adapter.session}


def test_032_screenshot_size_is_capped_to_model_image_limit(service):
    ok(service, 'get_app', app='TextEdit')
    ok(service, 'get_state', app='TextEdit', screenshot=True)
    observed = [a for n, a in service.drivers[0].calls if n == 'get_window_state']
    assert observed and all(a['max_image_dimension'] == adapter.MAX_IMAGE_DIMENSION for a in observed)


def test_032_supplemental_desktop_click_uses_native_desktop_pixels(service):
    ok(service, 'get_app', app='TextEdit')
    api = service.adapter
    state = {'pid': 42, 'image_background_input': {'exact_window': {
        'pid': 42, 'window_id': 55, 'status': 'matched'}},
        'image_bounds': (100.0, 200.0, 400.0, 300.0), 'image_size': (800, 600)}
    window = {'window_id': 55, 'native_inventory': True, 'native_verified': True}
    identity, params = api.pixel_click_route(state, window, {'x': 400, 'y': 300}, foreground=True)
    # Window pixel (400,300) is screen point (300,350); 0.32 wants 2x desktop pixels.
    assert identity == {'scope': 'desktop'}
    assert (params['x'], params['y']) == (600.0, 700.0)


@pytest.mark.parametrize('label', ['raccoon-docx', 'repl-0123'])
def test_032_ended_session_message_revives_once(label):
    driver = FakeDriver()
    api = adapter.Adapter(driver)
    rejection = {'isError': True, 'content': [{'type': 'text', 'text':
        "session has ended; tool call 'press_key' was rejected. "
        f"Call start_session with session '{label}' to start it again, or use a new session label."}]}
    original = driver.call

    def call(name, args):
        response = original(name, args)
        if name == 'press_key' and sum(n == 'press_key' for n, _ in driver.calls) == 1:
            return rejection
        return response

    driver.call = call
    api.call('press_key', {'key': 'return'})
    assert [n for n, _ in driver.calls] == ['press_key', 'start_session', 'press_key']
    assert driver.calls[1][1] == {'session': label}


def test_unnamed_032_session_rejection_is_not_revived():
    text = ("session has ended; tool call 'press_key' was rejected. "
            "Call start_session without a session label to start a new unnamed session.")
    assert adapter.ended_session('press_key', text) is None


def test_mcp_server_lists_tools_and_returns_images(service):
    owner = cua_server.SerialCoordinator(service.call_result,
        invalidate=service.invalidate_after_cancellation, close=service.close)
    server = cua_server.build_server(service, owner)
    from mcp.types import CallToolRequest, CallToolRequestParams, ListToolsRequest

    async def run():
        listed = await server.request_handlers[ListToolsRequest](ListToolsRequest(method='tools/list'))
        names = [t.name for t in listed.root.tools]
        call = server.request_handlers[CallToolRequest]
        await call(CallToolRequest(method='tools/call', params=CallToolRequestParams(
            name='get_app', arguments={'app': 'TextEdit'})))
        state = await call(CallToolRequest(method='tools/call', params=CallToolRequestParams(
            name='get_state', arguments={'app': 'TextEdit', 'screenshot': True})))
        bad = await call(CallToolRequest(method='tools/call', params=CallToolRequestParams(
            name='click', arguments={'app': 'TextEdit', 'target': 'OK'})))
        await owner.aclose()
        return names, state.root, bad.root

    names, state, bad = asyncio.run(run())
    assert names == ['list_apps', 'get_app', 'list_windows', 'get_state', 'click', 'drag',
                     'scroll', 'set_value', 'type_text', 'paste', 'press_key']
    assert not state.isError and [c.type for c in state.content] == ['text', 'image']
    assert bad.isError
    assert state.structuredContent['tool'] == 'get_state'
    assert state.structuredContent['adapter_context']['elements'][0]['element_token'] == 's1:1'
    events = state.structuredContent['backend_events']
    report = next(e['result'] for e in events if e['phase'] == 'result' and e['tool'] == 'get_window_state')
    assert report['structuredContent']['snapshot_id'] == 's1'
    assert not any(c['type'] == 'image' for c in report['content'])
    assert 'image' not in state.structuredContent['output']


def test_action_result_keeps_backend_effect_and_failure_details(service):
    ok(service, 'get_app', app='TextEdit')
    driver = service.drivers[0]
    driver.responses['type_text'] = result(effect='partial', receiver_pid=123, reason='fake partial write')
    response = service.call_result('type_text', {'app': 'TextEdit', 'text': 'x'})
    assert response.isError
    report = next(e['result']['structuredContent'] for e in response.structuredContent['backend_events']
                  if e['phase'] == 'result' and e['tool'] == 'type_text')
    assert report == {'effect': 'partial', 'receiver_pid': 123, 'reason': 'fake partial write'}
    assert response.structuredContent['requires_requery']
    assert sum(n == 'type_text' for n, _ in driver.calls) == 1
    assert all(e['request_id'] == response.structuredContent['request_id']
               for e in response.structuredContent['backend_events'])


@pytest.mark.asyncio
async def test_mcp_session_roundtrip_preserves_grounding_and_closes_owned_backend(service):
    import anyio
    from mcp import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams
    owner = cua_server.SerialCoordinator(service.call_result,
        invalidate=service.invalidate_after_cancellation, close=service.close)
    server = cua_server.build_server(service, owner)
    try:
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            async with anyio.create_task_group() as group:
                group.start_soon(server.run, *server_streams, server.create_initialization_options())
                async with ClientSession(*client_streams) as client:
                    await client.initialize()
                    response = await client.call_tool('get_app', {'app': 'TextEdit'})
                    assert not response.isError
                    structured = response.structuredContent
                    assert structured['adapter_context']['pid'] == 42
                    reports = [e['result']['structuredContent'] for e in structured['backend_events']
                               if e['phase'] == 'result' and e['tool'] == 'get_window_state']
                    assert reports[-1]['elements'][0]['element_token'] == 's1:1'
                    assert response.content[0].text.startswith('Window:')
                group.cancel_scope.cancel()
    finally:
        await owner.aclose()
    assert service.adapter is None and service.drivers[0].closed


@pytest.mark.asyncio
@pytest.mark.parametrize('worker_finishes_first', [False, True])
async def test_mcp_cancellation_requires_requery_before_another_write(service, worker_finishes_first):
    from mcp.types import CallToolRequest, CallToolRequestParams
    ok(service, 'get_app', app='TextEdit')
    driver = service.drivers[0]
    original_call = driver.call
    entered, release = threading.Event(), threading.Event()

    def blocking_call(name, args):
        if name == 'type_text':
            entered.set()
            assert release.wait(5), 'test did not release driver'
        return original_call(name, args)

    driver.call = blocking_call
    owner = cua_server.SerialCoordinator(service.call_result,
        invalidate=service.invalidate_after_cancellation, close=service.close)
    handler = cua_server.build_server(service, owner).request_handlers[CallToolRequest]
    def request(name, **args):
        return CallToolRequest(method='tools/call', params=CallToolRequestParams(
            name=name, arguments={'app': 'TextEdit', **args}))
    try:
        first = asyncio.create_task(handler(request('type_text', text='one')))
        assert await asyncio.to_thread(entered.wait, 2)
        second = asyncio.create_task(handler(request('press_key', key='Return')))
        await asyncio.sleep(0)
        first.cancel()
        if worker_finishes_first:
            release.set()
            # Drain the worker while the cancelled coroutine cannot yet resume.
            owner._pool.submit(lambda: None).result(timeout=2)
        with pytest.raises(asyncio.CancelledError):
            await first
        if not worker_finishes_first:
            assert not second.done()
        release.set()
        rejected = (await asyncio.wait_for(second, 2)).root
        assert rejected.isError and 'RequiresRequery' in rejected.content[0].text
        assert not any(name == 'press_key' for name, _ in driver.calls)
        # A screenshot alone does not acknowledge a changed AX input target.
        await handler(request('get_state', ax=False, screenshot=True))
        assert (await handler(request('press_key', key='Return'))).root.isError
        await handler(request('get_state'))
        assert not (await handler(request('press_key', key='Return'))).root.isError
        assert sum(name == 'type_text' for name, _ in driver.calls) == 1
        assert sum(name == 'press_key' for name, _ in driver.calls) == 1
    finally:
        release.set()
        await owner.aclose()


def _desktop_driver(service, width=1568, height=1018, original=(3024, 1964)):
    ok(service, 'list_apps')
    driver = service.drivers[0]
    driver.tools['get_desktop_state'] = {'name': 'get_desktop_state', 'inputSchema': {
        'type': 'object', 'properties': {'max_image_dimension': {}, 'session': {}}}}
    driver.responses['get_desktop_state'] = {
        'content': [{'type': 'image', 'mimeType': 'image/png', 'data': PNG}],
        'structuredContent': {'screenshot_width': width, 'screenshot_height': height,
                              'screenshot_original_width': original[0],
                              'screenshot_original_height': original[1], 'scale_factor': 2.0}}
    return driver




@pytest.mark.parametrize('tool,payload,driver_tool', [
    ('type_text', {'text': '/tmp/'}, 'type_text'),
    ('press_key', {'key': 'cmd+a'}, 'press_key'),
    ('paste', {'text': '/tmp/'}, 'press_key'),
])
def test_explicit_input_element_reaches_driver_without_click(service, tool, payload, driver_tool):
    ok(service, 'get_app', app='TextEdit')
    driver = service.drivers[0]
    driver.calls.clear()
    ok(service, tool, app='TextEdit', element=0, **payload)
    name, args = driver.calls[-1]
    assert name == driver_tool
    assert args['element_token'] == 's1:1'
    assert args['pid'] == 42 and args['window_id'] == 7
    assert args['delivery_mode'] == 'background'
    assert not any(n in {'click', 'bring_to_front', 'hotkey', 'set_value'} for n, _ in driver.calls)
    before = len(driver.calls)
    content, error = service.call(tool, {'app': 'TextEdit', 'element': 0, **payload})
    assert error and 'StaleElement' in texts(content)
    assert not any(n in {driver_tool, 'clipboard_write'} for n, _ in driver.calls[before:])


@pytest.mark.parametrize('element', [-1, True, '0', [1, 2], None])
def test_invalid_input_element_never_mutates_clipboard_or_sends_input(service, element):
    ok(service, 'get_app', app='TextEdit')
    driver = service.drivers[0]
    driver.calls.clear()
    _, error = service.call('paste', {'app': 'TextEdit', 'element': element, 'text': 'x'})
    assert error
    assert not any(n in {'clipboard_write', 'press_key', 'bring_to_front'} for n, _ in driver.calls)


def test_confirm_action_is_ax_and_never_pixel_fallback(service):
    ok(service, 'get_app', app='TextEdit')
    driver = service.drivers[0]
    driver.responses['get_window_state']['structuredContent']['elements'][0]['actions'] = ['AXConfirm']
    ok(service, 'get_state', app='TextEdit')
    driver.calls.clear()
    ok(service, 'click', app='TextEdit', target=0, action='confirm')
    name, args = driver.calls[-1]
    assert name == 'click' and args['action'] == 'confirm'
    assert args['element_token'] == 's1:1' and 'x' not in args
    assert args['delivery_mode'] == 'background'
    assert not any(n in {'get_window_state', 'bring_to_front'} for n, _ in driver.calls)


def test_explicit_foreground_key_restores_original_focus_then_desktop_hotkey(service):
    ok(service, 'get_app', app='TextEdit')
    driver = service.drivers[0]
    driver.calls.clear()
    ok(service, 'press_key', app='TextEdit', key='cmd+o', delivery_mode='foreground')
    names = [n for n, _ in driver.calls]
    assert names.index('bring_to_front') < names.index('hotkey')
    calls = [a for n, a in driver.calls if n == 'hotkey']
    assert len(calls) == 1 and calls[0]['scope'] == 'desktop'
    assert calls[0]['keys'] == ['cmd', 'o']

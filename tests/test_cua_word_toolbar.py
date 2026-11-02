"""Word Find's top-level toolbar is not owned by its snapshot source window."""

import pytest
from box_agent.mcp_servers import cua_adapter as adapter
from tests.test_cua_adapter_port import FakeDriver, no_native_input_in_fake_driver_tests


@pytest.fixture
def toolbar(monkeypatch):
    driver = FakeDriver()
    api = adapter.Adapter(driver)
    state = {'pid': 42, 'bundle_id': 'com.microsoft.Word'}
    api.handles['app'] = state
    rows = driver.responses['list_windows']['structuredContent']['windows']
    rows[0].update(z_index=1, is_focused=False)
    bounds = {'x': 10, 'y': 20, 'width': 300, 'height': 80}
    rows.append(
        {
            **rows[0],
            'window_id': 39,
            'title': 'Find',
            'bounds': bounds,
            'z_index': 2,
            'is_focused': True,
        }
    )
    driver.responses['list_apps']['structuredContent']['apps'][0].update(
        active=True, bundle_id='com.microsoft.Word'
    )
    original = driver.call

    def call(name, args):
        r = original(name, args)
        if name == 'get_window_state':
            d = r['structuredContent']
            if args['window_id'] == 39:
                d.update(elements=[], degraded_reason='ax_window_unresolved: toolbar')
            else:
                d['elements'] = [
                    {
                        'role': 'AXToolbar',
                        'depth': 0,
                        'frame': bounds,
                        'element_token': 'doc:0',
                    },
                    {'role': 'AXTextField', 'depth': 1, 'element_token': 'doc:1'},
                    {
                        'role': 'AXButton',
                        'depth': 1,
                        'element_token': 'doc:2',
                        'actions': ['AXPress'],
                    },
                ]
        return r

    monkeypatch.setattr(driver, 'call', call)
    monkeypatch.setattr(adapter, 'native_focused_window_id', lambda *a, **k: 39)
    return api, state, driver


def test_toolbar_exposes_recovery_without_misleading_capture(toolbar):
    api, state, driver = toolbar
    r = api.observe(state, 'getAXStateAndScreenshot')
    assert r['image'] is None and 'observation-only' in r['state']
    assert 'Escape' in r['state']
    assert state['elements'][1]['element_token'] == 'doc:1'
    assert not state['attachments'] and 'image_frame' not in state
    assert api.observe(state, 'getScreenshot')['image'] is None
    driver.calls.clear()
    api.control('pressKey', 'app', {'key': 'escape', 'deliveryMode': 'foreground'})
    inputs = [(n, p) for n, p in driver.calls if n in {'press_key', 'bring_to_front'}]
    assert len(inputs) == 1
    assert {k: v for k, v in inputs[0][1].items() if k != 'session'} == {
        'pid': 42,
        'window_id': 39,
        'key': 'escape',
        'delivery_mode': 'foreground',
    }


@pytest.mark.parametrize(
    'failure', ['inactive', 'wrong_focus', 'missing_focus', 'moved', 'background']
)
def test_toolbar_recovery_requires_exact_live_foreground(toolbar, monkeypatch, failure):
    api, state, driver = toolbar
    api.observe(state, 'getAXStateAndScreenshot')
    if failure == 'inactive':
        driver.responses['list_apps']['structuredContent']['apps'][0]['active'] = False
    elif failure in {'wrong_focus', 'missing_focus'}:
        monkeypatch.setattr(
            adapter,
            'native_focused_window_id',
            lambda *a, **k: 7 if failure == 'wrong_focus' else None,
        )
    elif failure == 'moved':
        driver.responses['list_windows']['structuredContent']['windows'][1]['bounds'][
            'x'
        ] += 1
    driver.calls.clear()
    with pytest.raises(ValueError):
        api.control(
            'pressKey',
            'app',
            {
                'key': 'escape',
                'deliveryMode': 'background'
                if failure == 'background'
                else 'foreground',
            },
        )
    assert not any(
        n in {'press_key', 'hotkey', 'bring_to_front'} for n, _ in driver.calls
    )


@pytest.mark.parametrize(
    'method,args',
    [
        ('click', {'target': 2}),
        ('click', {'target': [10, 10]}),
        ('setValue', {'target': 1, 'value': 'bad'}),
        ('pressKey', {'target': 1, 'key': 'return'}),
    ],
)
def test_toolbar_never_rebinds_document_tokens_or_uses_bad_pixels(
    toolbar, method, args
):
    api, state, driver = toolbar
    api.observe(state, 'getAXStateAndScreenshot')
    driver.calls.clear()
    with pytest.raises(ValueError, match='observation-only'):
        api.control(
            method,
            'app',
            {
                **args,
                **({'deliveryMode': 'foreground'} if method != 'setValue' else {}),
            },
        )
    assert not any(
        n in {'click', 'set_value', 'press_key', 'hotkey', 'bring_to_front'}
        for n, _ in driver.calls
    )

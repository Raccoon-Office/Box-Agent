"""Regressions for Office nested save sheets and auditable helper failures."""

import json
import subprocess
import pytest
from tests.test_cua_adapter_port import (
    adapter,
    FakeDriver,
    result,
    no_native_input_in_fake_driver_tests,
)
from box_agent.mcp_servers import cua_panel_keyboard as panel


@pytest.mark.parametrize(
    'case,ids',
    [('E02', [794, 787, 776]), ('E04', [1028, 1023, 998]), ('W06', [638, 627, 624])],
)
@pytest.mark.parametrize('layer', [0, 8])
@pytest.mark.parametrize(
    'change', [None, 'missing_parent', 'moved_parent', 'wrong_pid', 'reordered']
)
def test_nested_save_sheet_requires_complete_live_ancestry(
    monkeypatch, case, ids, change, layer
):
    api = adapter.Adapter(FakeDriver())
    rows = [
        {
            'window_id': wid,
            'pid': 42,
            'layer': layer if i < 2 else 0,
            'is_on_screen': True,
            'z_index': 3 - i,
            'bounds': {
                'x': 100 - i * 20,
                'y': 100 - i * 20,
                'width': 460 + i * 200,
                'height': 336 + i * 150,
            },
        }
        for i, wid in enumerate(ids)
    ]
    api.driver.responses['list_windows'] = result(windows=rows)
    state = {
        'pid': 42,
        'attachment_ax': {
            ids[-1]: [
                {'role': 'AXWindow', 'depth': 0},
                {'role': 'AXSheet', 'depth': 1, 'frame': rows[1]['bounds']},
                {
                    'role': 'AXSheet',
                    'label': 'GoToWindow',
                    'depth': 2,
                    'frame': rows[0]['bounds'],
                },
                {'role': 'AXTextField', 'depth': 3, 'focused': True},
            ]
        },
    }
    api.build_attachments(state, rows)
    report = {
        'effect': 'confirmed',
        'read_only': True,
        'pid': 42,
        'window_id': ids[0],
        'parent_window_id': ids[1],
        'sheet_frame': list(adapter.rectangle(rows[0]['bounds'])),
        'parent_frame': list(adapter.rectangle(rows[1]['bounds'])),
        'ancestry': [
            {'window_id': r['window_id'], 'frame': list(adapter.rectangle(r['bounds']))}
            for r in rows
        ],
    }
    if change == 'missing_parent':
        report['ancestry'].pop(1)
    if change == 'moved_parent':
        report['ancestry'][1]['frame'][0] += 1
    if change == 'wrong_pid':
        report['pid'] = 99
    if change == 'reordered':
        report['ancestry'].reverse()
    monkeypatch.setattr(adapter, 'resolve_panel_geometry', lambda *a, **k: report)
    if change:
        with pytest.raises(ValueError, match='native panel geometry'):
            api.verify_panel_geometry(state, rows[0], rows[-1], rows=rows)
        assert api.refresh_focused_file_sheet(state, rows[0], rows, ids) is None
    else:
        # Capture is the outer document/Print, file-panel host is the middle sheet.
        api.verify_panel_geometry(state, rows[0], rows[-1], rows=rows)
        api.refresh_focused_file_sheet(state, rows[0], rows, ids)
        assert api.attachment_chain(state, ids[0], rows) == ids
        assert state['attachment_ax'] == {} and state['attachment_tokens'] == {}
        assert not any(n == 'get_window_state' for n, _ in api.driver.calls)


def test_cold_compilation_has_timeout_and_can_retry(tmp_path, monkeypatch):
    helper = panel
    monkeypatch.setattr(helper, '_binary', None)
    monkeypatch.setattr(helper, '_unavailable', None)
    monkeypatch.setattr(helper.Path, 'home', lambda: tmp_path)
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        raise subprocess.TimeoutExpired(argv, kwargs['timeout'])

    monkeypatch.setattr(helper.subprocess, 'run', run)
    response = helper._invoke(['panel-geometry', '42', '1'], timeout=0.05)
    assert response == {'effect': 'not_sent', 'code': 'native_helper_build_timeout'}
    assert len(calls) == 1 and 0 < calls[0][1]['timeout'] <= 0.05
    assert calls[0][0][1] == 'swiftc'
    assert helper._unavailable is None
    assert not any(p.is_file() for p in tmp_path.rglob('panel-keyboard'))
    helper._invoke(['panel-geometry', '42', '1'], timeout=0.05)
    assert len(calls) == 2

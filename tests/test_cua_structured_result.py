from types import SimpleNamespace
from unittest.mock import AsyncMock
import json

import pytest

from box_agent.plugins.cua.config import CuaConfig
from box_agent.plugins.cua.wiring import build_cua_bindings
from box_agent.plugins.cua.structured_result import cua_structured_text
from box_agent.tools.mcp_loader import MCPTool
from box_agent.tools.mcp_result_hooks import bind_mcp_result_adapter


def tool(payload, *, server="desktop", is_error=False):
    result = SimpleNamespace(content=[SimpleNamespace(text="Found 1 window(s).")],
                             structuredContent=payload, isError=is_error)
    session = SimpleNamespace(call_tool=AsyncMock(return_value=result))
    return MCPTool(name="list_windows", remote_name="list_windows", description="list",
                   parameters={"type": "object", "properties": {}},
                   session=session, server_name=server)


def binding(*, enabled=True, images=False):
    return build_cua_bindings(llm=SimpleNamespace(capabilities={"image_input": images}),
                             config=CuaConfig(server_name="desktop", feed_screenshots=False),
                             enabled=enabled)


@pytest.mark.asyncio
async def test_cua_windows_are_visible_even_without_vision():
    payload = {"windows": [{"pid": 45645, "window_id": 212384, "title": "计算器"}]}
    with bind_mcp_result_adapter(binding()):
        result = await tool(payload).execute()
    assert result.success
    assert '"window_id":212384' in result.content
    assert '"title":"计算器"' in result.content
    assert result.content.endswith("Found 1 window(s).")
    assert result.transient_followup_content is None


@pytest.mark.asyncio
async def test_driver_tokens_and_snapshot_ids_are_exposed_unchanged():
    payload = {"tree_markdown": "duplicate tree", "snapshot_id": "s00000004",
               "pid": 45645, "window_id": 212384,
               "elements": [{"element_index": 2, "element_token": "s00000004:2",
                             "role": "AXButton", "label": "全部清除"}],
               "background_input": {"routes": [{"status": "refused"}]}}
    with bind_mcp_result_adapter(binding()):
        result = await tool(payload).execute()
    assert '"snapshot_id":"s00000004"' in result.content
    assert '"element_token":"s00000004:2"' in result.content
    assert '"element_index":2' in result.content
    assert '"status":"refused"' in result.content
    assert "duplicate tree" not in result.content


@pytest.mark.parametrize("enabled,server", [(False, "desktop"), (True, "other")])
@pytest.mark.asyncio
async def test_disabled_cua_and_other_mcp_keep_original_output(enabled, server):
    with bind_mcp_result_adapter(binding(enabled=enabled)):
        result = await tool({"secret": "not projected"}, server=server).execute()
    assert result.content == "Found 1 window(s)."
    assert result.raw_output == {"mcp_structured_content": {"secret": "not projected"}}


@pytest.mark.asyncio
async def test_no_plugin_does_not_expose_structured_content():
    result = await tool({"windows": [{"window_id": 1}]}).execute()
    assert result.content == "Found 1 window(s)."


@pytest.mark.asyncio
async def test_error_results_preserve_diagnostics_without_becoming_actionable():
    with bind_mcp_result_adapter(binding()):
        result = await tool({"element_token": "unsafe"}, is_error=True).execute()
    assert not result.success
    assert '"element_token":"unsafe"' in result.content
    assert "use this state only for diagnosis" in result.content
    assert "refresh the target state before further actions" in result.content
    assert result.error
    assert result.transient_followup_content is None
    assert result.raw_output["mcp_structured_content"] == {"element_token": "unsafe"}


def test_structured_projection_removes_binary_but_keeps_capture_geometry():
    text = cua_structured_text({"screenshot": {"data": "x" * 2048, "width": 230, "height": 408},
                               "snapshot_id": "s00000004"})
    projected = json.loads(text.split("\n", 1)[1])
    assert projected["screenshot"] == {"width": 230, "height": 408}
    assert text.index("snapshot_id") < text.index('"screenshot"')


def test_model_projection_keeps_final_state_actions_and_errors_without_query_duplicates():
    import copy
    from box_agent.plugins.cua.structured_result import cua_model_payload
    events = [
        {'phase': 'dispatch', 'tool': 'get_window_state', 'arguments': {'window_id': 1}},
        {'phase': 'result', 'tool': 'get_window_state', 'result': {'structuredContent': {'elements': ['old-tree']}}},
        {'phase': 'result', 'tool': 'click', 'result': {'structuredContent': {'effect': 'unverifiable'}}},
        {'phase': 'result', 'tool': 'list_apps', 'result': {'isError': True, 'content': ['failed']}},
        {'phase': 'error', 'tool': 'list_windows', 'error': 'transport failure'},
    ]
    source = {'schema_version': 1, 'output': {'state': 'current-tree'},
              'adapter_context': {'elements': {'0': {'element_token': 'fresh'}}},
              'backend_events': events}
    original = copy.deepcopy(source)
    projected = cua_model_payload(source)
    assert source == original
    assert projected['output'] == source['output']
    assert projected['adapter_context'] == source['adapter_context']
    assert projected['backend_query_counts'] == {'get_window_state': 1, 'list_apps': 1}
    assert projected['backend_action_events'] == events[2:]
    assert 'backend_events' not in projected
    direct = {'elements': [{'element_token': 'direct'}], 'snapshot_id': 's1'}
    assert cua_model_payload(direct) == direct


def test_model_window_references_roundtrip_without_duplicate_window_rows():
    from box_agent.plugins.cua.structured_result import cua_model_payload
    source = {'schema_version': 1, 'output': {}, 'backend_events': [],
              'adapter_context': {'element_windows': {
                  0: {'window_id': 7, 'pid': 42, 'title': 'Document'},
                  1: {'window_id': 7, 'pid': 42, 'title': 'Document'},
                  2: {'window_id': 8, 'pid': 42, 'title': 'Sheet'},
              }}}
    context = cua_model_payload(source)['adapter_context']
    restored = {i: context['windows_by_id'][str(wid)] for i, wid in context['element_window_ids'].items()}
    assert restored == source['adapter_context']['element_windows']
    assert len(context['windows_by_id']) == 2
    source['adapter_context']['element_windows'][1]['title'] = 'Conflicting state'
    assert cua_model_payload(source)['adapter_context'] == source['adapter_context']


def test_model_action_context_omits_invalidated_ids_but_keeps_actual_delivery_report():
    from box_agent.plugins.cua.structured_result import cua_model_payload
    source = {'schema_version': 1, 'output': {'effect': 'unverifiable'},
              'requires_requery': True, 'adapter_context': {
                  'pid': 42, 'fresh': False, 'elements': {0: {'element_token': 'stale'}},
                  'element_windows': {0: {'window_id': 7}}},
              'backend_events': [{'phase': 'dispatch', 'tool': 'click',
                                  'arguments': {'element_token': 'executed-token'}}]}
    model = cua_model_payload(source)
    assert model['adapter_context'] == {'pid': 42, 'fresh': False}
    assert model['requires_requery'] is True
    assert model['backend_action_events'][0]['arguments']['element_token'] == 'executed-token'
    assert source['adapter_context']['elements'][0]['element_token'] == 'stale'


@pytest.mark.parametrize('detail', [
    {'is_error': True}, {'refusal': 'outside target'}, {'error': {'message': 'failed'}},
    {'effect': 'failed'}, {'effect': 'partial'}, {'effect': 'unverifiable'},
    {'status': 'refused'}, {'status': 'unknown_future_status'},
    {'code': 'window_id_not_found'}, {'success': False},
])
@pytest.mark.parametrize('location', ['result', 'structuredContent'])
def test_model_query_summary_preserves_error_and_uncertain_reports(detail, location):
    from box_agent.plugins.cua.structured_result import cua_model_payload
    result = {'structuredContent': {'elements': []}}
    result.update(detail) if location == 'result' else result['structuredContent'].update(detail)
    event = {'tool': 'get_window_state', 'phase': 'result', 'result': result}
    raw = {'schema_version': 1, 'output': None, 'backend_events': [event]}
    model = cua_model_payload(raw)
    assert model['backend_action_events'] == [event]
    assert model['backend_query_counts'] == {'get_window_state': 1}
    assert raw['backend_events'] == [event]


def test_model_query_summary_retains_missing_structured_evidence():
    from box_agent.plugins.cua.structured_result import cua_model_payload
    event = {'tool': 'list_windows', 'phase': 'result', 'result': {'content': []}}
    model = cua_model_payload({'schema_version': 1, 'output': {}, 'backend_events': [event]})
    assert model['backend_action_events'] == [event]

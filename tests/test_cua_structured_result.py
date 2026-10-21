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

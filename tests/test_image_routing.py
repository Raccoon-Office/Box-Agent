"""Image service selection is shared guidance, never an execution gate."""

import json
from collections import OrderedDict
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anyio
import pytest
from mcp import types
from mcp.server.lowlevel import Server

from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.project_context import compose_prompt_segments
from box_agent.prompt_capabilities import render_image_generation_policy
from box_agent.session_assembly import build_session_prompt
from box_agent.tools import mcp_apps, mcp_loader
from box_agent.tools.image_generation_tool import GenerateImageTool
from box_agent.tools.mcp_loader import MCPTool, _public_mcp_tool_name
from box_agent.tools.mcp_tool_catalog import MCPToolCatalog
from box_agent.tools.mcp_tool_search import MCPToolExposureManager, ToolSearchTool
from box_agent.tools.setup import build_image_generation_prompt


@pytest.fixture
def config():
    return Config(llm=LLMConfig(api_key="test"), agent=AgentConfig(enable_memory=False),
                  tools=ToolsConfig(enable_mcp=False, enable_skills=False))


@pytest.mark.parametrize("tools", [None, {"generate_image"}, {"tool_search"},
                                   {"mcp__seko-canvas__generate_image"}])
def test_image_routing_reaches_parent_and_child_without_inheriting_tool_inventory(tmp_path, tools, config):
    segments = []
    parent = build_session_prompt(
        config, "Task instructions", session_mode="general", workspace=tmp_path,
        sandbox_mode=False, available_tools=tools, child_prompt_segments=segments,
    )
    child = compose_prompt_segments(segments[0], segments=segments[1:])
    policy = render_image_generation_policy(tools)
    for prompt in (parent, child):
        assert prompt.count(policy) == 1
        assert "未明确指定服务时，默认使用 Box-Agent 内置生图工具" in prompt
        assert "沿用当前配置的生图服务" in prompt
        assert "无关新任务恢复默认" in prompt
        assert "已明确指定 Seko 的图片任务" in prompt
        assert "不视为指定 Seko" in prompt
        assert "不自动切换到 Seko" in prompt
        assert "拒绝后不得换服务绕过" in prompt
    assert "`generate_image`" not in policy
    assert "mcp__" not in policy


@pytest.mark.parametrize("tools", [set(), {"read_file"}, {"bash"}])
def test_image_routing_is_absent_without_image_or_discovery_capability(tools):
    assert render_image_generation_policy(tools) == ""


def test_native_image_guidance_respects_explicit_service_choice(config):
    description = GenerateImageTool().description
    assert "default image tool" in description
    assert "unless the user explicitly selects another image service" in description
    assert "follow-up edits" in description
    assert "runtime handles billing confirmation" in description
    prompt = build_image_generation_prompt(config, {}, tools={"generate_image"})
    assert "按统一生图选路规则应使用内置服务时" in prompt
    missing = build_image_generation_prompt(config, {}, tools={"tool_search"})
    assert "用户明确指定其他生图服务时" in missing
    assert "使用当前可用的对应工具" in missing


def seko_tool(server="seko-canvas", remote="generate_image", connector_id=None):
    descriptor = {
        "name": remote,
        "description": "Generate an image. Keep the original canvas instructions.",
        "inputSchema": {"type": "object", "properties": {"prompt": {"type": "string"}}},
        "_meta": {"ui": {"resourceUri": "ui://seko/generation.html", "visibility": ["model", "app"]}},
    }
    original = json.loads(json.dumps(descriptor))
    session = SimpleNamespace(call_tool=AsyncMock(return_value=types.CallToolResult(
        content=[types.TextContent(type="text", text="Generated")],
    )))
    tool = MCPTool(
        name=_public_mcp_tool_name(server, remote, connector_id),
        remote_name=remote, server_name=server, connector_id=connector_id,
        description=descriptor["description"], parameters=descriptor["inputSchema"],
        descriptor=descriptor, session=session,
    )
    return tool, original, session


@pytest.mark.parametrize("server,remote,connector_id,selected", [
    ("seko-canvas", "generate_image", "seko", True),
    ("renamed-server", "generate_image", "seko", True),
    ("seko", "generate_image", None, True),
    ("seko-canvas", "generate_image", None, True),
    ("seko-mcp", "generate_image", None, True),
    ("seko-mcp", "seko_generate_image", None, True),
    ("other", "generate_image", None, False),
    ("other-seko", "generate_image", None, False),
    ("seko", "generate_image", "other", False),
    ("seko-canvas", "generate_video", "seko", False),
    ("seko-canvas", "get_task", "seko", False),
])
def test_only_seko_image_descriptions_gain_selection_guidance(
    monkeypatch, server, remote, connector_id, selected,
):
    monkeypatch.setattr(mcp_apps, "_host_enabled", False)
    tool, original, _ = seko_tool(server, remote, connector_id)
    assert tool.description.startswith("SEKO image generation/editing:") is selected
    assert tool.description.endswith(original["description"])
    assert tool.mcp_search_description == tool.description
    assert tool.mcp_descriptor == original
    assert tool.parameters == original["inputSchema"]
    assert tool.mcp_visibility == frozenset({"model", "app"})
    assert tool.mcp_ui_resource_uri == "ui://seko/generation.html"
    assert tool.remote_name == remote


@pytest.mark.parametrize("stable", [False, True])
@pytest.mark.parametrize("host_enabled", [False, True])
async def test_seko_selection_guidance_survives_search_and_app_workflow(monkeypatch, stable, host_enabled):
    monkeypatch.setattr(mcp_apps, "_host_enabled", host_enabled)
    tool, original, _ = seko_tool(connector_id="seko")
    catalog, active = MCPToolCatalog(), OrderedDict()
    catalog.replace_server(tool.server_name, [tool])
    search = ToolSearchTool(catalog, active)
    manager = MCPToolExposureManager(catalog, active)
    if stable:
        manager.enable_stable_prefix(search)
    payload = json.loads((await search.execute(tool_names=[tool.name])).content)
    offered = payload["activated"][0]["description"]
    assert offered.startswith("SEKO image generation/editing:")
    assert "only when the user explicitly selects SEKO" in offered
    assert "follow-up edits" in offered
    assert "host's default image tool" in offered
    assert original["description"] in offered
    assert (mcp_apps.MCP_APP_MODEL_GUIDANCE in offered) is host_enabled
    purpose = payload["connector_catalogs"][0]["tools"][0]["purpose"]
    assert "only when the user explicitly selects SEKO" in purpose
    assert tool.mcp_descriptor == original


async def test_seko_guidance_does_not_add_an_execution_restriction(monkeypatch):
    monkeypatch.setattr(mcp_apps, "_host_enabled", False)
    tool, _, session = seko_tool()
    result = await tool.execute(prompt="A blue cat")
    assert result.success
    session.call_tool.assert_awaited_once_with("generate_image", arguments={"prompt": "A blue cat"})


async def test_sdk_connection_loads_seko_guidance_without_rewriting_app_metadata(monkeypatch):
    monkeypatch.setattr(mcp_apps, "_host_enabled", True)
    _, original, _ = seko_tool()
    server = Server("seko-canvas")

    @server.list_tools()
    async def list_tools():
        return [types.Tool(**original)]

    @asynccontextmanager
    async def transport():
        request_send, request_receive = anyio.create_memory_object_stream(10)
        response_send, response_receive = anyio.create_memory_object_stream(10)
        async with anyio.create_task_group() as group:
            async with request_send, request_receive, response_send, response_receive:
                group.start_soon(server.run, request_receive, response_send,
                                 server.create_initialization_options())
                try:
                    yield response_receive, request_send
                finally:
                    group.cancel_scope.cancel()

    async def open_transport(connection):
        return await connection.exit_stack.enter_async_context(transport())

    monkeypatch.setattr(mcp_loader.MCPServerConnection, "_connect_stdio", open_transport)
    connection = mcp_loader.MCPServerConnection(
        "seko-canvas", command="unused", connector_id="seko", connect_timeout=2,
    )
    assert await connection.connect()
    try:
        tool = connection.tools[0]
        assert tool.name == "mcp__seko-canvas__generate_image"
        assert tool.description.startswith("SEKO image generation/editing:")
        assert mcp_apps.MCP_APP_MODEL_GUIDANCE in tool.description
        assert tool.mcp_descriptor == original
    finally:
        await connection.disconnect()

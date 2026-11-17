"""First-party web schemas are available independently of MCP readiness."""

import asyncio
from collections import OrderedDict
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from box_agent.mcp_servers.web_extract import WebExtractTool
from box_agent.tools import mcp_loader, web_tools
from box_agent.tools.base import ToolInvocationContext, ToolResult, bind_tool_cancellation
from box_agent.tools.mcp_tool_catalog import MCPToolCatalog
from box_agent.tools.web_tools import FirstPartyWebTool, create_web_tools
from tests.test_mcp_tool_search import FakeMCPTool


@pytest.fixture
def web_backend(monkeypatch):
    catalog = MCPToolCatalog()
    wait = AsyncMock()
    monkeypatch.setattr(web_tools, "get_mcp_tool_catalog", lambda: catalog)
    monkeypatch.setattr(mcp_loader, "wait_for_mcp_server", wait, raising=False)
    return catalog, wait


def test_schemas_are_available_before_connecting(web_backend, monkeypatch):
    _, wait = web_backend
    search = FirstPartyWebTool("web_search")
    extract = FirstPartyWebTool("web_extract")
    assert search.parameters == mcp_loader.HOSTED_WEB_SEARCH_PARAMETERS
    assert extract.parameters == WebExtractTool(llm=None).parameters
    assert {search.to_schema()["name"], extract.to_schema()["name"]} == {
        "web_search", "web_extract",
    }
    assert search.reserved_mcp_entrypoint and extract.reserved_mcp_entrypoint
    assert getattr(search, "mcp_tool_id", None) is None
    assert search.server_name == mcp_loader.HOSTED_WEB_SEARCH_SERVER_NAME
    assert search.transient_followup_allowed is mcp_loader.MCPTool(
        "web_search", "", {}, SimpleNamespace(), server_name=search.server_name,
    ).transient_followup_allowed
    monkeypatch.setattr(web_tools, "allowed_mcp_result_adapters", lambda **kwargs: ("followup",))
    assert search.transient_followup_allowed
    search.parameters["required"].clear()
    assert search.parameters["required"] == ["query"]
    wait.assert_not_called()


@pytest.mark.asyncio
async def test_only_invocation_waits_then_forwards_arguments_and_context(web_backend):
    catalog, wait = web_backend
    backend = FakeMCPTool("web_search", mcp_loader.HOSTED_WEB_SEARCH_SERVER_NAME)
    expected = ToolResult(success=True, content="real backend result")
    backend.invoke = AsyncMock(return_value=expected)
    catalog.replace_server(backend.server_name, [backend])
    proxy = FirstPartyWebTool("web_search")
    context = ToolInvocationContext(parent_tool_call_id="outer")
    arguments = {"Query": "Iceland", "SearchType": "image", "count": 3}
    assert await proxy.invoke(arguments, context=context) is expected
    wait.assert_awaited_once_with(backend.server_name)
    backend.invoke.assert_awaited_once_with(arguments, context=context)


@pytest.mark.asyncio
async def test_cancelling_one_call_preserves_shared_connection_loading(web_backend, monkeypatch):
    catalog, _ = web_backend
    started = asyncio.Event()
    ready = asyncio.Event()

    async def load():
        started.set()
        await ready.wait()
        backend = FakeMCPTool("web_extract", "box-agent-web-extract")
        catalog.replace_server(backend.server_name, [backend])

    shared = asyncio.create_task(load())

    async def wait(server_name):
        await asyncio.shield(shared)

    monkeypatch.setattr(mcp_loader, "wait_for_mcp_server", wait)
    proxy = FirstPartyWebTool("web_extract")
    first = asyncio.create_task(proxy.invoke({"url": "https://example.org"}))
    await started.wait()
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not shared.done()
    ready.set()
    assert (await proxy.invoke({"url": "https://example.org"})).success
    await shared


@pytest.mark.asyncio
async def test_each_call_resolves_the_current_backend_after_reconnection(web_backend):
    catalog, _ = web_backend
    original = FakeMCPTool("web_extract", "box-agent-web-extract")
    replacement = FakeMCPTool("web_extract", original.server_name)
    proxy = FirstPartyWebTool("web_extract")
    original_schema = proxy.to_schema()
    catalog.replace_server(original.server_name, [original])
    assert (await proxy.invoke({"url": "https://example.org"})).success
    catalog.replace_server(replacement.server_name, [replacement])
    assert (await proxy.execute(url="https://example.org")).success
    assert (original.calls, replacement.calls) == (1, 1)
    assert proxy.to_schema() == original_schema


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["missing", "third-party", "connector", "wrong-remote", "conflict"])
async def test_unavailable_or_mismatched_backend_never_executes(web_backend, kind):
    catalog, _ = web_backend
    server = "other" if kind == "third-party" else "box-agent-web-extract"
    backend = FakeMCPTool(
        "web_extract", server,
        connector_id="other" if kind == "connector" else None,
        remote_name="other" if kind == "wrong-remote" else "web_extract",
    )
    if kind != "missing":
        catalog.replace_server(server, [backend])
    if kind == "conflict":
        catalog.replace_server("duplicate", [FakeMCPTool("web_extract", "duplicate")])
    result = await FirstPartyWebTool("web_extract").invoke({"url": "https://example.org"})
    assert not result.success and "WEB_TOOL_UNAVAILABLE" in result.error
    assert backend.calls == 0


@pytest.mark.asyncio
async def test_connection_failure_is_reported_and_next_call_can_retry(web_backend):
    catalog, wait = web_backend
    wait.side_effect = [RuntimeError("connection failed"), None]
    proxy = FirstPartyWebTool("web_extract")
    first = await proxy.invoke({"url": "https://example.org"})
    assert not first.success and "connection failed" in first.error
    backend = FakeMCPTool("web_extract", "box-agent-web-extract")
    catalog.replace_server(backend.server_name, [backend])
    assert (await proxy.invoke({"url": "https://example.org"})).success
    assert backend.calls == 1


@pytest.mark.asyncio
async def test_cancelled_context_stops_before_backend_execution(web_backend):
    catalog, _ = web_backend
    backend = FakeMCPTool("web_extract", "box-agent-web-extract")
    catalog.replace_server(backend.server_name, [backend])
    with bind_tool_cancellation(lambda: True):
        result = await FirstPartyWebTool("web_extract").invoke({"url": "https://example.org"})
    assert not result.success and "TOOL_EXECUTION_CANCELLED" in result.error
    assert result._invocation_rejected
    assert backend.calls == 0


def test_factory_registers_only_enabled_first_party_definitions(tmp_path, monkeypatch):
    for key in ("BOX_AGENT_USER_MCP_CONFIG_PATH", "BOX_AGENT_SYSTEM_MCP_CONFIG_PATH", "BOX_AGENT_CONNECTOR_MCP_CONFIG_PATH"):
        monkeypatch.delenv(key, raising=False)
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"mcpServers": {
        mcp_loader.HOSTED_WEB_SEARCH_SERVER_NAME: {"url": "https://example.org/mcp"},
        "box-agent-web-extract": {"command": "fake", "disabled": True},
        "third-party": {"command": "fake"},
    }}))
    assert [tool.name for tool in create_web_tools(str(config))] == ["web_search"]
    assert create_web_tools(str(tmp_path / "missing.json")) == []
    config.write_text("invalid json")
    assert create_web_tools(str(config)) == []


def test_factory_rejects_connector_owned_lookalikes(monkeypatch):
    definitions = {
        "box-agent-web-extract": SimpleNamespace(
            connector_id="third-party", config={"command": "fake"},
        ),
    }
    monkeypatch.setattr(mcp_loader, "_resolve_registered_sources", lambda path: definitions)
    assert create_web_tools("unused.json") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("deferred_mcp", [False, True], ids=["eager", "deferred"])
async def test_engine_preserves_web_backend_and_charges_one_invocation(web_backend, deferred_mcp):
    from box_agent.events import ToolCallResult
    from box_agent.hooks import BaseHook
    from box_agent.runtime import run_agent_loop
    from box_agent.schema import FunctionCall, LLMResponse, Message, ToolCall
    from box_agent.tools.delegated_budget import DelegatedBudget, bind_budgets
    from box_agent.tools.engine.engine import DefaultToolEngine
    from box_agent.tools.mcp_tool_search import MCPToolExposureManager, ToolSearchTool
    from tests.test_mcp_tool_search import MockLLM

    catalog, _ = web_backend
    backend = FakeMCPTool("web_extract", "box-agent-web-extract")
    catalog.replace_server(backend.server_name, [backend])
    activated = OrderedDict()
    manager = MCPToolExposureManager(catalog, activated, deferred_mcp=deferred_mcp)
    manager.enable_stable_prefix(ToolSearchTool(catalog, activated))
    tools = {"web_extract": FirstPartyWebTool("web_extract")}
    prepared = DefaultToolEngine(tools=tools, tool_exposure=manager).prepare_tools()
    assert [definition.name for definition in prepared.definitions] == ["web_extract"]
    assert prepared.validate_call("web_extract") is None
    assert catalog.get_by_model_name("web_extract").tool is backend

    starts = []

    class Hook(BaseHook):
        async def on_tool_start(self, *, tool_call_id, tool_name, arguments):
            starts.append((tool_call_id, tool_name, arguments))

    arguments = {"url": "https://example.org"}
    llm = MockLLM([
        LLMResponse(content="", finish_reason="tool_use", tool_calls=[ToolCall(
            id="extract", type="function", function=FunctionCall(
                name="web_extract", arguments=arguments,
            ),
        )]),
        LLMResponse(content="done", finish_reason="stop"),
    ])
    budget = DelegatedBudget(1)
    with bind_budgets((budget,)):
        results = [event async for event in run_agent_loop(
            llm=llm, tools=tools, tool_exposure_manager=manager,
            messages=[Message(role="user", content="Extract the page.")],
            hooks=[Hook()], max_steps=2, max_tool_calls=1,
        ) if isinstance(event, ToolCallResult)]
    assert llm.offered_names[0] == ["web_extract"]
    assert len(results) == 1 and results[0].success
    assert backend.calls == budget.used == 1
    assert starts == [("extract", "web_extract", arguments)]

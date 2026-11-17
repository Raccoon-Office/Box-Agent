"""Exercise stable web entry points through the ACP session API."""

import asyncio
from collections import Counter
import json
from types import SimpleNamespace

import pytest

from box_agent.acp import BoxACPAgent
from box_agent.config import AgentConfig, Config, LLMConfig, MCPConfig, ToolsConfig
from box_agent.schema import FunctionCall, StreamEvent, ToolCall
from box_agent.session_log import SessionLog
from box_agent.tools import mcp_loader
from box_agent.tools.mcp_tool_catalog import get_mcp_tool_catalog
from box_agent.tools.setup import (
    initialize_base_tools,
    merge_mcp_tools,
    register_mcp_tools,
    sync_mcp_tools,
)
from tests.acp_helpers import acp_params
from tests.test_acp import DoneLLM, DummyConn
from tests.test_mcp_tool_search import FakeMCPTool


_SERVERS = {
    "mcp-server-askecho-search-infinity": "web_search",
    "box-agent-web-extract": "web_extract",
}


class WebLLM(DoneLLM):
    def __init__(self):
        super().__init__()
        self.web_request = None
        self.emitted = asyncio.Event()

    async def generate_stream(self, messages, tools=None, **kwargs):
        if self.web_request is None:
            async for event in super().generate_stream(messages, tools, **kwargs):
                yield event
            return
        self.calls += 1
        name, arguments = self.web_request
        self.web_request = None
        call = ToolCall(
            id=f"web-{self.calls}", type="function",
            function=FunctionCall(name=name, arguments=arguments),
        )
        self.emitted.set()
        yield StreamEvent(type="tool_calls", tool_calls=[call])
        yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[call])


def _config(workspace):
    return Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(workspace_dir=str(workspace)),
        tools=ToolsConfig(
            enable_bash=False, enable_file_tools=False, enable_todo=False,
            enable_plan=False, enable_sub_agent=False, enable_skills=False,
            mcp=MCPConfig(deferred_loading_enabled=True),
        ),
    )


async def _session(host, path):
    path.mkdir()
    return await host.new_session(**acp_params(SimpleNamespace(
        cwd=str(path), field_meta={},
    )))


async def _prompt(host, session_id, text="hello"):
    return await host.prompt(**acp_params(SimpleNamespace(
        sessionId=session_id, prompt=[{"text": text}], field_meta={},
    )))


@pytest.fixture
async def cold_web_runtime(tmp_path, monkeypatch):
    await mcp_loader.cleanup_mcp_connections()
    config_path = tmp_path / "mcp.json"
    config_path.write_text(json.dumps({"mcpServers": {
        server: {"command": "fake", "alwaysLoad": True} for server in _SERVERS
    }}), encoding="utf-8")
    monkeypatch.setenv("BOX_AGENT_MCP_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("BOX_AGENT_USER_MCP_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("BOX_AGENT_SYSTEM_MCP_CONFIG_PATH", str(tmp_path / "absent.json"))
    monkeypatch.delenv("BOX_AGENT_CONNECTOR_MCP_CONFIG_PATH", raising=False)
    gates = {server: asyncio.Event() for server in _SERVERS}
    started = {server: asyncio.Event() for server in _SERVERS}
    connections = Counter()
    implementations = {}
    waiting = asyncio.Event()
    wait_for_server = mcp_loader.wait_for_mcp_server

    async def observe_wait(name):
        waiting.set()
        await wait_for_server(name)

    async def connect(connection):
        connections[connection.name] += 1
        started[connection.name].set()
        await gates[connection.name].wait()
        tool = FakeMCPTool(
            _SERVERS[connection.name], connection.name, always_load=True,
        )
        implementations[tool.name] = tool
        connection.tools = [tool]
        return True

    monkeypatch.setattr(mcp_loader.MCPServerConnection, "connect", connect)
    monkeypatch.setattr(mcp_loader, "wait_for_mcp_server", observe_wait)
    llm = WebLLM()
    config = _config(tmp_path)
    tools, _, mcp_task, _ = await initialize_base_tools(config, output=lambda _: None)
    host = BoxACPAgent(DummyConn(), config, llm, tools, "system", mcp_task=mcp_task)
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started.values())), 3)
        yield SimpleNamespace(
            host=host, llm=llm, task=mcp_task, gates=gates, tools=tools,
            connections=connections, implementations=implementations, waiting=waiting,
        )
    finally:
        for gate in gates.values():
            gate.set()
        await asyncio.gather(mcp_task, return_exceptions=True)
        await host.aclose()
        await mcp_loader.cleanup_mcp_connections()


@pytest.mark.asyncio
async def test_hello_offers_web_schemas_without_waiting_for_cold_connections(cold_web_runtime, tmp_path):
    runtime = cold_web_runtime
    first = await _session(runtime.host, tmp_path / "first")
    assert (await asyncio.wait_for(_prompt(runtime.host, first.sessionId), 3)).stopReason == "end_turn"
    log = runtime.host._sessions[first.sessionId].agent.session_log
    header = next(log.events_of_type("request/header"))["data"]["header"]
    assert set(_SERVERS.values()) <= {tool["name"] for tool in header["tools"]}
    definitions = {tool["name"]: tool["input_schema"] for tool in header["tools"]}
    assert "query" in definitions["web_search"]["required"]
    assert "url" in definitions["web_extract"]["required"]
    assert not runtime.task.done()
    assert not runtime.implementations

    for gate in runtime.gates.values():
        gate.set()
    await runtime.task
    second = await _session(runtime.host, tmp_path / "second")
    assert (await asyncio.wait_for(_prompt(runtime.host, second.sessionId), 3)).stopReason == "end_turn"
    assert runtime.connections == Counter({server: 1 for server in _SERVERS})


@pytest.mark.asyncio
@pytest.mark.parametrize("server_name, tool_name, arguments", [
    ("mcp-server-askecho-search-infinity", "web_search", {"query": "Iceland", "count": 1}),
    ("box-agent-web-extract", "web_extract", {"url": "https://example.org"}),
])
async def test_web_call_waits_only_for_its_own_connection_and_persists_backend_identity(
    cold_web_runtime, tmp_path, server_name, tool_name, arguments,
):
    runtime = cold_web_runtime
    workspace = tmp_path / "web-call"
    session = await _session(runtime.host, workspace)
    runtime.llm.web_request = (tool_name, arguments)
    prompt = asyncio.create_task(_prompt(runtime.host, session.sessionId, "use the web tool"))
    await asyncio.wait_for(runtime.waiting.wait(), 3)
    assert not prompt.done()
    assert not runtime.implementations
    runtime.gates[server_name].set()
    assert (await asyncio.wait_for(prompt, 3)).stopReason == "end_turn"
    log = runtime.host._sessions[session.sessionId].agent.session_log
    assert runtime.implementations[tool_name].calls == 1
    assert set(runtime.implementations) == {tool_name}
    assert not runtime.task.done()
    entry = get_mcp_tool_catalog().get_by_model_name(tool_name)
    assert entry is not None
    log.close()
    reopened = SessionLog.open(
        log.path.parent.parent, session_id=log.header["id"], cwd=workspace,
    )
    try:
        calls = [event["data"] for event in reopened.events_of_type("tool/call")]
        results = [event["data"] for event in reopened.events_of_type("tool/result")]
        assert len(calls) == len(results) == 1
        assert calls[0]["name"] == results[0]["message"]["name"] == tool_name
        assert calls[0]["callId"] == results[0]["message"]["tool_call_id"]
        assert calls[0]["arguments"] == arguments
        assert results[0]["result"]["success"] is True
        for metadata in (calls[0], results[0]["result"]):
            assert metadata["tool_id"] == entry.tool_id
            assert metadata["server_name"] == server_name
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_cancelling_web_wait_keeps_shared_connection_available(cold_web_runtime, tmp_path):
    runtime = cold_web_runtime
    first = await _session(runtime.host, tmp_path / "cancelled")
    runtime.llm.web_request = ("web_search", {"query": "Iceland"})
    prompt = asyncio.create_task(_prompt(runtime.host, first.sessionId, "search Iceland"))
    await asyncio.wait_for(runtime.waiting.wait(), 3)
    await runtime.host.cancel(**acp_params(SimpleNamespace(sessionId=first.sessionId)))
    assert (await asyncio.wait_for(prompt, 3)).stopReason == "cancelled"
    assert not runtime.task.done()
    assert not runtime.task.cancelled()

    runtime.gates["mcp-server-askecho-search-infinity"].set()
    runtime.llm.emitted.clear()
    runtime.llm.web_request = ("web_search", {"query": "Iceland"})
    second = await _session(runtime.host, tmp_path / "retry")
    assert (await asyncio.wait_for(_prompt(runtime.host, second.sessionId, "search Iceland"), 3)).stopReason == "end_turn"
    log = runtime.host._sessions[second.sessionId].agent.session_log
    results = [event["data"]["result"] for event in log.events_of_type("tool/result")]
    assert runtime.implementations["web_search"].calls == 1, results
    assert runtime.connections == Counter({server: 1 for server in _SERVERS})


def test_background_mcp_registration_preserves_stable_web_entries():
    from box_agent.tools.web_tools import FirstPartyWebTool

    stable = FirstPartyWebTool("web_search")
    remote = FakeMCPTool("web_search", "mcp-server-askecho-search-infinity")
    tools = {stable.name: stable}
    register_mcp_tools(tools, [remote])
    assert tools[stable.name] is stable
    sync_mcp_tools(tools, [remote], {})
    assert tools[stable.name] is stable
    base_tools = [stable]
    merge_mcp_tools(base_tools, [remote])
    assert base_tools == [stable]


@pytest.mark.asyncio
async def test_invalid_mcp_configuration_does_not_block_hello(tmp_path, monkeypatch):
    await mcp_loader.cleanup_mcp_connections()
    config_path = tmp_path / "invalid-mcp.json"
    config_path.write_text("{invalid json", encoding="utf-8")
    monkeypatch.setenv("BOX_AGENT_MCP_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("BOX_AGENT_USER_MCP_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("BOX_AGENT_SYSTEM_MCP_CONFIG_PATH", str(tmp_path / "absent.json"))
    monkeypatch.delenv("BOX_AGENT_CONNECTOR_MCP_CONFIG_PATH", raising=False)
    config = _config(tmp_path)
    tools, _, mcp_task, _ = await initialize_base_tools(config, output=lambda _: None)
    host = BoxACPAgent(DummyConn(), config, DoneLLM(), tools, "system", mcp_task=mcp_task)
    try:
        session = await _session(host, tmp_path / "hello")
        assert (await asyncio.wait_for(_prompt(host, session.sessionId), 3)).stopReason == "end_turn"
        assert not set(_SERVERS.values()) & {tool.name for tool in tools}
    finally:
        await asyncio.gather(mcp_task, return_exceptions=True)
        await host.aclose()
        await mcp_loader.cleanup_mcp_connections()

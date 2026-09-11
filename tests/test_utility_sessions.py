"""Real Agent construction and provider exposure for ACP utility sessions."""

from types import SimpleNamespace

import pytest

from box_agent.acp import BoxACPAgent
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.schema import LLMResponse, StreamEvent
from box_agent.tools.base import Tool, ToolResult


class RecordingLLM:
    model = "utility-test"
    max_output_tokens = 1024

    def __init__(self):
        self.calls = []

    async def generate_stream(self, messages, tools=None, **_kwargs):
        self.calls.append((list(messages), list(tools or [])))
        yield StreamEvent(type="text", delta="utility done")
        yield StreamEvent(type="finish", finish_reason="stop")

    async def generate(self, messages, tools=None, **_kwargs):
        return LLMResponse(content="utility done", finish_reason="stop")


class SentinelTool(Tool):
    name = "base_fixture"
    description = "Test-only base tool"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, **_kwargs):
        return ToolResult(success=True, content="fixture")


class Connection:
    async def sessionUpdate(self, _payload):
        pass


def configured_adapter(tmp_path, monkeypatch, llm):
    profile = tmp_path / "owned-profile"
    profile.mkdir(exist_ok=True)
    workspace = profile / "workspace"
    workspace.mkdir(exist_ok=True)
    monkeypatch.setenv("BOX_AGENT_HOME", str(profile))
    # 独立测试 profile 使用自己的资源目录，不继承开发机或 CI 宿主的路径覆盖。
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.delenv("BOX_AGENT_SKILL_TOOLS_ROOT", raising=False)
    config = Config(
        llm=LLMConfig(api_key="test-key", model=llm.model),
        agent=AgentConfig(workspace_dir=str(workspace), enable_memory=False),
        tools=ToolsConfig(
            enable_file_tools=False,
            enable_bash=False,
            enable_todo=False,
            enable_plan=False,
            enable_sub_agent=False,
            enable_skills=False,
            enable_mcp=False,
        ),
    )
    return BoxACPAgent(Connection(), config, llm, [SentinelTool()], "system"), workspace


@pytest.mark.asyncio
@pytest.mark.parametrize("utility", [None, False, True])
@pytest.mark.parametrize("inherited_paths", [False, True], ids=["isolated", "host-paths"])
async def test_utility_has_no_registered_or_provider_visible_tools_without_changing_normal_sessions(
    tmp_path, monkeypatch, utility, inherited_paths,
):
    if inherited_paths:
        # 宿主目录位于测试的独立 profile 之外，不能被新会话继承。
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "host-browsers"))
        monkeypatch.setenv("BOX_AGENT_SKILL_TOOLS_ROOT", str(tmp_path / "host-skill-tools"))
    llm = RecordingLLM()
    adapter, workspace = configured_adapter(tmp_path, monkeypatch, llm)
    meta = {} if utility is None else {"utility": utility}
    session = await adapter.newSession(SimpleNamespace(cwd=str(workspace), field_meta=meta))
    agent = adapter._sessions[session.sessionId].agent
    if utility:
        assert agent.tools == {}
        assert "## Discoverable tools" not in agent.system_prompt
        await adapter.prompt(
            SimpleNamespace(
                sessionId=session.sessionId,
                prompt=[{"text": "hello utility"}],
                field_meta={},
            )
        )
        assert llm.calls and all(not tools for _, tools in llm.calls)
    else:
        assert {"goal_read", "goal_write", "tool_search", "base_fixture"} <= agent.tools.keys()


@pytest.mark.asyncio
async def test_utility_preserves_box_session_log_context_after_recreation(tmp_path, monkeypatch):
    first_llm = RecordingLLM()
    first, workspace = configured_adapter(tmp_path, monkeypatch, first_llm)
    request = SimpleNamespace(
        cwd=str(workspace),
        field_meta={"utility": True, "session_id": "utility-restart"},
    )
    session = await first.newSession(request)
    log = first._sessions[session.sessionId].agent.session_log
    try:
        await first.prompt(
            SimpleNamespace(
                sessionId=session.sessionId,
                prompt=[{"text": "remember utility context"}],
                field_meta={},
            )
        )
    finally:
        log.close()
    llm = RecordingLLM()
    restarted, _ = configured_adapter(tmp_path, monkeypatch, llm)
    session = await restarted.newSession(request)
    state = restarted._sessions[session.sessionId]
    try:
        assert state.agent.tools == {}
        assert ("user", "remember utility context") in [
            (message.role, message.content) for message in state.agent.messages
        ]
        await restarted.prompt(
            SimpleNamespace(
                sessionId=session.sessionId,
                prompt=[{"text": "continue"}],
                field_meta={},
            )
        )
        assert llm.calls and all(not tools for _, tools in llm.calls)
    finally:
        state.agent.session_log.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("refresh", ["mcp/source/replace", "mcp/reconcile", "background"])
async def test_eager_mcp_refresh_keeps_utility_registry_and_provider_tools_empty(
    tmp_path, monkeypatch, refresh,
):
    from unittest.mock import AsyncMock
    from box_agent.tools import mcp_loader
    from tests.test_mcp_tool_search import FakeMCPTool

    llm = RecordingLLM()
    adapter, workspace = configured_adapter(tmp_path, monkeypatch, llm)
    adapter._config.tools.enable_mcp = True
    adapter._config.tools.mcp.deferred_loading_enabled = False
    remote = FakeMCPTool("connector_lookup", "law", connector_id="law")
    monkeypatch.setattr(mcp_loader, "get_all_mcp_tools", lambda: [remote])
    monkeypatch.setattr(mcp_loader, "get_mcp_tools_for_server", lambda name: [remote])
    result = {"success": True, "results": [{"name": "law", "action": "added", "success": True}]}
    monkeypatch.setattr(mcp_loader, "replace_mcp_source", AsyncMock(return_value=result))
    monkeypatch.setattr(mcp_loader, "reconcile_mcp_sources", AsyncMock(return_value=result))
    try:
        utility = await adapter.newSession(SimpleNamespace(cwd=str(workspace), field_meta={"utility": True}))
        normal = await adapter.newSession(SimpleNamespace(cwd=str(workspace), field_meta={}))
        state = adapter._sessions[utility.sessionId]
        assert state.agent.tools == {}
        state.turn_active = True
        adapter._sessions[normal.sessionId].turn_active = True
        if refresh == "background":
            adapter._sync_mcp_registries([remote])
            adapter._inject_mcp_runtime_update(name="law", state="connected", tool_count=1)
        else:
            await adapter.extMethod(refresh, {"source": "connector", "config": {"mcpServers": {}}})
        assert "connector_lookup" in adapter._sessions[normal.sessionId].agent.tools
        assert state.agent.tools == {}
        assert not state.mcp_fallback_tools
        assert state.inject_queue.empty()
        assert not adapter._sessions[normal.sessionId].inject_queue.empty()
        state.turn_active = False
        adapter._sessions[normal.sessionId].turn_active = False
        await adapter.prompt(SimpleNamespace(
            sessionId=utility.sessionId, prompt=[{"text": "write a title"}], field_meta={},
        ))
        assert llm.calls and all(not tools for _, tools in llm.calls)
    finally:
        await adapter.aclose()

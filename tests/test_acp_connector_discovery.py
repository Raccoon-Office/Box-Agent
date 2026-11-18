"""The real ACP/session/tool path consumes host discovery without prompt catalogs."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

import box_agent.acp as acp_module
from box_agent.acp import BoxACPAgent
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.schema import FunctionCall, StreamEvent, ToolCall
from box_agent.tools.mcp_tool_catalog import get_mcp_tool_catalog
from tests.acp_helpers import acp_params
from tests.test_acp import DummyConn, DoneLLM
from tests.test_connector_discovery import entry, snapshot
from tests.test_mcp_tool_search import FakeMCPTool


class CaptureLLM(DoneLLM):
    def __init__(self):
        super().__init__()
        self.requests = []

    async def generate_stream(self, messages, tools=None, **kwargs):
        self.requests.append({"messages": [message.model_dump(exclude_none=True) for message in messages],
                              "tools": [deepcopy(tool) if isinstance(tool, dict) else tool.to_openai_schema() for tool in tools or []]})
        async for event in super().generate_stream(messages, tools, **kwargs):
            yield event


def config(tmp_path, interface=2):
    return Config(llm=LLMConfig(api_key="test-key"), agent=AgentConfig(
        max_steps=8, workspace_dir=str(tmp_path), enable_memory=False, tool_interface_version=interface,
    ), tools=ToolsConfig(enable_sub_agent=False, allow_full_access=True))


async def create(host, tmp_path, name="session", *, enabled=True, catalog=None):
    meta = {"session_id": name, "selected_connector_ids": []}
    if enabled:
        meta["host_capabilities"] = {"connector_discovery": 1}
        meta["connector_discovery"] = snapshot() if catalog is None else catalog
    return await host.new_session(**acp_params(SimpleNamespace(cwd=str(tmp_path), field_meta=meta)))


async def prompt(host, session_id, text="你好", *, metadata=None):
    return await host.prompt(**acp_params(SimpleNamespace(
        sessionId=session_id, prompt=[{"type": "text", "text": text}], field_meta=metadata or {},
    )))


@pytest.mark.asyncio
@pytest.mark.parametrize("interface,enabled,expected", [(2, True, True), (2, False, False), (1, True, False)])
async def test_initialize_and_session_negotiation_keep_legacy_fallback(tmp_path, interface, enabled, expected):
    llm = CaptureLLM()
    host = BoxACPAgent(DummyConn(), config(tmp_path, interface), llm, [], "system")
    try:
        initialized = await host.initialize(protocol_version=1)
        assert initialized.field_meta["connector_discovery"] == {"versions": [1]}
        session = await create(host, tmp_path, enabled=enabled)
        assert ("connector_discovery_versions" in session.field_meta["capabilities"]) is expected
        await prompt(host, session.sessionId, metadata={"connector_discovery": snapshot()})
        request = json.dumps(llm.requests[-1], ensure_ascii=False)
        assert ("Search or call only connected connectors" in request) is not expected
        assert ("<connector-status>" in request) is not expected
        assert "腾讯会议" not in request
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_session_mode_restores_and_existing_v2_sessions_do_not_upgrade(tmp_path):
    host = BoxACPAgent(DummyConn(), config(tmp_path), CaptureLLM(), [], "system")
    try:
        original = await create(host, tmp_path, "new-mode")
        assert original.field_meta["capabilities"]["connector_discovery_versions"] == [1]
        restored = await create(host, tmp_path, "new-mode")
        assert restored.field_meta["capabilities"]["connector_discovery_versions"] == [1]
        await create(host, tmp_path, "old-mode", enabled=False)
        old = await create(host, tmp_path, "old-mode", enabled=True)
        assert "connector_discovery_versions" not in old.field_meta["capabilities"]
        downgraded = await create(host, tmp_path, "new-mode", enabled=False)
        assert "connector_discovery_versions" not in downgraded.field_meta["capabilities"]
        restored = await create(host, tmp_path, "new-mode", enabled=True)
        assert restored.field_meta["capabilities"]["connector_discovery_versions"] == [1]
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_final_model_request_does_not_grow_with_unsearched_catalog(tmp_path):
    llm = CaptureLLM()
    host = BoxACPAgent(DummyConn(), config(tmp_path), llm, [], "system")
    sizes = []
    try:
        for count in (10, 41, 200):
            catalog = snapshot([entry(f"private-{index}", name=f"Unsearched {index}",
                                      description="Private unused capability") for index in range(count)])
            session = await create(host, tmp_path, f"budget-{count}", catalog=catalog)
            await prompt(host, session.sessionId, metadata={"connector_discovery": catalog})
            serialized = json.dumps(llm.requests[-1], ensure_ascii=False, sort_keys=True)
            assert "Unsearched" not in serialized and "Private unused" not in serialized
            assert "catalog_revision" not in serialized and "connector_discovery" not in serialized
            sizes.append(len(serialized))
        assert len(set(sizes)) == 1, sizes
    finally:
        await host.aclose()


class DiscoveryFlowLLM(CaptureLLM):
    def __init__(self):
        super().__init__()
        self.step = 0

    async def generate_stream(self, messages, tools=None, **kwargs):
        self.requests.append({"messages": [message.model_dump(exclude_none=True) for message in messages],
                              "tools": [deepcopy(tool) if isinstance(tool, dict) else tool.to_openai_schema() for tool in tools or []]})
        self.step += 1
        if self.step in (1, 2, 4, 5):
            if self.step == 1:
                name, arguments = "tool_search", {"query": "会议纪要"}
            elif self.step in (2, 4):
                name, arguments = "tool_search", {"connector": "tmeet"}
            else:
                result = next(message for message in reversed(messages) if message.role == "tool" and message.name == "tool_search")
                activated = json.loads(result.content)["activated"]
                assert len(activated) == 1
                name, arguments = "call_tool", {"tool_ref": activated[0]["tool_ref"], "arguments_json": "{}"}
            yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[ToolCall(
                id=f"discovery-{self.step}", type="function", function=FunctionCall(name=name, arguments=arguments),
            )])
        else:
            yield StreamEvent(type="text", delta="请连接腾讯会议后继续。" if self.step == 3 else "已完成。")
            yield StreamEvent(type="finish", finish_reason="stop")


@pytest.mark.asyncio
async def test_real_acp_flow_requests_setup_then_uses_fresh_selection_and_runtime(tmp_path, monkeypatch):
    catalog = get_mcp_tool_catalog()
    catalog.clear()
    tool = FakeMCPTool("mcp__tmeet__minutes", "tmeet-server", "读取会议纪要", connector_id="tmeet")
    catalog.replace_server("tmeet-server", [tool])
    live = set()
    monkeypatch.setattr(acp_module, "get_mcp_connector_server_names", lambda: {"tmeet": frozenset({"tmeet-server"})})
    monkeypatch.setattr(acp_module, "get_mcp_status", lambda: [{
        "owner": "connector", "connectorId": "tmeet", "connectorName": "腾讯会议",
        "name": "tmeet-server", "state": "connected" if live else "disconnected",
    }])
    llm, conn = DiscoveryFlowLLM(), DummyConn()
    host = BoxACPAgent(conn, config(tmp_path), llm, [], "system")
    unavailable = snapshot([entry(kind="mcp", skills=[])])
    try:
        session = await create(host, tmp_path, catalog=unavailable)
        await prompt(host, session.sessionId, "整理腾讯会议纪要", metadata={
            "connector_discovery": unavailable, "selected_connector_ids": [],
        })
        assert llm.step == 3 and tool.calls == 0
        updates = [notification.model_dump(by_alias=True, exclude_none=True) for notification in conn.updates]
        encoded = json.dumps(updates, ensure_ascii=False)
        assert "connector_action_required" in encoded and '"action": "connect"' in encoded
        live.add("tmeet")
        ready = snapshot([entry(kind="mcp", skills=[], configured=True, enabled=True,
                                status="connected", execution_ready=True)], revision=2)
        await prompt(host, session.sessionId, "腾讯会议已就绪，请继续未完成工作", metadata={
            "connector_discovery": ready, "selected_connector_ids": ["tmeet"],
        })
        assert tool.calls == 1 and llm.step == 6
        final_request = llm.requests[-1]
        user_text = [message["content"] for message in final_request["messages"] if message["role"] == "user"]
        assert sum("整理腾讯会议纪要" in str(content) for content in user_text) == 1
        assert "<connector-status>" not in json.dumps(final_request, ensure_ascii=False)
        assert host._sessions[session.sessionId].agent.connector_discovery.permitted("tmeet")
    finally:
        await host.aclose()
        catalog.clear()


@pytest.mark.asyncio
async def test_acp_ready_cli_method_is_loadable_without_semantic_match_and_revoked_on_next_turn(tmp_path):
    from box_agent.tools.skill_loader import SKILL_SLOT_SENTINEL, SkillLoader
    from box_agent.tools.skill_tool import GetSkillTool

    folder = tmp_path / "skills" / "connector-tmeet" / "method"
    folder.mkdir(parents=True)
    folder.joinpath("SKILL.md").write_text(
        "---\nname: tmeet-skill\ndescription: unique rare method\n---\nVerified CLI method body.\n",
        encoding="utf-8",
    )
    loader = SkillLoader(sources=[(folder.parent.parent, "connector")])
    loader.discover_skills()
    ready = snapshot([entry(configured=True, enabled=True, status="connected", execution_ready=True)])
    host = BoxACPAgent(DummyConn(), config(tmp_path), CaptureLLM(), [GetSkillTool(loader)],
                       f"system\n{SKILL_SLOT_SENTINEL}", skill_loader=loader)
    try:
        session = await create(host, tmp_path, catalog=ready)
        state = host._sessions[session.sessionId]
        assert not state.connector_skill_grants
        assert not (await state.agent.tools["get_skill"].execute(skill_name="tmeet-skill")).success
        await prompt(host, session.sessionId, "继续", metadata={
            "connector_discovery": ready, "selected_connector_ids": ["tmeet"],
        })
        state.connector_skill_grants.clear()  # A discovered method need not match the selector's query.
        result = await state.agent.tools["get_skill"].execute(skill_name="tmeet-skill")
        assert result.success and "Verified CLI method body" in result.content
        await prompt(host, session.sessionId, "继续", metadata={
            "connector_discovery": ready, "selected_connector_ids": [],
        })
        assert not (await state.agent.tools["get_skill"].execute(skill_name="tmeet-skill")).success
    finally:
        await host.aclose()

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
        assert ("For videos, short videos or storyboard stories, suggest SEKO Canvas" in request) is expected
        assert ("unless the user chose otherwise" in request) is expected
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
    def __init__(self, connector_id):
        super().__init__()
        self.connector_id = connector_id
        self.step = 0

    async def generate_stream(self, messages, tools=None, **kwargs):
        self.requests.append({"messages": [message.model_dump(exclude_none=True) for message in messages],
                              "tools": [deepcopy(tool) if isinstance(tool, dict) else tool.to_openai_schema() for tool in tools or []]})
        self.step += 1
        if self.step <= 4:
            if self.step == 1:
                name, arguments = "tool_search", {"query": self.connector_id}
            elif self.step in (2, 3):
                name, arguments = "tool_search", {"connector": self.connector_id}
            else:
                result = next(message for message in reversed(messages) if message.role == "tool" and message.name == "tool_search")
                activated = json.loads(result.content)["activated"]
                assert len(activated) == 1
                name, arguments = "call_tool", {"tool_ref": activated[0]["tool_ref"], "arguments_json": "{}"}
            calls = [ToolCall(
                id=f"discovery-{self.step}", type="function", function=FunctionCall(name=name, arguments=arguments),
            )]
            if self.step == 2:
                calls.append(ToolCall(
                    id="premature-work", type="function", function=FunctionCall(
                        name="write_file", arguments={"path": "premature.md", "content": "Should wait."},
                    ),
                ))
            yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=calls)
        else:
            yield StreamEvent(type="text", delta="已完成。")
            yield StreamEvent(type="finish", finish_reason="stop")


@pytest.mark.asyncio
@pytest.mark.parametrize("connector_id,action,task_text", [
    ("tmeet", "connect", "整理腾讯会议纪要"),
    ("seko", "select", "制作一个介绍小浣熊的视频 seko"),
    ("seko", "connect", "制作一个介绍小浣熊的视频"),
])
async def test_real_acp_waits_at_receipt_and_uses_fresh_selection_on_resume(
    tmp_path, monkeypatch, connector_id, action, task_text,
):
    catalog = get_mcp_tool_catalog()
    catalog.clear()
    server = f"{connector_id}-server"
    tool = FakeMCPTool(f"mcp__{connector_id}__deliver", server, "执行用户指定的任务", connector_id=connector_id)
    catalog.replace_server(server, [tool])
    live = {connector_id} if action == "select" else set()
    monkeypatch.setattr(acp_module, "get_mcp_connector_server_names", lambda: {connector_id: frozenset({server})})
    monkeypatch.setattr(acp_module, "get_mcp_status", lambda: [{
        "owner": "connector", "connectorId": connector_id,
        "name": server, "state": "connected" if live else "disconnected",
    }])
    llm, conn = DiscoveryFlowLLM(connector_id), DummyConn()
    host = BoxACPAgent(conn, config(tmp_path), llm, [], "system")
    initial_entry = entry(connector_id, kind="mcp", skills=[])
    if action == "select":
        initial_entry.update(configured=True, enabled=True, status="connected", execution_ready=True)
    initial = snapshot([initial_entry])
    try:
        session = await create(host, tmp_path, catalog=initial)
        first = await prompt(host, session.sessionId, task_text, metadata={
            "connector_discovery": initial, "selected_connector_ids": [],
        })
        assert first.field_meta["runStatus"] == "waiting_for_user"
        assert llm.step == 2 and tool.calls == 0
        assert not (tmp_path / "premature.md").exists()
        updates = [notification.model_dump(by_alias=True, exclude_none=True) for notification in conn.updates]
        receipts = [
            output
            for event in updates
            if isinstance(output := event.get("update", {}).get("rawOutput"), dict)
            and output.get("type") == "connector_action_required"
        ]
        assert len(receipts) == 1
        assert receipts[0]["connector_id"] == connector_id
        assert receipts[0]["action"] == action and receipts[0]["wait_for_user"] is True
        live.add(connector_id)
        ready = snapshot([entry(connector_id, kind="mcp", skills=[], configured=True, enabled=True,
                                status="connected", execution_ready=True)], revision=2)
        final = await prompt(host, session.sessionId, "连接器已就绪，请继续未完成工作", metadata={
            "connector_discovery": ready, "selected_connector_ids": [connector_id],
        })
        assert final.field_meta["runStatus"] == "completed"
        assert tool.calls == 1 and llm.step == 5
        final_request = llm.requests[-1]
        user_text = [message["content"] for message in final_request["messages"] if message["role"] == "user"]
        assert sum(task_text in str(content) for content in user_text) == 1
        assert "<connector-status>" not in json.dumps(final_request, ensure_ascii=False)
        assert host._sessions[session.sessionId].agent.connector_discovery.permitted(connector_id)
    finally:
        await host.aclose()
        catalog.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", ["knowledge", "ready_tool", "required_source"])
async def test_acp_decline_continues_original_task_with_ready_capabilities(tmp_path, monkeypatch, fallback):
    task, answer = {
        "knowledge": ("什么是一审和二审？", "一审是初次审理，二审通常是上诉后的审理。"),
        "ready_tool": ("查公开资料解释一审和二审。", "根据查到的公开资料，一审和二审是不同审级。"),
        "required_source": ("只用指定法律库核验条文，并说明一审和二审的区别。",
                            "可以解释两个审级的区别。指定法律库中的条文仍待核验。"),
    }[fallback]

    class DeclineLLM(DoneLLM):
        def __init__(self):
            super().__init__()
            self.requests = []
            searches = ["law", "law"]
            if fallback == "ready_tool":
                searches.append("public-law")
            self.searches = iter(searches)
            self.called = False

        async def generate_stream(self, messages, tools=None, **kwargs):
            self.requests.append([message.model_dump(exclude_none=True) for message in messages])
            connector_id = next(self.searches, None)
            if connector_id:
                name, arguments = "tool_search", {"connector": connector_id}
            elif fallback == "ready_tool" and not self.called:
                search = next(message for message in reversed(messages) if message.name == "tool_search")
                activated, = json.loads(search.content)["activated"]
                assert activated["connector_id"] == "public-law"
                name, arguments = "call_tool", {"tool_ref": activated["tool_ref"], "arguments_json": "{}"}
                self.called = True
            else:
                yield StreamEvent(type="text", delta=answer)
                yield StreamEvent(type="finish", finish_reason="stop")
                return
            yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[ToolCall(
                id=f"lookup-{len(self.requests)}", type="function",
                function=FunctionCall(name=name, arguments=arguments),
            )])

    catalog = get_mcp_tool_catalog()
    catalog.clear()
    skipped = FakeMCPTool("mcp__law__query", "law", "检索指定法律库", connector_id="law")
    public = FakeMCPTool("mcp__public_law__query", "public-law", "检索公开法律资料", connector_id="public-law")
    for connector_id, tool in (("law", skipped), ("public-law", public)):
        catalog.replace_server(connector_id, [tool])
    monkeypatch.setattr(acp_module, "get_mcp_connector_server_names", lambda: {
        connector_id: frozenset({connector_id}) for connector_id in ("law", "public-law")
    })
    monkeypatch.setattr(acp_module, "get_mcp_status", lambda: [
        {"owner": "connector", "connectorId": connector_id, "name": connector_id, "state": "connected"}
        for connector_id in ("law", "public-law")
    ])
    entries = [entry(connector_id, kind="mcp", skills=[], configured=True, enabled=True,
                     status="connected", execution_ready=True) for connector_id in ("law", "public-law")]
    initial = snapshot([*entries, entry("other-law")])
    llm, conn = DeclineLLM(), DummyConn()
    host = BoxACPAgent(conn, config(tmp_path), llm, [], "system")
    try:
        session = await create(host, tmp_path, catalog=initial)
        first = await prompt(host, session.sessionId, task, metadata={"connector_discovery": initial})
        assert first.field_meta["runStatus"] == "waiting_for_user"
        final = await prompt(host, session.sessionId, "暂不用法律库，请继续原任务。", metadata={
            "connector_discovery": initial,
            "selected_connector_ids": ["law", "public-law"] if fallback == "ready_tool" else [],
            "connector_continuation": {"request_id": "original-task-choice", "skip_connector_id": "law"},
        })
        assert final.field_meta["runStatus"] == "completed"
        assert skipped.calls == 0
        assert public.calls == (1 if fallback == "ready_tool" else 0)
        assert sum(task in str(message["content"]) for message in llm.requests[-1]
                   if message["role"] == "user") == 1
        updates = [notification.model_dump(by_alias=True, exclude_none=True)["update"] for notification in conn.updates]
        receipts = [update["rawOutput"] for update in updates if isinstance(update.get("rawOutput"), dict)
                    and update["rawOutput"].get("type") == "connector_action_required"]
        assert [receipt["connector_id"] for receipt in receipts] == ["law"]
        assert any(isinstance(content := update.get("content"), dict) and content.get("text") == answer
                   for update in updates)
        discovery = host._sessions[session.sessionId].agent.connector_discovery
        assert not discovery.permitted("law")
        await prompt(host, session.sessionId, "这次使用法律库。", metadata={
            "connector_discovery": initial, "selected_connector_ids": ["law"],
        })
        assert discovery.permitted("law")
    finally:
        await host.aclose()
        catalog.clear()


@pytest.mark.asyncio
async def test_acp_declining_one_connector_still_waits_for_the_next_connector(tmp_path):
    class SequentialConnectorLLM(DoneLLM):
        def __init__(self):
            super().__init__()
            self.searches = iter(["jinshuju", "jinshuju", "klingai-cn", "jinshuju"])

        async def generate_stream(self, messages, tools=None, **kwargs):
            connector_id = next(self.searches, None)
            if connector_id is None:
                async for event in super().generate_stream(messages, tools, **kwargs):
                    yield event
                return
            self.calls += 1
            yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[ToolCall(
                id=f"connector-{self.calls}", type="function",
                function=FunctionCall(name="tool_search", arguments={"connector": connector_id}),
            )])

    llm, conn = SequentialConnectorLLM(), DummyConn()
    host = BoxACPAgent(conn, config(tmp_path), llm, [], "system")
    initial = snapshot([entry("jinshuju", name="金数据", kind="mcp", skills=[]),
                        entry("klingai-cn", name="可灵 AI", kind="mcp", skills=[])])

    def receipts():
        outputs = [notification.model_dump(by_alias=True, exclude_none=True)["update"].get("rawOutput")
                   for notification in conn.updates]
        return [output["connector_id"] for output in outputs if isinstance(output, dict)
                and output.get("type") == "connector_action_required"]

    try:
        session = await create(host, tmp_path, catalog=initial)
        first = await prompt(host, session.sessionId, "依次查询金数据和可灵连接器。", metadata={
            "connector_discovery": initial,
        })
        assert first.field_meta["runStatus"] == "waiting_for_user"
        assert receipts() == ["jinshuju"]
        resumed = await prompt(host, session.sessionId, "暂不连接金数据，继续原任务。", metadata={
            "connector_discovery": initial,
            "connector_continuation": {"request_id": "skip-jinshuju", "skip_connector_id": "jinshuju"},
        })
        assert resumed.field_meta["runStatus"] == "waiting_for_user"
        assert receipts() == ["jinshuju", "klingai-cn"]
        assert llm.calls == 3
        discovery = host._sessions[session.sessionId].agent.connector_discovery
        assert not discovery.permitted("jinshuju") and not discovery.permitted("klingai-cn")

        ordinary = await prompt(host, session.sessionId, "这次连接金数据。", metadata={
            "connector_discovery": initial,
        })
        assert ordinary.field_meta["runStatus"] == "waiting_for_user"
        assert receipts() == ["jinshuju", "klingai-cn", "jinshuju"]
    finally:
        await host.aclose()


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

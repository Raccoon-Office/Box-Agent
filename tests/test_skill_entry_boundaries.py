"""Public host regressions for source binding, restoration and paging admission."""

from hashlib import sha256
from types import SimpleNamespace

import pytest

from box_agent.agent import Agent
from box_agent.events import ErrorEvent
from box_agent.schema import FunctionCall, StreamEvent, ToolCall
from box_agent.session_log import SessionLog
from box_agent.skill_dependencies import SkillDependencyError
from box_agent.tools.base import Tool, ToolResult
from box_agent.tools.skill_loader import SkillLoader
from box_agent.tools.skill_tool import GetSkillTool


class CapturingProvider:
    model = "offline-test"
    max_output_tokens = 1024

    def __init__(self, request_skill=None, skill_read_limit=10):
        self.requests = []
        self.request_skill = request_skill
        self.skill_read_limit = skill_read_limit

    async def generate_stream(self, messages, tools=None, **kwargs):
        self.requests.append([message.model_copy(deep=True) for message in messages])
        if self.request_skill and len(self.requests) == 1:
            yield StreamEvent(type="finish", finish_reason="tool", tool_calls=[ToolCall(
                id="read-selected", type="function", function=FunctionCall(
                    name="get_skill", arguments={"skill_name": self.request_skill, "limit": self.skill_read_limit}))])
            return
        yield StreamEvent(type="text", delta="done")
        yield StreamEvent(type="finish", finish_reason="stop")


def loader_at(path):
    folder = path / "demo"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: demo\ndescription: example\n---\nMETHOD_BODY\n")
    loader = SkillLoader(sources=[(path, "user")], skill_settings_path=path / "settings.json")
    loader.discover_skills()
    return loader


class OtherTool(Tool):
    def __init__(self, loader):
        self.skill_loader = loader

    @property
    def name(self):
        return "other_tool"

    @property
    def description(self):
        return "Unrelated integration with its own source"

    @property
    def parameters(self):
        return {"type": "object", "properties": {}}

    async def execute(self):
        return ToolResult(success=True, content="ok")


@pytest.mark.asyncio
async def test_public_agent_binds_only_real_skill_tools_before_actual_request(tmp_path):
    loader_a, loader_b = loader_at(tmp_path / "a"), loader_at(tmp_path / "b")
    provider = CapturingProvider()
    agent = Agent(llm_client=provider, system_prompt="BASE",
                  tools=[OtherTool(loader_a), GetSkillTool(loader_b)], workspace_dir=str(tmp_path),
                  deferred_mcp_loading_enabled=False, max_steps=1)
    agent.add_user_message("task")
    _ = [event async for event in agent.run_events()]
    assert len(provider.requests) == 1
    assert agent.skill_runtime.loader is loader_b


def seed_log(root, cwd):
    log = SessionLog.create(root, session_id="restore-skill", cwd=cwd)
    log.append("skill/change", {"skills": [{"name": "demo", "sha256": sha256(b"historical").hexdigest(), "loadOrder": 1}]})
    log.flush()
    path = log.path
    log.close()
    return path


@pytest.mark.asyncio
async def test_agent_without_current_source_blocks_before_provider_and_log_write(tmp_path):
    path = seed_log(tmp_path / "sessions", tmp_path)
    before = path.read_bytes()
    log = SessionLog.open(tmp_path / "sessions", session_id="restore-skill", cwd=tmp_path)
    provider = CapturingProvider()
    try:
        agent = Agent(llm_client=provider, system_prompt="BASE", tools=[], session_log=log,
                      workspace_dir=str(tmp_path), deferred_mcp_loading_enabled=False, max_steps=1)
        agent.add_user_message("continue")
        with pytest.raises(SkillDependencyError, match="No Skill source"):
            _ = [event async for event in agent.run_events()]
        assert provider.requests == []
        assert path.read_bytes() == before
    finally:
        log.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("skills_enabled", [False, True])
async def test_acp_without_loader_continues_without_rewriting_historical_skill_facts(tmp_path, monkeypatch, skills_enabled):
    import box_agent.acp as acp_module
    from box_agent.acp import BoxACPAgent
    from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig

    path = seed_log(tmp_path / "sessions", tmp_path)
    before = path.read_bytes()
    monkeypatch.setattr(acp_module, "state_path", lambda name: tmp_path / name)

    class Conn:
        async def sessionUpdate(self, payload):
            pass

    provider = CapturingProvider()
    config = Config(llm=LLMConfig(api_key="offline"), agent=AgentConfig(max_steps=1, workspace_dir=str(tmp_path)),
                    tools=ToolsConfig(enable_skills=skills_enabled, enable_todo=False, enable_plan=False,
                                      enable_sub_agent=False, enable_mcp=False))
    adapter = BoxACPAgent(Conn(), config, provider, [], "BASE", skill_loader=None)
    try:
        for _ in range(2):
            response = await adapter.newSession(SimpleNamespace(cwd=str(tmp_path), field_meta={
                "session_id": "restore-skill", "session_mode": "general"}))
            state = adapter._sessions[response.sessionId]
            assert state.agent.skill_runtime.active_names == ()
            await adapter.prompt(SimpleNamespace(
                sessionId=response.sessionId, prompt=[{"text": "continue"}], field_meta={},
            ))
            assert provider.requests
            assert "METHOD_BODY" not in str(provider.requests)
            records = [event for event in state.agent.session_log.events if event["type"] == "skill/change"]
            assert len(records) == 1
            assert state.agent.session_log.replay().skills[0]["name"] == "demo"
            assert path.read_bytes().startswith(before)
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_acp_restore_borrows_real_tool_loader_when_separate_loader_is_omitted(tmp_path, monkeypatch):
    import box_agent.acp as acp_module
    from box_agent.acp import BoxACPAgent
    from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig

    seed_log(tmp_path / "sessions", tmp_path)
    monkeypatch.setattr(acp_module, "state_path", lambda name: tmp_path / name)

    class Conn:
        async def sessionUpdate(self, payload):
            pass

    provider = CapturingProvider()
    loader = loader_at(tmp_path / "skills")
    config = Config(llm=LLMConfig(api_key="offline"), agent=AgentConfig(max_steps=1, workspace_dir=str(tmp_path)),
                    tools=ToolsConfig(enable_todo=False, enable_plan=False, enable_sub_agent=False, enable_mcp=False))
    adapter = BoxACPAgent(Conn(), config, provider, [GetSkillTool(loader)], "BASE", skill_loader=None)
    session = await adapter.newSession(SimpleNamespace(cwd=str(tmp_path), field_meta={
        "session_id": "restore-skill", "session_mode": "general"}))
    try:
        await adapter.prompt(SimpleNamespace(sessionId=session.sessionId, prompt=[{"text": "continue"}], field_meta={}))
        assert adapter._sessions[session.sessionId].agent.skill_runtime.loader is loader
        assert len(provider.requests) == 1
        assert "METHOD_BODY" in str(provider.requests[0])
    finally:
        adapter._sessions[session.sessionId].agent.session_log.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("size,with_reader", [(32000, False), (100, False), (32000, True)])
async def test_explicit_material_needs_body_or_an_offered_paging_reader(tmp_path, size, with_reader):
    provider = CapturingProvider(request_skill="first" if with_reader else None)
    tools = [GetSkillTool(loader_at(tmp_path / "skills"))] if with_reader else []
    agent = Agent(llm_client=provider, system_prompt="BASE", tools=tools, workspace_dir=str(tmp_path),
                  deferred_mcp_loading_enabled=False, token_limit=5000, max_steps=3)
    for name in ("first", "second"):
        agent.activate_skill_instructions(name, name.upper() + "\n" + ("x" * 79 + "\n") * (size // 80))
    agent.add_user_message("follow both selected methods")
    events = [event async for event in agent.run_events()]
    if size == 32000 and not with_reader:
        assert provider.requests == []
        assert any(isinstance(event, ErrorEvent) and "reader" in event.message.lower() for event in events)
    else:
        assert len(provider.requests) == (2 if with_reader else 1)
        text = str(provider.requests[0])
        assert ("get_skill" in text and "paged" in text) if with_reader else ("FIRST" in text and "SECOND" in text)
        if with_reader:
            result = next(message for message in provider.requests[1] if message.role == "tool")
            assert result.tool_call_id == "read-selected"
            assert "FIRST" in result.content and '"has_more": true' in result.content
    assert ("get_skill" in agent.tools) == with_reader


@pytest.mark.asyncio
@pytest.mark.parametrize("with_loader", [False, True])
@pytest.mark.parametrize("operation", ["clear", "deactivate"])
async def test_explicit_removal_cancels_restoration_before_first_request(tmp_path, with_loader, operation):
    seed_log(tmp_path / "sessions", tmp_path)
    log = SessionLog.open(tmp_path / "sessions", session_id="restore-skill", cwd=tmp_path)
    provider = CapturingProvider()
    tools = [GetSkillTool(loader_at(tmp_path / "skills"))] if with_loader else []
    try:
        agent = Agent(llm_client=provider, system_prompt="BASE", tools=tools, session_log=log,
                      workspace_dir=str(tmp_path), deferred_mcp_loading_enabled=False, max_steps=1)
        if operation == "clear":
            agent.clear_active_skill_instructions()
        else:
            assert agent.deactivate_skill_instructions("demo") is True
            assert agent.deactivate_skill_instructions("demo") is False
        assert log.replay().skills == []
        for _ in range(2):
            agent.add_user_message("continue without the removed method")
            _ = [event async for event in agent.run_events()]
        assert len(provider.requests) == 2
        assert "METHOD_BODY" not in str(provider.requests)
        assert "No Skill source" not in str(provider.requests)
        assert log.replay().skills == []
    finally:
        log.close()


@pytest.mark.asyncio
async def test_removing_one_unresolved_skill_keeps_other_pending_records(tmp_path):
    log = SessionLog.create(tmp_path / "sessions", session_id="two-methods", cwd=tmp_path)
    rows = [{"name": name, "sha256": sha256(name.encode()).hexdigest(), "loadOrder": order}
            for order, name in enumerate(("first", "second"), 1)]
    log.append("skill/change", {"skills": rows})
    log.flush()
    provider = CapturingProvider()
    try:
        agent = Agent(llm_client=provider, system_prompt="BASE", tools=[], session_log=log,
                      workspace_dir=str(tmp_path), deferred_mcp_loading_enabled=False, max_steps=1)
        assert agent.deactivate_skill_instructions("first") is True
        assert log.replay().skills == [rows[1]]
        agent.add_user_message("continue")
        with pytest.raises(SkillDependencyError, match="No Skill source"):
            _ = [event async for event in agent.run_events()]
        assert provider.requests == []
        assert agent.deactivate_skill_instructions("second") is True
        _ = [event async for event in agent.run_events()]
        assert len(provider.requests) == 1
        assert log.replay().skills == []
    finally:
        log.close()


@pytest.mark.asyncio
async def test_registering_current_host_methods_preserves_other_pending_log_records(tmp_path):
    log = SessionLog.create(tmp_path / "sessions", session_id="replace-methods", cwd=tmp_path)
    rows = [{"name": name, "sha256": sha256(name.encode()).hexdigest(), "loadOrder": order}
            for order, name in enumerate(("first", "second"), 1)]
    log.append("skill/change", {"skills": rows})
    log.flush()
    provider = CapturingProvider()
    try:
        agent = Agent(llm_client=provider, system_prompt="BASE", tools=[], session_log=log,
                      workspace_dir=str(tmp_path), deferred_mcp_loading_enabled=False, max_steps=1)
        agent.activate_skill_instructions("first", "REPLACEMENT_FIRST")
        agent.activate_skill_instructions("new-method", "NEW_METHOD")
        records = log.replay().skills
        assert [row["name"] for row in records] == ["second", "first", "new-method"]
        assert records[0] == rows[1]
        assert [row["loadOrder"] for row in records] == [2, 3, 4]
        before = log.path.read_bytes()
        agent.activate_skill_instructions("new-method", "NEW_METHOD")
        assert log.path.read_bytes() == before
        agent.add_user_message("continue with the registered current methods")
        with pytest.raises(SkillDependencyError, match="No Skill source"):
            _ = [event async for event in agent.run_events()]
        assert provider.requests == []
        assert agent.deactivate_skill_instructions("second") is True
        _ = [event async for event in agent.run_events()]
        assert len(provider.requests) == 1
        assert "REPLACEMENT_FIRST" in str(provider.requests)
        assert "NEW_METHOD" in str(provider.requests)
        assert [row["name"] for row in log.replay().skills] == ["first", "new-method"]
    finally:
        log.close()


@pytest.mark.asyncio
async def test_clearing_restored_references_still_strips_verified_legacy_system_suffix(tmp_path):
    from box_agent.tools.skill_preload import build_active_skills_prompt

    provider = CapturingProvider()
    agent = Agent(llm_client=provider, system_prompt="BASE", tools=[], workspace_dir=str(tmp_path),
                  deferred_mcp_loading_enabled=False, max_steps=1)
    body = "LEGACY_METHOD_MUST_NOT_REAPPEAR"
    agent.restore_active_skill_instructions([("demo", body, sha256(body.encode()).hexdigest(), 1)])
    old_system = build_active_skills_prompt("BASE", {"demo": body})
    agent.set_system_prompt(old_system)
    agent.clear_active_skill_instructions()
    agent.add_user_message("continue without the removed method")
    _ = [event async for event in agent.run_events()]
    assert len(provider.requests) == 1
    assert provider.requests[0][0].content == "BASE"
    assert body not in str(provider.requests)
    assert agent.messages[0].content == old_system


@pytest.mark.asyncio
async def test_failed_restoration_stays_required_across_runs_until_reader_is_available(tmp_path):
    from box_agent.skill_runtime import SkillRuntime

    loader = loader_at(tmp_path / "skills")
    path = tmp_path / "skills" / "demo" / "SKILL.md"
    path.write_text("---\nname: demo\ndescription: example\n---\nMETHOD_BODY\n" + ("x" * 79 + "\n") * 500)
    provider = CapturingProvider(request_skill="demo", skill_read_limit=20)
    agent = Agent(llm_client=provider, system_prompt="BASE", tools=[], skill_runtime=SkillRuntime(loader),
                  workspace_dir=str(tmp_path), deferred_mcp_loading_enabled=False, token_limit=5000, max_steps=3)
    agent.restore_active_skill_instructions([("demo", "old text", sha256(b"old text").hexdigest(), 1)])
    for _ in range(2):
        agent.add_user_message("continue using the restored method")
        events = [event async for event in agent.run_events()]
        assert any(isinstance(event, ErrorEvent) and "reader" in event.message.lower() for event in events)
        assert provider.requests == []
        assert agent.skill_runtime.turn_deliveries == {}
    agent.tools["get_skill"] = GetSkillTool(loader)
    agent.add_user_message("continue with the now available reader")
    _ = [event async for event in agent.run_events()]
    assert len(provider.requests) == 2
    assert "paged" in str(provider.requests[0])
    result = next(message for message in provider.requests[1] if message.role == "tool")
    assert result.tool_call_id == "read-selected"
    assert "METHOD_BODY" in result.content


class RecoverableSkillStore:
    """A plugin store that rejects one Skill write, then accepts retries."""

    def __init__(self, log, failure):
        self.log, self.failure, self.armed, self.pending_flush = log, failure, True, False

    def __getattr__(self, name):
        return getattr(self.log, name)

    def append(self, kind, payload, **kwargs):
        if kind == "skill/change" and self.armed:
            self.armed = False
            if self.failure == "append":
                raise OSError("retryable Skill append failure")
            self.pending_flush = True
        return self.log.append(kind, payload, **kwargs)

    def flush(self):
        if self.pending_flush:
            self.pending_flush = False
            raise OSError("retryable Skill flush failure")
        return self.log.flush()


@pytest.mark.parametrize("operation", ["activate", "deactivate", "clear"])
@pytest.mark.parametrize("failure", ["append", "flush"])
def test_public_skill_change_retries_persistence_even_after_in_memory_change(tmp_path, operation, failure):
    seed_log(tmp_path / "sessions", tmp_path)
    log = SessionLog.open(tmp_path / "sessions", session_id="restore-skill", cwd=tmp_path)
    store = RecoverableSkillStore(log, failure)
    try:
        agent = Agent(llm_client=CapturingProvider(), system_prompt="BASE", tools=[], session_log=store,
                      workspace_dir=str(tmp_path), deferred_mcp_loading_enabled=False, max_steps=1)
        actions = {
            "activate": lambda: agent.activate_skill_instructions("demo", "CURRENT_METHOD"),
            "deactivate": lambda: agent.deactivate_skill_instructions("demo"),
            "clear": agent.clear_active_skill_instructions,
        }
        with pytest.raises(OSError, match="retryable Skill"):
            actions[operation]()
        actions[operation]()
        log.flush()
        records = log.replay().skills
        if operation == "activate":
            assert records[0]["sha256"] == sha256(b"CURRENT_METHOD").hexdigest()
        else:
            assert records == []
        before = log.path.read_bytes()
        if operation != "clear":
            actions[operation]()
            assert log.path.read_bytes() == before
    finally:
        log.close()


@pytest.mark.asyncio
async def test_next_run_retries_pending_skill_write_before_requesting_model(tmp_path):
    seed_log(tmp_path / "sessions", tmp_path)
    log = SessionLog.open(tmp_path / "sessions", session_id="restore-skill", cwd=tmp_path)
    store = RecoverableSkillStore(log, "append")
    provider = CapturingProvider()
    try:
        agent = Agent(llm_client=provider, system_prompt="BASE", tools=[], session_log=store,
                      workspace_dir=str(tmp_path), deferred_mcp_loading_enabled=False, max_steps=1)
        with pytest.raises(OSError, match="retryable Skill"):
            agent.clear_active_skill_instructions()
        agent.add_user_message("continue without any restored method")
        store.armed = True
        with pytest.raises(OSError, match="retryable Skill"):
            _ = [event async for event in agent.run_events()]
        assert provider.requests == []
        _ = [event async for event in agent.run_events()]
        assert len(provider.requests) == 1
        assert "METHOD_BODY" not in str(provider.requests)
        assert log.replay().skills == []
    finally:
        log.close()

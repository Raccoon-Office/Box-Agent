"""Managed adapter assembly behavior, using deterministic local capabilities."""
from types import SimpleNamespace
import pytest
import box_agent.acp as acp
import box_agent.session_assembly as assembly
from box_agent.config import Config, AgentConfig, LLMConfig, ToolsConfig
from tests.test_acp import DummyConn, DoneLLM

@pytest.mark.asyncio
async def test_legacy_positional_session_profile_starts_without_log_restore(tmp_path):
    from box_agent.agent_session import AgentSession
    from box_agent.session_context import HostBindings, SessionOptions

    options = SessionOptions(tmp_path, None, True, "python")
    assert options.profile == "python"
    assert options.resume_session_log is False
    session = await AgentSession.open(
        config=Config(llm=LLMConfig(api_key="test"), agent=AgentConfig(enable_memory=False),
                      tools=ToolsConfig(enable_mcp=False, enable_skills=False)),
        options=options,
        host=HostBindings(llm_client=DoneLLM(), tools=[], system_prompt="system"),
    )
    try:
        assert session.plugin_session is not None
        assert session.plugin_session.resources.context.options.profile == "python"
        assert session.agent.session_log is None
    finally:
        await session.aclose()


@pytest.mark.asyncio
async def test_acp_uses_shared_preparation_and_managed_session(tmp_path, monkeypatch):
    config = Config(llm=LLMConfig(api_key="test"), agent=AgentConfig(workspace_dir=str(tmp_path), enable_memory=False), tools=ToolsConfig(enable_mcp=False, enable_skills=False, enable_file_tools=False, enable_bash=False, enable_sub_agent=False))
    calls = []
    original = assembly.prepare_prompt
    async def prompt(resources):
        calls.append(resources.context)
        await original(resources)
        resources.system_prompt += "\nASSEMBLY SENTINEL"
    monkeypatch.setattr(assembly, "prepare_prompt", prompt)
    adapter = acp.BoxACPAgent(DummyConn(), config, DoneLLM(), [], "system")
    result = await adapter.newSession(SimpleNamespace(cwd=None, field_meta={"utility": True}))
    state = adapter._sessions[result.sessionId]
    assert calls and calls[0].config is config
    assert "ASSEMBLY SENTINEL" in state.agent.system_prompt
    assert state.plugin_session is not None
    await adapter.aclose()
    assert state._closed


@pytest.mark.asyncio
@pytest.mark.parametrize("utility,artifact_mode", [(False, "output"), (False, "project"), (True, "output")])
async def test_acp_preparation_preserves_direct_prompt_and_tool_contract(tmp_path, monkeypatch, utility, artifact_mode):
    from box_agent.tools.setup import add_workspace_tools, build_image_generation_prompt
    from box_agent.project_context import append_prompt_segment
    config = Config(
        llm=LLMConfig(api_key="test"),
        agent=AgentConfig(workspace_dir=str(tmp_path), enable_memory=False, enable_memory_extraction=False),
        tools=ToolsConfig(enable_mcp=False, enable_skills=False, enable_file_tools=True, enable_bash=False, enable_sub_agent=False),
    )
    class Memory:
        recalls = 0
        def read_core(self):
            return "name: deterministic test user with enough memory"
        def recall(self):
            self.recalls += 1
            return "MEMORY BLOCK"
    memory = Memory()
    recorded = []
    def workspace_tools(tools, cfg, workspace, **kwargs):
        direct = list(tools)
        add_workspace_tools(direct, cfg, workspace, **kwargs)
        scratch = add_workspace_tools(tools, cfg, workspace, **kwargs)
        recorded.append(([tool.to_schema() for tool in direct], [tool.to_schema() for tool in tools]))
        return scratch
    monkeypatch.setattr(acp, "add_workspace_tools", workspace_tools)
    adapter = acp.BoxACPAgent(DummyConn(), config, DoneLLM(), [], "BASE\n{SANDBOX_INFO}\n{FILE_DELIVERY_INFO}", memory_manager=memory)
    result = await adapter.newSession(SimpleNamespace(cwd=None, field_meta={"utility": utility, "artifact_mode": artifact_mode}))
    state = adapter._sessions[result.sessionId]
    resources = state.plugin_session.resources
    options = resources.context.options
    expected = adapter._build_session_prompt(
        options.session_mode, workspace=tmp_path, policy=options.effective_policy,
        env_context=state.env_context, skill_runtime_context=state.skill_runtime_context,
        enable_general_directory_policy=(not utility and options.session_mode in {None, "general"}),
    )
    if not utility:
        expected = append_prompt_segment(expected, "MEMORY BLOCK")
        expected = f"{expected.rstrip()}\n\n{build_image_generation_prompt(config)}"
        assert recorded and recorded[0][0] == recorded[0][1]
        assert [tool.to_schema() for tool in resources.tools] == recorded[0][0]
    else:
        assert resources.tools == [] and recorded == []
    assert resources.system_prompt == expected
    assert memory.recalls == (0 if utility else 1)
    assert state.memory_extractor is None
    await adapter.aclose()


@pytest.mark.asyncio
async def test_acp_sessions_share_runtime_and_rebind_closes_only_old_session(tmp_path, monkeypatch):
    monkeypatch.setattr(acp, "state_path", lambda _name: tmp_path / "sessions")
    config = Config(llm=LLMConfig(api_key="test"), agent=AgentConfig(workspace_dir=str(tmp_path), enable_memory=False), tools=ToolsConfig(enable_mcp=False, enable_skills=False))
    adapter = acp.BoxACPAgent(DummyConn(), config, DoneLLM(), [], "base")
    request = SimpleNamespace(cwd=None, field_meta={"utility": True, "session_id": "product-managed"})
    first = await adapter.newSession(request)
    old = adapter._sessions[first.sessionId]
    second = await adapter.newSession(request)
    current = adapter._sessions[second.sessionId]
    assert old._closed
    assert not current._closed
    assert old.plugin_session.runtime is current.plugin_session.runtime
    await adapter.aclose()
    assert current._closed


@pytest.mark.asyncio
async def test_managed_session_keeps_connector_skill_grants_for_parent_and_child(tmp_path, monkeypatch):
    from box_agent.tools.skill_loader import SKILL_SLOT_SENTINEL, SkillLoader
    from box_agent.tools.skill_catalog_tool import ListSkillsTool
    from box_agent.tools.skill_tool import GetSkillTool
    from tests.test_connector_skill_source import _write_connector_skill

    connector_root = tmp_path / "connector-skills"
    _write_connector_skill(connector_root)
    loader = SkillLoader(sources=[(connector_root, "connector")])
    loader.discover_skills()
    monkeypatch.setattr(acp, "_connected_connector_ids", lambda selected: frozenset(selected))
    config = Config(
        llm=LLMConfig(api_key="test"),
        agent=AgentConfig(workspace_dir=str(tmp_path), enable_memory=False),
        tools=ToolsConfig(enable_mcp=False, enable_skills=True, enable_sub_agent=True,
                          enable_file_tools=False, enable_bash=False),
    )
    adapter = acp.BoxACPAgent(
        DummyConn(), config, DoneLLM(), [GetSkillTool(loader), ListSkillsTool(loader)],
        f"BASE\n{SKILL_SLOT_SENTINEL}", skill_loader=loader,
    )
    try:
        response = await adapter.newSession(SimpleNamespace(cwd=str(tmp_path), field_meta={}))
        state = adapter._sessions[response.sessionId]
        skill_tool = state.agent.tools["get_skill"]
        catalog_tool = state.agent.tools["list_skills"]
        child_tool = state.agent.tools["sub_agent"]
        assert (await catalog_tool.execute(query="pkulaw")).raw_output["skills"] == []
        denied = await skill_tool.execute(skill_name="pkulaw")
        assert not denied.success and "not enabled" in denied.error
        denied_child = await child_tool.execute(task="Search the law", skills=["pkulaw"], required_tools=[])
        assert not denied_child.success
        catalog_update = state.skill_selector.update("pkulaw")
        assert "pkulaw" not in (catalog_update or "")
        assert "pkulaw" not in state.skill_selector.matched_skill_names

        await adapter.prompt(SimpleNamespace(
            sessionId=response.sessionId, prompt=[{"text": "pkulaw legal search"}],
            field_meta={"selected_connector_ids": ["pkulaw"]},
        ))
        assert state.connector_skill_grants == {"pkulaw"}
        visible = (await catalog_tool.execute(query="pkulaw")).raw_output["skills"]
        assert [row["name"] for row in visible] == ["pkulaw"]
        assert visible[0]["available"] is True
        assert (await skill_tool.execute(skill_name="pkulaw")).success
        allowed_child = await child_tool.execute(task="Search the law", skills=["pkulaw"], required_tools=[])
        assert allowed_child.success
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_cli_shared_prompt_controls_model_and_session_closes(tmp_path, monkeypatch):
    import sys

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("BOX_AGENT_HOME", raising=False)
    # Python 3.10 has exc_info(), but not exception().
    monkeypatch.delattr(sys, "exception", raising=False)
    import box_agent.cli as cli
    from tests.test_cli_runtime import _CaptureStreamLLM
    from box_agent.events import DoneEvent, StopReason
    config_path = tmp_path / "config.yaml"
    config_path.write_text("api_key: test\n")
    config = Config(llm=LLMConfig(api_key="test"), agent=AgentConfig(workspace_dir=str(tmp_path), enable_memory=False), tools=ToolsConfig(enable_mcp=False, enable_skills=False))
    monkeypatch.setattr(cli.Config, "get_default_config_path", staticmethod(lambda: config_path))
    monkeypatch.setattr(cli.Config, "from_yaml", staticmethod(lambda _path: config))
    monkeypatch.setattr(cli, "LLMClient", _CaptureStreamLLM)
    async def base(*args, **kwargs):
        return [], None, None, None
    monkeypatch.setattr(cli, "initialize_base_tools", base)
    monkeypatch.setattr(cli, "add_workspace_tools", lambda *args, **kwargs: None)
    original = assembly.prepare_prompt
    sessions = []
    async def prompt(resources):
        await original(resources)
        resources.system_prompt += "\nCLI SHARED MARKER"
    original_finish = assembly.finish_session
    async def finish(session, resources):
        sessions.append(session)
        await original_finish(session, resources)
    async def run(agent, **kwargs):
        assert "CLI SHARED MARKER" in agent.system_prompt
        assert kwargs["options"].kernel_services is not None
        yield DoneEvent(stop_reason=StopReason.END_TURN, final_content="done")
    monkeypatch.setattr(assembly, "prepare_prompt", prompt)
    monkeypatch.setattr(assembly, "finish_session", finish)
    monkeypatch.setattr(cli.Agent, "run_events", run)
    assert await cli.run_agent(tmp_path, task="hello", verify_api=False, sandbox_mode=False, goal_autopilot_enabled=False) == 0
    assert len(sessions) == 1 and sessions[0]._closed

@pytest.mark.asyncio
async def test_acp_unavailable_skill_state_does_not_block_managed_session(tmp_path, monkeypatch):
    config = Config(llm=LLMConfig(api_key="test"), agent=AgentConfig(workspace_dir=str(tmp_path), enable_memory=False), tools=ToolsConfig(enable_mcp=False, enable_skills=False))
    original_factory = acp.Agent
    closed = []
    original_tools = assembly.prepare_tools
    async def tools(resources):
        await original_tools(resources)
        resources.cleanup.callback(closed.append, resources.context.session_key)
    def agent_factory(**kwargs):
        agent = original_factory(**kwargs)
        agent.restored_skills = [{"name": "missing", "sha256": "hash", "loadOrder": 0}]
        return agent
    monkeypatch.setattr(acp, "Agent", agent_factory)
    monkeypatch.setattr(assembly, "prepare_tools", tools)
    adapter = acp.BoxACPAgent(DummyConn(), config, DoneLLM(), [], "system")
    result = await adapter.newSession(SimpleNamespace(cwd=None, field_meta={"utility": True}))
    assert adapter._sessions[result.sessionId].plugin_session is not None
    assert closed == []
    await adapter.aclose()
    assert len(closed) == 1
    assert adapter._plugin_runtime._sessions == {}


@pytest.mark.asyncio
async def test_acp_finish_failure_releases_unpublished_session(tmp_path, monkeypatch):
    config = Config(llm=LLMConfig(api_key="test"), agent=AgentConfig(workspace_dir=str(tmp_path), enable_memory=False), tools=ToolsConfig(enable_mcp=False, enable_skills=False))
    closed = []
    original_tools = assembly.prepare_tools

    async def tools(resources):
        await original_tools(resources)
        resources.cleanup.callback(closed.append, resources.context.session_key)

    async def finish(session, resources):
        raise ValueError("session finalization failed")

    monkeypatch.setattr(assembly, "prepare_tools", tools)
    monkeypatch.setattr(assembly, "finish_session", finish)
    adapter = acp.BoxACPAgent(DummyConn(), config, DoneLLM(), [], "system")
    with pytest.raises(ValueError, match="session finalization failed"):
        await adapter.newSession(SimpleNamespace(cwd=None, field_meta={"utility": True}))
    assert len(closed) == 1
    assert adapter._sessions == {}
    assert adapter._plugin_runtime._sessions == {}
    await adapter.aclose()


@pytest.mark.asyncio
async def test_owned_client_cleanup_preserves_primary_error_without_python311_api(monkeypatch):
    import sys

    monkeypatch.delattr(sys, "exception", raising=False)
    closed = []

    class PrimaryError(ValueError):
        add_note = None

    class Client:
        async def aclose(self):
            closed.append(self)
            raise RuntimeError("cleanup failed")

    client = Client()
    with pytest.raises(PrimaryError, match="task failed") as caught:
        try:
            raise PrimaryError("task failed")
        finally:
            await assembly.close_owned_clients([client, client])
    assert closed == [client]
    assert isinstance(caught.value.__cause__, RuntimeError)

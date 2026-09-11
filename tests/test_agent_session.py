"""Session-owned configuration and state across the public execution path."""

import json
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.events import ArtifactEvent, DoneEvent, StepStart, StopReason
from box_agent.schema import FunctionCall, StreamEvent, ToolCall


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))


class ToolThenAnswerLLM:
    def __init__(self):
        self.calls = 0

    async def generate_stream(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                type="finish",
                finish_reason="tool_use",
                tool_calls=[ToolCall(
                    id="goal-1", type="function",
                    function=FunctionCall(name="goal_read", arguments={}),
                )],
            )
        else:
            yield StreamEvent(type="text", delta="The task is complete.")
            yield StreamEvent(type="finish", finish_reason="stop")


def session_config(tmp_path, *, max_steps=2):
    return Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(
            workspace_dir=str(tmp_path), max_steps=max_steps,
            goal_autopilot_enabled=False, enable_memory_extraction=False,
        ),
        tools=ToolsConfig(
            enable_mcp=False, enable_skills=False, enable_bash=False,
            enable_file_tools=False, enable_todo=False, enable_plan=False,
            enable_sub_agent=False,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "max_steps, expected_reason, expected_calls",
    [(1, StopReason.MAX_STEPS, 1), (2, StopReason.END_TURN, 2)],
)
async def test_session_config_controls_actual_loop_step_limit(
    tmp_path, max_steps, expected_reason, expected_calls,
):
    from box_agent.agent_session import AgentSession

    config = session_config(tmp_path, max_steps=max_steps)
    llm = ToolThenAnswerLLM()
    session = AgentSession.create(
        config=config, llm_client=llm, system_prompt="system", tools=[],
    )
    session.agent.add_user_message("Read the current goal and report it.")
    events = [event async for event in session.run_events(
        options=session.build_run_options(logger=None),
    )]

    assert session.config is config
    assert session.run_handle.config is config
    assert llm.calls == expected_calls
    assert [e.stop_reason for e in events if isinstance(e, DoneEvent)] == [expected_reason]


@pytest.mark.asyncio
async def test_session_artifacts_stay_relative_to_session_cwd(tmp_path):
    from box_agent.agent_session import AgentSession
    from box_agent.tools.file_tools import WriteTool

    class WriteArtifactLLM(ToolThenAnswerLLM):
        async def generate_stream(self, **kwargs):
            async for event in super().generate_stream(**kwargs):
                if event.tool_calls:
                    event.tool_calls = [ToolCall(
                        id="write-1", type="function",
                        function=FunctionCall(
                            name="write_file",
                            arguments={"path": "report/summary.md", "content": "Verified result."},
                        ),
                    )]
                yield event

    workspace = tmp_path / "session-cwd"
    legacy_root = tmp_path / "legacy-output"
    session = AgentSession.create(
        config=session_config(tmp_path / "config-workspace"),
        llm_client=WriteArtifactLLM(), system_prompt="system",
        tools=[WriteTool(workspace_dir=str(workspace))],
        workspace_dir=workspace,
    )
    session.agent.add_user_message("Write the verified result to report/summary.md.")
    events = [event async for event in session.run_events(
        options=session.build_run_options(logger=None, artifact_root_dir=legacy_root),
    )]

    assert (workspace / "report" / "summary.md").read_text() == "Verified result."
    assert [event.rel_path for event in events if isinstance(event, ArtifactEvent)] == [
        "report/summary.md",
    ]
    assert session.agent.workspace_dir == workspace
    assert not legacy_root.exists()
    assert not (workspace / "output").exists()


@pytest.mark.asyncio
async def test_session_cancellation_reaches_loop_without_acp(tmp_path):
    from box_agent.agent_session import AgentSession

    llm = ToolThenAnswerLLM()
    session = AgentSession.create(
        config=session_config(tmp_path), llm_client=llm,
        system_prompt="system", tools=[],
    )
    session.run_handle.cancelled = True
    session.agent.add_user_message("This request was cancelled.")
    events = [event async for event in session.run_events(
        options=session.build_run_options(logger=None),
    )]

    assert llm.calls == 0
    assert [e.stop_reason for e in events if isinstance(e, DoneEvent)] == [StopReason.CANCELLED]


def test_sessions_isolate_injection_and_skill_state(tmp_path):
    from box_agent.agent_session import AgentSession

    first = AgentSession.create(
        config=session_config(tmp_path / "one"), llm_client=ToolThenAnswerLLM(),
        system_prompt="system", tools=[],
    )
    second = AgentSession.create(
        config=session_config(tmp_path / "two"), llm_client=ToolThenAnswerLLM(),
        system_prompt="system", tools=[],
    )
    first.run_handle.inject_queue.put_nowait("only first")
    first.preloaded_skill_names.append("skill-one")
    first.explicitly_allowed_skill_names.add("skill-one")
    first.run_handle.cancelled = True

    assert second.inject_queue.empty()
    assert second.preloaded_skill_names == []
    assert second.explicitly_allowed_skill_names == set()
    assert second.cancelled is False
    assert first.build_run_options().inject_queue.get_nowait() == "only first"


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", [False, True])
async def test_acp_creates_configured_session_and_runs_through_it(tmp_path, monkeypatch, selected):
    import box_agent.acp as acp_module
    from box_agent.agent_session import AgentSession
    from tests.test_acp import DoneLLM, DummyConn
    from box_agent.tools.skill_loader import SkillLoader

    config = session_config(tmp_path)
    config.tools.enable_skills = True
    config.tool_limits.web_search.deep_research_total_calls = 7
    method = tmp_path / "skills" / "research-synthesis" / "SKILL.md"
    method.parent.mkdir(parents=True)
    method.write_text("---\nname: research-synthesis\ndescription: Evidence analysis\n---\nMETHOD_BODY\n")
    loader = SkillLoader(method.parent.parent)
    loader.discover_skills()
    adapter = acp_module.BoxACPAgent(DummyConn(), config, DoneLLM(), [], "system", skill_loader=loader)
    response = await adapter.newSession(SimpleNamespace(
        cwd=None, field_meta={"session_mode": "general"},
    ))
    session = adapter._sessions[response.sessionId]
    # Historical compatibility views cannot select methods or raise quotas.
    session.preloaded_skill_names.append("research-synthesis")
    seen = []
    original = AgentSession.run_events

    async def tracked_run(self, *, options=None):
        seen.append((self.config, options.web_search_total_limit))
        async for event in original(self, options=options):
            yield event

    monkeypatch.setattr(AgentSession, "run_events", tracked_run)
    # A new adapter default must not replace an existing session's config.
    adapter._config = config.model_copy(deep=True)
    adapter._config.tool_limits.web_search.deep_research_total_calls = 1
    result = await adapter.prompt(SimpleNamespace(
        sessionId=response.sessionId, prompt=[{"text": "hello"}],
        field_meta={"selected_skill_names": ["research-synthesis"]} if selected else {},
    ))

    assert isinstance(session, AgentSession)
    assert session.config is config
    assert result.stopReason == "end_turn"
    assert seen == [(config, 7 if selected else None)]


@pytest.mark.asyncio
async def test_model_switch_keeps_session_config_context_budget(tmp_path):
    from box_agent.acp import BoxACPAgent
    from tests.test_lite_llm_routing import _DummyConn, _DummyLLM

    config = session_config(tmp_path)
    config.llm.context_window = 100_000
    config.llm.max_output_tokens = 20_000
    adapter = BoxACPAgent(_DummyConn(), config, _DummyLLM("main"), [], "system")
    response = await adapter.newSession(SimpleNamespace(cwd=str(tmp_path)))
    session = adapter._sessions[response.sessionId]
    adapter._config = config.model_copy(deep=True)
    adapter._config.llm.context_window = 200_000

    result = await adapter.prompt(SimpleNamespace(
        sessionId=response.sessionId, prompt=[{"text": "hello"}],
        field_meta={"llm_binding": {"source": "builtin", "model": "another-model"}},
    ))

    assert result.stopReason == "end_turn"
    assert session.agent.llm.model == "another-model"
    assert session.agent.token_limit == 72_000


@pytest.mark.asyncio
async def test_closing_session_stream_persists_interrupted_turn(tmp_path):
    from box_agent.agent_session import AgentSession
    from box_agent.session_log import SessionLog

    session_log = SessionLog.create(
        tmp_path / "logs", session_id="stream-close", cwd=tmp_path,
    )
    try:
        session = AgentSession.create(
            config=session_config(tmp_path), llm_client=ToolThenAnswerLLM(),
            system_prompt="system", tools=[], session_log=session_log,
        )
        session.agent.add_user_message("Start a task.")
        stream = session.run_events(options=session.build_run_options(logger=None))
        try:
            async for event in stream:
                if isinstance(event, StepStart):
                    break
        finally:
            await stream.aclose()

        durable_events = [
            json.loads(line) for line in session_log.path.read_bytes().splitlines()
        ]
        turn_ends = [e for e in durable_events if e["type"] == "turn/end"]
        assert len(turn_ends) == 1
        assert turn_ends[0]["data"]["reason"] == {"kind": "interrupted"}
    finally:
        session_log.close()


@pytest.mark.asyncio
async def test_acp_legacy_state_without_config_runs_with_adapter_defaults(tmp_path):
    from box_agent.acp import BoxACPAgent, SessionState
    from box_agent.agent import Agent
    from tests.test_acp import DoneLLM, DummyConn

    config = session_config(tmp_path)
    llm = DoneLLM()
    adapter = BoxACPAgent(DummyConn(), config, llm, [], "system")
    state = SessionState(agent=Agent(
        llm_client=llm, system_prompt="system", tools=[],
        workspace_dir=str(tmp_path), deferred_mcp_loading_enabled=False,
    ))
    state.agent.add_user_message("hello")

    result = await adapter._run_turn(state, "legacy-session")

    assert result == "end_turn"
    assert state.config is config


@pytest.mark.asyncio
async def test_session_stream_owns_active_state_and_cancellation(tmp_path):
    from box_agent.agent_session import AgentSession

    session = AgentSession.create(
        config=session_config(tmp_path), llm_client=ToolThenAnswerLLM(),
        system_prompt="system", tools=[],
    )
    session.agent.add_user_message("Read the goal.")
    stream = session.run_events()
    try:
        await anext(stream)
        assert session.turn_active is True
        session.request_cancel()
        events = [event async for event in stream]
    finally:
        await stream.aclose()

    assert session.turn_active is False
    assert session.agent.last_stop_reason == "cancelled"
    assert [e.stop_reason for e in events if isinstance(e, DoneEvent)] == [StopReason.CANCELLED]


@pytest.mark.asyncio
async def test_session_stream_does_not_end_enclosing_host_turn(tmp_path):
    from box_agent.agent_session import AgentSession

    session = AgentSession.create(
        config=session_config(tmp_path), llm_client=ToolThenAnswerLLM(),
        system_prompt="system", tools=[],
    )
    session.turn_active = True
    stream = session.run_events(options=session.build_run_options(logger=None))
    await anext(stream)
    await stream.aclose()

    assert session.turn_active is True


@pytest.mark.asyncio
async def test_acp_closes_session_stream_before_returning_prompt(tmp_path):
    from box_agent.acp import BoxACPAgent
    from tests.test_lite_llm_routing import _DummyConn, _DummyLLM

    adapter = BoxACPAgent(
        _DummyConn(), session_config(tmp_path), _DummyLLM("main"), [], "system",
    )
    response = await adapter.newSession(SimpleNamespace(cwd=str(tmp_path)))
    state = adapter._sessions[response.sessionId]
    first = await adapter.prompt(SimpleNamespace(
        sessionId=response.sessionId, prompt=[{"text": "first"}],
    ))
    assert first.stopReason == "end_turn"
    # Finalizers must not reactivate a prompt after ACP reports completion.
    for _ in range(3):
        await asyncio.sleep(0)
    assert state.turn_active is False
    assert await adapter.extMethod("inject", {
        "sessionId": response.sessionId, "text": "too late", "injectionId": "late",
    }) == {"error": "no_active_turn"}
    second = await adapter.prompt(SimpleNamespace(
        sessionId=response.sessionId, prompt=[{"text": "second"}],
        field_meta={"llm_binding": {"source": "builtin", "model": "next-model"}},
    ))
    assert second.stopReason == "end_turn"
    assert state.agent.llm.model == "next-model"

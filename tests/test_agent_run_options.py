from __future__ import annotations

import asyncio
from dataclasses import fields, replace
from pathlib import Path

import pytest

import box_agent.agent as agent_module
from box_agent.agent import Agent, AgentRunOptions
from box_agent.config import ToolLimitsConfig
from box_agent.context_resources import ResourceClass, ResourceDescriptor
from box_agent.events import ContentEvent, DoneEvent, StopReason, SummarizationEvent
from box_agent.schema import FunctionCall, StreamEvent, ToolCall
from box_agent.tools.base import Tool, ToolResult
from box_agent.skill_runtime import SkillRuntime


class DummyLLM:
    pass


class ReferenceCapturingLLM:
    model = "test-model"
    max_output_tokens = 1024

    def __init__(self):
        self.requests = []

    async def generate_stream(self, *, messages, **kwargs):
        self.requests.append([message.model_copy(deep=True) for message in messages])
        yield StreamEvent(type="text", delta="done")
        yield StreamEvent(type="finish", finish_reason="stop")


@pytest.mark.asyncio
async def test_agent_run_forwards_core_execution_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    async def fake_run_agent_loop(**kwargs):
        captured.update(kwargs)
        yield DoneEvent(stop_reason=StopReason.END_TURN, final_content="done")

    monkeypatch.setattr(agent_module, "run_agent_loop", fake_run_agent_loop)

    skill_runtime = SkillRuntime(None)
    agent = Agent(
        llm_client=DummyLLM(),
        system_prompt="system",
        tools=[],
        workspace_dir=str(tmp_path),
        thinking_enabled=True,
        tool_limits=ToolLimitsConfig(web_search={"total_calls": 31}),
        skill_runtime=skill_runtime,
    )

    result = await agent.run(
        force_plan_start=True,
        artifact_detection_enabled=False,
        current_turn_text="current user request",
    )

    assert result == "done"
    assert captured["force_plan_start"] is True
    assert "completion_gate" not in captured
    assert captured["artifact_detection_enabled"] is False
    assert captured["thinking_enabled"] is True
    assert captured["tool_limits"].web_search.total_calls == 31
    assert captured["skill_engine"] is skill_runtime is agent.skill_runtime
    assert "active_skill_activator" not in captured
    assert captured["current_turn_text"] == "current user request"
    assert captured["context_resource_ledger"] is agent.context_resource_ledger
    assert captured["context_resource_dedup_enabled"] is True


@pytest.mark.asyncio
async def test_agent_run_events_forwards_host_run_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    async def fake_run_agent_loop(**kwargs):
        captured.update(kwargs)
        yield DoneEvent(stop_reason=StopReason.END_TURN, final_content="done")

    monkeypatch.setattr(agent_module, "run_agent_loop", fake_run_agent_loop)
    agent = Agent(
        llm_client=DummyLLM(),
        system_prompt="system",
        tools=[],
        workspace_dir=str(tmp_path),
    )
    host_llm = object()
    summary_llm = object()
    cancelled = lambda: False
    host_logger = object()
    permission_negotiator = object()
    hooks = [object()]
    memory_manager = object()
    memory_extractor = object()
    inject_queue: asyncio.Queue[object] = asyncio.Queue()
    plan_approval = {"state": "approved"}
    fingerprint_context = {"source": "host"}
    artifact_root = tmp_path / "host-output"
    def fingerprint_sink(_payload: dict) -> None:
        pass

    options = replace(
        agent.default_run_options(),
        llm=host_llm,
        summary_llm=summary_llm,
        is_cancelled=cancelled,
        logger=host_logger,
        permission_negotiator=permission_negotiator,
        hooks=hooks,
        memory_manager=memory_manager,
        memory_extractor=memory_extractor,
        session_id="host-session",
        memory_turn_id="turn-1",
        inject_queue=inject_queue,
        turn_id="turn-1",
        title="Quarterly review",
        force_plan_start=True,
        require_plan_approval=True,
        plan_approval=plan_approval,
        plan_start_text="write a plan",
        pause_after_plan_write=True,
        max_tool_calls=9,
        max_delegated_tool_calls=12,
        web_search_total_limit=36,
        no_progress_limit=2,
        artifact_detection_enabled=False,
        artifact_root_dir=artifact_root,
        cache_fingerprint_context=fingerprint_context,
        cache_fingerprint_sink=fingerprint_sink,
        current_turn_text="host current turn",
    )

    events = [event async for event in agent.run_events(options=options)]

    assert len(events) == 1
    identity_fields = {
        "llm",
        "summary_llm",
        "is_cancelled",
        "logger",
        "permission_negotiator",
        "hooks",
        "memory_manager",
        "memory_extractor",
        "inject_queue",
        "plan_approval",
        "cache_fingerprint_context",
        "cache_fingerprint_sink",
    }
    for option_field in fields(AgentRunOptions):
        if option_field.name == "artifact_root_dir":
            assert option_field.name not in captured
            continue
        if option_field.name == "kernel_services" and options.kernel_services is None:
            assert "kernel_services" not in captured
            continue
        actual = captured[option_field.name]
        expected = getattr(options, option_field.name)
        if option_field.name in identity_fields:
            assert actual is expected, option_field.name
        else:
            assert actual == expected, option_field.name
    assert "workflow_policy" not in captured
    assert {"_services", "services", "registry", "plugin_host"}.isdisjoint(captured)
    assert captured["context_resource_ledger"] is agent.context_resource_ledger


@pytest.mark.asyncio
async def test_agent_run_consumes_run_events_and_returns_done_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = Agent(
        llm_client=DummyLLM(),
        system_prompt="system",
        tools=[],
        workspace_dir=str(tmp_path),
    )
    seen: dict[str, object] = {}
    rendered: list[object] = []

    async def fake_run_events(cancel_event=None, *, options=None, **kwargs):
        seen["cancel_event"] = cancel_event
        seen["options"] = options
        seen["kwargs"] = kwargs
        yield ContentEvent(content="streamed")
        yield DoneEvent(stop_reason=StopReason.END_TURN, final_content="final")

    monkeypatch.setattr(agent, "run_events", fake_run_events)
    monkeypatch.setattr(agent, "_render_event", rendered.append)

    result = await agent.run(current_turn_text="current request")

    assert result == "final"
    assert agent.last_stop_reason == StopReason.END_TURN.value
    assert [type(event).__name__ for event in rendered] == [
        "ContentEvent",
        "DoneEvent",
    ]
    assert seen["options"].current_turn_text == "current request"


class _OrderedEventsLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, **_kwargs):
        raise AssertionError("this fixture must not need context summarization")

    async def generate_stream(self, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                type="finish",
                finish_reason="tool_use",
                tool_calls=[
                    ToolCall(
                        id="echo-1",
                        type="function",
                        function=FunctionCall(
                            name="echo",
                            arguments={"text": "evidence"},
                        ),
                    )
                ],
            )
            return
        yield StreamEvent(type="text", delta="final")
        yield StreamEvent(type="finish", finish_reason="stop")


class _OrderedEventsEchoTool(Tool):
    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "Echo deterministic evidence."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        }

    async def execute(self, text: str) -> ToolResult:
        return ToolResult(success=True, content=text)


@pytest.mark.asyncio
async def test_agent_public_event_order_for_deterministic_tool_turn(
    tmp_path: Path,
) -> None:
    agent = Agent(
        llm_client=_OrderedEventsLLM(),
        system_prompt="system",
        tools=[_OrderedEventsEchoTool()],
        max_steps=3,
        workspace_dir=str(tmp_path),
    )
    agent.add_user_message("use echo")
    options = replace(agent.default_run_options(), logger=None)

    event_types = [
        type(event).__name__
        async for event in agent.run_events(options=options)
    ]

    assert event_types == [
        "StepStart",
        "LLMOutputEvent",
        "ToolCallStart",
        "ToolCallResult",
        "StepEnd",
        "StepStart",
        "ContentEvent",
        "LLMOutputEvent",
        "StepEnd",
        "DoneEvent",
    ]


def test_agent_public_runtime_configuration_and_history_reset(tmp_path: Path) -> None:
    agent = Agent(
        llm_client=DummyLLM(),
        system_prompt="system",
        tools=[],
        workspace_dir=str(tmp_path),
    )
    permission_negotiator = object()
    memory_extractor = object()
    proposal_negotiator = object()

    agent.set_permission_negotiator(permission_negotiator)
    agent.set_memory_extractor(memory_extractor)
    agent.set_memory_proposal_negotiator(proposal_negotiator)
    agent.messages.extend(
        [
            agent.messages[0].__class__(role="user", content="hello"),
            agent.messages[0].__class__(role="assistant", content="hi"),
        ]
    )
    agent.context_resource_ledger.register_full_source(
        "read-1",
        ResourceDescriptor(
            resource_id=str(tmp_path / "reference.md"),
            content_version="a" * 64,
            start_line=1,
            end_line=1,
            total_lines=1,
            resource_class=ResourceClass.RECONSTRUCTABLE,
        ),
        "full body",
    )

    options = agent.default_run_options()
    assert options.permission_negotiator is permission_negotiator
    assert options.memory_extractor is memory_extractor
    assert agent.clear_history() == 2
    assert len(agent.messages) == 1
    assert agent.context_resource_ledger.epoch == 1
    assert agent.context_resource_ledger.source_ids == ()


async def test_agent_preserves_deduplicated_references_across_prompt_updates(
    tmp_path: Path,
) -> None:
    llm = ReferenceCapturingLLM()
    agent = Agent(
        llm_client=llm,
        system_prompt="system",
        tools=[],
        workspace_dir=str(tmp_path),
    )

    agent.activate_skill_instructions("pptx", "# Skill: pptx\n\nOld instructions.")
    agent.activate_skill_instructions("pptx", "# Skill: pptx\n\nOld instructions.")

    agent.add_user_message("first input")
    await agent.run()
    assert "Old instructions." not in agent.system_prompt
    assert str(llm.requests[0]).count("Old instructions.") == 1
    assert llm.requests[0][-1].role == "user"
    assert "Host-provided Skill reference" in str(llm.requests[0][-1].content)
    assert "Old instructions." not in str(agent.messages)
    assert agent.messages[1].content == "first input"

    base_prompt = agent.system_prompt.replace("system", "updated", 1)
    agent.set_system_prompt(base_prompt)
    agent.activate_skill_instructions("pptx", "# Skill: pptx\n\nNew instructions.")
    agent.add_user_message("second input")
    await agent.run()

    assert agent.messages[0].content == agent.system_prompt
    assert agent.system_prompt.startswith("updated")
    assert "Old instructions." not in agent.system_prompt
    assert "New instructions." not in agent.system_prompt
    assert "Old instructions." not in str(llm.requests[-1])
    assert str(llm.requests[-1]).count("New instructions.") == 1
    assert llm.requests[-1][-1].role == "user"
    assert agent.messages[-2].content == "second input"
    assert "New instructions." not in str(agent.messages)


async def test_agent_reports_reference_budget_without_truncating_source_and_can_clear(
    tmp_path: Path,
) -> None:
    llm = ReferenceCapturingLLM()
    agent = Agent(
        llm_client=llm,
        system_prompt="system",
        tools=[],
        workspace_dir=str(tmp_path),
    )
    first = "FIRST_REQUIRED_RULE\n" + "a" * 70_000
    second = "SECOND_REQUIRED_RULE\n" + "b" * 70_000

    agent.activate_skill_instructions("first", first)
    agent.activate_skill_instructions("second", second)
    diagnostics = agent.active_skill_diagnostics()

    assert diagnostics["names"] == ("first", "second")
    assert diagnostics["budget_exceeded"] is True
    assert agent.skill_runtime.state.reads["first"].prompt == first
    assert agent.skill_runtime.state.reads["second"].prompt == second
    assert "FIRST_REQUIRED_RULE" not in agent.system_prompt
    assert "SECOND_REQUIRED_RULE" not in agent.system_prompt
    agent.add_user_message("bounded input")
    events = [event async for event in agent.run_events()]
    assert llm.requests == []
    assert any(isinstance(event, DoneEvent) and event.stop_reason == StopReason.ERROR for event in events)
    # One bounded recovery can append compaction metadata. The original user
    # text and full Skill sources survive; no truncated method is delivered.
    assert sum(isinstance(event, SummarizationEvent) for event in events) == 1
    assert any(message.role == "user" and message.content == "bounded input"
               for message in agent.messages)
    assert "FIRST_REQUIRED_RULE" not in str(agent.messages)
    assert "SECOND_REQUIRED_RULE" not in str(agent.messages)
    assert agent.skill_runtime.state.reads["first"].prompt == first
    assert agent.skill_runtime.state.reads["second"].prompt == second

    assert agent.deactivate_skill_instructions("first") is True
    assert "FIRST_REQUIRED_RULE" not in agent.system_prompt
    assert agent.skill_runtime.state.reads["second"].prompt == second
    assert agent.skill_runtime.state.selected == ("second",)
    assert agent.deactivate_skill_instructions("missing") is False

    agent.clear_active_skill_instructions()
    assert agent.skill_runtime.state.reads == {}
    assert agent.skill_runtime.state.selected == ()
    assert agent.active_skill_diagnostics()["names"] == ()

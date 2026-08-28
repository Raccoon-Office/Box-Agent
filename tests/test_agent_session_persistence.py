"""Agent integration tests for durable Session Log checkpoints."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from box_agent.agent import Agent
from box_agent.events import DoneEvent
from box_agent.hooks import BaseHook
from box_agent.schema import FunctionCall, LLMResponse, Message, StreamEvent, ToolCall
from box_agent.session_log import SessionLog, SessionLogDurabilityError
from box_agent.tools.base import Tool, ToolResult
from box_agent.tools.plan_tool import PlanReadTool, PlanStore, PlanWriteTool
from box_agent.tools.todo_tool import TodoReadTool, TodoStore, TodoWriteTool


def _read_durable_events(path: Path) -> list[dict]:
    raw = path.read_bytes()
    assert raw.endswith(b"\n")
    return [json.loads(line) for line in raw.splitlines()[1:]]


class _CheckpointInspectingLLM:
    model = "test-model"
    max_output_tokens = 1024

    def __init__(self, path: Path) -> None:
        self.path = path
        self.saw_durable_request = False

    async def generate_stream(self, **_kwargs):
        event_types = [event["type"] for event in _read_durable_events(self.path)]
        self.saw_durable_request = event_types[:4] == [
            "turn/start",
            "user/message",
            "step/start",
            "request/header",
        ]
        yield StreamEvent(type="text", delta="durable answer")
        yield StreamEvent(type="finish", finish_reason="stop")


@pytest.mark.asyncio
async def test_agent_persists_request_before_provider_and_restores_messages(tmp_path):
    session_id = "agent-session"
    log = SessionLog.create(tmp_path / "sessions", session_id=session_id, cwd=tmp_path)
    llm = _CheckpointInspectingLLM(log.path)
    agent = Agent(
        llm_client=llm,
        system_prompt="system",
        tools=[],
        workspace_dir=str(tmp_path),
        deferred_mcp_loading_enabled=False,
        session_log=log,
    )
    agent.add_user_message("persist me")

    events = [event async for event in agent.run_events()]
    log.close()

    assert llm.saw_durable_request
    assert any(isinstance(event, DoneEvent) for event in events)
    restored = SessionLog.open(
        tmp_path / "sessions",
        session_id=session_id,
        cwd=tmp_path,
    )
    assert [(message.role, message.content) for message in restored.replay().messages] == [
        ("user", "persist me"),
        ("assistant", "durable answer"),
    ]
    assert [event["type"] for event in restored.events][-2:] == [
        "step/end",
        "turn/end",
    ]
    restored.close()


class _ToolCallingLLM:
    model = "test-model"
    max_output_tokens = 1024

    def __init__(self) -> None:
        self.calls = 0

    async def generate_stream(self, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                type="finish",
                finish_reason="tool",
                tool_calls=[
                    ToolCall(
                        id="durable-call",
                        type="function",
                        function=FunctionCall(
                            name="side_effect",
                            arguments={"value": "original"},
                        ),
                    )
                ],
            )
            return
        yield StreamEvent(type="text", delta="done")
        yield StreamEvent(type="finish", finish_reason="stop")


class _ArgumentHook(BaseHook):
    async def on_tool_start(self, **_kwargs):
        return {"value": "modified"}


class _DurabilityInspectingTool(Tool):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.executed = False
        self.saw_durable_call = False

    @property
    def name(self):
        return "side_effect"

    @property
    def description(self):
        return "Record one externally visible side effect."

    @property
    def parameters(self):
        return {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        }

    async def execute(self, value: str):
        calls = [
            event
            for event in _read_durable_events(self.path)
            if event["type"] == "tool/call"
        ]
        self.saw_durable_call = bool(calls) and calls[-1]["data"] == {
            "turn": 1,
            "step": 1,
            "callId": "durable-call",
            "name": "side_effect",
            "arguments": {"value": "modified"},
        }
        self.executed = True
        return ToolResult(
            success=True,
            content=f"saved:{value}",
            raw_output={"child_session_id": "child-session"},
        )


@pytest.mark.asyncio
async def test_tool_side_effect_starts_only_after_exact_call_is_durable(tmp_path):
    session_id = "tool-checkpoint"
    root = tmp_path / "sessions"
    log = SessionLog.create(root, session_id=session_id, cwd=tmp_path)
    tool = _DurabilityInspectingTool(log.path)
    agent = Agent(
        llm_client=_ToolCallingLLM(),
        system_prompt="system",
        tools=[tool],
        workspace_dir=str(tmp_path),
        hooks=[_ArgumentHook()],
        deferred_mcp_loading_enabled=False,
        session_log=log,
    )
    agent.add_user_message("perform it")

    events = [event async for event in agent.run_events()]
    log.close()

    assert any(isinstance(event, DoneEvent) for event in events)
    assert tool.executed
    assert tool.saw_durable_call
    restored = SessionLog.open(root, session_id=session_id, cwd=tmp_path)
    durable_result = next(
        event
        for event in restored.events
        if event["type"] == "tool/result"
    )
    assert durable_result["data"]["result"]["rawOutput"] == {
        "child_session_id": "child-session"
    }
    restored.close()


@pytest.mark.asyncio
async def test_flush_failure_prevents_provider_call(tmp_path, monkeypatch):
    log = SessionLog.create(
        tmp_path / "sessions",
        session_id="flush-failure",
        cwd=tmp_path,
    )
    llm = _CheckpointInspectingLLM(log.path)
    agent = Agent(
        llm_client=llm,
        system_prompt="system",
        tools=[],
        workspace_dir=str(tmp_path),
        deferred_mcp_loading_enabled=False,
        session_log=log,
    )
    agent.add_user_message("must not reach provider")
    monkeypatch.setattr(log, "flush", lambda: (_ for _ in ()).throw(OSError("disk")))

    with pytest.raises(OSError, match="disk"):
        _ = [event async for event in agent.run_events()]

    assert not llm.saw_durable_request
    log.close()


class _SummaryCheckpointLLM:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.saw_start = False

    async def generate(self, **_kwargs):
        self.saw_start = (
            _read_durable_events(self.path)[-1]["type"] == "compaction/start"
        )
        return LLMResponse(
            content="<summary>durable compacted history</summary>",
            finish_reason="stop",
        )


class _PostCompactionLLM:
    model = "test-model"
    max_output_tokens = 1024

    def __init__(self, path: Path) -> None:
        self.path = path
        self.saw_replacement = False

    async def generate_stream(self, **_kwargs):
        event_types = [event["type"] for event in _read_durable_events(self.path)]
        self.saw_replacement = event_types[-2:] == [
            "request/header",
            "request/context",
        ] and "compaction/end" in event_types
        yield StreamEvent(type="text", delta="after compaction")
        yield StreamEvent(type="finish", finish_reason="stop")


@pytest.mark.asyncio
async def test_compaction_is_durable_before_live_context_switch(tmp_path):
    session_id = "compaction-checkpoint"
    root = tmp_path / "sessions"
    log = SessionLog.create(root, session_id=session_id, cwd=tmp_path)
    llm = _PostCompactionLLM(log.path)
    summary_llm = _SummaryCheckpointLLM(log.path)
    agent = Agent(
        llm_client=llm,
        system_prompt="system",
        tools=[],
        workspace_dir=str(tmp_path),
        token_limit=5_000,
        deferred_mcp_loading_enabled=False,
        session_log=log,
    )
    for index in range(24):
        role = "user" if index % 2 == 0 else "assistant"
        agent.messages.append(Message(role=role, content=f"old-{index}:" + "x" * 2_000))
    agent.add_user_message("latest request")
    options = replace(agent.default_run_options(), summary_llm=summary_llm)

    _ = [event async for event in agent.run_events(options=options)]

    assert summary_llm.saw_start
    assert llm.saw_replacement
    assert any(
        message.role == "user" and "durable compacted history" in str(message.content)
        for message in log.replay().messages
    )
    assert any(
        event["type"] == "user/message"
        and str(event["data"].get("content", "")).startswith("old-0:")
        for event in log.events
    )
    log.close()


@pytest.mark.asyncio
async def test_agent_restores_goal_plan_and_todos_from_session_log(tmp_path):
    root = tmp_path / "sessions"
    log = SessionLog.create(root, session_id="domain-restore", cwd=tmp_path)
    plan_store = PlanStore()
    todo_store = TodoStore()
    agent = Agent(
        llm_client=_ToolCallingLLM(),
        system_prompt="system",
        tools=[
            PlanWriteTool(plan_store),
            PlanReadTool(plan_store),
            TodoWriteTool(todo_store),
            TodoReadTool(todo_store),
        ],
        workspace_dir=str(tmp_path),
        deferred_mcp_loading_enabled=False,
        session_log=log,
    )
    agent.set_goal("persist state")
    plan_result = await agent.tools["plan_write"].invoke(
        {
            "action": "set",
            "title": "Durable plan",
            "steps": [{"title": "Implement"}],
        }
    )
    todo_result = await agent.tools["todo_write"].invoke(
        {
            "action": "set",
            "todos": [
                {
                    "task": "Implement",
                    "status": "in_progress",
                    "priority": "high",
                }
            ],
        }
    )
    assert plan_result.success and todo_result.success
    log.close()

    restored_log = SessionLog.open(
        root,
        session_id="domain-restore",
        cwd=tmp_path,
    )
    restored_plan_store = PlanStore()
    restored_todo_store = TodoStore()
    restored_agent = Agent(
        llm_client=_ToolCallingLLM(),
        system_prompt="system",
        tools=[
            PlanWriteTool(restored_plan_store),
            PlanReadTool(restored_plan_store),
            TodoWriteTool(restored_todo_store),
            TodoReadTool(restored_todo_store),
        ],
        workspace_dir=str(tmp_path),
        deferred_mcp_loading_enabled=False,
        session_log=restored_log,
    )

    assert restored_agent.goal is not None
    assert restored_agent.goal.objective == "persist state"
    assert restored_plan_store.get()["title"] == "Durable plan"
    assert restored_todo_store.list()[0]["task"] == "Implement"
    restored_log.close()


class _PlanCallingLLM:
    model = "test-model"
    max_output_tokens = 1024

    def __init__(self) -> None:
        self.calls = 0

    async def generate_stream(self, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                type="finish",
                finish_reason="tool",
                tool_calls=[
                    ToolCall(
                        id="plan-call",
                        type="function",
                        function=FunctionCall(
                            name="plan_write",
                            arguments={"action": "set", "title": "Must persist"},
                        ),
                    )
                ],
            )
            return
        yield StreamEvent(type="text", delta="must not run")
        yield StreamEvent(type="finish", finish_reason="stop")


@pytest.mark.asyncio
async def test_state_persistence_failure_aborts_before_next_provider_call(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "sessions"
    log = SessionLog.create(root, session_id="state-flush-failure", cwd=tmp_path)
    llm = _PlanCallingLLM()
    store = PlanStore()
    agent = Agent(
        llm_client=llm,
        system_prompt="system",
        tools=[PlanWriteTool(store), PlanReadTool(store)],
        workspace_dir=str(tmp_path),
        deferred_mcp_loading_enabled=False,
        session_log=log,
    )
    agent.add_user_message("make a plan")
    original_flush = log.flush
    flush_count = 0

    def fail_plan_flush():
        nonlocal flush_count
        flush_count += 1
        if flush_count == 3:
            log._failed = True
            raise SessionLogDurabilityError("disk full")
        original_flush()

    monkeypatch.setattr(log, "flush", fail_plan_flush)

    with pytest.raises(SessionLogDurabilityError, match="disk full"):
        _ = [event async for event in agent.run_events()]

    assert llm.calls == 1
    log.close()


def test_active_skill_metadata_restores_only_with_matching_content(tmp_path):
    root = tmp_path / "sessions"
    log = SessionLog.create(root, session_id="skill-restore", cwd=tmp_path)
    agent = Agent(
        llm_client=_ToolCallingLLM(),
        system_prompt="system",
        tools=[],
        workspace_dir=str(tmp_path),
        deferred_mcp_loading_enabled=False,
        session_log=log,
    )
    agent.activate_skill_instructions("review", "trusted skill prompt")
    persisted = log.replay().skills
    log.close()

    assert persisted[0]["name"] == "review"
    restored_log = SessionLog.open(
        root,
        session_id="skill-restore",
        cwd=tmp_path,
    )
    restored = Agent(
        llm_client=_ToolCallingLLM(),
        system_prompt="system",
        tools=[],
        workspace_dir=str(tmp_path),
        deferred_mcp_loading_enabled=False,
        session_log=restored_log,
    )
    restored.restore_active_skill_instructions(
        [
            (
                "review",
                "trusted skill prompt",
                persisted[0]["sha256"],
                persisted[0]["loadOrder"],
            )
        ]
    )

    assert "trusted skill prompt" in restored.system_prompt
    with pytest.raises(ValueError, match="content hash changed"):
        restored.restore_active_skill_instructions(
            [("review", "changed", persisted[0]["sha256"], 1)]
        )
    restored_log.close()

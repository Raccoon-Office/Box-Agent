"""Agent integration tests for durable Session Log checkpoints."""

import json
import os
import subprocess
import sys
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from box_agent.agent import Agent
from box_agent.events import DoneEvent, SummarizationEvent
from box_agent.hooks import BaseHook
from box_agent.schema import FunctionCall, LLMResponse, Message, StreamEvent, ToolCall
from box_agent.session_log import (
    SessionLog,
    SessionLogDurabilityError,
    SessionLogReplayError,
)
from box_agent.session_projection import SessionProjection
from box_agent.tools.base import Tool, ToolResult
from box_agent.tools.plan_tool import PlanReadTool, PlanStore, PlanWriteTool
from box_agent.tools.skill_loader import SkillLoader
from box_agent.tools.skill_tool import GetSkillTool
from box_agent.tools.todo_tool import TodoReadTool, TodoStore, TodoWriteTool


def _read_durable_events(path: Path) -> list[dict]:
    raw = path.read_bytes()
    assert raw.endswith(b"\n")
    return [json.loads(line) for line in raw.splitlines()[1:]]


class _ReferenceCapturingLLM:
    model = "test-model"
    max_output_tokens = 1024

    def __init__(self):
        self.requests = []

    async def generate_stream(self, *, messages, **kwargs):
        self.requests.append([message.model_copy(deep=True) for message in messages])
        yield StreamEvent(type="text", delta="done")
        yield StreamEvent(type="finish", finish_reason="stop")


def _make_reference_loader(tmp_path: Path) -> SkillLoader:
    directory = tmp_path / "skills" / "review"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review method\n---\nEXACT-REVIEW-METHOD\n",
        encoding="utf-8",
    )
    loader = SkillLoader(sources=[(directory.parent, "builtin")])
    loader.discover_skills()
    return loader


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


def test_agent_clear_history_persists_surface_reset(tmp_path):
    sessions = tmp_path / "sessions"
    log = SessionLog.create(sessions, session_id="clear-session", cwd=tmp_path)
    agent = Agent(
        llm_client=_CheckpointInspectingLLM(log.path),
        system_prompt="system",
        tools=[],
        workspace_dir=str(tmp_path),
        deferred_mcp_loading_enabled=False,
        session_log=log,
    )
    agent.add_user_message("remove me")
    assert agent.clear_history() == 1
    log.append(
        "user/message",
        Message(role="user", content="keep me").model_dump(mode="json"),
        surface_op="append",
    )
    log.flush()
    log.close()

    restored_log = SessionLog.open(
        sessions,
        session_id="clear-session",
        cwd=tmp_path,
    )
    assert [message.content for message in restored_log.replay().messages] == [
        "keep me"
    ]
    restored_log.close()


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


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["kernel", "agent"])
@pytest.mark.parametrize("nudge", ["near_limit", "no_progress"])
@pytest.mark.parametrize("interrupt_during", ["provider", "summary"])
async def test_runtime_wrapup_survives_interruption_before_model_response(
    tmp_path, entrypoint, nudge, interrupt_during,
):
    from box_agent.core import run_agent_loop

    class Interrupted(BaseException):
        pass

    root = tmp_path / "sessions"
    log = SessionLog.create(root, session_id="wrapup-checkpoint", cwd=tmp_path)
    marker = "步数预算即将用尽" if nudge == "near_limit" else "没有取得有效进展"
    recovered = []
    expected = []

    def interrupt(messages, phase):
        assert phase == interrupt_during
        expected.extend(message.content for message in messages if marker in str(message.content))
        assert len(expected) == 1
        if phase == "summary":
            assert _read_durable_events(log.path)[-1]["type"] == "compaction/start"
        # Open the on-disk checkpoint before unwinding/closing the active log:
        # cleanup must not make an unflushed message look crash-safe.
        snapshot_root = tmp_path / "recovery"
        snapshot = snapshot_root / log.path.relative_to(root)
        snapshot.parent.mkdir(parents=True)
        snapshot.write_bytes(log.path.read_bytes())
        reopened = SessionLog.open(snapshot_root, session_id="wrapup-checkpoint", cwd=tmp_path)
        try:
            recovered.extend(message.content for message in reopened.replay().messages
                             if marker in str(message.content))
        finally:
            reopened.close()
        raise Interrupted

    class FailingTool(Tool):
        name = "failing_probe"
        description = "Return an unsuccessful result."
        parameters = {"type": "object", "properties": {}}

        async def execute(self):
            return ToolResult(success=False, content="No useful result", error="unavailable")

    class Provider:
        model = "test-model"
        max_output_tokens = 1024

        async def generate_stream(self, *, messages, **kwargs):
            if any(marker in str(message.content) for message in messages):
                interrupt(messages, "provider")
            if interrupt_during == "summary":
                # Grow history after the first request so compaction and the
                # wrap-up injection both happen on the next step.
                history.extend(Message(role="user" if index % 2 == 0 else "assistant",
                                       content=f"history-{index}:" + "x" * 2000)
                               for index in range(24))
            yield StreamEvent(type="finish", finish_reason="tool", tool_calls=[
                ToolCall(id="failed-call", type="function", function=FunctionCall(
                    name="failing_probe", arguments={},
                )),
            ])

    class Summary:
        async def generate(self, *, messages, **kwargs):
            interrupt(messages, "summary")

        async def generate_stream(self, *, messages, **kwargs):
            interrupt(messages, "summary")
            yield  # Keep the streaming interface without producing a response.

    agent = Agent(
        llm_client=Provider(), tools=[FailingTool()], system_prompt="system",
        workspace_dir=str(tmp_path), deferred_mcp_loading_enabled=False,
        session_log=log, max_steps=11 if nudge == "near_limit" else 300,
        token_limit=8000 if interrupt_during == "summary" else 100000,
    )
    agent.add_user_message("Complete the task")
    history = agent.messages
    options = replace(agent.default_run_options(), summary_llm=Summary(),
                      no_progress_limit=1 if nudge == "no_progress" else None)
    events = (agent.run_events(options=options) if entrypoint == "agent" else run_agent_loop(
        llm=agent.llm, tools=agent.tools, messages=history,
        max_steps=agent.max_steps, token_limit=agent.token_limit,
        session_log=log, session_turn=1, workspace_dir=str(tmp_path),
        no_progress_limit=options.no_progress_limit, summary_llm=options.summary_llm,
    ))
    try:
        with pytest.raises(Interrupted):
            async for _ in events:
                pass
    finally:
        await events.aclose()
        log.close()
    assert recovered == expected
    assert len(recovered) == 1


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

    async def generate_stream(self, **_kwargs):
        self.saw_start = (
            _read_durable_events(self.path)[-1]["type"] == "compaction/start"
        )
        yield StreamEvent(
            type="text", delta="<summary>durable compacted history</summary>"
        )
        yield StreamEvent(type="finish", finish_reason="stop")


class _PostCompactionLLM:
    model = "test-model"
    max_output_tokens = 1024

    def __init__(self, path: Path) -> None:
        self.path = path
        self.saw_replacement = False
        self.normal_messages: list[Message] = []

    async def generate_stream(self, **kwargs):
        self.normal_messages = list(kwargs.get("messages", ()))
        event_types = [event["type"] for event in _read_durable_events(self.path)]
        self.saw_replacement = event_types[-2:] == [
            "request/header",
            "request/context",
        ] and "compaction/end" in event_types
        yield StreamEvent(type="text", delta="after compaction")
        yield StreamEvent(type="finish", finish_reason="stop")


class _ReplayAuthoritativeStore:
    """Make the post-flush replay visibly authoritative for the kernel test."""

    def __init__(self, log: SessionLog) -> None:
        self.log = log
        self.after_replace = False
        self.replay_override_used = False

    def __getattr__(self, name):
        return getattr(self.log, name)

    def replace_surface(self, messages, **kwargs):
        result = self.log.replace_surface(messages, **kwargs)
        self.after_replace = True
        return result

    def replay(self):
        projection = self.log.replay()
        if not self.after_replace or self.replay_override_used or not projection.messages:
            return projection
        self.replay_override_used = True
        messages = list(projection.messages)
        first = messages[0]
        messages[0] = first.model_copy(
            update={"content": f"{first.content}\n[canonical replay surface]"}
        )
        return SessionProjection(
            messages=messages,
            goal=projection.goal,
            plan=projection.plan,
            todos=projection.todos,
            skills=projection.skills,
        )


class _ReplayFailureStore(_ReplayAuthoritativeStore):
    def replay(self):
        if self.after_replace:
            raise ValueError("replay failed after committed replacement")
        return self.log.replay()


class _NoReplayStore:
    """Third-party SessionStorePort implementing only the minimal contract.

    It delegates the durable operations (append / append_unlogged_messages /
    replace_surface / flush) plus the Agent-layer reads it needs to run a turn
    to a real SessionLog, but deliberately does NOT expose ``replay``. The
    kernel must feature-detect the missing capability and keep the validated
    post-compaction in-memory surface instead of forcing replay(). No
    ``__getattr__`` delegation so ``getattr(store, "replay", None) is None``.
    """

    def __init__(self, log: SessionLog) -> None:
        self._log = log

    def append(self, *args, **kwargs):
        return self._log.append(*args, **kwargs)

    def append_unlogged_messages(self, *args, **kwargs):
        return self._log.append_unlogged_messages(*args, **kwargs)

    def replace_surface(self, *args, **kwargs):
        return self._log.replace_surface(*args, **kwargs)

    def flush(self):
        return self._log.flush()

    @property
    def events(self):
        return self._log.events

    @property
    def failed(self):
        return self._log.failed


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
        # Keep room for tool_search's local/connector schema after compaction,
        # while the long history still forces the real summary path below.
        token_limit=8_000,
        deferred_mcp_loading_enabled=False,
        session_log=log,
    )
    for index in range(24):
        role = "user" if index % 2 == 0 else "assistant"
        agent.messages.append(Message(role=role, content=f"old-{index}:" + "x" * 2_000))
    agent.add_user_message("latest request")
    options = replace(agent.default_run_options(), summary_llm=summary_llm)

    events = [event async for event in agent.run_events(options=options)]

    compactions = [event for event in events if isinstance(event, SummarizationEvent)]
    assert len(compactions) == 1
    assert compactions[0].mode == "summary"
    assert compactions[0].estimated_tokens > 8_000 > compactions[0].estimated_after
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
async def test_compaction_replaces_live_messages_from_post_flush_replay(tmp_path):
    root = tmp_path / "sessions"
    log = SessionLog.create(root, session_id="replay-authoritative", cwd=tmp_path)
    store = _ReplayAuthoritativeStore(log)
    llm = _PostCompactionLLM(log.path)
    summary_llm = _SummaryCheckpointLLM(log.path)
    agent = Agent(
        llm_client=llm,
        system_prompt="system",
        tools=[],
        workspace_dir=str(tmp_path),
        token_limit=8_000,
        deferred_mcp_loading_enabled=False,
        session_log=store,
    )
    for index in range(24):
        role = "user" if index % 2 == 0 else "assistant"
        agent.messages.append(Message(role=role, content=f"old-{index}:" + "x" * 2_000))
    agent.add_user_message("latest request")

    events = [event async for event in agent.run_events(
        options=replace(agent.default_run_options(), summary_llm=summary_llm)
    )]

    assert any(isinstance(event, SummarizationEvent) for event in events)
    assert store.replay_override_used
    assert "[canonical replay surface]" in str(llm.normal_messages[1].content)
    assert "[canonical replay surface]" in str(agent.messages[1].content)
    log.close()


@pytest.mark.asyncio
async def test_compaction_replay_failure_does_not_close_turn_with_old_live_surface(tmp_path):
    root = tmp_path / "sessions"
    log = SessionLog.create(root, session_id="replay-failure", cwd=tmp_path)
    store = _ReplayFailureStore(log)
    agent = Agent(
        llm_client=_PostCompactionLLM(log.path),
        system_prompt="system",
        tools=[],
        workspace_dir=str(tmp_path),
        token_limit=8_000,
        deferred_mcp_loading_enabled=False,
        session_log=store,
    )
    for index in range(24):
        role = "user" if index % 2 == 0 else "assistant"
        agent.messages.append(Message(role=role, content=f"old-{index}:" + "x" * 2_000))
    agent.add_user_message("latest request")

    with pytest.raises(SessionLogReplayError, match="committed"):
        _ = [event async for event in agent.run_events(
            options=replace(agent.default_run_options(), summary_llm=_SummaryCheckpointLLM(log.path))
        )]

    assert "turn/end" not in [event["type"] for event in log.events]
    persisted_messages = log.replay().messages
    assert any(
        "durable compacted history" in str(message.content)
        for message in persisted_messages
    )
    assert all("old-0:" not in str(message.content) for message in persisted_messages)
    log.close()


@pytest.mark.asyncio
async def test_compaction_without_native_replay_keeps_in_memory_surface(tmp_path):
    """SessionStorePort compatibility: a third-party Store that only implements
    the minimal contract (no native ``replay``) must still compact. The kernel
    keeps the validated post-compaction in-memory surface instead of requiring
    replay() (docs/design/skill-engine.md §6)."""
    root = tmp_path / "sessions"
    log = SessionLog.create(root, session_id="no-replay-store", cwd=tmp_path)
    store = _NoReplayStore(log)
    assert getattr(store, "replay", None) is None
    llm = _PostCompactionLLM(log.path)
    summary_llm = _SummaryCheckpointLLM(log.path)
    agent = Agent(
        llm_client=llm,
        system_prompt="system",
        tools=[],
        workspace_dir=str(tmp_path),
        token_limit=8_000,
        deferred_mcp_loading_enabled=False,
        session_log=store,
    )
    for index in range(24):
        role = "user" if index % 2 == 0 else "assistant"
        agent.messages.append(Message(role=role, content=f"old-{index}:" + "x" * 2_000))
    agent.add_user_message("latest request")

    events = [event async for event in agent.run_events(
        options=replace(agent.default_run_options(), summary_llm=summary_llm)
    )]

    # Compaction ran and did not raise SessionLogReplayError.
    assert any(isinstance(event, SummarizationEvent) for event in events)
    assert any(isinstance(event, DoneEvent) for event in events)
    # The live surface came from the in-memory post-compaction messages: it
    # carries the summary and drops the old history, without replay().
    assert any(
        "durable compacted history" in str(message.content)
        for message in agent.messages
    )
    assert all("old-0:" not in str(message.content) for message in agent.messages)
    # The durable surface is still committed via replace_surface + flush.
    assert any(
        "durable compacted history" in str(message.content)
        for message in log.replay().messages
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


@pytest.mark.parametrize("current_prompt", ["trusted skill prompt", "updated skill prompt"])
async def test_active_skill_restores_current_content_as_reference_after_upgrade(tmp_path, current_prompt):
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
    llm = _ReferenceCapturingLLM()
    restored = Agent(
        llm_client=llm,
        system_prompt="system",
        tools=[],
        workspace_dir=str(tmp_path),
        deferred_mcp_loading_enabled=False,
        session_log=restored_log,
    )
    before_restore = restored_log.path.read_bytes()
    restored.restore_active_skill_instructions(
        [
            (
                "review",
                current_prompt,
                persisted[0]["sha256"],
                persisted[0]["loadOrder"],
            )
        ]
    )

    current_hash = sha256(current_prompt.encode()).hexdigest()
    assert current_prompt not in restored.system_prompt
    assert restored.skill_runtime.state.reads["review"].revision == current_hash
    assert restored_log.path.read_bytes() == before_restore
    before = restored_log.path.read_bytes()
    before_state = restored.skill_runtime.log_records()
    before_sequence = restored.skill_runtime.state.sequence
    restored.activate_skill_instructions("review", current_prompt)
    assert restored_log.path.read_bytes() == before
    assert restored.skill_runtime.log_records() == before_state
    assert restored.skill_runtime.state.sequence == before_sequence

    restored.activate_skill_instructions("another-skill", "another prompt")
    persisted_current = restored_log.replay().skills[0]
    assert persisted_current["name"] == "review"
    assert persisted_current["sha256"] == current_hash
    assert persisted_current["loadOrder"] == persisted[0]["loadOrder"]
    restored.add_user_message("continue review")
    await restored.run()
    assert current_prompt not in llm.requests[0][0].content
    assert current_prompt in str(llm.requests[0][-1].content)
    if current_prompt != "trusted skill prompt":
        assert persisted[0]["sha256"] in str(llm.requests[0][-1].content)
        assert current_hash in str(llm.requests[0][-1].content)
        assert "historical" in str(llm.requests[0][-1].content)
    user_turns = [
        message.content for message in restored.messages
        if message.role == "user" and message.source != "runtime"
    ]
    assert user_turns[-1] == "continue review"
    durable_skill_messages = [
        str(message.content) for message in restored.messages
        if message.role == "user" and message.source == "runtime"
    ]
    assert durable_skill_messages
    assert any("another prompt" in body for body in durable_skill_messages)
    assert all(current_prompt not in body for body in durable_skill_messages)
    restored_log.close()


@pytest.mark.parametrize("legacy_record", [False, True])
def test_agent_constructor_restores_loader_skill_without_appending_load_events(tmp_path, legacy_record):
    loader = _make_reference_loader(tmp_path)
    root = tmp_path / "sessions"
    log = SessionLog.create(root, session_id="loader-restore", cwd=tmp_path)
    original = Agent(llm_client=_ReferenceCapturingLLM(), system_prompt="system",
                     tools=[GetSkillTool(loader)], workspace_dir=str(tmp_path), session_log=log)
    assert original.skill_runtime.read("review").success
    persisted = log.replay().skills
    if legacy_record:
        persisted = [{key: record[key] for key in ("name", "sha256", "loadOrder")} for record in persisted]
        log.append("skill/change", {"skills": persisted})
        log.flush()
    log.close()
    restored_log = SessionLog.open(root, session_id="loader-restore", cwd=tmp_path)
    before = restored_log.path.read_bytes()

    restored = Agent(llm_client=_ReferenceCapturingLLM(), system_prompt="system",
                     tools=[GetSkillTool(loader)], workspace_dir=str(tmp_path), session_log=restored_log)

    record = restored.skill_runtime.state.reads["review"]
    assert record.revision == persisted[0]["sha256"]
    assert record.source == persisted[0].get("source", "builtin") == "builtin"
    assert record.path == str(loader.get_skill("review").skill_path)
    assert record.prompt == loader.get_skill("review").to_prompt()
    assert record.reason == "restored"
    assert "EXACT-REVIEW-METHOD" not in restored.system_prompt
    assert restored_log.path.read_bytes() == before
    assert restored_log.replay().skills == persisted
    restored_log.close()


@pytest.mark.parametrize("legacy_record", [False, True])
@pytest.mark.parametrize("change", ["hash", "source"])
async def test_agent_restore_uses_current_skill_and_reports_known_changes_without_rewriting_log(
    tmp_path, change, legacy_record,
):
    loader = _make_reference_loader(tmp_path)
    log = SessionLog.create(tmp_path / "sessions", session_id="changed-skill", cwd=tmp_path)
    original = Agent(llm_client=_ReferenceCapturingLLM(), system_prompt="system",
                     tools=[GetSkillTool(loader)], workspace_dir=str(tmp_path), session_log=log)
    assert original.skill_runtime.read("review").success
    original_records = log.replay().skills
    if legacy_record:
        original_records = [{key: record[key] for key in ("name", "sha256", "loadOrder")}
                            for record in original_records]
        log.append("skill/change", {"skills": original_records})
    log.flush()
    before = log.path.read_bytes()
    if change == "hash":
        path = tmp_path / "skills" / "review" / "SKILL.md"
        path.write_text(path.read_text().replace("EXACT-REVIEW-METHOD", "CHANGED-METHOD"))
    changed_loader = SkillLoader(sources=[(tmp_path / "skills", "user" if change == "source" else "builtin")])
    changed_loader.discover_skills()
    if change == "source":
        # A source label can change while path and rendered body hash stay equal.
        assert sha256(changed_loader.get_skill("review").to_prompt().encode()).hexdigest() == original_records[0]["sha256"]

    llm = _ReferenceCapturingLLM()
    restored = Agent(llm_client=llm, system_prompt="system", tools=[GetSkillTool(changed_loader)],
                     workspace_dir=str(tmp_path), session_log=log)

    assert log.path.read_bytes() == before
    assert log.replay().skills == original_records
    current = changed_loader.get_skill("review")
    current_hash = sha256(current.to_prompt().encode()).hexdigest()
    record = restored.skill_runtime.state.reads["review"]
    assert record.prompt == current.to_prompt()
    assert record.revision == current_hash
    assert record.source == current.source
    assert record.order == original_records[0]["loadOrder"]
    if change == "hash" or not legacy_record:
        assert record.delivered_ranges == ()
        assert not record.delivered_complete

    before_state = restored.skill_runtime.log_records()
    before_sequence = restored.skill_runtime.state.sequence
    restored.activate_skill_instructions("review", current.to_prompt())
    assert log.path.read_bytes() == before
    assert restored.skill_runtime.log_records() == before_state
    assert restored.skill_runtime.state.sequence == before_sequence
    restored.add_user_message("continue review")
    await restored.run()
    reference = str(llm.requests[0][-1].content)
    assert current.to_prompt() in reference.replace("\\n", "\n")
    assert current.to_prompt() not in llm.requests[0][0].content
    if change == "hash" or not legacy_record:
        assert original_records[0]["sha256"] in reference
        assert current_hash in reference
        assert "historical" in reference
    if change == "hash":
        assert "EXACT-REVIEW-METHOD" not in reference
    elif not legacy_record:
        assert "builtin" in reference and "user" in reference
    assert log.replay().skills[0]["sha256"] == current_hash
    assert log.replay().skills[0]["source"] == current.source
    log.close()


async def test_restored_loader_skill_can_be_rediscovered_in_first_real_request(tmp_path):
    loader = _make_reference_loader(tmp_path)
    log = SessionLog.create(tmp_path / "sessions", session_id="rediscover", cwd=tmp_path)
    original = Agent(llm_client=_ReferenceCapturingLLM(), system_prompt="system",
                     tools=[GetSkillTool(loader)], workspace_dir=str(tmp_path), session_log=log)
    assert original.skill_runtime.read("review").success
    llm = _ReferenceCapturingLLM()
    restored = Agent(llm_client=llm, system_prompt="system", tools=[GetSkillTool(loader)],
                     workspace_dir=str(tmp_path), session_log=log)
    restored.add_user_message("Continue the earlier task")

    await restored.run()

    assert "EXACT-REVIEW-METHOD" not in llm.requests[0][0].content
    assert "review" in str(llm.requests[0])
    assert restored.messages[-2].content == "Continue the earlier task"
    log.close()


def test_legacy_restore_api_uses_configured_loader_instead_of_supplied_historical_body(tmp_path):
    loader = _make_reference_loader(tmp_path)
    restored = Agent(llm_client=_ReferenceCapturingLLM(), system_prompt="system",
                     tools=[GetSkillTool(loader)], workspace_dir=str(tmp_path))
    restored.restore_active_skill_instructions([("review", "historical inline text", sha256(b"historical inline text").hexdigest(), 7)])
    record = restored.skill_runtime.state.reads["review"]
    assert record.prompt == loader.get_skill("review").to_prompt()
    assert record.revision == sha256(record.prompt.encode()).hexdigest()
    assert record.source == "builtin"
    assert record.order == 7
    assert not record.delivered_complete


async def test_new_skill_reference_log_is_readable_by_pr1_projection(tmp_path):
    current_root = Path(__file__).resolve().parents[1]
    legacy_sha = "3e83bb3b287e244dd1a2887fd4bc14be2eb19e32"
    configured = os.environ.get("BOX_AGENT_LEGACY_WORKTREE")
    legacy_root = Path(configured) if configured else tmp_path / "legacy-reader"
    if configured:
        actual_sha = subprocess.run(["git", "-C", str(legacy_root), "rev-parse", "HEAD"],
                                    check=True, capture_output=True, text=True).stdout.strip()
        assert actual_sha == legacy_sha
    else:
        # Load the actual old reader and its schema from fixed Git objects.
        # An empty package initializer avoids importing unrelated old Agent
        # code; none of the reader/schema implementation is substituted.
        for path in ("box_agent/session_log.py", "box_agent/schema/__init__.py", "box_agent/schema/schema.py"):
            original = subprocess.run(["git", "-C", str(current_root), "show", f"{legacy_sha}:{path}"],
                                      capture_output=True, timeout=10)
            if original.returncode:
                pytest.skip("Legacy Git objects unavailable; supply BOX_AGENT_LEGACY_WORKTREE for cross-version replay")
            destination = legacy_root / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(original.stdout)
        (legacy_root / "box_agent" / "__init__.py").write_text("", encoding="utf-8")
    loader = _make_reference_loader(tmp_path)
    root = tmp_path / "sessions"
    log = SessionLog.create(root, session_id="new-skill-old-reader", cwd=tmp_path)
    agent = Agent(llm_client=_ReferenceCapturingLLM(), system_prompt="system",
                  tools=[GetSkillTool(loader)], workspace_dir=str(tmp_path), session_log=log)
    agent.skill_runtime.select(["review"])
    agent.add_user_message("Review this input")
    await agent.run()
    expected = log.replay()
    # Explicit selections are durable runtime messages now.  They are replayable
    # by the old reader through the normal user-message surface and no longer
    # require the legacy request/context skillReferences side channel.
    assert not any(
        event["type"] == "request/context" and "skillReferences" in event["data"]
        for event in log.events
    )
    before = log.path.read_bytes()
    log.close()
    script = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from box_agent.session_log import SessionLog
log = SessionLog.open(Path(sys.argv[2]), session_id=sys.argv[3], cwd=Path(sys.argv[4]))
projection = log.replay()
print(json.dumps({"skills": projection.skills, "messages": [[m.role, m.content] for m in projection.messages]}))
log.close()
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script, str(legacy_root), str(root), "new-skill-old-reader", str(tmp_path)],
        cwd=tmp_path, capture_output=True, text=True, timeout=20, check=True,
    )
    replayed = json.loads(result.stdout)
    assert replayed["skills"] == expected.skills
    assert replayed["messages"] == [[message.role, message.content] for message in expected.messages]
    assert log.path.read_bytes() == before


@pytest.mark.parametrize("failure", ["append", "flush"])
def test_clear_history_keeps_live_messages_when_reset_commit_fails(tmp_path, monkeypatch, failure):
    from box_agent.agent import Agent
    from box_agent.schema import Message

    log = SessionLog.create(tmp_path / "sessions", session_id="reset-failure", cwd=tmp_path)
    agent = Agent(llm_client=object(), system_prompt="system", tools=[], session_log=log,
                  workspace_dir=str(tmp_path), deferred_mcp_loading_enabled=False)
    agent.messages.append(Message(role="user", content="keep this history"))
    original = [message.model_copy(deep=True) for message in agent.messages]

    def fail(*args, **kwargs):
        log._failed = True
        raise OSError("reset write failed")

    with monkeypatch.context() as patch:
        patch.setattr(log, failure, fail)
        with pytest.raises(OSError, match="reset write failed"):
            agent.clear_history()
    assert agent.messages == original
    log.close()

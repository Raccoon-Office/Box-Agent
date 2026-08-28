"""Observable turn-boundary behavior for trusted interactive tools."""

from __future__ import annotations

from pathlib import Path

import pytest

from box_agent.agent import Agent, should_continue_goal_autopilot
from box_agent.events import DoneEvent, StopReason, ToolCallResult
from box_agent.hooks import BaseHook
from box_agent.runtime import run_agent_loop
from box_agent.schema import FunctionCall, LLMResponse, Message, StreamEvent, ToolCall
from box_agent.session_log import SessionLog
from box_agent.tools.base import Tool, ToolResult
from box_agent.tools.request_user_input_tool import RequestUserInputTool


class _SequenceLLM:
    model = "test-model"
    max_output_tokens = 1_024

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = responses
        self.calls = 0

    async def generate_stream(self, **_kwargs):
        response = self._responses[self.calls]
        self.calls += 1
        if response.content:
            yield StreamEvent(type="text", delta=response.content)
        yield StreamEvent(
            type="finish",
            finish_reason=response.finish_reason,
            tool_calls=response.tool_calls,
        )


class _RecordingTool(Tool):
    parallel_safe = True

    def __init__(self, name: str) -> None:
        self._name = name
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "Record an observable test side effect."

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "additionalProperties": False}

    async def execute(self) -> ToolResult:
        self.calls += 1
        return ToolResult(success=True, content=f"{self.name} executed")


class _DoneHook(BaseHook):
    def __init__(self) -> None:
        self.reasons: list[StopReason] = []

    async def on_done(self, *, stop_reason, final_content):
        self.reasons.append(stop_reason)


def _call(call_id: str, name: str, arguments: dict | None = None) -> ToolCall:
    return ToolCall(
        id=call_id,
        type="function",
        function=FunctionCall(name=name, arguments=arguments or {}),
    )


def _messages() -> list[Message]:
    return [
        Message(role="system", content="system"),
        Message(role="user", content="do the task"),
    ]


async def _collect(loop) -> list:
    return [event async for event in loop]


@pytest.mark.asyncio
async def test_successful_interactive_tool_skips_only_later_siblings():
    before = _RecordingTool("before")
    after = _RecordingTool("after")
    llm = _SequenceLLM(
        [
            LLMResponse(
                content="",
                finish_reason="tool",
                tool_calls=[
                    _call("before-1", "before"),
                    _call(
                        "ask-1",
                        "request_user_input",
                        {
                            "question": "Which value should I use?",
                            "missing_fields": ["value"],
                        },
                    ),
                    _call("after-1", "after"),
                ],
            )
        ]
    )

    events = await _collect(
        run_agent_loop(
            llm=llm,
            messages=_messages(),
            tools={
                "before": before,
                "request_user_input": RequestUserInputTool(),
                "after": after,
            },
            max_steps=3,
        )
    )

    assert llm.calls == 1
    assert before.calls == 1
    assert after.calls == 0
    skipped = next(
        event
        for event in events
        if isinstance(event, ToolCallResult) and event.tool_call_id == "after-1"
    )
    assert skipped.success is False
    assert "interactive tool 'request_user_input'" in (skipped.error or "")
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.stop_reason is StopReason.WAITING_FOR_USER


@pytest.mark.asyncio
async def test_failed_interactive_tool_allows_model_to_continue():
    llm = _SequenceLLM(
        [
            LLMResponse(
                content="",
                finish_reason="tool",
                tool_calls=[
                    _call(
                        "ask-invalid",
                        "request_user_input",
                        {"question": "Need input?", "missing_fields": []},
                    )
                ],
            ),
            LLMResponse(content="I could not request input.", finish_reason="stop"),
        ]
    )

    events = await _collect(
        run_agent_loop(
            llm=llm,
            messages=_messages(),
            tools={"request_user_input": RequestUserInputTool()},
            max_steps=3,
        )
    )

    assert llm.calls == 2
    result = next(event for event in events if isinstance(event, ToolCallResult))
    assert result.success is False
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.stop_reason is StopReason.END_TURN


@pytest.mark.asyncio
async def test_ordinary_tool_does_not_end_turn_on_success():
    ordinary = _RecordingTool("ordinary")
    llm = _SequenceLLM(
        [
            LLMResponse(
                content="",
                finish_reason="tool",
                tool_calls=[_call("ordinary-1", "ordinary")],
            ),
            LLMResponse(content="done", finish_reason="stop"),
        ]
    )

    events = await _collect(
        run_agent_loop(
            llm=llm,
            messages=_messages(),
            tools={"ordinary": ordinary},
            max_steps=3,
        )
    )

    assert ordinary.ends_turn_on_success is False
    assert ordinary.calls == 1
    assert llm.calls == 2
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.stop_reason is StopReason.END_TURN


@pytest.mark.asyncio
async def test_waiting_reason_is_durable_and_next_user_message_resumes(tmp_path: Path):
    root = tmp_path / "sessions"
    session_log = SessionLog.create(root, session_id="waiting", cwd=tmp_path)
    hook = _DoneHook()
    llm = _SequenceLLM(
        [
            LLMResponse(
                content="",
                finish_reason="tool",
                tool_calls=[
                    _call(
                        "ask-1",
                        "request_user_input",
                        {
                            "question": "Which value should I use?",
                            "missing_fields": ["value"],
                        },
                    )
                ],
            ),
            LLMResponse(content="resumed with the answer", finish_reason="stop"),
        ]
    )
    agent = Agent(
        llm_client=llm,
        system_prompt="system",
        tools=[RequestUserInputTool()],
        workspace_dir=str(tmp_path),
        deferred_mcp_loading_enabled=False,
        session_log=session_log,
        hooks=[hook],
    )
    agent.add_user_message("start")

    first = [event async for event in agent.run_events()]
    first_done = next(event for event in first if isinstance(event, DoneEvent))
    assert first_done.stop_reason is StopReason.WAITING_FOR_USER
    assert hook.reasons == [StopReason.WAITING_FOR_USER]
    first_turn_end = [
        event for event in session_log.events if event["type"] == "turn/end"
    ][-1]
    assert first_turn_end["data"]["reason"] == {"kind": "waiting_for_user"}

    agent.add_user_message("Use value 42.")
    second = [event async for event in agent.run_events()]
    second_done = next(event for event in second if isinstance(event, DoneEvent))
    assert second_done.stop_reason is StopReason.END_TURN
    assert llm.calls == 2
    session_log.close()

    restored = SessionLog.open(root, session_id="waiting", cwd=tmp_path)
    assert any(
        message.role == "assistant" and message.content == "resumed with the answer"
        for message in restored.replay().messages
    )
    restored.close()


def test_goal_autopilot_does_not_cross_waiting_for_user(tmp_path: Path):
    agent = Agent(
        llm_client=_SequenceLLM([]),
        system_prompt="system",
        tools=[],
        workspace_dir=str(tmp_path),
        deferred_mcp_loading_enabled=False,
    )
    agent.set_goal("wait for a real answer")

    assert should_continue_goal_autopilot(agent, "waiting_for_user") is False

"""Shared-loop recovery after a provider drops an in-progress response."""

import asyncio

import pytest

from box_agent.events import (
    ContentEvent, DoneEvent, ErrorEvent, InjectedMessageEvent, ProgressEvent, StopReason,
)
from box_agent.retry import StreamInterrupted
from box_agent.runtime import run_agent_loop
from box_agent.schema import FunctionCall, LLMResponse, Message, StreamEvent, ToolCall
from box_agent.session_log import SessionLog
from box_agent.tools.base import Tool, ToolResult


class ScriptedStream:
    def __init__(self, scripts):
        self.scripts = scripts
        self.requests = []

    async def generate(self, *args, **kwargs):
        return LLMResponse(content='{"continue":false}', finish_reason="stop")

    async def generate_stream(self, messages, **kwargs):
        script = self.scripts[len(self.requests)]
        self.requests.append([message.model_copy(deep=True) for message in messages])
        for item in script:
            if isinstance(item, Exception):
                raise item
            yield item


def dropped(text):
    return [
        StreamEvent(type="text", delta=text),
        StreamEvent(type="activity", activity={
            "phase": "tool_arguments", "tool_name": "record", "argument_chars": 10,
        }),
        StreamInterrupted(ConnectionError("connection reset"), partial_text=text),
    ]


def tool_call(value):
    return StreamEvent(type="finish", finish_reason="tool_calls", tool_calls=[
        ToolCall(id=value, type="function", function=FunctionCall(
            name="record", arguments={"value": value},
        )),
    ])


class RecordTool(Tool):
    name = "record"
    description = "Record a value."

    def __init__(self):
        self.values = []

    @property
    def parameters(self):
        return {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}

    async def execute(self, value):
        self.values.append(value)
        return ToolResult(success=True, content=f"Recorded {value}")


def messages():
    return [Message(role="system", content="system"), Message(role="user", content="Record before and after.")]


@pytest.mark.asyncio
async def test_recovery_keeps_completed_tools_and_persists_partial_context(tmp_path):
    llm = ScriptedStream([
        [tool_call("before")],
        dropped("先写大纲并校验。"),
        [tool_call("after")],
        [StreamEvent(type="text", delta="已完成。"), StreamEvent(type="finish", finish_reason="stop")],
    ])
    tool = RecordTool()
    history = messages()
    session_log = SessionLog.create(tmp_path / "sessions", session_id="recovery", cwd=tmp_path)
    try:
        session_log.append("turn/start", {"turn": 1})
        session_log.append_unlogged_messages(history[1:], turn=1, step=None)
        events = [event async for event in run_agent_loop(
            llm=llm, messages=history, tools={tool.name: tool}, max_steps=6,
            workspace_dir=str(tmp_path), session_log=session_log, session_turn=1,
        )]
        replayed = session_log.replay().messages
        assert any(message.content == "先写大纲并校验。" for message in replayed)
        assert any("System recovery" in str(message.content) for message in replayed)
    finally:
        session_log.close()

    assert tool.values == ["before", "after"]
    retry_history = llm.requests[2]
    assert any(message.role == "tool" and message.content == "Recorded before" for message in retry_history)
    assert retry_history[-2].content == "先写大纲并校验。"
    assert retry_history[-2].tool_calls is None
    assert "not executed" in retry_history[-1].content
    assert "".join(event.content for event in events if isinstance(event, ContentEvent)) == "先写大纲并校验。已完成。"
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert sum(isinstance(event, ProgressEvent) for event in events) == 1
    assert next(event for event in events if isinstance(event, DoneEvent)).stop_reason is StopReason.END_TURN


@pytest.mark.asyncio
@pytest.mark.parametrize("between", [False, True])
async def test_recovery_is_limited_to_one_attempt_per_turn(between):
    scripts = [dropped("第一段。")]
    if between:
        scripts.append([tool_call("saved")])
    scripts.append(dropped("第二段。"))
    llm = ScriptedStream(scripts)
    tool = RecordTool()
    history = messages()
    events = [event async for event in run_agent_loop(
        llm=llm, messages=history, tools={tool.name: tool}, max_steps=8,
    )]
    assert len(llm.requests) == len(scripts)
    assert sum(isinstance(event, InjectedMessageEvent) for event in events) == 1
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert len(errors) == 1 and "任务尚未完成" in errors[0].message
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.stop_reason is StopReason.INTERRUPTED
    assert done.final_content == "第二段。"


@pytest.mark.asyncio
async def test_recovery_does_not_exceed_step_budget():
    llm = ScriptedStream([dropped("未完成。")])
    events = [event async for event in run_agent_loop(
        llm=llm, messages=messages(), tools={}, max_steps=1,
    )]
    assert len(llm.requests) == 1
    injected = [event for event in events if isinstance(event, InjectedMessageEvent)]
    assert len(injected) == 1
    assert "最后一步交付提醒" in injected[0].content
    assert injected[0].user_visible is False
    assert next(event for event in events if isinstance(event, DoneEvent)).stop_reason is StopReason.INTERRUPTED


@pytest.mark.asyncio
async def test_cancellation_during_recovery_prevents_another_model_request():
    llm = ScriptedStream([dropped("未完成。")])
    cancelled = asyncio.Event()
    events = []
    async for event in run_agent_loop(
        llm=llm, messages=messages(), tools={}, max_steps=5,
        is_cancelled=cancelled.is_set,
    ):
        events.append(event)
        if isinstance(event, ProgressEvent):
            cancelled.set()
    assert len(llm.requests) == 1
    assert next(event for event in events if isinstance(event, DoneEvent)).stop_reason is StopReason.CANCELLED


@pytest.mark.asyncio
async def test_cancellation_racing_with_disconnect_stays_cancelled():
    cancelled = asyncio.Event()

    class CancellingStream(ScriptedStream):
        async def generate_stream(self, *args, **kwargs):
            yield StreamEvent(type="text", delta="未完成。")
            cancelled.set()
            raise StreamInterrupted(ConnectionError("connection reset"), partial_text="未完成。")

    events = [event async for event in run_agent_loop(
        llm=CancellingStream([]), messages=messages(), tools={}, max_steps=5,
        is_cancelled=cancelled.is_set,
    )]
    assert not any(isinstance(event, (InjectedMessageEvent, ErrorEvent)) for event in events)
    assert next(event for event in events if isinstance(event, DoneEvent)).stop_reason is StopReason.CANCELLED

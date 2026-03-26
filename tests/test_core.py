"""Tests for the shared agent execution core (box_agent.core)."""

import asyncio

import pytest

from box_agent.core import _detect_artifacts, run_agent_loop
from box_agent.events import (
    ArtifactEvent,
    ContentEvent,
    DoneEvent,
    ErrorEvent,
    StepEnd,
    StepStart,
    StopReason,
    ThinkingEvent,
    ToolCallResult,
    ToolCallStart,
)
from box_agent.schema import FunctionCall, LLMResponse, Message, ToolCall
from box_agent.tools.base import Tool, ToolResult


# ── Helpers ─────────────────────────────────────────────────────


class MockLLM:
    """Deterministic LLM that yields pre-configured responses in order."""

    def __init__(self, responses: list[LLMResponse]):
        self._responses = list(responses)
        self._idx = 0

    async def generate(self, messages, tools=None):
        resp = self._responses[self._idx]
        self._idx += 1
        return resp


class EchoTool(Tool):
    @property
    def name(self):
        return "echo"

    @property
    def description(self):
        return "Echoes text back"

    @property
    def parameters(self):
        return {"type": "object", "properties": {"text": {"type": "string"}}}

    async def execute(self, text: str = ""):
        return ToolResult(success=True, content=f"echo:{text}")


class FailTool(Tool):
    @property
    def name(self):
        return "fail"

    @property
    def description(self):
        return "Always fails"

    @property
    def parameters(self):
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        raise RuntimeError("boom")


async def collect(gen) -> list:
    return [ev async for ev in gen]


def _msgs():
    return [
        Message(role="system", content="sys"),
        Message(role="user", content="hi"),
    ]


# ── Tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_simple_conversation():
    """No tool calls — should yield StepStart, Content, StepEnd, Done."""
    llm = MockLLM([LLMResponse(content="hello", finish_reason="stop")])
    events = await collect(run_agent_loop(llm=llm, messages=_msgs(), tools={}, max_steps=5))

    types = [type(e) for e in events]
    assert StepStart in types
    assert ContentEvent in types
    assert StepEnd in types
    assert DoneEvent in types

    done = [e for e in events if isinstance(e, DoneEvent)][0]
    assert done.stop_reason == StopReason.END_TURN
    assert done.final_content == "hello"


@pytest.mark.asyncio
async def test_thinking_event():
    """LLM with thinking should yield ThinkingEvent."""
    llm = MockLLM([LLMResponse(content="ok", thinking="let me think", finish_reason="stop")])
    events = await collect(run_agent_loop(llm=llm, messages=_msgs(), tools={}, max_steps=5))

    thinking = [e for e in events if isinstance(e, ThinkingEvent)]
    assert len(thinking) == 1
    assert thinking[0].content == "let me think"


@pytest.mark.asyncio
async def test_tool_call_cycle():
    """One tool call then a final response."""
    llm = MockLLM([
        LLMResponse(
            content="calling tool",
            tool_calls=[ToolCall(id="t1", type="function", function=FunctionCall(name="echo", arguments={"text": "ping"}))],
            finish_reason="tool",
        ),
        LLMResponse(content="done", finish_reason="stop"),
    ])
    events = await collect(run_agent_loop(llm=llm, messages=_msgs(), tools={"echo": EchoTool()}, max_steps=5))

    starts = [e for e in events if isinstance(e, ToolCallStart)]
    results = [e for e in events if isinstance(e, ToolCallResult)]
    assert len(starts) == 1
    assert starts[0].tool_name == "echo"
    assert len(results) == 1
    assert results[0].success is True
    assert "echo:ping" in results[0].content


@pytest.mark.asyncio
async def test_unknown_tool():
    """Tool call to non-existent tool yields ToolCallResult(success=False)."""
    llm = MockLLM([
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="t1", type="function", function=FunctionCall(name="nope", arguments={}))],
            finish_reason="tool",
        ),
        LLMResponse(content="ok", finish_reason="stop"),
    ])
    events = await collect(run_agent_loop(llm=llm, messages=_msgs(), tools={"echo": EchoTool()}, max_steps=5))

    results = [e for e in events if isinstance(e, ToolCallResult)]
    assert len(results) == 1
    assert results[0].success is False
    assert "Unknown tool" in (results[0].error or "")


@pytest.mark.asyncio
async def test_tool_exception():
    """Tool that raises should yield ToolCallResult(success=False), not crash."""
    llm = MockLLM([
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="t1", type="function", function=FunctionCall(name="fail", arguments={}))],
            finish_reason="tool",
        ),
        LLMResponse(content="recovered", finish_reason="stop"),
    ])
    events = await collect(run_agent_loop(llm=llm, messages=_msgs(), tools={"fail": FailTool()}, max_steps=5))

    results = [e for e in events if isinstance(e, ToolCallResult)]
    assert len(results) == 1
    assert results[0].success is False
    assert "boom" in (results[0].error or "")


@pytest.mark.asyncio
async def test_cancellation_at_step_start():
    """Cancellation before first LLM call yields Done(CANCELLED)."""
    llm = MockLLM([LLMResponse(content="should not reach", finish_reason="stop")])
    events = await collect(
        run_agent_loop(llm=llm, messages=_msgs(), tools={}, max_steps=5, is_cancelled=lambda: True)
    )

    done = [e for e in events if isinstance(e, DoneEvent)]
    assert len(done) == 1
    assert done[0].stop_reason == StopReason.CANCELLED


@pytest.mark.asyncio
async def test_cancellation_after_tool():
    """Cancellation after a tool call stops the loop."""
    tool_executed = []

    class TrackingEchoTool(Tool):
        @property
        def name(self):
            return "echo"

        @property
        def description(self):
            return "Echoes text"

        @property
        def parameters(self):
            return {"type": "object", "properties": {"text": {"type": "string"}}}

        async def execute(self, text: str = ""):
            tool_executed.append(True)
            return ToolResult(success=True, content=f"echo:{text}")

    llm = MockLLM([
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="t1", type="function", function=FunctionCall(name="echo", arguments={"text": "x"}))],
            finish_reason="tool",
        ),
        LLMResponse(content="unreachable", finish_reason="stop"),
    ])
    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"echo": TrackingEchoTool()},
            max_steps=5,
            is_cancelled=lambda: len(tool_executed) > 0,  # cancel once tool has run
        )
    )

    done = [e for e in events if isinstance(e, DoneEvent)]
    assert len(done) == 1
    assert done[0].stop_reason == StopReason.CANCELLED


@pytest.mark.asyncio
async def test_max_steps():
    """Reaching max_steps yields Done(MAX_STEPS)."""
    # Each response has a tool call, so the loop continues
    responses = [
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id=f"t{i}", type="function", function=FunctionCall(name="echo", arguments={"text": str(i)}))],
            finish_reason="tool",
        )
        for i in range(3)
    ]
    llm = MockLLM(responses)
    events = await collect(
        run_agent_loop(llm=llm, messages=_msgs(), tools={"echo": EchoTool()}, max_steps=3)
    )

    done = [e for e in events if isinstance(e, DoneEvent)]
    assert len(done) == 1
    assert done[0].stop_reason == StopReason.MAX_STEPS


@pytest.mark.asyncio
async def test_llm_error():
    """LLM exception yields ErrorEvent + Done(ERROR)."""

    class FailLLM:
        async def generate(self, messages, tools=None):
            raise ConnectionError("network down")

    events = await collect(run_agent_loop(llm=FailLLM(), messages=_msgs(), tools={}, max_steps=5))

    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(errors) == 1
    assert errors[0].is_fatal
    assert "network down" in errors[0].message

    done = [e for e in events if isinstance(e, DoneEvent)]
    assert done[0].stop_reason == StopReason.ERROR


@pytest.mark.asyncio
async def test_messages_mutated_in_place():
    """Core appends assistant + tool messages to the passed-in list."""
    msgs = _msgs()
    llm = MockLLM([
        LLMResponse(
            content="using tool",
            tool_calls=[ToolCall(id="t1", type="function", function=FunctionCall(name="echo", arguments={"text": "hi"}))],
            finish_reason="tool",
        ),
        LLMResponse(content="done", finish_reason="stop"),
    ])
    await collect(run_agent_loop(llm=llm, messages=msgs, tools={"echo": EchoTool()}, max_steps=5))

    roles = [m.role for m in msgs]
    # system, user, assistant (tool call), tool, assistant (final)
    assert roles == ["system", "user", "assistant", "tool", "assistant"]


# ── Artifact detection tests ─────────────────────────────────


def test_artifact_detect_workspace_root(tmp_path):
    """File at workspace root is found."""
    (tmp_path / "chart.png").write_bytes(b"\x89PNG")
    arts = _detect_artifacts("t1", "jupyter", "Here is the result [chart.png]", str(tmp_path))
    assert len(arts) == 1
    assert arts[0].filename == "chart.png"
    assert arts[0].artifact_type == "image"
    assert arts[0].mime_type == "image/png"
    assert arts[0].size_bytes == 4


def test_artifact_detect_sandbox_session_subdir(tmp_path):
    """File at workspace/sandbox/<session_id>/ is found (Jupyter's actual path)."""
    session_dir = tmp_path / "sandbox" / "abc123"
    session_dir.mkdir(parents=True)
    (session_dir / "output.csv").write_text("a,b\n1,2")
    arts = _detect_artifacts("t2", "jupyter", "Saved to [output.csv]", str(tmp_path))
    assert len(arts) == 1
    assert arts[0].filename == "output.csv"
    assert arts[0].artifact_type == "file"
    assert "csv" in arts[0].mime_type


def test_artifact_detect_no_match(tmp_path):
    """No artifact when file doesn't exist."""
    arts = _detect_artifacts("t3", "jupyter", "See [missing.png]", str(tmp_path))
    assert arts == []


def test_artifact_detect_multiple(tmp_path):
    """Multiple file references in one output."""
    (tmp_path / "a.png").write_bytes(b"\x89PNG")
    (tmp_path / "b.pdf").write_bytes(b"%PDF")
    arts = _detect_artifacts("t4", "jupyter", "Results: [a.png] and [b.pdf]", str(tmp_path))
    assert len(arts) == 2
    names = {a.filename for a in arts}
    assert names == {"a.png", "b.pdf"}

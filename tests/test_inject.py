"""Tests for in-stream message injection (inject_queue)."""

import asyncio

import pytest

from box_agent.core import run_agent_loop
from box_agent.events import (
    ContentEvent,
    DoneEvent,
    InjectedMessageEvent,
    StepEnd,
    StepStart,
    StopReason,
    ToolCallResult,
    ToolCallStart,
)
from box_agent.schema import FunctionCall, LLMResponse, Message, StreamEvent, ToolCall
from box_agent.tools.base import Tool, ToolResult


# ── Helpers ─────────────────────────────────────────────────────


class MockLLM:
    """Deterministic LLM that yields pre-configured responses in order."""

    def __init__(self, responses: list[LLMResponse]):
        self._responses = list(responses)
        self._idx = 0

    async def generate_stream(self, messages, tools=None):
        resp = self._responses[self._idx]
        self._idx += 1
        if resp.thinking:
            yield StreamEvent(type="thinking", delta=resp.thinking)
        if resp.content:
            yield StreamEvent(type="text", delta=resp.content)
        yield StreamEvent(
            type="finish",
            finish_reason=resp.finish_reason,
            usage=resp.usage,
            tool_calls=resp.tool_calls,
        )


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


async def collect(gen) -> list:
    return [ev async for ev in gen]


def _msgs():
    return [
        Message(role="system", content="sys"),
        Message(role="user", content="hi"),
    ]


# ── Tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inject_at_step_boundary():
    """Injected message is drained at step boundary and appears in messages."""
    queue: asyncio.Queue[str] = asyncio.Queue()
    msgs = _msgs()

    # Step 1: tool call, step 2: final response
    llm = MockLLM([
        LLMResponse(
            content="calling tool",
            tool_calls=[ToolCall(id="t1", type="function", function=FunctionCall(name="echo", arguments={"text": "a"}))],
            finish_reason="tool",
        ),
        LLMResponse(content="done", finish_reason="stop"),
    ])

    # Pre-load injection so it's ready when step 2 starts
    await queue.put("extra context here")

    events = await collect(
        run_agent_loop(llm=llm, messages=msgs, tools={"echo": EchoTool()}, max_steps=5, inject_queue=queue)
    )

    # InjectedMessageEvent should appear in events
    injected = [e for e in events if isinstance(e, InjectedMessageEvent)]
    assert len(injected) == 1
    assert injected[0].content == "extra context here"

    # The injected message should be in the message history
    user_msgs = [m for m in msgs if m.role == "user"]
    assert any("extra context" in m.content for m in user_msgs)


@pytest.mark.asyncio
async def test_no_tool_calls_continues_with_injection():
    """When LLM wants to stop but inject queue has content, loop continues."""
    queue: asyncio.Queue[str] = asyncio.Queue()
    msgs = _msgs()

    class InjectDuringStreamLLM:
        """LLM that injects a message into the queue during its first streaming response."""

        def __init__(self, responses, inject_queue):
            self._responses = list(responses)
            self._idx = 0
            self._queue = inject_queue

        async def generate_stream(self, messages, tools=None):
            resp = self._responses[self._idx]
            self._idx += 1
            if resp.content:
                yield StreamEvent(type="text", delta=resp.content)
            # Inject after first response streaming (simulates user typing during LLM output)
            if self._idx == 1:
                await self._queue.put("follow-up question")
            yield StreamEvent(
                type="finish",
                finish_reason=resp.finish_reason,
                usage=resp.usage,
                tool_calls=resp.tool_calls,
            )

    llm = InjectDuringStreamLLM(
        [
            LLMResponse(content="first reply", finish_reason="stop"),
            LLMResponse(content="after injection", finish_reason="stop"),
        ],
        queue,
    )

    events = await collect(
        run_agent_loop(llm=llm, messages=msgs, tools={}, max_steps=5, inject_queue=queue)
    )

    # With streaming, check DoneEvent for final content
    done = [e for e in events if isinstance(e, DoneEvent)]
    assert len(done) == 1
    assert done[0].final_content == "after injection"

    # Should have had 2 steps (first ended with continue, second is final)
    steps = [e for e in events if isinstance(e, StepStart)]
    assert len(steps) == 2


@pytest.mark.asyncio
async def test_empty_queue_no_effect():
    """Empty inject queue does not change behavior (backward compat)."""
    queue: asyncio.Queue[str] = asyncio.Queue()
    msgs = _msgs()

    llm = MockLLM([LLMResponse(content="hello", finish_reason="stop")])
    events = await collect(
        run_agent_loop(llm=llm, messages=msgs, tools={}, max_steps=5, inject_queue=queue)
    )

    done = [e for e in events if isinstance(e, DoneEvent)]
    assert len(done) == 1
    assert done[0].stop_reason == StopReason.END_TURN
    assert done[0].final_content == "hello"

    # No injection events
    injected = [e for e in events if isinstance(e, InjectedMessageEvent)]
    assert len(injected) == 0


@pytest.mark.asyncio
async def test_none_queue_no_effect():
    """inject_queue=None (default) works the same as before."""
    msgs = _msgs()
    llm = MockLLM([LLMResponse(content="hello", finish_reason="stop")])
    events = await collect(
        run_agent_loop(llm=llm, messages=msgs, tools={}, max_steps=5, inject_queue=None)
    )

    done = [e for e in events if isinstance(e, DoneEvent)]
    assert len(done) == 1
    assert done[0].stop_reason == StopReason.END_TURN


@pytest.mark.asyncio
async def test_multiple_injections_drain_in_order():
    """Multiple queued messages are drained FIFO at step boundary."""
    queue: asyncio.Queue[str] = asyncio.Queue()
    msgs = _msgs()

    llm = MockLLM([
        LLMResponse(
            content="calling tool",
            tool_calls=[ToolCall(id="t1", type="function", function=FunctionCall(name="echo", arguments={"text": "x"}))],
            finish_reason="tool",
        ),
        LLMResponse(content="done", finish_reason="stop"),
    ])

    # Queue multiple messages
    await queue.put("first injection")
    await queue.put("second injection")
    await queue.put("third injection")

    events = await collect(
        run_agent_loop(llm=llm, messages=msgs, tools={"echo": EchoTool()}, max_steps=5, inject_queue=queue)
    )

    injected = [e for e in events if isinstance(e, InjectedMessageEvent)]
    assert len(injected) == 3
    assert injected[0].content == "first injection"
    assert injected[1].content == "second injection"
    assert injected[2].content == "third injection"


@pytest.mark.asyncio
async def test_inject_during_tool_execution():
    """Injection queued during tool execution is picked up at next step boundary."""
    queue: asyncio.Queue[str] = asyncio.Queue()
    msgs = _msgs()

    class SlowEchoTool(Tool):
        @property
        def name(self):
            return "echo"

        @property
        def description(self):
            return "Slow echo"

        @property
        def parameters(self):
            return {"type": "object", "properties": {"text": {"type": "string"}}}

        async def execute(self, text: str = ""):
            # Simulate injection arriving during tool execution
            await queue.put("injected during tool")
            return ToolResult(success=True, content=f"echo:{text}")

    llm = MockLLM([
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="t1", type="function", function=FunctionCall(name="echo", arguments={"text": "go"}))],
            finish_reason="tool",
        ),
        LLMResponse(content="final", finish_reason="stop"),
    ])

    events = await collect(
        run_agent_loop(llm=llm, messages=msgs, tools={"echo": SlowEchoTool()}, max_steps=5, inject_queue=queue)
    )

    injected = [e for e in events if isinstance(e, InjectedMessageEvent)]
    assert len(injected) == 1
    assert injected[0].content == "injected during tool"

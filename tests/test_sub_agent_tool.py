"""Test cases for SubAgentTool."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from box_agent.events import DoneEvent, StopReason
from box_agent.schema import LLMResponse, Message, StreamEvent
from box_agent.tools.base import Tool, ToolResult
from box_agent.tools.sub_agent_tool import SubAgentTool


# ── Helpers ──────────────────────────────────────────────────


class DummyTool(Tool):
    """A trivial tool for tests."""

    @property
    def name(self) -> str:
        return "dummy"

    @property
    def description(self) -> str:
        return "A dummy tool"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(success=True, content="dummy result")


def _make_llm(text: str = "summary", tool_calls=None):
    """Return a mock LLM whose generate_stream yields the given text then finishes."""
    llm = AsyncMock()

    async def fake_stream(*, messages, tools):
        yield StreamEvent(type="text", delta=text)
        yield StreamEvent(
            type="finish",
            finish_reason="stop" if not tool_calls else "tool_use",
            tool_calls=tool_calls,
        )

    llm.generate_stream = fake_stream
    return llm


# ── Basic properties ─────────────────────────────────────────


def test_name():
    llm = AsyncMock()
    tool = SubAgentTool(llm=llm, parent_tools={})
    assert tool.name == "sub_agent"


def test_parallel_safe():
    llm = AsyncMock()
    tool = SubAgentTool(llm=llm, parent_tools={})
    assert tool.parallel_safe is True


def test_default_parallel_safe_is_false():
    """Other tools should have parallel_safe == False by default."""
    dummy = DummyTool()
    assert dummy.parallel_safe is False


def test_schema():
    llm = AsyncMock()
    tool = SubAgentTool(llm=llm, parent_tools={})
    schema = tool.to_schema()
    assert schema["name"] == "sub_agent"
    assert "task" in schema["input_schema"]["properties"]

    openai_schema = tool.to_openai_schema()
    assert openai_schema["function"]["name"] == "sub_agent"


# ── Tool filtering ───────────────────────────────────────────


def test_child_tools_exclude_sub_agent():
    """SubAgentTool must not include itself in the child tool set."""
    llm = AsyncMock()
    dummy = DummyTool()
    parent = {"dummy": dummy, "sub_agent": SubAgentTool(llm=llm, parent_tools={})}
    tool = SubAgentTool(llm=llm, parent_tools=parent)
    assert "sub_agent" not in tool._child_tools
    assert "dummy" in tool._child_tools


# ── Execution ────────────────────────────────────────────────


async def test_basic_execution():
    """Sub-agent returns the LLM's final text as ToolResult content."""
    llm = _make_llm(text="Analysis complete: revenue up 20%")
    tool = SubAgentTool(llm=llm, parent_tools={})
    result = await tool.execute(task="Analyze revenue data")
    assert result.success is True
    assert "revenue up 20%" in result.content


async def test_empty_output_returns_error():
    """If the LLM produces no content, the tool should report failure."""
    llm = _make_llm(text="")
    tool = SubAgentTool(llm=llm, parent_tools={})
    result = await tool.execute(task="Do something")
    assert result.success is False
    assert "without producing output" in result.error


async def test_llm_exception_returns_error():
    """If the LLM raises, ToolResult should contain the error info."""
    llm = AsyncMock()

    async def boom(*, messages, tools):
        raise RuntimeError("API timeout")
        yield  # make it an async generator  # noqa: E501

    llm.generate_stream = boom
    tool = SubAgentTool(llm=llm, parent_tools={})
    result = await tool.execute(task="Try this")
    # run_agent_loop catches the exception and yields DoneEvent with error as final_content,
    # so SubAgentTool wraps it as a successful result containing the error text.
    assert "API timeout" in result.content


async def test_max_steps_respected():
    """Sub-agent should stop after max_steps even if LLM keeps requesting tools."""
    from box_agent.schema import FunctionCall, ToolCall

    call_count = 0

    async def looping_stream(*, messages, tools):
        nonlocal call_count
        call_count += 1
        # Always request a tool call to keep the loop going
        tc = ToolCall(
            id=f"tc-{call_count}",
            type="function",
            function=FunctionCall(name="dummy", arguments={}),
        )
        yield StreamEvent(type="text", delta=f"step {call_count}")
        yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[tc])

    llm = AsyncMock()
    llm.generate_stream = looping_stream

    dummy = DummyTool()
    tool = SubAgentTool(
        llm=llm,
        parent_tools={"dummy": dummy},
        max_steps=3,
    )
    result = await tool.execute(task="Loop forever")
    # Should have stopped — call_count should be capped at max_steps
    assert call_count <= 4  # max_steps=3 means 3 LLM calls


# ── Parallel execution in core ───────────────────────────────


async def test_parallel_execution_in_core():
    """Multiple parallel_safe tool calls should be gathered concurrently."""
    import asyncio
    from box_agent.core import run_agent_loop
    from box_agent.schema import FunctionCall, ToolCall

    execution_order = []

    class SlowSubAgent(Tool):
        parallel_safe = True

        @property
        def name(self) -> str:
            return "sub_agent"

        @property
        def description(self) -> str:
            return "test"

        @property
        def parameters(self) -> dict:
            return {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]}

        async def execute(self, task: str) -> ToolResult:
            execution_order.append(f"start:{task}")
            await asyncio.sleep(0.05)
            execution_order.append(f"end:{task}")
            return ToolResult(success=True, content=f"Done: {task}")

    # LLM: first call returns 2 sub_agent tool calls, second call ends
    call_num = 0

    async def fake_stream(*, messages, tools):
        nonlocal call_num
        call_num += 1
        if call_num == 1:
            yield StreamEvent(type="text", delta="Delegating")
            yield StreamEvent(
                type="finish",
                finish_reason="tool_use",
                tool_calls=[
                    ToolCall(id="tc-1", type="function", function=FunctionCall(name="sub_agent", arguments={"task": "A"})),
                    ToolCall(id="tc-2", type="function", function=FunctionCall(name="sub_agent", arguments={"task": "B"})),
                ],
            )
        else:
            yield StreamEvent(type="text", delta="All done")
            yield StreamEvent(type="finish", finish_reason="stop", tool_calls=None)

    llm = AsyncMock()
    llm.generate_stream = fake_stream

    messages = [
        Message(role="system", content="You are helpful."),
        Message(role="user", content="Do two things"),
    ]
    tools = {"sub_agent": SlowSubAgent()}

    events = []
    async for event in run_agent_loop(llm=llm, messages=messages, tools=tools, max_steps=5):
        events.append(event)

    # Both starts should appear before either result (parallel execution)
    start_events = [e for e in events if hasattr(e, "tool_name") and hasattr(e, "arguments") and not hasattr(e, "success")]
    result_events = [e for e in events if hasattr(e, "success") and hasattr(e, "tool_name")]

    sub_starts = [e for e in start_events if e.tool_name == "sub_agent"]
    sub_results = [e for e in result_events if e.tool_name == "sub_agent"]

    assert len(sub_starts) == 2
    assert len(sub_results) == 2

    # Verify parallel execution: both starts happen before both ends
    assert execution_order[0].startswith("start:")
    assert execution_order[1].startswith("start:")

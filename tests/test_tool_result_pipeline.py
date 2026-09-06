"""Contracts for shared tool-result post-processing."""

from __future__ import annotations

import json

import pytest

from box_agent.events import ToolCallResult
from box_agent.kernel.tool_result_pipeline import (
    ToolResultPipelineInput,
    process_tool_result,
)
from box_agent.runtime import run_agent_loop
from box_agent.schema import FunctionCall, LLMResponse, Message, StreamEvent, ToolCall
from box_agent.tool_result_storage import ToolResultStorage
from box_agent.tools.base import Tool, ToolResult


class _OneToolCallLLM:
    def __init__(self, tool_name: str) -> None:
        self._tool_name = tool_name
        self._calls = 0

    async def generate(self, **_kwargs):
        raise AssertionError("this fixture must not need context summarization")

    async def generate_stream(self, messages, tools=None, **_):
        self._calls += 1
        if self._calls == 1:
            yield StreamEvent(
                type="finish",
                finish_reason="tool",
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        type="function",
                        function=FunctionCall(name=self._tool_name, arguments={}),
                    )
                ],
            )
            return
        yield StreamEvent(type="text", delta="done")
        yield StreamEvent(type="finish", finish_reason="stop")


class _EchoTool(Tool):
    def __init__(self, *, parallel_safe: bool) -> None:
        self.parallel_safe = parallel_safe

    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "Return one deterministic result."

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self) -> ToolResult:
        return ToolResult(success=True, content="echoed")


def test_pipeline_appends_tool_message_before_returning_result_events(tmp_path) -> None:
    messages = [Message(role="user", content="run it")]

    outcome = process_tool_result(
        ToolResultPipelineInput(
            messages=messages,
            tool_call_id="call-1",
            tool_name="echo",
            arguments={},
            result=ToolResult(success=True, content="echoed"),
            visible_content="echoed",
            visible_error=None,
            result_storage=ToolResultStorage(tmp_path),
        )
    )

    assert messages[-1].role == "tool"
    assert messages[-1].tool_call_id == "call-1"
    assert outcome.events[0].tool_call_id == "call-1"


def test_pipeline_returns_web_search_deltas_against_shared_seen_state(tmp_path) -> None:
    messages = [Message(role="user", content="search")]
    seen_result_keys: set[str] = set()
    storage = ToolResultStorage(tmp_path)
    content = json.dumps(
        {"refs": [{"title": "Primary result", "url": "https://example.com/page"}]}
    )

    first = process_tool_result(
        ToolResultPipelineInput(
            messages=messages,
            tool_call_id="search-1",
            tool_name="web_search",
            arguments={"query": "primary result"},
            result=ToolResult(success=True, content=content),
            visible_content=content,
            visible_error=None,
            result_storage=storage,
            web_search_seen_result_keys=seen_result_keys,
        )
    )
    second = process_tool_result(
        ToolResultPipelineInput(
            messages=messages,
            tool_call_id="search-2",
            tool_name="web_search",
            arguments={"query": "primary result"},
            result=ToolResult(success=True, content=content),
            visible_content=content,
            visible_error=None,
            result_storage=storage,
            web_search_seen_result_keys=seen_result_keys,
        )
    )

    assert first.web_search_new_results == 1
    assert second.web_search_new_results == 0
    assert second.web_search_duplicate_results == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("parallel_safe", [False, True], ids=["serial", "parallel"])
async def test_agent_loop_has_matching_tool_message_when_result_is_yielded(
    parallel_safe: bool,
) -> None:
    messages = [Message(role="user", content="run it")]
    tool = _EchoTool(parallel_safe=parallel_safe)
    stream = run_agent_loop(
        llm=_OneToolCallLLM(tool.name),
        messages=messages,
        tools={tool.name: tool},
        max_steps=2,
        artifact_detection_enabled=False,
    )

    try:
        async for event in stream:
            if not isinstance(event, ToolCallResult):
                continue
            assert any(
                message.role == "tool" and message.tool_call_id == event.tool_call_id
                for message in messages
            )
            break
    finally:
        await stream.aclose()

"""Tests for the shared agent execution core (box_agent.core)."""

import asyncio
import json
import pytest

from box_agent.core import (
    _detect_artifacts,
    _detect_new_files,
    _snapshot_workspace,
    run_agent_loop,
    text_is_short_acknowledgement,
    text_is_short_non_task_reply,
    text_requests_plan_start,
)
from box_agent.core import FINAL_SUMMARY_TOOL_CALL_THRESHOLD as _FS_THRESHOLD
from box_agent.events import (
    ArtifactEvent,
    ContentEvent,
    DoneEvent,
    ErrorEvent,
    InjectedMessageEvent,
    LLMOutputEvent,
    PlanSnapshotEvent,
    ProgressEvent,
    StepEnd,
    StepStart,
    StopReason,
    ThinkingEvent,
    ToolCallResult,
    ToolCallStart,
)
from box_agent.schema import FunctionCall, LLMResponse, Message, StreamEvent, ToolCall
from box_agent.tools.base import Tool, ToolResult
from box_agent.tools.file_tools import AppendTool, EditTool, ReadTool, WriteTool


# ── Helpers ─────────────────────────────────────────────────────


def test_text_requests_plan_start_handles_short_plan_phrases():
    for text in [
        "计划 plan",
        "计划",
        "plan",
        "使用plan 生成一个内马尔图片",
        "出一个计划我看一下",
        "please make a plan first",
    ]:
        assert text_requests_plan_start(text)

    for text in [
        "普通聊天，不需要计划",
        "不用 plan，直接执行",
        "planet",
        "做一份 15 页售前竞标方案 PPT",
        "ok",
        "好的",
    ]:
        assert not text_requests_plan_start(text)


def test_short_acknowledgement_and_non_task_reply_detection():
    for text in ["ok", "OK!", "go ahead", "继续执行", "好的", "可以"]:
        assert text_is_short_acknowledgement(text)
        assert text_is_short_non_task_reply(text)

    for text in ["hi", "hello", "谢谢"]:
        assert not text_is_short_acknowledgement(text)
        assert text_is_short_non_task_reply(text)

    for text in ["ok，继续修 todo bar", "好的，帮我改这个文件", "hello build a page"]:
        assert not text_is_short_acknowledgement(text)
        assert not text_is_short_non_task_reply(text)


class MockLLM:
    """Deterministic LLM that yields pre-configured responses in order."""

    def __init__(self, responses: list[LLMResponse]):
        self._responses = list(responses)
        self._idx = 0

    async def generate(self, messages, tools=None):
        resp = self._responses[self._idx]
        self._idx += 1
        return resp

    async def generate_stream(self, messages, tools=None, **_):
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


class ChunkedStreamLLM:
    """LLM test double that emits visible text in multiple stream chunks."""

    def __init__(self, chunks: list[str], *, finish_reason: str = "stop"):
        self.chunks = chunks
        self.finish_reason = finish_reason

    async def generate(self, messages, tools=None):
        return LLMResponse(content="".join(self.chunks), finish_reason=self.finish_reason)

    async def generate_stream(self, messages, tools=None, **_):
        for chunk in self.chunks:
            yield StreamEvent(type="text", delta=chunk)
        yield StreamEvent(type="finish", finish_reason=self.finish_reason)


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


class CountingEchoTool(EchoTool):
    def __init__(self):
        self.calls = 0

    async def execute(self, text: str = ""):
        self.calls += 1
        return await super().execute(text=text)


class CountingWebSearchTool(Tool):
    def __init__(self):
        self.calls = 0

    @property
    def name(self):
        return "web_search"

    @property
    def description(self):
        return "Searches the web"

    @property
    def parameters(self):
        return {"type": "object", "properties": {"query": {"type": "string"}}}

    async def execute(self, query: str = ""):
        self.calls += 1
        return ToolResult(success=True, content=f"result:{query}")


class JsonWebSearchTool(Tool):
    def __init__(self, urls_by_query: dict[str, str]):
        self.calls: list[str] = []
        self.urls_by_query = urls_by_query

    @property
    def name(self):
        return "web_search"

    @property
    def description(self):
        return "Searches the web"

    @property
    def parameters(self):
        return {"type": "object", "properties": {"query": {"type": "string"}}}

    async def execute(self, query: str = ""):
        self.calls.append(query)
        url = self.urls_by_query.get(query, f"https://example.com/{query}")
        return ToolResult(
            success=True,
            content=json.dumps(
                {
                    "refs": [
                        {
                            "title": f"Result for {query}",
                            "url": url,
                            "snippet": f"Snippet for {query}",
                        }
                    ]
                }
            ),
        )


class RawOutputTool(Tool):
    @property
    def name(self):
        return "raw"

    @property
    def description(self):
        return "Returns a structured raw_output payload"

    @property
    def parameters(self):
        return {"type": "object", "properties": {}}

    async def execute(self):
        return ToolResult(
            success=True,
            content="structured result",
            raw_output={"type": "memory_search", "matched_memories": [{"text": "- remembered"}]},
        )


class PlanWriteStubTool(Tool):
    @property
    def name(self):
        return "plan_write"

    @property
    def description(self):
        return "Publishes a plan"

    @property
    def parameters(self):
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        title = str(kwargs.get("title") or "Test plan")
        return ToolResult(
            success=True,
            content="plan set",
            raw_output={
                "type": "plan_snapshot",
                "version": 1,
                "action": "set",
                "plan": {
                    "id": "plan-test",
                    "title": title,
                    "objective": str(kwargs.get("objective") or ""),
                    "status": "active",
                    "steps": list(kwargs.get("steps") or []),
                    "verification": list(kwargs.get("verification") or []),
                    "risks": [],
                    "assumptions": [],
                },
                "summary": {
                    "steps": len(kwargs.get("steps") or []),
                    "verification": len(kwargs.get("verification") or []),
                    "risks": 0,
                    "assumptions": 0,
                },
            },
        )


class ModelContextTool(Tool):
    @property
    def name(self):
        return "model_context"

    @property
    def description(self):
        return "Returns full content plus compact model context"

    @property
    def parameters(self):
        return {"type": "object", "properties": {}}

    async def execute(self):
        return ToolResult(
            success=True,
            content="FULL_VISIBLE_OUTPUT_SECRET",
            model_context="COMPACT_MODEL_CONTEXT",
        )


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


def _task_msgs():
    return [
        Message(role="system", content="sys"),
        Message(role="user", content="写一个文件并验证结果"),
    ]


def _echo_tool_calls(count: int) -> list[ToolCall]:
    return [
        ToolCall(
            id=f"t{i}",
            type="function",
            function=FunctionCall(name="echo", arguments={"text": str(i)}),
        )
        for i in range(count)
    ]


class NamedTool(Tool):
    """Minimal tool that reports an arbitrary name (for threshold tests)."""

    def __init__(self, tool_name: str):
        self._name = tool_name

    @property
    def name(self):
        return self._name

    @property
    def description(self):
        return f"Tool named {self._name}"

    @property
    def parameters(self):
        return {"type": "object", "properties": {"text": {"type": "string"}}}

    async def execute(self, text: str = ""):
        return ToolResult(success=True, content=f"{self._name}:{text}")


def _named_tool_calls(name: str, count: int) -> list[ToolCall]:
    return [
        ToolCall(
            id=f"{name}-{i}",
            type="function",
            function=FunctionCall(name=name, arguments={"text": str(i)}),
        )
        for i in range(count)
    ]


class MemoryManagerStub:
    def __init__(self, matches):
        self.matches = matches

    def auto_match_context(self, query: str):
        self.query = query
        return self.matches


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
async def test_long_plain_answer_streams_before_finish_without_duplicate():
    chunks = ["李白是唐代诗人，" * 20, "他的诗歌想象瑰丽，" * 20, "后世称他为诗仙。"]
    llm = ChunkedStreamLLM(chunks)

    events = await collect(run_agent_loop(llm=llm, messages=_msgs(), tools={}, max_steps=5))

    content_events = [e for e in events if isinstance(e, ContentEvent)]
    assert any(e._streaming for e in content_events)
    assert "".join(e.content for e in content_events) == "".join(chunks)


@pytest.mark.asyncio
async def test_plan_start_snapshot_emits_before_llm_output_for_explicit_planning_request():
    llm = MockLLM([LLMResponse(content="我来规划。", finish_reason="stop")])
    messages = [
        Message(role="system", content="sys"),
        Message(role="user", content="开发一个 React 个人介绍网站，先做规划"),
    ]

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=messages,
            tools={"plan_write": PlanWriteStubTool()},
            max_steps=5,
        )
    )

    plan_index = next(i for i, event in enumerate(events) if isinstance(event, PlanSnapshotEvent))
    content_index = next(i for i, event in enumerate(events) if isinstance(event, ContentEvent))
    plan_event = events[plan_index]

    assert plan_index < content_index
    assert plan_event.payload["type"] == "plan_snapshot"
    assert plan_event.payload["action"] == "start"
    assert plan_event.payload["plan"]["status"] == "draft"


@pytest.mark.asyncio
async def test_plan_start_snapshot_not_emitted_without_plan_tool():
    llm = MockLLM([LLMResponse(content="我来规划。", finish_reason="stop")])
    messages = [
        Message(role="system", content="sys"),
        Message(role="user", content="开发一个 React 个人介绍网站，先做规划"),
    ]

    events = await collect(run_agent_loop(llm=llm, messages=messages, tools={}, max_steps=5))

    assert not any(isinstance(event, PlanSnapshotEvent) for event in events)


@pytest.mark.asyncio
async def test_force_plan_start_snapshot_emits_without_planning_trigger():
    llm = MockLLM(
        [
            LLMResponse(
                content="",
                finish_reason="tool",
                tool_calls=[
                    ToolCall(
                        id="plan1",
                        type="function",
                        function=FunctionCall(
                            name="plan_write",
                            arguments={"action": "set", "title": "Forced plan"},
                        ),
                    )
                ],
            ),
            LLMResponse(content="hello", finish_reason="stop"),
        ]
    )

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=[
                Message(role="system", content="sys"),
                Message(role="user", content="hello"),
            ],
            tools={"plan_write": PlanWriteStubTool()},
            max_steps=5,
            force_plan_start=True,
        )
    )

    plan_events = [event for event in events if isinstance(event, PlanSnapshotEvent)]
    assert len(plan_events) == 1
    assert plan_events[0].payload["action"] == "start"
    assert any(
        isinstance(event, InjectedMessageEvent) and event.user_visible is False
        for event in events
    )
    assert any(
        isinstance(event, ToolCallStart) and event.tool_name == "plan_write"
        for event in events
    )


@pytest.mark.asyncio
async def test_force_plan_start_snapshot_not_emitted_without_plan_tool():
    llm = MockLLM([LLMResponse(content="hello", finish_reason="stop")])

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=[
                Message(role="system", content="sys"),
                Message(role="user", content="hello"),
            ],
            tools={},
            max_steps=5,
            force_plan_start=True,
        )
    )

    assert not any(isinstance(event, PlanSnapshotEvent) for event in events)


@pytest.mark.asyncio
async def test_require_plan_approval_blocks_non_plan_tools_and_marks_plan_pending():
    llm = MockLLM(
        [
            LLMResponse(
                content="",
                finish_reason="tool",
                tool_calls=[
                    ToolCall(
                        id="echo1",
                        type="function",
                        function=FunctionCall(name="echo", arguments={"text": "write files"}),
                    ),
                    ToolCall(
                        id="plan1",
                        type="function",
                        function=FunctionCall(
                            name="plan_write",
                            arguments={"action": "set", "title": "Approval plan"},
                        ),
                    ),
                ],
            ),
            LLMResponse(content="should not run", finish_reason="stop"),
        ]
    )
    echo = CountingEchoTool()

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_task_msgs(),
            tools={"echo": echo, "plan_write": PlanWriteStubTool()},
            max_steps=5,
            require_plan_approval=True,
        )
    )

    assert echo.calls == 0
    echo_result = next(
        event
        for event in events
        if isinstance(event, ToolCallResult) and event.tool_name == "echo"
    )
    assert echo_result.success is False
    assert echo_result.user_visible is False
    assert "paused until the user approves" in (echo_result.error or "")
    plan_result = next(
        event
        for event in events
        if isinstance(event, ToolCallResult) and event.tool_name == "plan_write"
    )
    assert plan_result.raw_output["approval"]["required"] is True
    assert plan_result.raw_output["approval"]["state"] == "pending"
    assert plan_result.raw_output["plan"]["status"] == "draft"
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.final_content == "计划已生成，等待用户确认后再执行。"
    assert not any(
        isinstance(event, ContentEvent) and event.content == "should not run"
        for event in events
    )


@pytest.mark.asyncio
async def test_require_plan_approval_does_not_force_plan_for_short_acknowledgement():
    llm = MockLLM([LLMResponse(content="好的，我等你的下一步。", finish_reason="stop")])

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=[
                Message(role="system", content="sys"),
                Message(role="user", content="ok"),
            ],
            tools={"plan_write": PlanWriteStubTool()},
            max_steps=5,
            require_plan_approval=True,
        )
    )

    assert not any(isinstance(event, PlanSnapshotEvent) for event in events)
    assert not any(
        isinstance(event, InjectedMessageEvent)
        and "Host UI requires an explicit user approval" in event.content
        for event in events
    )
    assert not any(
        isinstance(event, ToolCallStart) and event.tool_name == "plan_write"
        for event in events
    )
    assert any(
        isinstance(event, ContentEvent) and event.content == "好的，我等你的下一步。"
        for event in events
    )


@pytest.mark.asyncio
async def test_pause_after_plan_write_marks_organic_plan_pending_and_stops_siblings():
    llm = MockLLM(
        [
            LLMResponse(
                content="",
                finish_reason="tool",
                tool_calls=[
                    ToolCall(
                        id="plan1",
                        type="function",
                        function=FunctionCall(
                            name="plan_write",
                            arguments={"action": "set", "title": "Organic plan"},
                        ),
                    ),
                    ToolCall(
                        id="echo1",
                        type="function",
                        function=FunctionCall(name="echo", arguments={"text": "too soon"}),
                    ),
                ],
            ),
            LLMResponse(content="should not run", finish_reason="stop"),
        ]
    )
    echo = CountingEchoTool()

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"echo": echo, "plan_write": PlanWriteStubTool()},
            max_steps=5,
            pause_after_plan_write=True,
        )
    )

    assert echo.calls == 0
    plan_result = next(
        event
        for event in events
        if isinstance(event, ToolCallResult) and event.tool_name == "plan_write"
    )
    assert plan_result.raw_output["approval"]["required"] is True
    assert plan_result.raw_output["approval"]["state"] == "pending"
    echo_result = next(
        event
        for event in events
        if isinstance(event, ToolCallResult) and event.tool_name == "echo"
    )
    assert echo_result.success is False
    assert echo_result.user_visible is False
    assert "paused until the user approves" in (echo_result.error or "")
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.final_content == "计划已生成，等待用户确认后再执行。"
    assert not any(
        isinstance(event, ContentEvent) and event.content == "should not run"
        for event in events
    )


@pytest.mark.asyncio
async def test_require_plan_approval_approved_decision_allows_execution():
    llm = MockLLM(
        [
            LLMResponse(
                content="",
                finish_reason="tool",
                tool_calls=[
                    ToolCall(
                        id="echo1",
                        type="function",
                        function=FunctionCall(name="echo", arguments={"text": "go"}),
                    )
                ],
            ),
            LLMResponse(content="done", finish_reason="stop"),
        ]
    )
    echo = CountingEchoTool()

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"echo": echo, "plan_write": PlanWriteStubTool()},
            max_steps=5,
            require_plan_approval=True,
            plan_approval={"decision": "approved", "request_id": "plan-test"},
        )
    )

    assert echo.calls == 1
    result = next(
        event
        for event in events
        if isinstance(event, ToolCallResult) and event.tool_name == "echo"
    )
    assert result.success is True
    assert result.content == "echo:go"


@pytest.mark.asyncio
async def test_short_visible_text_streams_immediately_without_duplicate():
    llm = MockLLM([LLMResponse(content="短回复", finish_reason="stop")])

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={},
            max_steps=5,
            session_id="sess-test",
        )
    )

    content_events = [event for event in events if isinstance(event, ContentEvent)]
    assert content_events == [ContentEvent(content="短回复", _streaming=True)]
    assert "".join(event.content for event in content_events) == "短回复"


@pytest.mark.asyncio
async def test_thinking_event():
    """LLM with thinking should yield ThinkingEvent."""
    llm = MockLLM([LLMResponse(content="ok", thinking="let me think", finish_reason="stop")])
    events = await collect(run_agent_loop(llm=llm, messages=_msgs(), tools={}, max_steps=5))

    thinking = [e for e in events if isinstance(e, ThinkingEvent)]
    assert len(thinking) >= 1
    # With streaming, thinking content is in delta events
    thinking_text = "".join(e.content for e in thinking)
    assert "let me think" in thinking_text


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
    visible_text = "".join(e.content for e in events if isinstance(e, ContentEvent))
    assert visible_text == "calling tooldone"
    progress = [e for e in events if isinstance(e, ProgressEvent)]
    assert progress == []


@pytest.mark.asyncio
async def test_tool_result_preserves_raw_output_for_cli_and_hosts():
    llm = MockLLM([
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="t1", type="function", function=FunctionCall(name="raw", arguments={}))],
            finish_reason="tool",
        ),
        LLMResponse(content="done", finish_reason="stop"),
    ])
    events = await collect(run_agent_loop(llm=llm, messages=_msgs(), tools={"raw": RawOutputTool()}, max_steps=5))

    results = [e for e in events if isinstance(e, ToolCallResult)]
    assert len(results) == 1
    assert results[0].raw_output == {
        "type": "memory_search",
        "matched_memories": [{"text": "- remembered"}],
    }


@pytest.mark.asyncio
async def test_tool_model_context_is_used_only_for_message_history():
    msgs = _msgs()
    llm = MockLLM([
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="t1",
                    type="function",
                    function=FunctionCall(name="model_context", arguments={}),
                )
            ],
            finish_reason="tool",
        ),
        LLMResponse(content="done", finish_reason="stop"),
    ])
    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=msgs,
            tools={"model_context": ModelContextTool()},
            max_steps=5,
        )
    )

    result = next(e for e in events if isinstance(e, ToolCallResult))
    assert result.content == "FULL_VISIBLE_OUTPUT_SECRET"

    tool_msg = next(m for m in msgs if m.role == "tool")
    assert tool_msg.content == "COMPACT_MODEL_CONTEXT"


@pytest.mark.asyncio
async def test_read_file_artifact_keeps_full_event_but_compacts_model_history(tmp_path):
    marker = "SHOULD_NOT_STAY_IN_MODEL_HISTORY"
    deck = tmp_path / "deck.html"
    deck.write_text(
        "\n".join(["<html>", "<body>"] + [f"<section>slide {i}</section>" for i in range(70)] + [marker, "</body>", "</html>"]),
        encoding="utf-8",
    )
    msgs = _msgs()
    llm = MockLLM([
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="t1",
                    type="function",
                    function=FunctionCall(name="read_file", arguments={"path": "deck.html"}),
                )
            ],
            finish_reason="tool",
        ),
        LLMResponse(content="done", finish_reason="stop"),
    ])
    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=msgs,
            tools={"read_file": ReadTool(workspace_dir=str(tmp_path))},
            max_steps=5,
            workspace_dir=str(tmp_path),
        )
    )

    result = next(e for e in events if isinstance(e, ToolCallResult))
    assert marker in result.content

    tool_msg = next(m for m in msgs if m.role == "tool")
    assert "[Full file content omitted from model history]" in tool_msg.content
    assert "deck.html" in tool_msg.content
    assert marker not in tool_msg.content


@pytest.mark.asyncio
async def test_write_file_large_artifact_arguments_are_compacted_in_model_history(tmp_path):
    marker = "SHOULD_NOT_STAY_IN_ASSISTANT_TOOL_ARGS"
    html = "\n".join(
        ["<!doctype html>", "<html>", "<body>"]
        + [f"<section class='slide'>slide {i}</section>" for i in range(80)]
        + [marker, "</body>", "</html>"]
    )
    msgs = _msgs()
    llm = MockLLM([
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="t1",
                    type="function",
                    function=FunctionCall(name="write_file", arguments={"path": "deck.html", "content": html}),
                )
            ],
            finish_reason="tool",
        ),
        LLMResponse(content="done", finish_reason="stop"),
    ])

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=msgs,
            tools={"write_file": WriteTool(workspace_dir=str(tmp_path))},
            max_steps=5,
            workspace_dir=str(tmp_path),
        )
    )

    start = next(e for e in events if isinstance(e, ToolCallStart))
    llm_output = next(e for e in events if isinstance(e, LLMOutputEvent))
    assert marker in llm_output.tool_calls[0]["function"]["arguments"]["content"]
    assert marker in start.arguments["content"]
    assert (tmp_path / "deck.html").read_text(encoding="utf-8") == html

    assistant_msg = next(m for m in msgs if m.role == "assistant" and m.tool_calls)
    stored_args = assistant_msg.tool_calls[0].function.arguments
    assert "[Full tool-call argument omitted from model history]" in stored_args["content"]
    assert "deck.html" in stored_args["content"]
    assert marker not in stored_args["content"]


@pytest.mark.asyncio
async def test_write_file_qa_json_arguments_are_compacted_even_when_small(tmp_path):
    marker = "SHOULD_NOT_STAY_IN_QA_TOOL_ARGS"
    content = f'{{"ok": false, "details": "{marker}"}}'
    msgs = _msgs()
    llm = MockLLM([
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="t1",
                    type="function",
                    function=FunctionCall(name="write_file", arguments={"path": "qa.json", "content": content}),
                )
            ],
            finish_reason="tool",
        ),
        LLMResponse(content="done", finish_reason="stop"),
    ])

    await collect(
        run_agent_loop(
            llm=llm,
            messages=msgs,
            tools={"write_file": WriteTool(workspace_dir=str(tmp_path))},
            max_steps=5,
            workspace_dir=str(tmp_path),
        )
    )

    assert (tmp_path / "qa.json").read_text(encoding="utf-8") == content
    assistant_msg = next(m for m in msgs if m.role == "assistant" and m.tool_calls)
    stored_args = assistant_msg.tool_calls[0].function.arguments
    assert "[Full tool-call argument omitted from model history]" in stored_args["content"]
    assert "qa.json" in stored_args["content"]
    assert marker not in stored_args["content"]


@pytest.mark.asyncio
async def test_append_file_large_artifact_arguments_are_compacted_in_model_history(tmp_path):
    marker = "APPENDED_HTML_SHOULD_NOT_STAY_IN_ASSISTANT_TOOL_ARGS"
    html = "\n".join(
        ["<!doctype html>", "<html>", "<body>"]
        + [f"<section>chunk {i}</section>" for i in range(20)]
        + [marker, "</body>", "</html>"]
    )
    msgs = _msgs()
    llm = MockLLM([
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="t1",
                    type="function",
                    function=FunctionCall(name="append_file", arguments={"path": "deck.html", "content": html}),
                )
            ],
            finish_reason="tool",
        ),
        LLMResponse(content="done", finish_reason="stop"),
    ])

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=msgs,
            tools={"append_file": AppendTool(workspace_dir=str(tmp_path))},
            max_steps=5,
            workspace_dir=str(tmp_path),
        )
    )

    start = next(e for e in events if isinstance(e, ToolCallStart))
    llm_output = next(e for e in events if isinstance(e, LLMOutputEvent))
    assert marker in llm_output.tool_calls[0]["function"]["arguments"]["content"]
    assert marker in start.arguments["content"]
    assert (tmp_path / "deck.html").read_text(encoding="utf-8") == html

    assistant_msg = next(m for m in msgs if m.role == "assistant" and m.tool_calls)
    stored_args = assistant_msg.tool_calls[0].function.arguments
    assert "[Full tool-call argument omitted from model history]" in stored_args["content"]
    assert "deck.html" in stored_args["content"]
    assert marker not in stored_args["content"]


@pytest.mark.asyncio
async def test_edit_file_large_artifact_arguments_omit_previews_in_model_history(tmp_path):
    old_marker = "OLD_HTML_SHOULD_NOT_STAY_IN_ASSISTANT_TOOL_ARGS"
    new_marker = "NEW_HTML_SHOULD_NOT_STAY_IN_ASSISTANT_TOOL_ARGS"
    original = "\n".join(
        ["<!doctype html>", "<html>", "<body>"]
        + [f"<section class='slide'>slide {i}</section>" for i in range(80)]
        + [old_marker, "</body>", "</html>"]
    )
    updated = original.replace(old_marker, new_marker)
    (tmp_path / "deck.html").write_text(original, encoding="utf-8")

    msgs = _msgs()
    llm = MockLLM([
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="t1",
                    type="function",
                    function=FunctionCall(
                        name="edit_file",
                        arguments={"path": "deck.html", "old_str": original, "new_str": updated},
                    ),
                )
            ],
            finish_reason="tool",
        ),
        LLMResponse(content="done", finish_reason="stop"),
    ])

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=msgs,
            tools={"edit_file": EditTool(workspace_dir=str(tmp_path))},
            max_steps=5,
            workspace_dir=str(tmp_path),
        )
    )

    start = next(e for e in events if isinstance(e, ToolCallStart))
    assert old_marker in start.arguments["old_str"]
    assert new_marker in start.arguments["new_str"]
    assert (tmp_path / "deck.html").read_text(encoding="utf-8") == updated

    assistant_msg = next(m for m in msgs if m.role == "assistant" and m.tool_calls)
    stored_args = assistant_msg.tool_calls[0].function.arguments
    assert "[Full tool-call argument omitted from model history]" in stored_args["old_str"]
    assert "[Full tool-call argument omitted from model history]" in stored_args["new_str"]
    assert old_marker not in stored_args["old_str"]
    assert new_marker not in stored_args["new_str"]
    assert "Preview first" not in stored_args["old_str"]
    assert "Preview first" not in stored_args["new_str"]


@pytest.mark.asyncio
async def test_auto_memory_match_injects_weak_context_before_llm_call():
    llm = MockLLM([LLMResponse(content="done", finish_reason="stop")])
    messages = _msgs()
    memory = MemoryManagerStub([
        {
            "id": "context:7",
            "source": "context",
            "category": "context",
            "text": "- 科技公司入职培训 PPT 已生成预览。",
        }
    ])

    events = await collect(
        run_agent_loop(llm=llm, messages=messages, tools={}, max_steps=5, memory_manager=memory)
    )

    results = [e for e in events if isinstance(e, ToolCallResult)]
    assert len(results) == 1
    assert results[0].tool_call_id == "memory-auto-match"
    assert results[0].raw_output == {
        "type": "memory_search",
        "trigger": "auto",
        "query": "hi",
        "matched_memories": [
            {
                "id": "context:7",
                "source": "context",
                "category": "context",
                "text": "- 科技公司入职培训 PPT 已生成预览。",
            }
        ],
    }
    user_message = next(msg for msg in messages if msg.role == "user")
    assert "Possibly relevant memory" in user_message.content
    assert "Use them only if they are clearly relevant" in user_message.content
    assert "ignore them and do not assume continuity" in user_message.content


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
async def test_tool_heavy_turn_injects_hidden_final_summary_guidance():
    """After many visible tools, the runtime nudges the model to end with a conclusion."""
    llm = MockLLM(
        [
            LLMResponse(
                content="",
                tool_calls=_echo_tool_calls(_FS_THRESHOLD + 1),
                finish_reason="tool",
            ),
            LLMResponse(content="结论：已完成。", finish_reason="stop"),
        ]
    )

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"echo": EchoTool()},
            max_steps=5,
        )
    )

    injected = [e for e in events if isinstance(e, InjectedMessageEvent)]
    assert any(
        not e.user_visible
        and "many visible tool calls" in e.content
        and "final user-visible response must be a concise conclusion" in e.content
        for e in injected
    )
    done = [e for e in events if isinstance(e, DoneEvent)]
    assert done[-1].final_content == "结论：已完成。"


@pytest.mark.asyncio
async def test_at_threshold_does_not_inject_final_summary_guidance():
    """Exactly at the threshold (boundary) does not trigger the wrap-up nudge."""
    llm = MockLLM(
        [
            LLMResponse(
                content="",
                tool_calls=_echo_tool_calls(_FS_THRESHOLD),
                finish_reason="tool",
            ),
            LLMResponse(content="done", finish_reason="stop"),
        ]
    )

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"echo": EchoTool()},
            max_steps=5,
        )
    )

    injected = [e for e in events if isinstance(e, InjectedMessageEvent)]
    assert not any("many visible tool calls" in e.content for e in injected)
    done = [e for e in events if isinstance(e, DoneEvent)]
    assert done[-1].final_content == "done"


@pytest.mark.asyncio
async def test_tool_heavy_empty_final_answer_retries_for_conclusion():
    """A long tool-heavy turn should not end with an empty visible answer."""
    llm = MockLLM(
        [
            LLMResponse(
                content="",
                tool_calls=_echo_tool_calls(_FS_THRESHOLD + 1),
                finish_reason="tool",
            ),
            LLMResponse(content="", finish_reason="stop"),
            LLMResponse(content="最终结论：文件已处理。", finish_reason="stop"),
        ]
    )

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"echo": EchoTool()},
            max_steps=5,
        )
    )

    injected = [e for e in events if isinstance(e, InjectedMessageEvent)]
    assert any("many visible tool calls" in e.content for e in injected)
    assert any("produced no visible final answer" in e.content for e in injected)
    done = [e for e in events if isinstance(e, DoneEvent)]
    assert done[-1].final_content == "最终结论：文件已处理。"


@pytest.mark.asyncio
async def test_setup_tools_do_not_count_toward_final_summary_threshold():
    """Setup/bookkeeping tools (skills, plan, todos, memory) must not trip the
    wrap-up nudge — even far beyond the threshold they are not 'process log' work."""
    n = _FS_THRESHOLD + 10
    llm = MockLLM(
        [
            LLMResponse(
                content="",
                tool_calls=_named_tool_calls("todo_write", n),
                finish_reason="tool",
            ),
            LLMResponse(content="done", finish_reason="stop"),
        ]
    )

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"todo_write": NamedTool("todo_write")},
            max_steps=5,
        )
    )

    injected = [e for e in events if isinstance(e, InjectedMessageEvent)]
    assert not any("many visible tool calls" in e.content for e in injected)
    done = [e for e in events if isinstance(e, DoneEvent)]
    assert done[-1].final_content == "done"


@pytest.mark.asyncio
async def test_substantive_calls_still_count_when_mixed_with_setup_tools():
    """Excluded setup tools don't count, but substantive tools beyond the
    threshold still trigger the wrap-up nudge."""
    setup = _named_tool_calls("get_skill", 8)
    substantive = _echo_tool_calls(_FS_THRESHOLD + 1)
    # Re-id substantive calls to avoid collisions with any other ids.
    mixed = setup + substantive
    llm = MockLLM(
        [
            LLMResponse(content="", tool_calls=mixed, finish_reason="tool"),
            LLMResponse(content="结论：完成。", finish_reason="stop"),
        ]
    )

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"get_skill": NamedTool("get_skill"), "echo": EchoTool()},
            max_steps=5,
        )
    )

    injected = [e for e in events if isinstance(e, InjectedMessageEvent)]
    assert any("many visible tool calls" in e.content for e in injected)
    done = [e for e in events if isinstance(e, DoneEvent)]
    assert done[-1].final_content == "结论：完成。"


@pytest.mark.asyncio
async def test_web_search_budget_synthesizes_result_and_allows_final_answer():
    """Repeated web_search calls are capped and converted into protocol-valid
    tool results so the model can stop cleanly instead of searching forever."""

    def web_call(i):
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id=f"web-{i}",
                    type="function",
                    function=FunctionCall(name="web_search", arguments={"query": f"q{i}"}),
                )
            ],
            finish_reason="tool",
        )

    responses = [web_call(i) for i in range(25)]
    responses.append(LLMResponse(content="final from gathered evidence", finish_reason="stop"))
    web_search = CountingWebSearchTool()

    events = await collect(
        run_agent_loop(
            llm=MockLLM(responses),
            messages=_msgs(),
            tools={"web_search": web_search},
            max_steps=30,
        )
    )

    assert web_search.calls == 24
    tool_starts = [e for e in events if isinstance(e, ToolCallStart)]
    assert len([e for e in tool_starts if e.tool_name == "web_search" and e.user_visible]) == 24
    assert len([e for e in tool_starts if e.tool_name == "web_search" and not e.user_visible]) == 1
    tool_results = [e for e in events if isinstance(e, ToolCallResult)]
    assert any(
        not e.success and not e.user_visible and e.tool_name == "web_search" and "budget reached" in (e.error or "")
        for e in tool_results
    )
    injected = [e for e in events if isinstance(e, InjectedMessageEvent)]
    assert any("web_search 调用已达到预算上限" in e.content and not e.user_visible for e in injected)
    done = [e for e in events if isinstance(e, DoneEvent)]
    assert len(done) == 1
    assert done[0].stop_reason == StopReason.END_TURN


@pytest.mark.asyncio
async def test_web_search_fanout_is_batched_with_hidden_deferrals():
    tool_calls = [
        ToolCall(
            id=f"web-{i}",
            type="function",
            function=FunctionCall(name="web_search", arguments={"query": f"q{i}"}),
        )
        for i in range(8)
    ]
    llm = MockLLM(
        [
            LLMResponse(content="", tool_calls=tool_calls, finish_reason="tool"),
            LLMResponse(content="final", finish_reason="stop"),
        ]
    )
    web_search = CountingWebSearchTool()

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"web_search": web_search},
            max_steps=5,
        )
    )

    assert web_search.calls == 6
    starts = [e for e in events if isinstance(e, ToolCallStart) and e.tool_name == "web_search"]
    assert len([e for e in starts if e.user_visible]) == 6
    assert len([e for e in starts if not e.user_visible]) == 2
    hidden_errors = [
        e
        for e in events
        if isinstance(e, ToolCallResult)
        and e.tool_name == "web_search"
        and not e.user_visible
        and not e.success
    ]
    assert len(hidden_errors) == 2
    assert all("deferred by runtime batching" in (e.error or "") for e in hidden_errors)
    injected = [e for e in events if isinstance(e, InjectedMessageEvent) and not e.user_visible]
    assert any("Deferred this batch: 2" in e.content for e in injected)


@pytest.mark.asyncio
async def test_web_search_skips_duplicate_queries_and_dedupes_result_urls():
    tool_calls = [
        ToolCall(
            id="web-a",
            type="function",
            function=FunctionCall(name="web_search", arguments={"query": "OpenAI release"}),
        ),
        ToolCall(
            id="web-b",
            type="function",
            function=FunctionCall(name="web_search", arguments={"query": " openai   release "}),
        ),
        ToolCall(
            id="web-c",
            type="function",
            function=FunctionCall(name="web_search", arguments={"query": "OpenAI official"}),
        ),
    ]
    llm = MockLLM(
        [
            LLMResponse(content="", tool_calls=tool_calls, finish_reason="tool"),
            LLMResponse(content="final", finish_reason="stop"),
        ]
    )
    web_search = JsonWebSearchTool(
        {
            "OpenAI release": "https://openai.com/news/example?utm_source=test#section",
            "OpenAI official": "https://openai.com/news/example/",
        }
    )

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"web_search": web_search},
            max_steps=5,
        )
    )

    assert web_search.calls == ["OpenAI release", "OpenAI official"]
    results = {
        e.tool_call_id: e
        for e in events
        if isinstance(e, ToolCallResult) and e.tool_name == "web_search"
    }
    assert results["web-b"].user_visible is False
    assert "Duplicate web_search query skipped" in (results["web-b"].error or "")
    deduped_payload = json.loads(results["web-c"].content)
    assert deduped_payload["refs"] == []
    assert deduped_payload["DedupedDuplicateCount"] == 1
    assert deduped_payload["DedupedNewCount"] == 0
    injected = [e for e in events if isinstance(e, InjectedMessageEvent) and not e.user_visible]
    assert any("Duplicate queries skipped this batch: 1" in e.content for e in injected)
    assert any("duplicate structured results this batch: 1" in e.content for e in injected)


@pytest.mark.asyncio
async def test_no_progress_breaker_injects_wrapup_and_stops():
    """After no_progress_limit consecutive failing steps, the breaker injects a
    synthesis nudge so the agent stops flailing instead of running to max_steps."""

    def fail_call(i):
        return LLMResponse(
            content="",
            tool_calls=[ToolCall(id=f"f{i}", type="function", function=FunctionCall(name="fail", arguments={"reason": str(i)}))],
            finish_reason="tool",
        )

    responses = [
        fail_call(0),  # step 0 → no_progress_steps = 1
        fail_call(1),  # step 1 → no_progress_steps = 2 (== limit)
        LLMResponse(content="Final answer from what I have.", finish_reason="stop"),  # step 2
    ]
    events = await collect(
        run_agent_loop(
            llm=MockLLM(responses),
            messages=_msgs(),
            tools={"fail": FailTool()},
            max_steps=20,
            no_progress_limit=2,
        )
    )

    injected = [e for e in events if isinstance(e, InjectedMessageEvent)]
    assert any("没有取得有效进展" in e.content for e in injected)
    done = [e for e in events if isinstance(e, DoneEvent)]
    assert len(done) == 1
    assert done[0].stop_reason == StopReason.END_TURN


@pytest.mark.asyncio
async def test_no_progress_breaker_disabled_by_default():
    """Without no_progress_limit, no stall nudge is injected (parent behavior)."""

    def fail_call(i):
        return LLMResponse(
            content="",
            tool_calls=[ToolCall(id=f"f{i}", type="function", function=FunctionCall(name="fail", arguments={"reason": str(i)}))],
            finish_reason="tool",
        )

    events = await collect(
        run_agent_loop(
            llm=MockLLM([fail_call(0), fail_call(1), fail_call(2)]),
            messages=_msgs(),
            tools={"fail": FailTool()},
            max_steps=3,  # exhausts without any breaker injection
        )
    )
    injected = [e for e in events if isinstance(e, InjectedMessageEvent)]
    assert not any("没有取得有效进展" in e.content for e in injected)


@pytest.mark.asyncio
async def test_no_progress_resets_on_successful_tool():
    """A successful tool call with content resets the no-progress counter."""

    def fail_call(i):
        return LLMResponse(
            content="",
            tool_calls=[ToolCall(id=f"f{i}", type="function", function=FunctionCall(name="fail", arguments={"reason": str(i)}))],
            finish_reason="tool",
        )

    echo_call = LLMResponse(
        content="",
        tool_calls=[ToolCall(id="e", type="function", function=FunctionCall(name="echo", arguments={"text": "ok"}))],
        finish_reason="tool",
    )
    # fail, success (resets), fail — never 2 consecutive failures, so no breaker.
    responses = [fail_call(0), echo_call, fail_call(1),
                 LLMResponse(content="done", finish_reason="stop")]
    events = await collect(
        run_agent_loop(
            llm=MockLLM(responses),
            messages=_msgs(),
            tools={"fail": FailTool(), "echo": EchoTool()},
            max_steps=20,
            no_progress_limit=2,
        )
    )
    injected = [e for e in events if isinstance(e, InjectedMessageEvent)]
    assert not any("没有取得有效进展" in e.content for e in injected)


@pytest.mark.asyncio
async def test_max_parallel_tools_caps_concurrency():
    """Even when the model emits many parallel_safe calls in one step, no more
    than max_parallel_tools execute concurrently; all still run."""

    class ConcurrencyProbeTool(Tool):
        parallel_safe = True

        def __init__(self):
            self.current = 0
            self.peak = 0
            self.total = 0

        @property
        def name(self):
            return "probe"

        @property
        def description(self):
            return "probe"

        @property
        def parameters(self):
            return {"type": "object", "properties": {"i": {"type": "string"}}}

        async def execute(self, **kwargs):
            self.current += 1
            self.total += 1
            self.peak = max(self.peak, self.current)
            await asyncio.sleep(0.02)
            self.current -= 1
            return ToolResult(success=True, content="ok")

    probe = ConcurrencyProbeTool()
    n_calls = 10
    responses = [
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(id=f"p{i}", type="function", function=FunctionCall(name="probe", arguments={"i": str(i)}))
                for i in range(n_calls)
            ],
            finish_reason="tool",
        ),
        LLMResponse(content="done", finish_reason="stop"),
    ]
    await collect(
        run_agent_loop(
            llm=MockLLM(responses),
            messages=_msgs(),
            tools={"probe": probe},
            max_steps=5,
            max_parallel_tools=3,
        )
    )

    assert probe.total == n_calls  # all calls ran
    assert probe.peak <= 3  # never exceeded the cap


@pytest.mark.asyncio
async def test_llm_error():
    """LLM exception yields ErrorEvent + Done(ERROR)."""

    class FailLLM:
        async def generate(self, messages, tools=None):
            raise ConnectionError("network down")

        async def generate_stream(self, messages, tools=None, **_):
            raise ConnectionError("network down")
            yield  # make it a valid async generator  # noqa: E501

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


def test_artifact_detect_in_output_dir(tmp_path):
    """File under {workspace}/output/ is found via regex."""
    out = tmp_path / "output"
    out.mkdir()
    (out / "chart.png").write_bytes(b"\x89PNG")
    arts = _detect_artifacts("t1", "jupyter", "Here is the result [chart.png]", str(tmp_path))
    assert len(arts) == 1
    a = arts[0]
    assert a.filename == "chart.png"
    assert a.kind == "image"
    assert a.mime == "image/png"
    assert a.size == 4
    assert a.rel_path == "output/chart.png"
    assert a.abs_path.endswith("output/chart.png")
    assert a.uri.startswith("file://")
    assert a.sha256 != ""
    assert a.produced_at != ""


def test_artifact_detect_in_explicit_artifact_root(tmp_path):
    """Host-supplied session output roots are scanned instead of shared output/."""
    session_out = tmp_path / "session-a" / "output"
    session_out.mkdir(parents=True)
    (session_out / "chart.png").write_bytes(b"\x89PNG")

    arts = _detect_artifacts(
        "t1",
        "jupyter",
        "Here is the result [chart.png]",
        str(tmp_path),
        artifact_root_dir=session_out,
    )

    assert len(arts) == 1
    assert arts[0].rel_path == "session-a/output/chart.png"
    assert arts[0].abs_path == str(session_out / "chart.png")


def test_artifact_detect_data_kind(tmp_path):
    """CSV under output/ is classified as data."""
    out = tmp_path / "output"
    out.mkdir()
    (out / "results.csv").write_text("a,b\n1,2")
    arts = _detect_artifacts("t2", "jupyter", "Saved to [results.csv]", str(tmp_path))
    assert len(arts) == 1
    assert arts[0].kind == "data"
    assert "csv" in arts[0].mime
    assert arts[0].rel_path == "output/results.csv"


def test_artifact_detect_ignores_workspace_root(tmp_path):
    """Files at workspace root (user-supplied inputs) are NOT picked up."""
    (tmp_path / "user-upload.png").write_bytes(b"\x89PNG")
    arts = _detect_artifacts("t3", "jupyter", "See [user-upload.png]", str(tmp_path))
    assert arts == []


def test_artifact_detect_no_match(tmp_path):
    """No artifact when file doesn't exist."""
    (tmp_path / "output").mkdir()
    arts = _detect_artifacts("t4", "jupyter", "See [missing.png]", str(tmp_path))
    assert arts == []


def test_artifact_detect_multiple(tmp_path):
    """Multiple file references in one output."""
    out = tmp_path / "output"
    out.mkdir()
    (out / "a.png").write_bytes(b"\x89PNG")
    (out / "b.pdf").write_bytes(b"%PDF")
    arts = _detect_artifacts("t5", "jupyter", "Results: [a.png] and [b.pdf]", str(tmp_path))
    assert len(arts) == 2
    names = {a.filename for a in arts}
    assert names == {"a.png", "b.pdf"}
    kinds = {a.kind for a in arts}
    assert kinds == {"image", "document"}


def test_artifact_detect_path_traversal_blocked(tmp_path):
    """Filenames with traversal that resolve outside output/ are rejected."""
    out = tmp_path / "output"
    out.mkdir()
    (tmp_path / "secret.txt").write_text("nope")
    arts = _detect_artifacts("t6", "jupyter", "See [../secret.txt]", str(tmp_path))
    assert arts == []


def test_detect_new_files_dedupes_against_regex_artifacts(tmp_path):
    """Diff-based detection must skip files already emitted by regex detection.

    Regression for the AttributeError ``'ArtifactEvent' object has no attribute
    'path'`` that surfaced in production whenever a tool produced any artifact:
    core.py built the dedupe set from ``a.path`` but ArtifactEvent only exposes
    ``abs_path`` / ``rel_path``. The bug masked every downstream tool failure as
    a generic ACP "Internal error".
    """
    out = tmp_path / "output"
    out.mkdir()
    (out / "chart.png").write_bytes(b"\x89PNG")

    pre_files: set = set()
    regex_arts = _detect_artifacts("tc", "jupyter", "Saved [chart.png]", str(tmp_path))
    assert len(regex_arts) == 1

    already = {a.abs_path for a in regex_arts}
    post_files = _snapshot_workspace(str(tmp_path))

    new_arts = _detect_new_files("tc", pre_files, post_files, already, str(tmp_path))
    assert new_arts == [], "file already emitted by regex pass must not be re-emitted"


def test_snapshot_workspace_uses_explicit_artifact_root(tmp_path):
    session_out = tmp_path / "session-b" / "output"
    default_out = tmp_path / "output"
    session_out.mkdir(parents=True)
    default_out.mkdir()
    expected = session_out / "deck.pptx"
    expected.write_bytes(b"ppt")
    (default_out / "old.png").write_bytes(b"old")

    files = _snapshot_workspace(str(tmp_path), artifact_root_dir=session_out)

    assert files == {expected}


@pytest.mark.asyncio
async def test_run_agent_loop_emits_artifact_without_attribute_error(tmp_path):
    """End-to-end regression for ``ArtifactEvent.path`` AttributeError.

    Before the fix, ``{a.path for a in regex_artifacts}`` raised AttributeError
    inside the post-tool artifact-detection block whenever a tool produced a
    file, which surfaced as ACP "Internal error" and masked real tool failures.
    Drives ``run_agent_loop`` with a tool that writes a PNG under ``output/``
    and references it in its result; the artifact must be yielded once, no
    ErrorEvent should fire, and the loop must reach ``DoneEvent``.
    """
    from pathlib import Path as _Path

    class WriteAndAnnounceTool(Tool):
        def __init__(self, workspace_dir: str):
            self._ws = workspace_dir

        @property
        def name(self):
            return "make_chart"

        @property
        def description(self):
            return "Writes a PNG under output/ and references it in result"

        @property
        def parameters(self):
            return {"type": "object", "properties": {}}

        async def execute(self):
            out = _Path(self._ws) / "output"
            out.mkdir(parents=True, exist_ok=True)
            (out / "chart.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            return ToolResult(success=True, content="Saved [chart.png]")

    msgs = _msgs()
    llm = MockLLM([
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="t1",
                    type="function",
                    function=FunctionCall(name="make_chart", arguments={}),
                )
            ],
            finish_reason="tool",
        ),
        LLMResponse(content="done", finish_reason="stop"),
    ])

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=msgs,
            tools={"make_chart": WriteAndAnnounceTool(str(tmp_path))},
            max_steps=5,
            workspace_dir=str(tmp_path),
        )
    )

    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert errors == [], f"unexpected ErrorEvent(s): {errors}"

    artifacts = [e for e in events if isinstance(e, ArtifactEvent)]
    assert len(artifacts) == 1, f"expected exactly one artifact, got {artifacts}"
    assert artifacts[0].rel_path == "output/chart.png"

    done = [e for e in events if isinstance(e, DoneEvent)]
    assert done and done[0].stop_reason == StopReason.END_TURN


@pytest.mark.asyncio
async def test_run_agent_loop_can_disable_output_artifact_detection(tmp_path):
    from pathlib import Path as _Path

    class WriteAndAnnounceTool(Tool):
        @property
        def name(self):
            return "make_chart"

        @property
        def description(self):
            return "Writes a PNG under output/ and references it in result"

        @property
        def parameters(self):
            return {"type": "object", "properties": {}}

        async def execute(self):
            out = _Path(tmp_path) / "output"
            out.mkdir(parents=True, exist_ok=True)
            (out / "chart.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            return ToolResult(success=True, content="Saved [chart.png]")

    msgs = _msgs()
    llm = MockLLM([
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="t1",
                    type="function",
                    function=FunctionCall(name="make_chart", arguments={}),
                )
            ],
            finish_reason="tool",
        ),
        LLMResponse(content="done", finish_reason="stop"),
    ])

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=msgs,
            tools={"make_chart": WriteAndAnnounceTool()},
            max_steps=5,
            workspace_dir=str(tmp_path),
            artifact_detection_enabled=False,
        )
    )

    assert [e for e in events if isinstance(e, ArtifactEvent)] == []
    done = [e for e in events if isinstance(e, DoneEvent)]
    assert done and done[0].stop_reason == StopReason.END_TURN


# ── Stream interrupted tests ─────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_interrupted_preserves_partial_assistant_message(tmp_path):
    """When the upstream LLM closes the chunked HTTP stream mid-response,
    core.py must NOT mark the error fatal and MUST persist the partial
    assistant content into history, so user-triggered "继续" has a coherent
    base to continue from instead of restarting empty.
    """
    from box_agent.retry import StreamInterrupted

    class DroppingStreamLLM:
        async def generate(self, messages, tools=None):  # pragma: no cover
            raise AssertionError("should not be called")

        async def generate_stream(self, messages, tools=None, **_):
            yield StreamEvent(type="text", delta="第一篇：")
            yield StreamEvent(type="text", delta="中国乘用车市场总览")
            raise StreamInterrupted(
                last_exception=RuntimeError(
                    "peer closed connection without sending complete message body "
                    "(incomplete chunked read)"
                ),
                partial_text="第一篇：中国乘用车市场总览",
                partial_thinking="",
                provider_request_id="req-x",
            )

    msgs = _msgs()
    events = await collect(
        run_agent_loop(
            llm=DroppingStreamLLM(),
            messages=msgs,
            tools={},
            max_steps=2,
            workspace_dir=str(tmp_path),
        )
    )

    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(errors) == 1, f"expected exactly one ErrorEvent, got {errors}"
    assert errors[0].is_fatal is False, "stream interruption must not be fatal"
    assert "interrupted" in errors[0].message.lower()

    done = [e for e in events if isinstance(e, DoneEvent)]
    assert done and done[0].stop_reason == StopReason.INTERRUPTED
    assert done[0].final_content == "第一篇：中国乘用车市场总览"

    assistant_msgs = [m for m in msgs if m.role == "assistant"]
    assert assistant_msgs, "partial assistant message must be appended to history"
    assert assistant_msgs[-1].content == "第一篇：中国乘用车市场总览"


# ── Micro-compact tests ──────────────────────────────────────────


from box_agent.core import _micro_compact, _KEEP_RECENT_TOOL_RESULTS, _MIN_COMPACT_LEN


def _make_tool_msg(name: str, content: str, tc_id: str = "tc-0") -> Message:
    return Message(role="tool", content=content, tool_call_id=tc_id, name=name)


def test_micro_compact_no_op_when_few_tool_msgs():
    """Should not compact when tool messages <= KEEP_RECENT."""
    msgs = [
        Message(role="system", content="sys"),
        Message(role="user", content="hi"),
        Message(role="assistant", content="ok"),
        _make_tool_msg("bash", "x" * 500, "tc-1"),
        _make_tool_msg("bash", "y" * 500, "tc-2"),
        _make_tool_msg("bash", "z" * 500, "tc-3"),
    ]
    assert _micro_compact(msgs) == 0
    # Content should be unchanged
    assert msgs[3].content == "x" * 500


def test_micro_compact_replaces_old_tool_results():
    """Old tool results beyond KEEP_RECENT should be compacted."""
    msgs = [
        Message(role="system", content="sys"),
        Message(role="user", content="analyze data"),
        Message(role="assistant", content="calling tools"),
        _make_tool_msg("execute_code", "DataFrame with 1000 rows\n" + "x" * 500, "tc-1"),
        _make_tool_msg("execute_code", "Statistical summary\n" + "y" * 500, "tc-2"),
        Message(role="assistant", content="more analysis"),
        _make_tool_msg("bash", "file list\n" + "z" * 500, "tc-3"),
        _make_tool_msg("execute_code", "Final chart\n" + "w" * 500, "tc-4"),
        _make_tool_msg("read", "recent content\n" + "v" * 500, "tc-5"),
        _make_tool_msg("execute_code", "latest result\n" + "u" * 500, "tc-6"),
    ]
    # 6 tool messages, keep last 3 → compact first 3
    compacted = _micro_compact(msgs)
    assert compacted == 3

    # First 3 tool messages should be compacted
    assert msgs[3].content.startswith("[Previous result from execute_code:")
    assert msgs[4].content.startswith("[Previous result from execute_code:")
    assert msgs[6].content.startswith("[Previous result from bash:")

    # Last 3 tool messages should be intact
    assert msgs[7].content.startswith("Final chart")
    assert msgs[8].content.startswith("recent content")
    assert msgs[9].content.startswith("latest result")


def test_micro_compact_preserves_short_content():
    """Tool results shorter than MIN_COMPACT_LEN should not be compacted."""
    msgs = [
        Message(role="system", content="sys"),
        _make_tool_msg("bash", "short", "tc-1"),  # short, should be skipped
        _make_tool_msg("execute_code", "x" * 500, "tc-2"),  # long, should be compacted
        _make_tool_msg("bash", "z" * 500, "tc-3"),
        _make_tool_msg("read", "a" * 500, "tc-4"),
        _make_tool_msg("execute_code", "b" * 500, "tc-5"),
    ]
    compacted = _micro_compact(msgs)
    # First 2 are candidates; tc-1 is short so only tc-2 gets compacted
    assert compacted == 1
    assert msgs[1].content == "short"  # preserved
    assert msgs[2].content.startswith("[Previous result from execute_code:")


def test_micro_compact_preserves_tool_call_id():
    """Compacted messages must keep tool_call_id and name for protocol correctness."""
    msgs = [
        Message(role="system", content="sys"),
        _make_tool_msg("bash", "x" * 500, "tc-42"),
        _make_tool_msg("read", "y" * 500, "tc-43"),
        _make_tool_msg("bash", "z" * 500, "tc-44"),
        _make_tool_msg("read", "w" * 500, "tc-45"),
    ]
    _micro_compact(msgs)
    # First message should be compacted but retain metadata
    assert msgs[1].tool_call_id == "tc-42"
    assert msgs[1].name == "bash"


def test_micro_compact_first_line_hint():
    """Compacted placeholder should include the first line as a hint."""
    msgs = [
        Message(role="system", content="sys"),
        _make_tool_msg("execute_code", "Revenue: $1.2M\nRow 1: ...\n" + "x" * 500, "tc-1"),
        _make_tool_msg("bash", "ok", "tc-2"),
        _make_tool_msg("read", "a" * 500, "tc-3"),
        _make_tool_msg("bash", "b" * 500, "tc-4"),
    ]
    _micro_compact(msgs)
    assert "Revenue: $1.2M" in msgs[1].content


def test_micro_compact_web_search_retains_evidence_from_json_payload():
    """Old web_search results should keep useful evidence, not a bare JSON brace."""
    payload = json.dumps(
        {
            "Query": "2026 AI model releases",
            "ResultCount": 2,
            "AuthLevel": 1,
            "Results": [
                {
                    "Title": "OpenAI announces GPT example",
                    "Url": "https://openai.com/example",
                    "Snippet": "Official release notes for a model update.",
                },
                {
                    "Title": "Anthropic announces Claude example",
                    "Url": "https://anthropic.com/example",
                    "Snippet": "Company news page for a model release.",
                },
            ],
        },
        indent=2,
    )
    msgs = [
        Message(role="system", content="sys"),
        _make_tool_msg("web_search", payload, "tc-1"),
        _make_tool_msg("bash", "ok", "tc-2"),
        _make_tool_msg("read", "a" * 500, "tc-3"),
        _make_tool_msg("bash", "b" * 500, "tc-4"),
    ]

    _micro_compact(msgs)

    assert "compacted evidence retained" in msgs[1].content
    assert "query=2026 AI model releases" in msgs[1].content
    assert "OpenAI announces GPT example" in msgs[1].content
    assert "https://openai.com/example" in msgs[1].content
    assert "Official release notes" in msgs[1].content
    assert msgs[1].content != "[Previous result from web_search: {...]"


# ── Artifact envelope / helpers ──────────────────────────────


def test_safe_output_name_kebab_lowercase():
    from box_agent.core import safe_output_name
    assert safe_output_name("My Chart Final.PNG") == "my-chart-final.png"
    assert safe_output_name("结果.csv", default_ext=".bin") == "结果.csv" or safe_output_name("结果.csv").endswith(".csv")
    assert safe_output_name("", default_ext="md") == "artifact.md"


def test_avoid_collision(tmp_path):
    from box_agent.core import avoid_collision
    (tmp_path / "chart.png").write_bytes(b"x")
    p = avoid_collision(tmp_path, "chart.png")
    assert p.name == "chart-2.png"
    p.write_bytes(b"x")
    assert avoid_collision(tmp_path, "chart.png").name == "chart-3.png"


def test_artifact_envelope_shape(tmp_path):
    from box_agent.acp import _artifact_envelope
    from box_agent.core import ensure_output_dir, _make_artifact
    out = ensure_output_dir(tmp_path)
    f = out / "report.xlsx"
    f.write_bytes(b"PK\x03\x04")
    art = _make_artifact("tc-1", f, tmp_path)
    env = _artifact_envelope(art, str(out), session_id="office-session-1")
    assert env["type"] == "artifact"
    assert env["kind"] == "spreadsheet"
    assert env["filename"] == "report.xlsx"
    assert env["rel_path"] == "output/report.xlsx"
    assert env["abs_path"].endswith("output/report.xlsx")
    assert env["uri"].startswith("file://")
    assert env["size"] == 4
    assert env["sha256"]
    assert env["produced_at"]
    assert env["tool_call_id"] == "tc-1"
    assert env["output_dir"] == str(out)
    assert env["session_id"] == "office-session-1"
    assert env["sessionId"] == "office-session-1"
    # canonical schema only — no legacy aliases
    assert "artifact_type" not in env
    assert "path" not in env
    assert "mime_type" not in env
    assert "size_bytes" not in env
    assert "sandbox_workspace" not in env


# ── Summarization (_maybe_summarize / _create_summary) ───────


class _FakeSummaryLLM:
    """Records calls and returns a canned summary."""

    def __init__(self, response: str = "concise summary", *, raise_exc: Exception | None = None):
        self._response = response
        self._raise = raise_exc
        self.calls: list[dict] = []

    async def generate(self, messages, tools=None, *, thinking_enabled: bool = False, session_id: str = "", **_):
        self.calls.append({
            "n_messages": len(messages),
            "tools": tools,
            "thinking_enabled": thinking_enabled,
            "session_id": session_id,
        })
        if self._raise is not None:
            raise self._raise
        return LLMResponse(content=self._response, thinking=None, tool_calls=None, finish_reason="stop")

    async def generate_stream(self, messages, tools=None, **_):
        raise NotImplementedError


@pytest.mark.asyncio
async def test_create_summary_passes_thinking_disabled_and_no_tools():
    """_create_summary must explicitly disable thinking and omit tools (cross-provider safety)."""
    from box_agent.core import _create_summary
    llm = _FakeSummaryLLM("ok")
    out = await _create_summary(llm, [Message(role="assistant", content="did something")], 1)
    assert out == "ok"
    assert len(llm.calls) == 1
    assert llm.calls[0]["thinking_enabled"] is False
    assert llm.calls[0]["tools"] is None


@pytest.mark.asyncio
async def test_create_summary_propagates_exceptions():
    """Failure path must raise — old behavior returned the un-summarized input (bloat bug)."""
    from box_agent.core import _create_summary
    llm = _FakeSummaryLLM(raise_exc=RuntimeError("provider down"))
    with pytest.raises(RuntimeError):
        await _create_summary(llm, [Message(role="assistant", content="x")], 1)


@pytest.mark.asyncio
async def test_maybe_summarize_drops_exec_msgs_on_llm_failure():
    """When _create_summary raises, exec_msgs should be DROPPED, never returned verbatim."""
    from box_agent.core import _maybe_summarize
    msgs = [
        Message(role="system", content="sys"),
        Message(role="user", content="please do X"),
        Message(role="assistant", content="A" * 5000),  # bulk content
        Message(role="tool", content="B" * 5000, tool_call_id="t1", name="bash"),
    ]
    llm = _FakeSummaryLLM(raise_exc=RuntimeError("network"))
    new_msgs, skip_next, _est = await _maybe_summarize(llm, msgs, token_limit=10, api_total_tokens=0, skip_check=False)
    assert new_msgs is not None
    assert skip_next is True
    # System + user kept; exec_msgs (assistant+tool) dropped because summary failed
    assert [m.role for m in new_msgs] == ["system", "user"]
    # Token count strictly less than original
    assert sum(len(str(m.content)) for m in new_msgs) < sum(len(str(m.content)) for m in msgs)


@pytest.mark.asyncio
async def test_maybe_summarize_inserts_summary_marker():
    from box_agent.core import _maybe_summarize, _SUMMARY_MARKER
    msgs = [
        Message(role="system", content="sys"),
        Message(role="user", content="task"),
        Message(role="assistant", content="x" * 5000),
    ]
    llm = _FakeSummaryLLM("brief")
    new_msgs, _, _ = await _maybe_summarize(llm, msgs, token_limit=10, api_total_tokens=0, skip_check=False)
    assert new_msgs is not None
    # Last message is the summary marker as a user-role replacement
    assert new_msgs[-1].role == "user"
    assert new_msgs[-1].content.startswith(_SUMMARY_MARKER)
    assert "brief" in new_msgs[-1].content


@pytest.mark.asyncio
async def test_maybe_summarize_collapses_orphan_summary_markers():
    """Stale summary markers with no exec_msgs after them should be dropped on next compaction."""
    from box_agent.core import _maybe_summarize, _SUMMARY_MARKER
    msgs = [
        Message(role="system", content="sys"),
        Message(role="user", content="round 0 prompt"),
        Message(role="user", content=f"{_SUMMARY_MARKER}\n\nold summary 1"),
        Message(role="user", content=f"{_SUMMARY_MARKER}\n\nold summary 2"),  # orphan
        Message(role="user", content="round 3 prompt"),
        Message(role="assistant", content="z" * 5000),
    ]
    llm = _FakeSummaryLLM("new sum")
    new_msgs, _, _ = await _maybe_summarize(llm, msgs, token_limit=10, api_total_tokens=0, skip_check=False)
    assert new_msgs is not None
    # The orphan stale markers (no exec after) are dropped; only the active
    # round (round 3 prompt + new summary) plus the initial user msg remain.
    summary_count = sum(1 for m in new_msgs if m.role == "user" and isinstance(m.content, str) and m.content.startswith(_SUMMARY_MARKER))
    # At most one summary marker (the freshly created one for round 3)
    assert summary_count == 1


@pytest.mark.asyncio
async def test_maybe_summarize_skip_check_short_circuits():
    from box_agent.core import _maybe_summarize
    llm = _FakeSummaryLLM("never called")
    new_msgs, skip_next, est = await _maybe_summarize(llm, [Message(role="user", content="x")], token_limit=1000, api_total_tokens=0, skip_check=True)
    assert new_msgs is None
    assert skip_next is False
    assert est == 0
    assert llm.calls == []


@pytest.mark.asyncio
async def test_maybe_summarize_below_threshold_noop():
    from box_agent.core import _maybe_summarize
    msgs = [Message(role="system", content="s"), Message(role="user", content="x")]
    llm = _FakeSummaryLLM("never called")
    new_msgs, skip_next, _est = await _maybe_summarize(llm, msgs, token_limit=10_000, api_total_tokens=100, skip_check=False)
    assert new_msgs is None
    assert skip_next is False
    assert llm.calls == []


def test_micro_compact_token_budget_shrinks_keep_window_when_recent_oversized():
    """If the recent N tool results exceed the token budget, keep window shrinks
    so a few enormous outputs cannot bypass Layer 1 compaction."""
    from box_agent.core import (
        _micro_compact,
        _KEEP_RECENT_TOOL_RESULTS,
        _KEEP_RECENT_TOOL_TOKEN_BUDGET,
        _approx_tokens_for_content,
    )

    # Build incompressible-ish content (varied tokens). Each ~7000+ tokens so
    # 3 of them blow past the 12k budget regardless of tokenizer.
    import string
    import random
    rng = random.Random(0)
    big_payload = " ".join(
        "".join(rng.choices(string.ascii_letters + string.digits, k=6))
        for _ in range(7000)
    )
    assert _approx_tokens_for_content(big_payload) > _KEEP_RECENT_TOOL_TOKEN_BUDGET // 2

    msgs: list[Message] = [Message(role="system", content="sys")]
    for i in range(_KEEP_RECENT_TOOL_RESULTS + 1):  # 4 tool messages
        msgs.append(Message(role="assistant", content="", tool_calls=None))
        msgs.append(Message(role="tool", content=big_payload, tool_call_id=f"t{i}", name="bash"))

    compacted = _micro_compact(msgs)
    # Strict-N keep would compact 4 - 3 = 1. Token-aware keep should compact more.
    assert compacted >= 2, f"token-aware keep should compact more than the strict N-recent baseline; got {compacted}"

    # The most recent tool message must always be preserved verbatim.
    last_tool = [m for m in msgs if m.role == "tool"][-1]
    assert last_tool.content == big_payload


def test_micro_compact_preserves_at_least_one_recent_when_single_giant():
    """Single giant tool result must not be compacted (keep_count never < 1)."""
    from box_agent.core import _micro_compact
    msgs = [
        Message(role="system", content="sys"),
        Message(role="assistant", content=""),
        Message(role="tool", content="y" * 100_000, tool_call_id="t0", name="bash"),
    ]
    n = _micro_compact(msgs)
    assert n == 0
    assert len(msgs[-1].content) == 100_000


# ── _cleanup_incomplete_messages ─────────────────────────────


def test_cleanup_keeps_complete_assistant_turn():
    """A trailing assistant turn with content and no tool_calls is complete."""
    from box_agent.core import _cleanup_incomplete_messages
    msgs = [
        Message(role="system", content="sys"),
        Message(role="user", content="hi"),
        Message(role="assistant", content="hello there"),
    ]
    n = _cleanup_incomplete_messages(msgs)
    assert n == 0
    assert msgs[-1].content == "hello there"


def test_cleanup_removes_empty_assistant_turn():
    """An assistant turn with no content and no tool_calls is incomplete (LLM cut off)."""
    from box_agent.core import _cleanup_incomplete_messages
    msgs = [
        Message(role="system", content="sys"),
        Message(role="user", content="hi"),
        Message(role="assistant", content=""),
    ]
    n = _cleanup_incomplete_messages(msgs)
    assert n == 1
    assert msgs[-1].role == "user"


def test_cleanup_removes_partial_tool_call_turn():
    """Assistant has 2 tool_calls but only 1 tool response → incomplete."""
    from box_agent.core import _cleanup_incomplete_messages
    msgs = [
        Message(role="system", content="sys"),
        Message(role="user", content="hi"),
        Message(role="assistant", content="", tool_calls=[
            ToolCall(id="t1", type="function", function=FunctionCall(name="echo", arguments={})),
            ToolCall(id="t2", type="function", function=FunctionCall(name="echo", arguments={})),
        ]),
        Message(role="tool", content="result1", tool_call_id="t1", name="echo"),
    ]
    n = _cleanup_incomplete_messages(msgs)
    assert n == 2  # removed assistant + 1 tool
    assert msgs[-1].role == "user"


def test_cleanup_keeps_complete_tool_call_turn():
    """Assistant with N tool_calls and N tool responses is complete — don't touch."""
    from box_agent.core import _cleanup_incomplete_messages
    msgs = [
        Message(role="system", content="sys"),
        Message(role="user", content="hi"),
        Message(role="assistant", content="", tool_calls=[
            ToolCall(id="t1", type="function", function=FunctionCall(name="echo", arguments={})),
        ]),
        Message(role="tool", content="result1", tool_call_id="t1", name="echo"),
    ]
    before = list(msgs)
    n = _cleanup_incomplete_messages(msgs)
    assert n == 0
    assert msgs == before


def test_cleanup_keeps_thinking_only_assistant():
    """Assistant with only thinking (no content, no tool_calls) — treat as having output."""
    from box_agent.core import _cleanup_incomplete_messages
    msgs = [
        Message(role="system", content="sys"),
        Message(role="user", content="hi"),
        Message(role="assistant", content="", thinking="I was thinking..."),
    ]
    n = _cleanup_incomplete_messages(msgs)
    assert n == 0


def test_cleanup_noop_when_no_assistant_turn():
    from box_agent.core import _cleanup_incomplete_messages
    msgs = [Message(role="system", content="sys"), Message(role="user", content="hi")]
    before = list(msgs)
    n = _cleanup_incomplete_messages(msgs)
    assert n == 0
    assert msgs == before


# ── B1 regression: parallel branch artifact detection ───────────


class _ParallelFileTool(Tool):
    """A parallel_safe tool that writes a file into the workspace output dir,
    mirroring how sub_agent produces artifacts that only the diff layer catches
    (no [filename] reference in its text output)."""

    parallel_safe = True

    def __init__(self, workspace_dir: str, filename: str):
        self._workspace_dir = workspace_dir
        self._filename = filename

    @property
    def name(self):
        return "make_file"

    @property
    def description(self):
        return "Writes a file"

    @property
    def parameters(self):
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        out_dir = __import__("pathlib").Path(self._workspace_dir) / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / self._filename).write_text("generated by parallel tool")
        # Note: content does NOT reference the filename, so only the diff layer
        # can surface it.
        return ToolResult(success=True, content="created a file")


@pytest.mark.asyncio
async def test_parallel_tool_artifact_detected_via_diff(tmp_path):
    """B1: files produced by a parallel_safe tool must be surfaced as artifacts
    via the diff layer (previously the parallel branch had no detection)."""
    (tmp_path / "output").mkdir(exist_ok=True)
    tool = _ParallelFileTool(str(tmp_path), "parallel_artifact.txt")

    llm = MockLLM([
        LLMResponse(
            content="making files",
            tool_calls=[
                ToolCall(id="p1", type="function", function=FunctionCall(name="make_file", arguments={})),
            ],
            finish_reason="tool",
        ),
        LLMResponse(content="done", finish_reason="stop"),
    ])

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"make_file": tool},
            max_steps=5,
            workspace_dir=str(tmp_path),
            artifact_detection_enabled=True,
            artifact_root_dir=str(tmp_path / "output"),
        )
    )

    artifacts = [e for e in events if isinstance(e, ArtifactEvent)]
    assert any(a.filename == "parallel_artifact.txt" for a in artifacts), (
        f"expected parallel_artifact.txt in artifacts, got {[a.filename for a in artifacts]}"
    )


@pytest.mark.asyncio
async def test_parallel_two_tools_artifacts_no_duplicates(tmp_path):
    """Two parallel_safe tools each create a file; the single post-batch diff
    pass must emit each artifact exactly once (no per-result duplication)."""
    (tmp_path / "output").mkdir(exist_ok=True)

    class _MultiFileTool(_ParallelFileTool):
        @property
        def name(self):
            return self._tool_name

        def __init__(self, workspace_dir, filename, tool_name):
            super().__init__(workspace_dir, filename)
            self._tool_name = tool_name

    tool_a = _MultiFileTool(str(tmp_path), "a.txt", "make_a")
    tool_b = _MultiFileTool(str(tmp_path), "b.txt", "make_b")

    llm = MockLLM([
        LLMResponse(
            content="making files",
            tool_calls=[
                ToolCall(id="p1", type="function", function=FunctionCall(name="make_a", arguments={})),
                ToolCall(id="p2", type="function", function=FunctionCall(name="make_b", arguments={})),
            ],
            finish_reason="tool",
        ),
        LLMResponse(content="done", finish_reason="stop"),
    ])

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"make_a": tool_a, "make_b": tool_b},
            max_steps=5,
            workspace_dir=str(tmp_path),
            artifact_detection_enabled=True,
            artifact_root_dir=str(tmp_path / "output"),
        )
    )

    artifacts = [e for e in events if isinstance(e, ArtifactEvent)]
    names = sorted(a.filename for a in artifacts)
    assert names == ["a.txt", "b.txt"], f"expected exactly a.txt and b.txt once each, got {names}"

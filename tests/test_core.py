"""Tests for the shared agent execution core (box_agent.core)."""

import asyncio
import json
import threading
from pathlib import Path
from time import monotonic

import pytest

import box_agent.core as core
from box_agent.config import ToolLimitsConfig
from box_agent.core import (
    _detect_artifacts,
    _detect_new_files,
    _snapshot_workspace,
    text_is_short_acknowledgement,
    text_is_short_non_task_reply,
    text_requests_plan_start,
)
from box_agent.core import FINAL_SUMMARY_TOOL_CALL_THRESHOLD as _FS_THRESHOLD
from box_agent.loop_guards import CompletionGate, repeated_stream_pattern
from box_agent.runtime import run_agent_loop
from box_agent.events import (
    ArtifactEvent,
    ContentEvent,
    ContextCheckpointEvent,
    DoneEvent,
    ErrorEvent,
    InjectedMessageEvent,
    LLMActivityEvent,
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
from box_agent.workflow_policy import WorkflowCheckpointUpdate
from box_agent.workflow_checkpoint_store import load_workflow_checkpoint
from box_agent.workflows import EXTERNAL_SKILL_WORKFLOW_KIND
from box_agent.schema import FunctionCall, LLMResponse, Message, StreamEvent, ToolCall
from box_agent.tools.base import EventEmittingTool, Tool, ToolResult
from box_agent.tools.file_tools import AppendTool, EditTool, ReadTool, WriteTool
from box_agent.tools.staged_file_write_tool import StagedFileWriteTool
from box_agent.tools.sub_agent_tool import SubAgentTool


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


class _RecoverablePresentationPolicy:
    kind = "controlled_presentation"
    checkpoint_injection_id = "test-checkpoint"
    evidence_read_batch_size = 1
    stage = "outline"

    def build_checkpoint(self) -> str:
        return "CONTROLLED_PRESENTATION_STAGE=outline"

    def update_checkpoint(self, text: str) -> WorkflowCheckpointUpdate:
        return WorkflowCheckpointUpdate(text=text, changed=True)


class CapturingStreamLLM(MockLLM):
    """Mock LLM that keeps a snapshot of each message list it receives."""

    def __init__(self, responses: list[LLMResponse]):
        super().__init__(responses)
        self.message_calls: list[list[Message]] = []

    async def generate_stream(self, messages, tools=None, **_):
        self.message_calls.append([msg.model_copy(deep=True) for msg in messages])
        async for event in super().generate_stream(messages, tools=tools, **_):
            yield event


@pytest.mark.asyncio
async def test_context_limit_pauses_only_after_durable_workflow_checkpoint(
    tmp_path,
) -> None:
    (tmp_path / "output").mkdir()
    (tmp_path / "output" / "outline.json").write_text(
        '{"slides": []}\n',
        encoding="utf-8",
    )
    messages = [Message(role="system", content="system instructions exceed limit")]

    events = [
        event
        async for event in run_agent_loop(
            llm=MockLLM([]),
            messages=messages,
            tools={},
            max_steps=1,
            token_limit=1,
            workspace_dir=str(tmp_path),
            workflow_policy=_RecoverablePresentationPolicy(),
        )
    ]

    checkpoint = next(event for event in events if isinstance(event, ContextCheckpointEvent))
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert checkpoint.workflow_kind == "controlled_presentation"
    assert checkpoint.stage == "outline"
    assert done.stop_reason is StopReason.CHECKPOINT_PAUSED
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert Path(checkpoint.path).is_file()
    assert [message.role for message in messages] == ["system", "assistant"]
    assert messages[-1].content == done.final_content


@pytest.mark.asyncio
async def test_context_limit_keeps_error_for_unregistered_workflow(tmp_path) -> None:
    class UnregisteredPolicy(_RecoverablePresentationPolicy):
        kind = "third_party_untrusted"

    events = [
        event
        async for event in run_agent_loop(
            llm=MockLLM([]),
            messages=[Message(role="system", content="system instructions exceed limit")],
            tools={},
            max_steps=1,
            token_limit=1,
            workspace_dir=str(tmp_path),
            workflow_policy=UnregisteredPolicy(),
        )
    ]

    assert not any(isinstance(event, ContextCheckpointEvent) for event in events)
    assert any(isinstance(event, ErrorEvent) for event in events)
    assert next(event for event in events if isinstance(event, DoneEvent)).stop_reason is StopReason.ERROR


@pytest.mark.asyncio
async def test_tool_budget_pauses_incomplete_recoverable_workflow(tmp_path) -> None:
    (tmp_path / "output").mkdir()
    messages = _msgs()
    llm = MockLLM(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="budget-call",
                        type="function",
                        function=FunctionCall(
                            name="echo",
                            arguments={"text": "progress"},
                        ),
                    )
                ],
                finish_reason="tool",
            )
        ]
    )

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=messages,
            tools={"echo": EchoTool()},
            max_steps=3,
            completion_gate=CompletionGate(
                required_changed_artifact_globs=("output/**/*.pptx",),
                max_tool_calls=1,
                workflow_checkpoint_kind="controlled_presentation",
            ),
            workspace_dir=str(tmp_path),
        )
    )

    checkpoint = next(
        event for event in events if isinstance(event, ContextCheckpointEvent)
    )
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert checkpoint.workflow_kind == "controlled_presentation"
    assert done.stop_reason is StopReason.CHECKPOINT_PAUSED
    assert "Tool-call budget" in done.final_content
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert Path(checkpoint.path).is_file()
    assert [message.role for message in messages] == ["system", "assistant"]


@pytest.mark.asyncio
async def test_tool_budget_pauses_external_skill_with_data_only_checkpoint(
    tmp_path,
) -> None:
    (tmp_path / "output").mkdir()
    messages = _msgs()
    llm = MockLLM(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="external-skill-budget-call",
                        type="function",
                        function=FunctionCall(
                            name="echo",
                            arguments={"text": "progress"},
                        ),
                    )
                ],
                finish_reason="tool",
            )
        ]
    )

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=messages,
            tools={"echo": EchoTool()},
            max_steps=3,
            completion_gate=CompletionGate(
                required_changed_artifact_globs=("output/**/*.pptx",),
                max_tool_calls=1,
                workflow_checkpoint_kind=EXTERNAL_SKILL_WORKFLOW_KIND,
                workflow_options={
                    "skill_name": "ppt-master",
                    "skill_source": "user",
                    "task_text": "/ppt-master topic",
                    "artifact_globs": '["output/**/*.pptx"]',
                },
            ),
            workspace_dir=str(tmp_path),
        )
    )

    checkpoint_event = next(
        event for event in events if isinstance(event, ContextCheckpointEvent)
    )
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert checkpoint_event.workflow_kind == EXTERNAL_SKILL_WORKFLOW_KIND
    assert done.stop_reason is StopReason.CHECKPOINT_PAUSED
    checkpoint = load_workflow_checkpoint(
        workspace_dir=tmp_path,
        workflow_kind=EXTERNAL_SKILL_WORKFLOW_KIND,
    )
    assert checkpoint is not None
    assert checkpoint.workflow_options["skill_name"] == "ppt-master"
    assert checkpoint.adapter_id == "box-agent.external-skill.v1"


class NestedDelegationTool(Tool):
    @property
    def name(self) -> str:
        return "sub_agent"

    @property
    def description(self) -> str:
        return "Run a nested test delegation"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self) -> ToolResult:
        return ToolResult(
            success=True,
            content="Nested work saved canonical artifacts.",
            raw_output={
                "type": "sub_agent_delegation",
                "tool_calls": 2,
            },
        )


@pytest.mark.asyncio
async def test_nested_tool_calls_count_toward_recoverable_workflow_budget(
    tmp_path,
) -> None:
    (tmp_path / "output").mkdir()
    messages = _msgs()
    llm = MockLLM(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="nested-budget-call",
                        type="function",
                        function=FunctionCall(name="sub_agent", arguments={}),
                    )
                ],
                finish_reason="tool",
            )
        ]
    )

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=messages,
            tools={"sub_agent": NestedDelegationTool()},
            max_steps=3,
            completion_gate=CompletionGate(
                required_changed_artifact_globs=("output/**/*.pptx",),
                max_tool_calls=2,
                workflow_checkpoint_kind="controlled_presentation",
            ),
            workspace_dir=str(tmp_path),
        )
    )

    assert next(
        event for event in events if isinstance(event, DoneEvent)
    ).stop_reason is StopReason.CHECKPOINT_PAUSED
    assert any(isinstance(event, ContextCheckpointEvent) for event in events)


@pytest.mark.asyncio
async def test_completion_continuation_exhaustion_pauses_recoverable_workflow(
    tmp_path,
) -> None:
    (tmp_path / "output").mkdir()
    messages = _msgs()
    llm = MockLLM(
        [
            LLMResponse(content="Still working.", finish_reason="stop"),
            LLMResponse(content="Delivery remains incomplete.", finish_reason="stop"),
        ]
    )

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=messages,
            tools={},
            max_steps=3,
            completion_gate=CompletionGate(
                required_changed_artifact_globs=("output/**/*.pptx",),
                max_continuations=1,
                workflow_checkpoint_kind="controlled_presentation",
            ),
            workspace_dir=str(tmp_path),
        )
    )

    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.stop_reason is StopReason.CHECKPOINT_PAUSED
    assert "bounded continuation boundary" in done.final_content
    assert any(isinstance(event, ContextCheckpointEvent) for event in events)
    assert not any(isinstance(event, ErrorEvent) for event in events)


class ActiveSkillTool(Tool):
    loads_active_skill_instructions = True

    @property
    def name(self) -> str:
        return "get_skill"

    @property
    def description(self) -> str:
        return "Load a test skill"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {"skill_name": {"type": "string"}},
            "required": ["skill_name"],
        }

    async def execute(self, skill_name: str) -> ToolResult:
        return ToolResult(
            success=True,
            content=f"# Skill: {skill_name}\n\nMANDATORY_SKILL_RULE",
        )


@pytest.mark.asyncio
async def test_get_skill_moves_full_instructions_to_system_prompt() -> None:
    messages = _msgs()
    llm = CapturingStreamLLM(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="skill-1",
                        type="function",
                        function=FunctionCall(
                            name="get_skill",
                            arguments={"skill_name": "pptx"},
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(content="done", finish_reason="stop"),
        ]
    )

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=messages,
            tools={"get_skill": ActiveSkillTool()},
            max_steps=5,
        )
    )

    second_request = llm.message_calls[1]
    system_message = second_request[0]
    tool_message = next(message for message in second_request if message.role == "tool")
    result_event = next(event for event in events if isinstance(event, ToolCallResult))

    assert "## Active Skill Instructions" in system_message.content
    assert "MANDATORY_SKILL_RULE" in system_message.content
    assert "MANDATORY_SKILL_RULE" not in tool_message.content
    assert "loaded into active system instructions" in tool_message.content
    assert "MANDATORY_SKILL_RULE" in result_event.content


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


class CountingBrowserReadTool(Tool):
    def __init__(self):
        self.urls: list[str] = []

    @property
    def name(self):
        return "browser_read_page"

    @property
    def description(self):
        return "Reads a public web page"

    @property
    def parameters(self):
        return {"type": "object", "properties": {"url": {"type": "string"}}}

    async def execute(self, url: str = "", source_preference: str | None = None):
        self.urls.append(url)
        return ToolResult(success=True, content=f"page:{url}")


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


class NestedWebResultsTool(Tool):
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
        return ToolResult(
            success=True,
            content=json.dumps(
                {
                    "Result": {
                        "ResultCount": 2,
                        "WebResults": [
                            {
                                "Title": "Unrelated result",
                                "Url": "https://irrelevant.example.com/brazil-world-cup",
                            },
                            {
                                "Title": "FIFA result",
                                "Url": "https://www.fifa.com/tournaments/mens/worldcup",
                            },
                        ],
                    }
                }
            ),
        )


class LargeJsonWebSearchTool(Tool):
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
        return ToolResult(
            success=True,
            content=json.dumps(
                {
                    "Query": query,
                    "ResultCount": 1,
                    "Results": [
                        {
                            "Title": "Official policy result",
                            "Url": "https://example.gov/policy",
                            "Snippet": "A concise summary of the policy.",
                            "Content": "RAW_SEARCH_BODY_" + ("x" * 20000),
                        }
                    ],
                },
                ensure_ascii=False,
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


class RecordingBrowserSnapshotTool(Tool):
    def __init__(self):
        self.filenames: list[str] = []

    @property
    def name(self):
        return "browser_snapshot"

    @property
    def description(self):
        return "Persist the current browser accessibility snapshot"

    @property
    def parameters(self):
        return {
            "type": "object",
            "properties": {"filename": {"type": "string"}},
        }

    async def execute(self, filename: str = ""):
        self.filenames.append(filename)
        return ToolResult(success=True, content=f"snapshot:{filename}")


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


def test_repeated_stream_pattern_ignores_normal_prose_and_finds_tag_loop():
    assert repeated_stream_pattern("正常回答里可以有重复词，但不会连续复制八次。") is None
    assert repeated_stream_pattern(("`</think>`\n\n" * 8)) == "`</think>`"


@pytest.mark.asyncio
async def test_repetitive_stream_is_aborted_before_it_floods_history():
    loop_chunk = "`</think>`\n\n"
    messages = _msgs()
    llm = ChunkedStreamLLM([loop_chunk] * 20)

    events = await collect(
        run_agent_loop(llm=llm, messages=messages, tools={}, max_steps=5)
    )

    content_events = [event for event in events if isinstance(event, ContentEvent)]
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    done = [event for event in events if isinstance(event, DoneEvent)]
    assert len(content_events) == 7
    assert len(errors) == 1 and errors[0].is_fatal is True
    assert "repetitive output" in errors[0].message
    assert done and done[-1].stop_reason == StopReason.ERROR
    assert not any(message.role == "assistant" for message in messages)


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
async def test_long_running_tool_emits_liveness_activity(monkeypatch):
    class SlowTool(Tool):
        @property
        def name(self):
            return "slow"

        @property
        def description(self):
            return "slow tool"

        @property
        def parameters(self):
            return {"type": "object", "properties": {}}

        async def execute(self, **kwargs):
            await asyncio.sleep(0.025)
            return ToolResult(success=True, content="done")

    monkeypatch.setattr(core, "TOOL_ACTIVITY_INTERVAL_SECONDS", 0.01)
    llm = MockLLM(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="slow-1",
                        type="function",
                        function=FunctionCall(name="slow", arguments={}),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(content="done", finish_reason="stop"),
        ]
    )

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"slow": SlowTool()},
            max_steps=5,
        )
    )

    activity = [event for event in events if isinstance(event, LLMActivityEvent)]
    assert activity
    assert activity[0].payload == {
        "protocol": "agent_activity_v1",
        "phase": "tool_running",
        "tool_name": "slow",
    }
    result = next(event for event in events if isinstance(event, ToolCallResult))
    assert result.success is True


@pytest.mark.asyncio
async def test_silent_event_tool_emits_liveness_activity(monkeypatch):
    class SlowEventTool(EventEmittingTool):
        def __init__(self):
            super().__init__()

        @property
        def name(self):
            return "slow_event"

        @property
        def description(self):
            return "slow event tool"

        @property
        def parameters(self):
            return {"type": "object", "properties": {}}

        async def execute(self, **kwargs):
            await asyncio.sleep(0.15)
            return ToolResult(success=True, content="done")

    monkeypatch.setattr(core, "TOOL_ACTIVITY_INTERVAL_SECONDS", 0.01)
    llm = MockLLM(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="slow-event-1",
                        type="function",
                        function=FunctionCall(name="slow_event", arguments={}),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(content="done", finish_reason="stop"),
        ]
    )

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"slow_event": SlowEventTool()},
            max_steps=5,
        )
    )

    activity = [event for event in events if isinstance(event, LLMActivityEvent)]
    assert activity
    assert activity[0].payload["tool_name"] == "slow_event"


@pytest.mark.asyncio
async def test_parallel_tool_batch_emits_liveness_activity(monkeypatch):
    class SlowParallelTool(Tool):
        parallel_safe = True

        @property
        def name(self):
            return "slow_parallel"

        @property
        def description(self):
            return "slow parallel tool"

        @property
        def parameters(self):
            return {"type": "object", "properties": {}}

        async def execute(self, **kwargs):
            await asyncio.sleep(0.15)
            return ToolResult(success=True, content="done")

    monkeypatch.setattr(core, "TOOL_ACTIVITY_INTERVAL_SECONDS", 0.01)
    llm = MockLLM(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id=f"slow-parallel-{index}",
                        type="function",
                        function=FunctionCall(name="slow_parallel", arguments={}),
                    )
                    for index in range(2)
                ],
                finish_reason="tool",
            ),
            LLMResponse(content="done", finish_reason="stop"),
        ]
    )

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"slow_parallel": SlowParallelTool()},
            max_steps=5,
        )
    )

    activity = [event for event in events if isinstance(event, LLMActivityEvent)]
    assert activity
    assert activity[0].payload["tool_name"] == "parallel_tools"


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
async def test_repeated_sub_agent_framework_error_is_compacted_only_for_model_history():
    malformed = {
        "task": "Render a slide",
        "required_tools": '["file", "terminal", "vision"]',
        "capabilities": "",
    }
    messages = _msgs()
    llm = CapturingStreamLLM(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="bad-delegation-1",
                        type="function",
                        function=FunctionCall(name="sub_agent", arguments=malformed),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="bad-delegation-2",
                        type="function",
                        function=FunctionCall(name="sub_agent", arguments=malformed),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(content="done", finish_reason="stop"),
        ]
    )
    sub_agent = SubAgentTool(llm=MockLLM([]), parent_tools={})

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=messages,
            tools={"sub_agent": sub_agent},
            max_steps=5,
        )
    )

    results = [event for event in events if isinstance(event, ToolCallResult)]
    assert len(results) == 2
    assert all(result.raw_output["code"] == "INVALID_DELEGATION_SPEC" for result in results)
    assert all("Traceback" not in (result.error or "") for result in results)

    third_request_results = [
        message.content
        for message in llm.message_calls[2]
        if message.role == "tool" and message.name == "sub_agent"
    ]
    assert "minimal_valid_example" in third_request_results[0]
    assert "REPEATED_FRAMEWORK_FAILURE" in third_request_results[1]
    assert "minimal_valid_example" not in third_request_results[1]


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
async def test_read_file_artifact_keeps_bounded_page_in_model_history(tmp_path):
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
    assert "[Full file content omitted from model history]" not in tool_msg.content
    assert marker in tool_msg.content


@pytest.mark.asyncio
async def test_read_file_skill_reference_stays_available_for_next_model_turn(tmp_path):
    marker = '"deck_goal": "required schema marker"'
    reference = tmp_path / "skills" / "pptx" / "references" / "outline.md"
    reference.parent.mkdir(parents=True)
    reference.write_text(
        "# Outline contract\n" + ("workflow instruction\n" * 600) + marker,
        encoding="utf-8",
    )
    msgs = _msgs()
    llm = CapturingStreamLLM(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="t1",
                        type="function",
                        function=FunctionCall(
                            name="read_file",
                            arguments={"path": str(reference), "limit": 1000},
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(content="done", finish_reason="stop"),
        ]
    )

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=msgs,
            tools={"read_file": ReadTool(workspace_dir=str(tmp_path))},
            max_steps=5,
            workspace_dir=str(tmp_path),
        )
    )

    second_request_tool_results = [
        message.content
        for message in llm.message_calls[1]
        if message.role == "tool"
    ]
    assert any(marker in content for content in second_request_tool_results)
    assert all(
        "[Full file content omitted from model history]" not in content
        for content in second_request_tool_results
    )


@pytest.mark.asyncio
async def test_repeated_read_file_uses_receipt_only_after_full_source_survives(tmp_path):
    marker = "EXACT_REFERENCE_BODY"
    path = tmp_path / "reference.md"
    path.write_text(marker + "\n", encoding="utf-8")
    msgs = _msgs()
    llm = CapturingStreamLLM(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="read-1",
                        type="function",
                        function=FunctionCall(
                            name="read_file",
                            arguments={"path": "reference.md"},
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="read-2",
                        type="function",
                        function=FunctionCall(
                            name="read_file",
                            arguments={"path": "reference.md"},
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(content="done", finish_reason="stop"),
        ]
    )

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=msgs,
            tools={"read_file": ReadTool(workspace_dir=str(tmp_path))},
            max_steps=5,
            workspace_dir=str(tmp_path),
        )
    )

    visible_results = [
        event.content
        for event in events
        if isinstance(event, ToolCallResult) and event.tool_name == "read_file"
    ]
    tool_messages = [message for message in msgs if message.role == "tool"]
    assert len(visible_results) == 2
    assert all(marker in content for content in visible_results)
    assert marker in tool_messages[0].content
    assert "Resource already available" in tool_messages[1].content
    assert len(tool_messages[1].content) <= 300
    assert "read-1" in tool_messages[1].content


@pytest.mark.asyncio
async def test_changed_file_version_returns_full_content_again(tmp_path):
    path = tmp_path / "reference.md"
    path.write_text("VERSION_ONE\n", encoding="utf-8")

    class ChangeBeforeSecondRead(ReadTool):
        def __init__(self) -> None:
            super().__init__(workspace_dir=str(tmp_path))
            self.calls = 0

        async def execute(self, **kwargs):
            self.calls += 1
            if self.calls == 2:
                path.write_text("VERSION_TWO\n", encoding="utf-8")
            return await super().execute(**kwargs)

    msgs = _msgs()
    read_calls = [
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id=f"read-{index}",
                    type="function",
                    function=FunctionCall(
                        name="read_file",
                        arguments={"path": "reference.md"},
                    ),
                )
            ],
            finish_reason="tool",
        )
        for index in (1, 2)
    ]

    await collect(
        run_agent_loop(
            llm=MockLLM([*read_calls, LLMResponse(content="done", finish_reason="stop")]),
            messages=msgs,
            tools={"read_file": ChangeBeforeSecondRead()},
            max_steps=5,
            workspace_dir=str(tmp_path),
        )
    )

    tool_messages = [message.content for message in msgs if message.role == "tool"]
    assert "VERSION_ONE" in tool_messages[0]
    assert "VERSION_TWO" in tool_messages[1]
    assert "Resource already available" not in tool_messages[1]


@pytest.mark.asyncio
async def test_read_file_refresh_forces_full_model_history_content(tmp_path):
    marker = "REFRESHED_REFERENCE_BODY"
    (tmp_path / "reference.md").write_text(marker + "\n", encoding="utf-8")
    msgs = _msgs()
    llm = MockLLM(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="read-1",
                        type="function",
                        function=FunctionCall(
                            name="read_file",
                            arguments={"path": "reference.md"},
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="read-2",
                        type="function",
                        function=FunctionCall(
                            name="read_file",
                            arguments={"path": "reference.md", "refresh": True},
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(content="done", finish_reason="stop"),
        ]
    )

    await collect(
        run_agent_loop(
            llm=llm,
            messages=msgs,
            tools={"read_file": ReadTool(workspace_dir=str(tmp_path))},
            max_steps=5,
            workspace_dir=str(tmp_path),
        )
    )

    tool_messages = [message for message in msgs if message.role == "tool"]
    assert len(tool_messages) == 2
    assert all(marker in message.content for message in tool_messages)
    assert all("Resource already available" not in message.content for message in tool_messages)


@pytest.mark.asyncio
async def test_repeated_unchanged_read_file_refresh_uses_receipt(tmp_path):
    marker = "UNCHANGED_REFRESH_BODY"
    (tmp_path / "reference.md").write_text(marker + "\n", encoding="utf-8")
    msgs = _msgs()
    llm = MockLLM(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id=f"read-{index}",
                        type="function",
                        function=FunctionCall(
                            name="read_file",
                            arguments={
                                "path": "reference.md",
                                **({"refresh": True} if index > 1 else {}),
                            },
                        ),
                    )
                ],
                finish_reason="tool",
            )
            for index in (1, 2, 3)
        ]
        + [LLMResponse(content="done", finish_reason="stop")]
    )

    await collect(
        run_agent_loop(
            llm=llm,
            messages=msgs,
            tools={"read_file": ReadTool(workspace_dir=str(tmp_path))},
            max_steps=6,
            workspace_dir=str(tmp_path),
        )
    )

    tool_messages = [message.content for message in msgs if message.role == "tool"]
    assert marker in tool_messages[0]
    assert marker in tool_messages[1]
    assert "Resource already available" in tool_messages[2]
    assert "content version is unchanged" in tool_messages[2]
    assert marker not in tool_messages[2]


@pytest.mark.asyncio
async def test_context_resource_dedup_can_be_disabled(tmp_path):
    marker = "ROLLBACK_FULL_BODY"
    (tmp_path / "reference.md").write_text(marker + "\n", encoding="utf-8")
    msgs = _msgs()
    read_calls = [
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id=f"read-{index}",
                    type="function",
                    function=FunctionCall(
                        name="read_file",
                        arguments={"path": "reference.md"},
                    ),
                )
            ],
            finish_reason="tool",
        )
        for index in (1, 2)
    ]

    await collect(
        run_agent_loop(
            llm=MockLLM([*read_calls, LLMResponse(content="done", finish_reason="stop")]),
            messages=msgs,
            tools={"read_file": ReadTool(workspace_dir=str(tmp_path))},
            max_steps=5,
            workspace_dir=str(tmp_path),
            context_resource_dedup_enabled=False,
        )
    )

    tool_messages = [message for message in msgs if message.role == "tool"]
    assert len(tool_messages) == 2
    assert all(marker in message.content for message in tool_messages)


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
async def test_consecutive_html_writes_delay_compaction_for_one_model_turn(tmp_path):
    first_html = "<section class='slide'>FIRST_REAL_FRAGMENT</section>"
    second_html = "<section class='slide'>SECOND_REAL_FRAGMENT</section>"
    msgs = _msgs()
    llm = CapturingStreamLLM(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="write-1",
                        type="function",
                        function=FunctionCall(
                            name="write_file",
                            arguments={
                                "path": "drafts/slides_01_04.html",
                                "content": first_html,
                            },
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="write-2",
                        type="function",
                        function=FunctionCall(
                            name="write_file",
                            arguments={
                                "path": "drafts/slides_05_08.html",
                                "content": second_html,
                            },
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(content="done", finish_reason="stop"),
        ]
    )

    await collect(
        run_agent_loop(
            llm=llm,
            messages=msgs,
            tools={"write_file": WriteTool(workspace_dir=str(tmp_path))},
            max_steps=5,
            workspace_dir=str(tmp_path),
        )
    )

    second_request_calls = [
        tool_call
        for message in llm.message_calls[1]
        if message.role == "assistant" and message.tool_calls
        for tool_call in message.tool_calls
    ]
    assert second_request_calls[0].function.arguments["content"] == first_html

    third_request_calls = [
        tool_call
        for message in llm.message_calls[2]
        if message.role == "assistant" and message.tool_calls
        for tool_call in message.tool_calls
    ]
    assert "[Full tool-call argument omitted from model history]" in (
        third_request_calls[0].function.arguments["content"]
    )
    assert third_request_calls[1].function.arguments["content"] == second_html

    stored_calls = [
        tool_call
        for message in msgs
        if message.role == "assistant" and message.tool_calls
        for tool_call in message.tool_calls
    ]
    assert all(
        "[Full tool-call argument omitted from model history]"
        in tool_call.function.arguments["content"]
        for tool_call in stored_calls
    )
    assert (tmp_path / "drafts/slides_01_04.html").read_text() == first_html
    assert (tmp_path / "drafts/slides_05_08.html").read_text() == second_html


@pytest.mark.asyncio
async def test_model_history_placeholder_write_is_hidden_and_self_heals(tmp_path):
    first_html = "<section class='slide'>FIRST_REAL_FRAGMENT</section>"
    second_html = "<section class='slide'>SECOND_REAL_FRAGMENT</section>"
    placeholder = (
        "[Full tool-call argument omitted from model history]\n"
        "Tool: write_file\n"
        "Argument: content\n"
        "Path: drafts/slides_05_08.html"
    )

    class RecordingWriteTool(WriteTool):
        def __init__(self):
            super().__init__(workspace_dir=str(tmp_path))
            self.executions: list[tuple[str, str]] = []

        async def execute(self, path: str, content: str) -> ToolResult:
            self.executions.append((path, content))
            return await super().execute(path=path, content=content)

    write_tool = RecordingWriteTool()
    msgs = _msgs()
    llm = CapturingStreamLLM(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="write-1",
                        type="function",
                        function=FunctionCall(
                            name="write_file",
                            arguments={
                                "path": "drafts/slides_01_04.html",
                                "content": first_html,
                            },
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="placeholder-write",
                        type="function",
                        function=FunctionCall(
                            name="write_file",
                            arguments={
                                "path": "drafts/slides_05_08.html",
                                "content": placeholder,
                            },
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="write-2",
                        type="function",
                        function=FunctionCall(
                            name="write_file",
                            arguments={
                                "path": "drafts/slides_05_08.html",
                                "content": second_html,
                            },
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(content="done", finish_reason="stop"),
        ]
    )

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=msgs,
            tools={"write_file": write_tool},
            max_steps=6,
            workspace_dir=str(tmp_path),
        )
    )

    placeholder_start = next(
        event
        for event in events
        if isinstance(event, ToolCallStart)
        and event.tool_call_id == "placeholder-write"
    )
    placeholder_result = next(
        event
        for event in events
        if isinstance(event, ToolCallResult)
        and event.tool_call_id == "placeholder-write"
    )
    assert placeholder_start.user_visible is False
    assert placeholder_result.user_visible is False
    assert placeholder_result.success is False
    assert "INTERNAL_MODEL_HISTORY_PLACEHOLDER" in (placeholder_result.error or "")
    assert any(
        isinstance(event, InjectedMessageEvent)
        and event.user_visible is False
        and "Regenerate the missing real content" in event.content
        for event in events
    )
    assert write_tool.executions == [
        ("drafts/slides_01_04.html", first_html),
        ("drafts/slides_05_08.html", second_html),
    ]
    assert (tmp_path / "drafts/slides_05_08.html").read_text() == second_html


@pytest.mark.asyncio
@pytest.mark.parametrize("placeholder_tool_name", ["write_file", "append_file"])
async def test_placeholder_file_mutation_blocks_stale_downstream_until_staged_commit(
    tmp_path,
    placeholder_tool_name,
):
    placeholder = (
        "[Full tool-call argument omitted from model history]\n"
        "Tool: write_file\n"
        "Argument: content\n"
        "Path: deck.patch.json"
    )
    target = tmp_path / "deck.patch.json"
    target.write_text('{"rows":[1,2,3,4,5,6,7,8]}', encoding="utf-8")
    echo = CountingEchoTool()
    staged = StagedFileWriteTool(workspace_dir=str(tmp_path))
    llm = CapturingStreamLLM(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="placeholder-write",
                        type="function",
                        function=FunctionCall(
                            name=placeholder_tool_name,
                            arguments={"path": "deck.patch.json", "content": placeholder},
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="stale-downstream",
                        type="function",
                        function=FunctionCall(name="echo", arguments={"text": "apply old patch"}),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="staged-begin",
                        type="function",
                        function=FunctionCall(
                            name="staged_file_write",
                            arguments={
                                "action": "begin",
                                "path": "deck.patch.json",
                                "expected_chunks": 1,
                            },
                        ),
                    ),
                    ToolCall(
                        id="staged-append",
                        type="function",
                        function=FunctionCall(
                            name="staged_file_write",
                            arguments={
                                "action": "append_text",
                                "chunk_index": 0,
                                "content": '{"rows":[1,2,3,4,5,6]}',
                            },
                        ),
                    ),
                    ToolCall(
                        id="staged-commit",
                        type="function",
                        function=FunctionCall(
                            name="staged_file_write",
                            arguments={"action": "commit"},
                        ),
                    ),
                ],
                finish_reason="tool",
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="fresh-downstream",
                        type="function",
                        function=FunctionCall(name="echo", arguments={"text": "apply new patch"}),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(content="done", finish_reason="stop"),
        ]
    )

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={
                "write_file": WriteTool(workspace_dir=str(tmp_path)),
                "append_file": AppendTool(workspace_dir=str(tmp_path)),
                "staged_file_write": staged,
                "echo": echo,
            },
            max_steps=7,
            workspace_dir=str(tmp_path),
        )
    )

    blocked = next(
        event
        for event in events
        if isinstance(event, ToolCallResult) and event.tool_call_id == "stale-downstream"
    )
    assert blocked.success is False
    assert "INTERNAL_MODEL_HISTORY_PLACEHOLDER_RECOVERY_REQUIRED" in (blocked.error or "")
    assert echo.calls == 1
    assert target.read_text(encoding="utf-8") == '{"rows":[1,2,3,4,5,6]}'


@pytest.mark.asyncio
async def test_placeholder_append_can_recover_with_same_target_real_write(tmp_path):
    placeholder = (
        "[Full tool-call argument omitted from model history]\n"
        "Tool: append_file\n"
        "Argument: content\n"
        "Path: deck.patch.json"
    )
    target = tmp_path / "deck.patch.json"
    target.write_text('{"incomplete":', encoding="utf-8")
    echo = CountingEchoTool()
    llm = CapturingStreamLLM(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="placeholder-append",
                        type="function",
                        function=FunctionCall(
                            name="append_file",
                            arguments={"path": "deck.patch.json", "content": placeholder},
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="real-rewrite",
                        type="function",
                        function=FunctionCall(
                            name="write_file",
                            arguments={
                                "path": "deck.patch.json",
                                "content": '{"slides":{}}',
                            },
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="downstream",
                        type="function",
                        function=FunctionCall(name="echo", arguments={"text": "apply"}),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(content="done", finish_reason="stop"),
        ]
    )

    await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={
                "append_file": AppendTool(workspace_dir=str(tmp_path)),
                "write_file": WriteTool(workspace_dir=str(tmp_path)),
                "echo": echo,
            },
            max_steps=6,
            workspace_dir=str(tmp_path),
        )
    )

    assert echo.calls == 1
    assert target.read_text(encoding="utf-8") == '{"slides":{}}'


@pytest.mark.asyncio
async def test_repeated_placeholder_error_is_compacted_after_one_repair_hint(tmp_path):
    placeholder = (
        "[Full tool-call argument omitted from model history]\n"
        "Tool: write_file\n"
        "Argument: content\n"
        "Path: drafts/slide.html"
    )
    messages = _msgs()
    llm = CapturingStreamLLM(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="placeholder-1",
                        type="function",
                        function=FunctionCall(
                            name="write_file",
                            arguments={"path": "drafts/slide.html", "content": placeholder},
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="placeholder-2",
                        type="function",
                        function=FunctionCall(
                            name="write_file",
                            arguments={"path": "drafts/slide.html", "content": placeholder},
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(content="done", finish_reason="stop"),
        ]
    )

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=messages,
            tools={"write_file": WriteTool(workspace_dir=str(tmp_path))},
            max_steps=5,
            workspace_dir=str(tmp_path),
        )
    )

    results = [event for event in events if isinstance(event, ToolCallResult)]
    assert len(results) == 2
    assert all("INTERNAL_MODEL_HISTORY_PLACEHOLDER" in (result.error or "") for result in results)
    repair_events = [event for event in events if isinstance(event, InjectedMessageEvent)]
    assert sum("Regenerate the missing real content" in event.content for event in repair_events) == 1

    third_request_results = [
        message.content
        for message in llm.message_calls[2]
        if message.role == "tool" and message.name == "write_file"
    ]
    assert "INTERNAL_MODEL_HISTORY_PLACEHOLDER" in third_request_results[0]
    assert "REPEATED_FRAMEWORK_FAILURE" in third_request_results[1]
    assert not (tmp_path / "drafts/slide.html").exists()


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
async def test_auto_memory_match_waits_off_event_loop_for_memory_snapshot(tmp_path):
    from box_agent.memory import MemoryManager

    memory = MemoryManager(memory_dir=str(tmp_path / "memory"))
    memory.write_context("- saved context", topic="project")

    transaction_acquired = threading.Event()
    release_transaction = threading.Event()

    def hold_transaction() -> None:
        with memory.context_transaction():
            transaction_acquired.set()
            assert release_transaction.wait(timeout=2.0)

    holder = asyncio.create_task(asyncio.to_thread(hold_transaction))
    assert await asyncio.to_thread(transaction_acquired.wait, 2.0)

    llm = MockLLM([LLMResponse(content="done", finish_reason="stop")])
    run_task = asyncio.create_task(
        collect(
            run_agent_loop(
                llm=llm,
                messages=_msgs(),
                tools={},
                max_steps=5,
                memory_manager=memory,
            )
        )
    )

    heartbeat_at = 0.0
    started_at = monotonic()

    async def heartbeat() -> None:
        nonlocal heartbeat_at
        await asyncio.sleep(0.01)
        heartbeat_at = monotonic()

    heartbeat_task = asyncio.create_task(heartbeat())
    release_timer = threading.Timer(0.3, release_transaction.set)
    release_timer.start()
    try:
        await heartbeat_task
        heartbeat_delay = heartbeat_at - started_at
        assert not run_task.done()
    finally:
        release_transaction.set()
        release_timer.cancel()
        await asyncio.wait_for(holder, timeout=2.0)
        await asyncio.wait_for(run_task, timeout=2.0)

    assert heartbeat_delay < 0.15


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
async def test_cancellation_interrupts_opted_in_event_tool():
    """A cancellable long-running tool must not wait for execute() to return."""

    class BlockingSearchTool(EventEmittingTool):
        cancel_on_agent_cancel = True

        def __init__(self):
            super().__init__()
            self.started = False
            self.cancelled = False

        @property
        def name(self):
            return "search_files"

        @property
        def description(self):
            return "Blocks until cancelled"

        @property
        def parameters(self):
            return {"type": "object", "properties": {}}

        async def execute(self):
            self.started = True
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    tool = BlockingSearchTool()
    llm = MockLLM([
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="search-1",
                    type="function",
                    function=FunctionCall(name="search_files", arguments={}),
                )
            ],
            finish_reason="tool",
        )
    ])

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"search_files": tool},
            max_steps=5,
            is_cancelled=lambda: tool.started,
        )
    )

    results = [event for event in events if isinstance(event, ToolCallResult)]
    done = [event for event in events if isinstance(event, DoneEvent)]
    assert tool.cancelled is True
    assert len(results) == 1
    assert results[0].success is False
    assert "cancelled" in (results[0].error or "")
    assert done[-1].stop_reason == StopReason.CANCELLED


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
async def test_controlled_presentation_does_not_receive_conflicting_final_summary_nudge(
    tmp_path,
):
    """An incomplete filesystem-backed deck stage owns the next action.

    The generic >50-call wrap-up instruction must not tell the model to stop
    before outline/deck/HTML delivery is complete.
    """
    gate = CompletionGate(workflow_checkpoint_kind="controlled_presentation")
    llm = MockLLM(
        [
            LLMResponse(
                content="",
                tool_calls=_echo_tool_calls(_FS_THRESHOLD + 1),
                finish_reason="tool",
            ),
            LLMResponse(content="continuing", finish_reason="stop"),
        ]
    )

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"echo": EchoTool()},
            max_steps=5,
            completion_gate=gate,
            workspace_dir=str(tmp_path),
        )
    )

    injected = [e for e in events if isinstance(e, InjectedMessageEvent)]
    assert any("CONTROLLED_PRESENTATION_STAGE=outline" in e.content for e in injected)
    assert not any("many visible tool calls" in e.content for e in injected)


@pytest.mark.asyncio
async def test_research_evidence_calls_preserve_controlled_deck_delivery_budget(tmp_path):
    """Search/browser evidence has a separate guard from artifact production."""
    browser = CountingBrowserReadTool()
    echo = EchoTool()
    gate = CompletionGate(
        workflow_checkpoint_kind="controlled_presentation",
        workflow_options={"research_mode": "deep"},
        max_tool_calls=1,
    )
    llm = MockLLM(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="browser-1",
                        type="function",
                        function=FunctionCall(
                            name="browser_read_page",
                            arguments={"url": "https://example.com/source"},
                        ),
                    ),
                    ToolCall(
                        id="artifact-1",
                        type="function",
                        function=FunctionCall(
                            name="echo",
                            arguments={"text": "artifact"},
                        ),
                    ),
                ],
                finish_reason="tool",
            ),
            LLMResponse(content="done", finish_reason="stop"),
        ]
    )

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"browser_read_page": browser, "echo": echo},
            max_steps=5,
            completion_gate=gate,
            workspace_dir=str(tmp_path),
        )
    )

    results = [e for e in events if isinstance(e, ToolCallResult)]
    assert [result.success for result in results[:2]] == [True, True]


@pytest.mark.asyncio
async def test_controlled_research_batches_public_page_reads(tmp_path):
    browser = CountingBrowserReadTool()
    calls = [
        ToolCall(
            id=f"browser-{index}",
            type="function",
            function=FunctionCall(
                name="browser_read_page",
                arguments={"url": f"https://example.com/source-{index}"},
            ),
        )
        for index in range(1, 5)
    ]
    llm = MockLLM(
        [
            LLMResponse(content="", tool_calls=calls, finish_reason="tool"),
            LLMResponse(content="done", finish_reason="stop"),
        ]
    )

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"browser_read_page": browser},
            max_steps=5,
            completion_gate=CompletionGate(
                workflow_checkpoint_kind="controlled_presentation",
                workflow_options={"research_mode": "deep"},
                max_continuations=0,
            ),
            workspace_dir=str(tmp_path),
        )
    )

    assert browser.urls == [
        "https://example.com/source-1",
        "https://example.com/source-2",
    ]
    results = {
        event.tool_call_id: event
        for event in events
        if isinstance(event, ToolCallResult)
    }
    for call_id in ("browser-3", "browser-4"):
        assert results[call_id].success is False
        assert results[call_id].user_visible is False
        assert "page read deferred by runtime batching" in (
            results[call_id].error or ""
        )


@pytest.mark.asyncio
async def test_controlled_outline_accepts_url_from_prior_web_search(tmp_path):
    query = "official product release"
    source_url = "https://example.gov/releases/latest"
    outline_content = json.dumps(
        {
            "source_mode": "public_authoritative_research",
            "slides": [{"evidence": [source_url]}],
        }
    )
    llm = MockLLM(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="search-evidence",
                        type="function",
                        function=FunctionCall(
                            name="web_search",
                            arguments={"query": query},
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="write-outline",
                        type="function",
                        function=FunctionCall(
                            name="write_file",
                            arguments={
                                "path": "outline.json",
                                "content": outline_content,
                            },
                        ),
                    )
                ],
                finish_reason="tool",
            ),
        ]
    )

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={
                "web_search": JsonWebSearchTool({query: source_url}),
                "write_file": WriteTool(workspace_dir=str(tmp_path)),
            },
            max_steps=2,
            completion_gate=CompletionGate(
                workflow_checkpoint_kind="controlled_presentation",
                workflow_options={"research_mode": "deep"},
            ),
            workspace_dir=str(tmp_path),
        )
    )

    write_result = next(
        event
        for event in events
        if isinstance(event, ToolCallResult)
        and event.tool_call_id == "write-outline"
    )
    assert write_result.success is True
    assert (tmp_path / "outline.json").is_file()


@pytest.mark.asyncio
async def test_controlled_research_does_not_treat_search_result_as_page_read(tmp_path):
    query = "official report"
    source_url = "https://example.gov/reports/latest"
    research = tmp_path / "research"
    research.mkdir()
    (research / "topic_evidence.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "topic": "topic",
                "target_entities": [],
                "evidence": [
                    {
                        "entity": "Example",
                        "claim": "Example published the latest report.",
                        "source_url": source_url,
                        "source_type": "first_party",
                        "evidence_excerpt": "Example published the latest report.",
                        "confidence": "high",
                        "status": "verified",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    class CountingBashTool(Tool):
        def __init__(self):
            self.calls = 0

        @property
        def name(self):
            return "bash"

        @property
        def description(self):
            return "Runs a command"

        @property
        def parameters(self):
            return {"type": "object", "properties": {"command": {"type": "string"}}}

        async def execute(self, command: str = ""):
            self.calls += 1
            return ToolResult(success=True, content=command)

    validator_command = (
        "python validate_research_artifacts.py --research-dir research "
        "--topic topic --route B --report research/qa/topic_research_check.json"
    )
    llm = MockLLM(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="search-only",
                        type="function",
                        function=FunctionCall(
                            name="web_search",
                            arguments={"query": query},
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="validate-without-read",
                        type="function",
                        function=FunctionCall(
                            name="bash",
                            arguments={"command": validator_command},
                        ),
                    )
                ],
                finish_reason="tool",
            ),
        ]
    )
    bash = CountingBashTool()

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={
                "web_search": JsonWebSearchTool({query: source_url}),
                "bash": bash,
            },
            max_steps=2,
            completion_gate=CompletionGate(
                workflow_checkpoint_kind="controlled_presentation",
                workflow_options={"research_mode": "deep"},
            ),
            workspace_dir=str(tmp_path),
        )
    )

    result = next(
        event
        for event in events
        if isinstance(event, ToolCallResult)
        and event.tool_call_id == "validate-without-read"
    )
    assert bash.calls == 0
    assert result.success is False
    assert "CONTROLLED_PRESENTATION_UNREAD_EVIDENCE_URL" in (result.error or "")


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
async def test_single_visible_tool_empty_final_answer_retries_for_conclusion():
    """Even a short tool turn must not end successfully without a visible answer."""
    llm = MockLLM(
        [
            LLMResponse(
                content="",
                tool_calls=_echo_tool_calls(1),
                finish_reason="tool",
            ),
            LLMResponse(content="", finish_reason="stop"),
            LLMResponse(content="最终结论：搜索结果已整理。", finish_reason="stop"),
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
    assert any("produced no visible final answer" in e.content for e in injected)
    done = [e for e in events if isinstance(e, DoneEvent)]
    assert done[-1].stop_reason == StopReason.END_TURN
    assert done[-1].final_content == "最终结论：搜索结果已整理。"


@pytest.mark.asyncio
async def test_repeated_empty_final_answer_after_tool_returns_error():
    """A bounded retry failure is explicit instead of a successful empty END_TURN."""
    llm = MockLLM(
        [
            LLMResponse(
                content="",
                tool_calls=_echo_tool_calls(1),
                finish_reason="tool",
            ),
            LLMResponse(content="", finish_reason="stop"),
            LLMResponse(content="", finish_reason="stop"),
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

    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert errors
    assert "未生成最终答复" in errors[-1].message
    done = [e for e in events if isinstance(e, DoneEvent)]
    assert done[-1].stop_reason == StopReason.ERROR
    assert done[-1].final_content


@pytest.mark.asyncio
async def test_empty_final_answer_on_last_step_returns_error():
    llm = MockLLM(
        [
            LLMResponse(
                content="",
                tool_calls=_echo_tool_calls(1),
                finish_reason="tool",
            ),
            LLMResponse(content="", finish_reason="stop"),
        ]
    )

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"echo": EchoTool()},
            max_steps=2,
        )
    )

    retry_messages = [
        event
        for event in events
        if isinstance(event, InjectedMessageEvent)
        and "produced no visible final answer" in event.content
    ]
    assert retry_messages == []
    assert any(isinstance(event, ErrorEvent) for event in events)
    done = [event for event in events if isinstance(event, DoneEvent)]
    assert done[-1].stop_reason == StopReason.ERROR


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
            tool_limits=ToolLimitsConfig(web_search={"total_calls": 24}),
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
async def test_completion_gate_can_apply_a_stricter_web_search_budget():
    tool_calls = [
        ToolCall(
            id=f"web-presentation-{index}",
            type="function",
            function=FunctionCall(
                name="web_search",
                arguments={"query": f"presentation fact {index}"},
            ),
        )
        for index in range(6)
    ]
    web_search = CountingWebSearchTool()

    events = await collect(
        run_agent_loop(
            llm=MockLLM(
                [
                    LLMResponse(content="", tool_calls=tool_calls, finish_reason="tool"),
                    LLMResponse(content="final", finish_reason="stop"),
                ]
            ),
            messages=_msgs(),
            tools={"web_search": web_search},
            max_steps=5,
            completion_gate=CompletionGate(web_search_total_limit=4),
        )
    )

    assert web_search.calls == 4
    starts = [
        event
        for event in events
        if isinstance(event, ToolCallStart) and event.tool_name == "web_search"
    ]
    assert len([event for event in starts if event.user_visible]) == 4
    assert len([event for event in starts if not event.user_visible]) == 2
    hidden_errors = [
        event
        for event in events
        if isinstance(event, ToolCallResult)
        and event.tool_name == "web_search"
        and not event.user_visible
        and not event.success
    ]
    assert len(hidden_errors) == 2
    assert all("budget reached" in (event.error or "") for event in hidden_errors)
    injected = [
        event
        for event in events
        if isinstance(event, InjectedMessageEvent) and not event.user_visible
    ]
    assert any("total executed this turn: 4/4" in event.content for event in injected)
    assert any("web_search 调用已达到预算上限（4 次）" in event.content for event in injected)


@pytest.mark.asyncio
async def test_explicit_web_search_budget_can_expand_the_default():
    first_batch = [
        ToolCall(
            id=f"web-expanded-{index}",
            type="function",
            function=FunctionCall(
                name="web_search",
                arguments={"query": f"expanded research fact {index}"},
            ),
        )
        for index in range(6)
    ]
    second_batch = [
        ToolCall(
            id=f"web-expanded-{index}",
            type="function",
            function=FunctionCall(
                name="web_search",
                arguments={"query": f"expanded research fact {index}"},
            ),
        )
        for index in range(6, 9)
    ]
    web_search = CountingWebSearchTool()

    events = await collect(
        run_agent_loop(
            llm=MockLLM(
                [
                    LLMResponse(
                        content="",
                        tool_calls=first_batch,
                        finish_reason="tool",
                    ),
                    LLMResponse(
                        content="",
                        tool_calls=second_batch,
                        finish_reason="tool",
                    ),
                    LLMResponse(content="final", finish_reason="stop"),
                ]
            ),
            messages=_msgs(),
            tools={"web_search": web_search},
            max_steps=5,
            web_search_total_limit=8,
        )
    )

    assert web_search.calls == 8
    hidden_errors = [
        event
        for event in events
        if isinstance(event, ToolCallResult)
        and event.tool_name == "web_search"
        and not event.user_visible
        and not event.success
    ]
    assert len(hidden_errors) == 1
    assert "budget reached" in (hidden_errors[0].error or "")


@pytest.mark.asyncio
async def test_tool_limits_config_controls_web_search_batch_and_total():
    first_batch = [
        ToolCall(
            id=f"configured-web-{index}",
            type="function",
            function=FunctionCall(
                name="web_search",
                arguments={"query": f"configured query {index}"},
            ),
        )
        for index in range(4)
    ]
    second_batch = [
        ToolCall(
            id=f"configured-web-{index}",
            type="function",
            function=FunctionCall(
                name="web_search",
                arguments={"query": f"configured query {index}"},
            ),
        )
        for index in range(4, 6)
    ]
    web_search = CountingWebSearchTool()

    events = await collect(
        run_agent_loop(
            llm=MockLLM(
                [
                    LLMResponse(content="", tool_calls=first_batch, finish_reason="tool"),
                    LLMResponse(content="", tool_calls=second_batch, finish_reason="tool"),
                    LLMResponse(content="final", finish_reason="stop"),
                ]
            ),
            messages=_msgs(),
            tools={"web_search": web_search},
            max_steps=5,
            tool_limits=ToolLimitsConfig(
                web_search={"batch_size": 2, "total_calls": 3}
            ),
        )
    )

    assert web_search.calls == 3
    injected = [event for event in events if isinstance(event, InjectedMessageEvent)]
    assert any("total executed this turn: 2/3; batch size: 2" in event.content for event in injected)
    assert any("web_search 调用已达到预算上限（3 次）" in event.content for event in injected)


@pytest.mark.asyncio
async def test_total_tool_call_budget_is_a_hard_loop_limit():
    tool_calls = [
        ToolCall(
            id=f"echo-{index}",
            type="function",
            function=FunctionCall(name="echo", arguments={"text": str(index)}),
        )
        for index in range(3)
    ]
    llm = MockLLM(
        [
            LLMResponse(content="", tool_calls=tool_calls, finish_reason="tool"),
            LLMResponse(content="final", finish_reason="stop"),
        ]
    )
    echo = CountingEchoTool()

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"echo": echo},
            max_steps=3,
            max_tool_calls=2,
        )
    )

    assert echo.calls == 2
    results = [event for event in events if isinstance(event, ToolCallResult)]
    assert len(results) == 3
    assert results[-1].success is False
    assert "Total tool call budget reached" in (results[-1].error or "")
    injected = [event for event in events if isinstance(event, InjectedMessageEvent)]
    assert any("工具调用总预算已达到上限" in event.content for event in injected)


@pytest.mark.asyncio
async def test_identical_tool_calls_in_one_response_execute_only_once():
    messages = _msgs()
    duplicate_calls = [
        ToolCall(
            id=call_id,
            type="function",
            function=FunctionCall(name="echo", arguments={"text": "same"}),
        )
        for call_id in ("echo-first", "echo-duplicate")
    ]
    llm = MockLLM(
        [
            LLMResponse(content="", tool_calls=duplicate_calls, finish_reason="tool"),
            LLMResponse(content="done", finish_reason="stop"),
        ]
    )
    echo = CountingEchoTool()

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=messages,
            tools={"echo": echo},
            max_steps=3,
        )
    )

    assert echo.calls == 1
    starts = [event for event in events if isinstance(event, ToolCallStart)]
    assert [event.tool_call_id for event in starts] == ["echo-first", "echo-duplicate"]
    assert starts[0].user_visible is True
    assert starts[1].user_visible is False
    results = [event for event in events if isinstance(event, ToolCallResult)]
    assert len(results) == 2
    assert results[0].content == "echo:same"
    assert results[1].success is True
    assert results[1].user_visible is False
    assert "Duplicate tool call skipped" in results[1].content
    tool_messages = [message for message in messages if message.role == "tool"]
    assert [message.tool_call_id for message in tool_messages] == [
        "echo-first",
        "echo-duplicate",
    ]


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
async def test_web_search_skips_high_overlap_query_rewrites_but_keeps_new_gaps():
    profile_query = (
        "site:fcbarcelona.com/en/football/first-team/players "
        "Lamine Yamal profile"
    )
    rewritten_profile_query = (
        "site:fcbarcelona.com Lamine Yamal first team profile"
    )
    award_query = (
        "site:uefa.com Lamine Yamal Young Player of the Tournament EURO 2024"
    )
    llm = MockLLM(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="web-profile",
                        type="function",
                        function=FunctionCall(
                            name="web_search",
                            arguments={"query": profile_query},
                        ),
                    ),
                    ToolCall(
                        id="web-profile-rewrite",
                        type="function",
                        function=FunctionCall(
                            name="web_search",
                            arguments={"query": rewritten_profile_query},
                        ),
                    ),
                    ToolCall(
                        id="web-award",
                        type="function",
                        function=FunctionCall(
                            name="web_search",
                            arguments={"query": award_query},
                        ),
                    ),
                ],
                finish_reason="tool",
            ),
            LLMResponse(content="final", finish_reason="stop"),
        ]
    )
    web_search = JsonWebSearchTool(
        {
            profile_query: "https://www.fcbarcelona.com/en/football/first-team/players/129404",
            award_query: "https://www.uefa.com/euro2024/news/yamal-award/",
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

    assert web_search.calls == [profile_query, award_query]
    rewritten_result = next(
        event
        for event in events
        if isinstance(event, ToolCallResult)
        and event.tool_call_id == "web-profile-rewrite"
    )
    assert rewritten_result.success is False
    assert rewritten_result.user_visible is False
    assert "near-duplicate" in (rewritten_result.error or "")


@pytest.mark.asyncio
async def test_web_search_site_query_drops_off_domain_results():
    query = "site:fifa.com Brazil World Cup history"
    llm = MockLLM(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="web-site",
                        type="function",
                        function=FunctionCall(
                            name="web_search",
                            arguments={"query": query},
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(content="final", finish_reason="stop"),
        ]
    )
    web_search = JsonWebSearchTool(
        {query: "https://irrelevant.example.com/brazil-world-cup"}
    )

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"web_search": web_search},
            max_steps=5,
        )
    )

    result = next(
        event
        for event in events
        if isinstance(event, ToolCallResult) and event.tool_call_id == "web-site"
    )
    payload = json.loads(result.content)
    assert payload["refs"] == []
    assert payload["RequestedSiteDomain"] == "fifa.com"
    assert payload["SiteFilterDroppedCount"] == 1
    assert payload["SiteFilterMatchedCount"] == 0
    assert "Do not cite or relabel dropped results" in payload["SiteFilterNotice"]


@pytest.mark.asyncio
async def test_web_search_site_query_filters_nested_web_results_payload():
    query = "site:fifa.com Brazil World Cup history"
    llm = MockLLM(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="web-site-nested",
                        type="function",
                        function=FunctionCall(name="web_search", arguments={"query": query}),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(content="final", finish_reason="stop"),
        ]
    )

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"web_search": NestedWebResultsTool()},
            max_steps=5,
        )
    )

    result = next(
        event
        for event in events
        if isinstance(event, ToolCallResult) and event.tool_call_id == "web-site-nested"
    )
    payload = json.loads(result.content)
    nested_results = payload["Result"]["WebResults"]
    assert [item["Url"] for item in nested_results] == [
        "https://www.fifa.com/tournaments/mens/worldcup"
    ]
    assert payload["RequestedSiteDomain"] == "fifa.com"
    assert payload["SiteFilterDroppedCount"] == 1
    assert payload["SiteFilterMatchedCount"] == 1
    assert "irrelevant.example.com" not in result.content


def test_web_search_reranks_entity_matches_and_marks_direct_read_candidates():
    from box_agent.core import _dedupe_web_search_content

    query = "Example Corp Product One official launch"
    content = json.dumps(
        {
            "Query": query,
            "Results": [
                {
                    "Title": "Generic product launch roundup",
                    "Url": "https://news.example.net/roundup",
                    "Snippet": "Several unrelated products launched this year.",
                },
                {
                    "Title": "Example Corp officially launches Product One",
                    "Url": "https://example.com/news/product-one",
                    "Snippet": "Example Corp introduced Product One to customers.",
                    "SourceType": "official",
                },
            ],
        }
    )

    reranked, new_count, duplicate_count, _, inspected = (
        _dedupe_web_search_content(content, set(), {"query": query})
    )

    payload = json.loads(reranked)
    assert inspected is True
    assert new_count == 2
    assert duplicate_count == 0
    assert payload["Results"][0]["Url"] == "https://example.com/news/product-one"
    assert payload["SearchStatus"] == "ok"
    assert payload["SearchResultRanking"][0]["entity_match_score"] > (
        payload["SearchResultRanking"][1]["entity_match_score"]
    )
    assert payload["SearchResultRanking"][0]["first_party_level"] == 2
    assert payload["DirectReadCandidates"] == [
        "https://example.com/news/product-one"
    ]


def test_web_search_site_query_reports_provider_empty_state():
    from box_agent.core import _dedupe_web_search_content

    content = json.dumps({"Query": "site:example.com Product One", "Results": []})

    normalized, new_count, duplicate_count, _, inspected = (
        _dedupe_web_search_content(
            content,
            set(),
            {"query": "site:example.com Product One"},
        )
    )

    payload = json.loads(normalized)
    assert inspected is True
    assert new_count == 0
    assert duplicate_count == 0
    assert payload["SearchStatus"] == "site_no_results"
    assert payload["RequestedSiteDomain"] == "example.com"
    assert payload["SiteFilterMatchedCount"] == 0
    assert "No results were returned for site:example.com" in payload["SiteFilterNotice"]


def test_web_search_logs_ranked_top_results_sent_to_model(caplog):
    from box_agent.core import (
        _dedupe_web_search_content,
        _log_web_search_model_results,
        _tool_message_content_for_model,
    )
    from box_agent.tools.base import ToolResult

    query = "Example Corp Product One"
    normalized, *_ = _dedupe_web_search_content(
        json.dumps(
            {
                "Results": [
                    {
                        "Title": "Unrelated item",
                        "Url": "https://unrelated.example/item",
                        "Snippet": "No entity match.",
                    },
                    {
                        "Title": "Example Corp Product One",
                        "Url": "https://example.com/product-one",
                        "Snippet": "Example Corp product information.",
                    },
                ]
            }
        ),
        set(),
        {"query": query},
    )
    model_content = _tool_message_content_for_model(
        tool_name="web_search",
        arguments={"query": query},
        result=ToolResult(success=True, content=normalized),
        visible_content=normalized,
        visible_error=None,
    )

    with caplog.at_level("INFO", logger="box_agent.core"):
        _log_web_search_model_results({"query": query}, normalized, model_content)

    message = next(
        record.getMessage()
        for record in caplog.records
        if "web_search/model_results" in record.getMessage()
    )
    assert "Example Corp Product One" in message
    assert message.index("https://example.com/product-one") < message.index(
        "https://unrelated.example/item"
    )


def test_large_web_search_compaction_uses_reranked_top_results():
    from box_agent.core import (
        _dedupe_web_search_content,
        _tool_message_content_for_model,
    )
    from box_agent.tools.base import ToolResult

    query = "Example Corp Product One"
    results = [
        {
            "Title": f"Generic result {index}",
            "Url": f"https://generic.example/result-{index}",
            "Snippet": "Unrelated roundup " + ("x" * 2_000),
        }
        for index in range(1, 9)
    ]
    results.append(
        {
            "Title": "Example Corp Product One official page",
            "Url": "https://example.com/product-one",
            "Snippet": "Example Corp Product One details " + ("y" * 2_000),
            "SourceType": "official",
        }
    )
    normalized, *_ = _dedupe_web_search_content(
        json.dumps({"Results": results}),
        set(),
        {"query": query},
    )

    model_content = _tool_message_content_for_model(
        tool_name="web_search",
        arguments={"query": query},
        result=ToolResult(success=True, content=normalized),
        visible_content=normalized,
        visible_error=None,
    )

    assert "Large web_search result bounded for model history" in model_content
    assert "https://example.com/product-one" in model_content
    assert model_content.index("https://example.com/product-one") < model_content.index(
        "https://generic.example/result-1"
    )


@pytest.mark.asyncio
async def test_web_search_tool_result_is_compacted_for_the_next_model_step():
    tool_call = ToolCall(
        id="web-large",
        type="function",
        function=FunctionCall(name="web_search", arguments={"query": "policy 400g"}),
    )
    llm = CapturingStreamLLM(
        [
            LLMResponse(content="", tool_calls=[tool_call], finish_reason="tool"),
            LLMResponse(content="final", finish_reason="stop"),
        ]
    )

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"web_search": LargeJsonWebSearchTool()},
            max_steps=5,
        )
    )

    visible_result = next(
        e for e in events if isinstance(e, ToolCallResult) and e.tool_call_id == "web-large"
    )
    assert "RAW_SEARCH_BODY_" in visible_result.content

    assert len(llm.message_calls) >= 2
    tool_message = next(
        m
        for m in llm.message_calls[1]
        if m.role == "tool" and m.name == "web_search" and m.tool_call_id == "web-large"
    )
    assert tool_message.content != visible_result.content
    assert "query=policy 400g" in tool_message.content
    assert "Official policy result" in tool_message.content
    assert "https://example.gov/policy" in tool_message.content
    assert "A concise summary of the policy" in tool_message.content
    assert "RAW_SEARCH_BODY_" not in tool_message.content
    assert len(tool_message.content) < 10_000


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
async def test_search_files_empty_result_breaker_blocks_more_searches():
    class EmptySearchTool(Tool):
        def __init__(self):
            self.calls = 0

        @property
        def name(self):
            return "search_files"

        @property
        def description(self):
            return "search files"

        @property
        def parameters(self):
            return {"type": "object", "properties": {}}

        async def execute(self, **_kwargs):
            self.calls += 1
            return ToolResult(
                success=True,
                content="No matches found.",
                raw_output={"returned_matches": 0, "timed_out": False},
            )

    def search_call(index):
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id=f"search-{index}",
                    type="function",
                    function=FunctionCall(
                        name="search_files",
                        arguments={"pattern": f"missing-{index}"},
                    ),
                )
            ],
            finish_reason="tool",
        )

    tool = EmptySearchTool()
    events = await collect(
        run_agent_loop(
            llm=MockLLM(
                [
                    search_call(1),
                    search_call(2),
                    search_call(3),
                    search_call(4),
                    LLMResponse(content="file is missing", finish_reason="stop"),
                ]
            ),
            messages=_msgs(),
            tools={"search_files": tool},
            max_steps=10,
            tool_limits=ToolLimitsConfig(
                search_files={"consecutive_empty_limit": 3}
            ),
        )
    )

    assert tool.calls == 3
    assert any(
        isinstance(event, InjectedMessageEvent) and "文件搜索熔断器已打开" in event.content
        for event in events
    )
    blocked = next(
        event
        for event in events
        if isinstance(event, ToolCallResult) and event.tool_call_id == "search-4"
    )
    assert blocked.success is False
    assert "circuit breaker is open" in blocked.error


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
async def test_parallel_tool_timeout_keeps_completed_siblings():
    class PartlyHangingTool(Tool):
        parallel_safe = True

        def __init__(self):
            self.cancelled = False

        @property
        def name(self):
            return "probe"

        @property
        def description(self):
            return "probe"

        @property
        def parameters(self):
            return {"type": "object", "properties": {"task": {"type": "string"}}}

        async def execute(self, task: str):
            if task == "hang":
                try:
                    await asyncio.sleep(10)
                finally:
                    self.cancelled = True
            await asyncio.sleep(0.01)
            return ToolResult(success=True, content=f"ok:{task}")

    probe = PartlyHangingTool()
    responses = [
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(id="fast", type="function", function=FunctionCall(name="probe", arguments={"task": "fast"})),
                ToolCall(id="hang", type="function", function=FunctionCall(name="probe", arguments={"task": "hang"})),
            ],
            finish_reason="tool",
        ),
        LLMResponse(content="merged", finish_reason="stop"),
    ]

    events = await collect(
        run_agent_loop(
            llm=MockLLM(responses),
            messages=_msgs(),
            tools={"probe": probe},
            max_steps=5,
            max_parallel_tools=2,
            parallel_tool_timeout_seconds=0.03,
        )
    )

    results = [
        e
        for e in events
        if isinstance(e, ToolCallResult) and e.tool_name == "probe"
    ]
    assert len(results) == 2
    by_id = {result.tool_call_id: result for result in results}
    assert by_id["fast"].success is True
    assert by_id["fast"].content == "ok:fast"
    assert by_id["hang"].success is False
    assert "timed out" in (by_id["hang"].error or "")
    assert probe.cancelled is True
    assert any(isinstance(e, ContentEvent) and e.content == "merged" for e in events)


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


@pytest.mark.asyncio
async def test_browser_snapshot_relative_filename_uses_artifact_root(tmp_path):
    output_dir = tmp_path / "session-a" / "output"
    (output_dir / "research").mkdir(parents=True)
    snapshot = RecordingBrowserSnapshotTool()
    llm = MockLLM(
        [
            LLMResponse(
                content="capture source",
                tool_calls=[
                    ToolCall(
                        id="snapshot-1",
                        type="function",
                        function=FunctionCall(
                            name="browser_snapshot",
                            arguments={"filename": "research/source.md"},
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(content="done", finish_reason="stop"),
        ]
    )

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"browser_snapshot": snapshot},
            max_steps=5,
            workspace_dir=str(tmp_path),
            artifact_root_dir=output_dir,
        )
    )

    assert snapshot.filenames == [""]
    persisted = output_dir / "research" / "source.md"
    assert persisted.read_text(encoding="utf-8") == "snapshot:"
    assert any(
        isinstance(event, ArtifactEvent) and event.abs_path == str(persisted)
        for event in events
    )


@pytest.mark.asyncio
async def test_browser_snapshot_relative_filename_cannot_escape_artifact_root(tmp_path):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    snapshot = RecordingBrowserSnapshotTool()
    llm = MockLLM(
        [
            LLMResponse(
                content="capture source",
                tool_calls=[
                    ToolCall(
                        id="snapshot-escape",
                        type="function",
                        function=FunctionCall(
                            name="browser_snapshot",
                            arguments={"filename": "../outside.md"},
                        ),
                    )
                ],
                finish_reason="tool",
            ),
            LLMResponse(content="done", finish_reason="stop"),
        ]
    )

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"browser_snapshot": snapshot},
            max_steps=5,
            workspace_dir=str(tmp_path),
            artifact_root_dir=output_dir,
        )
    )

    assert snapshot.filenames == []
    result = next(event for event in events if isinstance(event, ToolCallResult))
    assert "BROWSER_SNAPSHOT_OUTPUT_PATH_INVALID" in (result.error or "")


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
async def test_provider_stale_with_partial_content_resumes_once(tmp_path):
    msgs = _msgs()
    llm = MockLLM(
        [
            LLMResponse(content="我先创建文件。", finish_reason="provider_stale"),
            LLMResponse(content="文件已经创建并验证。", finish_reason="stop"),
        ]
    )

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=msgs,
            tools={},
            max_steps=3,
            workspace_dir=str(tmp_path),
        )
    )

    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert errors == []
    done = [event for event in events if isinstance(event, DoneEvent)]
    assert done and done[-1].stop_reason == StopReason.END_TURN

    assistant_contents = [message.content for message in msgs if message.role == "assistant"]
    assert "我先创建文件。" in assistant_contents
    injected_messages = [
        event for event in events if isinstance(event, InjectedMessageEvent)
    ]
    assert len(injected_messages) == 1
    assert injected_messages[0].user_visible is False
    assert "从未完成的动作继续" in injected_messages[0].content
    assert "5,500" in injected_messages[0].content


@pytest.mark.asyncio
async def test_provider_stale_allows_new_partial_progress_after_empty_retry(tmp_path):
    msgs = _msgs()
    llm = MockLLM(
        [
            LLMResponse(content="", finish_reason="provider_stale"),
            LLMResponse(content="我已恢复，继续准备写入。", finish_reason="provider_stale"),
            LLMResponse(content="写入完成。", finish_reason="stop"),
        ]
    )

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=msgs,
            tools={},
            max_steps=4,
            workspace_dir=str(tmp_path),
        )
    )

    assert [event for event in events if isinstance(event, ErrorEvent)] == []
    done = [event for event in events if isinstance(event, DoneEvent)]
    assert done and done[-1].stop_reason == StopReason.END_TURN
    injected = [event for event in events if isinstance(event, InjectedMessageEvent)]
    assert len(injected) == 1
    assert "从未完成的动作继续" in injected[0].content


@pytest.mark.asyncio
async def test_provider_stale_retries_three_empty_responses_before_stopping(tmp_path):
    llm = MockLLM(
        [
            LLMResponse(content="", finish_reason="provider_stale"),
            LLMResponse(content="", finish_reason="provider_stale"),
            LLMResponse(content="", finish_reason="provider_stale"),
            LLMResponse(content="", finish_reason="provider_stale"),
        ]
    )

    events = await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={},
            max_steps=5,
            workspace_dir=str(tmp_path),
        )
    )

    assert llm._idx == 4
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert len(errors) == 1 and errors[0].is_fatal is True
    done = [event for event in events if isinstance(event, DoneEvent)]
    assert done and done[-1].stop_reason == StopReason.ERROR


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
    assert _micro_compact(msgs).transformed_count == 0
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
    assert compacted.transformed_count == 3

    # First 3 tool messages should be compacted
    assert msgs[3].content.startswith("[Previous result from execute_code:")
    assert msgs[4].content.startswith("[Previous result from execute_code:")
    assert msgs[6].content.startswith("[Previous result from bash:")

    # Last 3 tool messages should be intact
    assert msgs[7].content.startswith("Final chart")
    assert msgs[8].content.startswith("recent content")
    assert msgs[9].content.startswith("latest result")


@pytest.mark.parametrize("latest_todo_tool", ["todo_write", "todo_read"])
def test_micro_compact_preserves_only_latest_todo_state(latest_todo_tool):
    old_todo_content = "Old todo state\n" + "o" * 500
    latest_todo_content = "Canonical todo state with ids and statuses\n" + "t" * 500
    msgs = [
        Message(role="system", content="sys"),
        _make_tool_msg("todo_write", old_todo_content, "todo-old"),
        _make_tool_msg(latest_todo_tool, latest_todo_content, "todo-latest"),
        _make_tool_msg("bash", "one\n" + "a" * 500, "tc-1"),
        _make_tool_msg("read_file", "two\n" + "b" * 500, "tc-2"),
        _make_tool_msg("bash", "three\n" + "c" * 500, "tc-3"),
        _make_tool_msg("write_file", "four\n" + "d" * 500, "tc-4"),
    ]

    compacted = _micro_compact(msgs)

    assert compacted.transformed_count == 2
    assert msgs[1].content.startswith("[Previous result from todo_write:")
    assert msgs[2].content == latest_todo_content
    assert msgs[2].tool_call_id == "todo-latest"


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
    assert compacted.transformed_count == 1
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


def test_micro_compact_reports_replaced_resource_source_id():
    from box_agent.context_resources import (
        ContextResourceLedger,
        ResourceClass,
        ResourceDescriptor,
    )

    content = "resource body\n" + "x" * 500
    descriptor = ResourceDescriptor(
        resource_id="/workspace/source.txt",
        content_version="a" * 64,
        start_line=1,
        end_line=2,
        total_lines=2,
        resource_class=ResourceClass.RECONSTRUCTABLE,
    )
    ledger = ContextResourceLedger()
    ledger.register_full_source("source-1", descriptor, content)
    msgs = [
        Message(role="system", content="sys"),
        _make_tool_msg("read_file", content, "source-1"),
        _make_tool_msg("bash", "a" * 500, "tc-2"),
        _make_tool_msg("bash", "b" * 500, "tc-3"),
        _make_tool_msg("bash", "c" * 500, "tc-4"),
    ]

    result = _micro_compact(msgs, ledger)

    assert result.replaced_source_tool_call_ids == ("source-1",)
    ledger.invalidate_source_ids(result.replaced_source_tool_call_ids)
    assert ledger.source_ids == ()


def test_micro_compact_preserves_instruction_pinned_resource_source():
    from box_agent.context_resources import (
        ContextResourceLedger,
        ResourceClass,
        ResourceDescriptor,
    )

    content = "authoritative workflow\n" + "x" * 500
    descriptor = ResourceDescriptor(
        resource_id="/workspace/skills/ppt/references/contract.md",
        content_version="a" * 64,
        start_line=1,
        end_line=2,
        total_lines=2,
        resource_class=ResourceClass.INSTRUCTION_PINNED,
    )
    ledger = ContextResourceLedger()
    ledger.register_full_source("source-1", descriptor, content)
    msgs = [
        Message(role="system", content="sys"),
        _make_tool_msg("read_file", content, "source-1"),
        _make_tool_msg("bash", "a" * 500, "tc-2"),
        _make_tool_msg("bash", "b" * 500, "tc-3"),
        _make_tool_msg("bash", "c" * 500, "tc-4"),
    ]

    result = _micro_compact(msgs, ledger)

    assert msgs[1].content == content
    assert "source-1" not in result.replaced_source_tool_call_ids
    assert ledger.source("source-1") is not None


def test_resource_shedding_invalidates_only_reconstructable_source():
    from box_agent.context_resources import (
        ContextResourceLedger,
        ResourceClass,
        ResourceDescriptor,
        build_resource_receipt,
    )
    from box_agent.core import (
        _context_resource_history_decision,
        _shed_reconstructable_read_resources,
    )

    descriptor = ResourceDescriptor(
        resource_id="/workspace/reference.md",
        content_version="a" * 64,
        start_line=1,
        end_line=1,
        total_lines=1,
        resource_class=ResourceClass.RECONSTRUCTABLE,
    )
    content = "large exact source " * 4_000
    ledger = ContextResourceLedger()
    ledger.register_full_source("source-a", descriptor, content)
    receipt = build_resource_receipt(descriptor, ["source-a"])
    ledger.register_receipt("receipt-b", ["source-a"])
    msgs = [
        Message(role="system", content="sys"),
        Message(role="user", content="task"),
        _make_tool_msg("read_file", content, "source-a"),
        _make_tool_msg("read_file", receipt, "receipt-b"),
    ]

    result = _shed_reconstructable_read_resources(
        msgs,
        ledger,
        token_limit=50,
    )

    assert result.replaced_source_tool_call_ids == ("source-a",)
    assert msgs[2].content.startswith(
        "[Reconstructable read_file content shed from model history]"
    )
    ledger.invalidate_source_ids(result.replaced_source_tool_call_ids)
    assert ledger.covering_source_ids(descriptor, msgs) == ()
    assert ledger.receipt_ids == ("receipt-b",)
    reread_decision = _context_resource_history_decision(
        tool_name="read_file",
        arguments={"path": "reference.md"},
        result=ToolResult(
            success=True,
            content=content,
            raw_output={"context_resource": descriptor.as_raw_output()},
        ),
        messages=msgs,
        ledger=ledger,
    )
    assert reread_decision.receipt is None


def test_resource_shedding_never_replaces_instruction_pinned_source():
    from box_agent.context_resources import (
        ContextResourceLedger,
        ResourceClass,
        ResourceDescriptor,
    )
    from box_agent.core import _shed_reconstructable_read_resources

    descriptor = ResourceDescriptor(
        resource_id="/workspace/skills/ppt/references/contract.md",
        content_version="a" * 64,
        start_line=1,
        end_line=1,
        total_lines=1,
        resource_class=ResourceClass.INSTRUCTION_PINNED,
    )
    content = "authoritative instruction " * 4_000
    ledger = ContextResourceLedger()
    ledger.register_full_source("source-a", descriptor, content)
    msgs = [
        Message(role="system", content="sys"),
        Message(role="user", content="task"),
        _make_tool_msg("read_file", content, "source-a"),
    ]

    result = _shed_reconstructable_read_resources(
        msgs,
        ledger,
        token_limit=50,
    )

    assert result.transformed_count == 0
    assert msgs[-1].content == content
    assert ledger.source("source-a") is not None


@pytest.mark.asyncio
async def test_summary_rewrite_rotates_context_resource_epoch():
    from box_agent.context_resources import (
        ContextResourceLedger,
        ResourceClass,
        ResourceDescriptor,
    )

    content = "authoritative instruction " * 4_000
    descriptor = ResourceDescriptor(
        resource_id="/workspace/skills/ppt/references/contract.md",
        content_version="a" * 64,
        start_line=1,
        end_line=1,
        total_lines=1,
        resource_class=ResourceClass.INSTRUCTION_PINNED,
    )
    ledger = ContextResourceLedger()
    ledger.register_full_source("source-a", descriptor, content)
    msgs = [
        Message(role="system", content="sys"),
        Message(role="user", content="old request"),
        Message(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(
                    id="source-a",
                    type="function",
                    function=FunctionCall(
                        name="read_file",
                        arguments={"path": descriptor.resource_id},
                    ),
                )
            ],
        ),
        _make_tool_msg("read_file", content, "source-a"),
        Message(role="user", content="current request"),
    ]
    class SummaryThenFinalLLM:
        async def generate(self, **_kwargs):
            return LLMResponse(content="bounded summary", finish_reason="stop")

        async def generate_stream(self, **_kwargs):
            yield StreamEvent(type="text", delta="done")
            yield StreamEvent(type="finish", finish_reason="stop")

    llm = SummaryThenFinalLLM()

    await collect(
        run_agent_loop(
            llm=llm,
            messages=msgs,
            tools={},
            max_steps=2,
            token_limit=500,
            context_resource_ledger=ledger,
        )
    )

    assert ledger.epoch == 1
    assert ledger.source_ids == ()


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
            "messages": messages,
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
async def test_maybe_summarize_uses_bounded_fallback_on_llm_failure():
    """Summary failure keeps a bounded reference record instead of deleting execution data."""
    from box_agent.core import _maybe_summarize
    msgs = [
        Message(role="system", content="sys"),
        Message(role="user", content="please do X"),
        Message(role="assistant", content="A" * 50_000),
        Message(role="tool", content="B" * 50_000, tool_call_id="t1", name="bash"),
    ]
    llm = _FakeSummaryLLM(raise_exc=RuntimeError("network"))
    outcome = await _maybe_summarize(
        llm, msgs, token_limit=5_000, api_total_tokens=0, skip_check=False
    )
    new_msgs, skip_next, _est = outcome
    assert new_msgs is not None
    assert skip_next is False
    assert outcome.mode == "fallback"
    assert outcome.error == "network"
    assert [m.role for m in new_msgs] == ["system", "user", "user"]
    assert "Deterministic history fallback" in str(new_msgs[1].content)
    assert "tool=bash" in str(new_msgs[1].content)
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
    # The compact reference precedes the exact active user request.
    assert new_msgs[1].role == "user"
    assert new_msgs[1].content.startswith(_SUMMARY_MARKER)
    assert "brief" in new_msgs[1].content
    assert new_msgs[2] is msgs[1]


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
async def test_second_compaction_rolls_prior_summary_forward_once():
    from box_agent.core import _maybe_summarize, _SUMMARY_MARKER

    prior_fact = "IMPORTANT_PRIOR_FACT_42"
    msgs = [
        Message(role="system", content="sys"),
        Message(role="user", content=f"{_SUMMARY_MARKER}\n\n{prior_fact}"),
        Message(role="user", content="current request"),
        Message(role="assistant", content="new execution " * 5_000),
    ]
    llm = _FakeSummaryLLM("rolled history")
    outcome = await _maybe_summarize(
        llm, msgs, token_limit=1_000, api_total_tokens=0, skip_check=False
    )

    assert outcome.messages is not None
    summary_prompt = llm.calls[0]["messages"][1].content
    assert prior_fact in summary_prompt
    assert sum(
        1
        for message in outcome.messages
        if message.role == "user" and str(message.content).startswith(_SUMMARY_MARKER)
    ) == 1


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


@pytest.mark.asyncio
async def test_maybe_summarize_compacts_many_rounds_with_one_llm_call():
    from box_agent.core import _maybe_summarize

    msgs = [Message(role="system", content="system")]
    for index in range(12):
        msgs.extend(
            [
                Message(role="user", content=f"round {index}"),
                Message(role="assistant", content=(f"result-{index}-" * 700)),
            ]
        )
    llm = _FakeSummaryLLM("one bounded summary")
    outcome = await _maybe_summarize(
        llm, msgs, token_limit=1_000, api_total_tokens=0, skip_check=False
    )

    assert outcome.messages is not None
    assert outcome.summary_calls == 1
    assert len(llm.calls) == 1
    assert outcome.messages[-1] is msgs[-2]


@pytest.mark.asyncio
async def test_create_summary_bounds_provider_prompt():
    from box_agent.core import _create_summary

    msgs = [
        Message(role="tool", content=str(index) + "x" * 20_000, name="bash", tool_call_id=str(index))
        for index in range(30)
    ]
    llm = _FakeSummaryLLM("bounded")
    await _create_summary(llm, msgs, 1)

    prompt = llm.calls[0]["messages"][1].content
    assert len(prompt) < 65_000
    assert "summary source limit reached" in prompt


@pytest.mark.asyncio
async def test_maybe_summarize_preserves_latest_user_and_recent_tail_exactly():
    from box_agent.core import _maybe_summarize

    latest_user = Message(role="user", content="LATEST REQUEST MUST STAY EXACT")
    recent_tail = Message(role="assistant", content="recent evidence " * 50)
    msgs = [
        Message(role="system", content="sys"),
        Message(role="user", content="old request"),
        Message(role="assistant", content="old execution " * 4_000),
        latest_user,
        recent_tail,
    ]
    outcome = await _maybe_summarize(
        _FakeSummaryLLM("old history"),
        msgs,
        token_limit=5_000,
        api_total_tokens=0,
        skip_check=False,
    )

    assert outcome.messages is not None
    assert outcome.messages[-2] is latest_user
    assert outcome.messages[-1] is recent_tail


@pytest.mark.asyncio
@pytest.mark.parametrize("latest_todo_tool", ["todo_write", "todo_read"])
@pytest.mark.parametrize("summary_fails", [False, True])
async def test_maybe_summarize_preserves_latest_todo_state_exactly(
    latest_todo_tool,
    summary_fails,
):
    from box_agent.core import _maybe_summarize, _TODO_STATE_MARKER

    todo_state = "Canonical todo state with ids and statuses\n" + "t" * 500
    latest_user = Message(role="user", content="keep this request")
    msgs = [
        Message(role="system", content="sys"),
        latest_user,
        _make_tool_msg(latest_todo_tool, todo_state, "todo-latest"),
        Message(role="assistant", content="large execution " * 8_000),
    ]
    llm = _FakeSummaryLLM(
        "execution summarized",
        raise_exc=RuntimeError("provider down") if summary_fails else None,
    )

    outcome = await _maybe_summarize(
        llm,
        msgs,
        token_limit=5_000,
        api_total_tokens=0,
        skip_check=False,
    )

    assert outcome.messages is not None
    checkpoints = [
        message
        for message in outcome.messages
        if message.role == "user"
        and str(message.content).startswith(_TODO_STATE_MARKER)
    ]
    assert len(checkpoints) == 1
    assert checkpoints[0].content == (
        f"{_TODO_STATE_MARKER}\nTool: {latest_todo_tool}\n\n{todo_state}"
    )
    assert outcome.messages[-1] is latest_user

    second_input = [
        *outcome.messages,
        Message(role="assistant", content="more large execution " * 8_000),
    ]
    second_outcome = await _maybe_summarize(
        llm,
        second_input,
        token_limit=5_000,
        api_total_tokens=0,
        skip_check=False,
    )
    assert second_outcome.messages is not None
    repeated_checkpoints = [
        message
        for message in second_outcome.messages
        if message.role == "user"
        and str(message.content).startswith(_TODO_STATE_MARKER)
    ]
    assert [message.content for message in repeated_checkpoints] == [
        f"{_TODO_STATE_MARKER}\nTool: {latest_todo_tool}\n\n{todo_state}"
    ]


@pytest.mark.asyncio
async def test_maybe_summarize_counts_thinking_when_selecting_protected_tail():
    from box_agent.core import _maybe_summarize

    latest_user = Message(role="user", content="keep this request")
    oversized_reasoning = Message(
        role="assistant",
        content="",
        thinking="large hidden reasoning " * 8_000,
    )
    msgs = [
        Message(role="system", content="sys"),
        Message(role="user", content="old request"),
        Message(role="assistant", content="old execution"),
        latest_user,
        oversized_reasoning,
    ]

    outcome = await _maybe_summarize(
        _FakeSummaryLLM("complete execution summary"),
        msgs,
        token_limit=5_000,
        api_total_tokens=0,
        skip_check=False,
    )

    assert outcome.mode == "summary"
    assert outcome.messages is not None
    assert outcome.messages[-1] is latest_user
    assert oversized_reasoning not in outcome.messages


@pytest.mark.asyncio
async def test_maybe_summarize_uses_prompt_tokens_not_total_tokens_when_provided():
    from box_agent.core import _maybe_summarize

    llm = _FakeSummaryLLM("unused")
    outcome = await _maybe_summarize(
        llm,
        [Message(role="system", content="sys"), Message(role="user", content="small")],
        token_limit=5_000,
        api_total_tokens=50_000,
        api_prompt_tokens=10,
        skip_check=False,
    )

    assert outcome.mode == "none"
    assert llm.calls == []


@pytest.mark.asyncio
async def test_provider_prompt_pressure_can_compact_current_execution_suffix():
    from box_agent.core import _maybe_summarize

    latest_user = Message(role="user", content="keep this request")
    msgs = [
        Message(role="system", content="sys"),
        latest_user,
        Message(role="assistant", content="current execution"),
        Message(role="tool", content="recent output", name="bash", tool_call_id="t1"),
    ]
    outcome = await _maybe_summarize(
        _FakeSummaryLLM("current execution summarized"),
        msgs,
        token_limit=1_000,
        api_total_tokens=50_000,
        api_prompt_tokens=2_000,
        skip_check=False,
    )

    assert outcome.mode == "summary"
    assert outcome.messages is not None
    assert outcome.messages[-1] is latest_user
    assert outcome.summary_calls == 1


@pytest.mark.asyncio
async def test_summary_cooldown_uses_local_fallback_without_provider_call():
    from box_agent.core import _maybe_summarize

    llm = _FakeSummaryLLM("should not be called")
    outcome = await _maybe_summarize(
        llm,
        [
            Message(role="system", content="sys"),
            Message(role="user", content="task"),
            Message(role="assistant", content="large " * 10_000),
        ],
        token_limit=5_000,
        api_total_tokens=0,
        skip_check=False,
        allow_llm_summary=False,
    )

    assert outcome.mode == "fallback"
    assert outcome.summary_calls == 0
    assert outcome.error_type == "RuntimeError"
    assert llm.calls == []


def test_request_token_estimate_includes_tool_schemas():
    from box_agent.core import _estimate_request_tokens, _estimate_tokens

    class _SchemaTool:
        def to_openai_schema(self):
            return {"description": "large schema " * 2_000}

    msgs = [Message(role="system", content="sys"), Message(role="user", content="small")]
    message_tokens = _estimate_tokens(msgs)
    request_tokens = _estimate_request_tokens(msgs, {"large": _SchemaTool()})
    assert request_tokens > message_tokens + 1_000


def test_generic_large_tool_result_is_bounded_only_for_model_history():
    from box_agent.core import _tool_message_content_for_model
    from box_agent.tools.base import ToolResult

    full_content = "start\n" + "x" * 100_000 + "\nfinal exit status: 0"
    model_content = _tool_message_content_for_model(
        tool_name="bash",
        arguments={"command": "demo"},
        result=ToolResult(success=True, content=full_content),
        visible_content=full_content,
        visible_error=None,
    )

    assert len(model_content) < 24_000
    assert "Characters returned: 100027" in model_content
    assert "final exit status: 0" in model_content
    assert full_content.endswith("final exit status: 0")


def test_large_web_search_result_is_compact_for_synthesis_turn():
    from box_agent.core import _tool_message_content_for_model
    from box_agent.tools.base import ToolResult

    refs = [
        {
            "Title": f"Official result {index}",
            "Url": f"https://example.com/result-{index}",
            "Content": f"Evidence {index} " + ("x" * 5_000),
        }
        for index in range(1, 10)
    ]
    full_content = json.dumps(
        {"Result": {"ResultCount": len(refs), "WebResults": refs}},
        ensure_ascii=False,
    )

    model_content = _tool_message_content_for_model(
        tool_name="web_search",
        arguments={"Query": "latest official release"},
        result=ToolResult(success=True, content=full_content),
        visible_content=full_content,
        visible_error=None,
    )

    assert len(full_content) > 10_000
    assert len(model_content) < 10_000
    assert "Large web_search result bounded for model history" in model_content
    assert "https://example.com/result-1" in model_content
    assert "https://example.com/result-8" in model_content
    assert "https://example.com/result-9" not in model_content


@pytest.mark.parametrize("size", [12_000, 20_000, 50_000])
def test_unstructured_web_search_compaction_is_smaller_than_input(size):
    from box_agent.core import _tool_message_content_for_model
    from box_agent.tools.base import ToolResult

    full_content = "x" * size
    model_content = _tool_message_content_for_model(
        tool_name="web_search",
        arguments={"Query": "latest official release"},
        result=ToolResult(success=True, content=full_content),
        visible_content=full_content,
        visible_error=None,
    )

    assert len(model_content) < len(full_content)
    assert len(model_content) <= 10_000
    assert f"Characters omitted: {size - 9_000}" in model_content
    assert "Characters omitted: -" not in model_content


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
    assert compacted.transformed_count >= 2, (
        "token-aware keep should compact more than the strict N-recent baseline; "
        f"got {compacted.transformed_count}"
    )

    # The most recent tool message must always be preserved verbatim.
    last_tool = [m for m in msgs if m.role == "tool"][-1]
    assert last_tool.content == big_payload


def test_token_estimators_treat_special_token_text_as_plain_text():
    from box_agent.core import _approx_tokens_for_content, _estimate_tokens

    special_text = "search result contains literal <|endoftext|> text"
    msgs = [
        Message(role="system", content="sys"),
        Message(role="user", content=special_text),
        Message(
            role="assistant",
            content=[{"type": "text", "text": special_text}],
            thinking=special_text,
            tool_calls=[
                ToolCall(
                    id="call_1",
                    type="function",
                    function=FunctionCall(name="echo", arguments={"text": special_text}),
                )
            ],
        ),
        Message(role="tool", content=special_text, tool_call_id="call_1", name="echo"),
    ]

    assert _estimate_tokens(msgs) > 0
    assert _approx_tokens_for_content({"text": special_text}) > 0


def test_micro_compact_preserves_at_least_one_recent_when_single_giant():
    """Single giant tool result must not be compacted (keep_count never < 1)."""
    from box_agent.core import _micro_compact
    msgs = [
        Message(role="system", content="sys"),
        Message(role="assistant", content=""),
        Message(role="tool", content="y" * 100_000, tool_call_id="t0", name="bash"),
    ]
    result = _micro_compact(msgs)
    assert result.transformed_count == 0
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

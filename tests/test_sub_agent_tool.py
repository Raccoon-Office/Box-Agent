"""Test cases for SubAgentTool."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from box_agent.events import DoneEvent, StopReason, SubAgentEvent, WebSearchEvent
from box_agent.context_resources import ResourceDescriptor
from box_agent.schema import LLMResponse, Message, StreamEvent, TokenUsage
from box_agent.agent import Agent
from box_agent.tools.base import Tool, ToolResult
from box_agent.tools.file_tools import ReadTool, WriteTool
from box_agent.tools.skill_loader import SkillLoader
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


class WebSearchTool(Tool):
    """A web_search-shaped tool that returns reference metadata."""

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return "Search the web"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {"query": {"type": "string"}}}

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(
            success=True,
            content='{"refs":[{"reference_tag":"ref_1","title":"Example","url":"https://example.com"}]}',
        )


def _make_llm(text: str = "summary", tool_calls=None):
    """Return a mock LLM whose generate_stream yields the given text then finishes."""
    llm = AsyncMock()

    async def fake_stream(*, messages, tools, **kwargs):
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


def test_automatic_child_routing_selects_from_host_allowlist():
    class RoutingLLM:
        auto_model_candidates = (
            {
                "model": "model-general",
                "tags": ["general", "code"],
                "abilityLevel": 2,
            },
            {
                "model": "model-html",
                "tags": ["html", "frontend"],
                "abilityLevel": 2,
                "maxTokens": 50000,
            },
        )

        def __init__(self):
            self.bound = None

        def for_model(self, model, *, max_output_tokens=None):
            self.bound = (model, max_output_tokens)
            return f"bound:{model}"

    llm = RoutingLLM()
    tool = SubAgentTool(llm=llm, parent_tools={})

    child_llm, diagnostic = tool._resolve_task_llm(
        task="制作一个 HTML 前端页面",
    )

    assert child_llm == "bound:model-html"
    assert llm.bound == ("model-html", 50000)
    assert diagnostic["selected_model"] == "model-html"
    assert diagnostic["task_tags"] == ["frontend", "html"]


def test_manual_child_routing_inherits_parent_model():
    class ManualLLM:
        pass

    llm = ManualLLM()
    tool = SubAgentTool(llm=llm, parent_tools={})

    child_llm, diagnostic = tool._resolve_task_llm(
        task="制作一个 HTML 前端页面",
    )

    assert child_llm is llm
    assert diagnostic == {"mode": "inherit", "reason": "no_auto_model_pool"}


async def test_only_explicit_required_tools_influence_model_routing(monkeypatch):
    routed_tools = []

    def fake_resolve_model_client(llm, **kwargs):
        routed_tools.append(tuple(kwargs.get("required_tools", ())))
        return llm, {"mode": "test"}

    monkeypatch.setattr(
        "box_agent.tools.sub_agent_tool.resolve_model_client",
        fake_resolve_model_client,
    )
    llm = _make_llm()
    tool = SubAgentTool(llm=llm, parent_tools={"dummy": DummyTool()})

    inherited = await tool.invoke({"task": "Use the defaults"})
    explicit = await tool.invoke(
        {"task": "Use one tool", "required_tools": ["dummy"]}
    )

    assert inherited.success is True
    assert explicit.success is True
    assert routed_tools == [(), ("dummy",)]


def test_default_parallel_safe_is_false():
    """Other tools should have parallel_safe == False by default."""
    dummy = DummyTool()
    assert dummy.parallel_safe is False


def test_schema_exposes_single_loop_contract_with_machine_readable_defaults():
    llm = AsyncMock()
    tool = SubAgentTool(
        llm=llm,
        parent_tools={"dummy": DummyTool(), "web_search": WebSearchTool()},
    )
    schema = tool.to_schema()
    assert schema["name"] == "sub_agent"
    input_schema = schema["input_schema"]
    properties = input_schema["properties"]
    assert set(properties) == {
        "title",
        "task",
        "skills",
        "required_tools",
        "budget",
    }
    assert input_schema["required"] == ["task"]
    assert properties["title"]["default"] == ""
    assert properties["skills"]["default"] == []
    assert properties["required_tools"]["default"] == ["dummy", "web_search"]
    assert properties["budget"]["default"] == {
        "max_steps": 60,
        "max_tool_calls": 100,
    }
    assert properties["budget"]["properties"]["max_steps"]["default"] == 60
    assert properties["budget"]["properties"]["max_tool_calls"]["default"] == 100

    openai_schema = tool.to_openai_schema()
    assert openai_schema["function"]["name"] == "sub_agent"


def test_description_explains_delegation_fit_and_complete_brief_contract():
    llm = AsyncMock()
    tool = SubAgentTool(llm=llm, parent_tools={})
    description = tool.description

    assert "independent context, parallel latency, or evidence isolation" in description
    assert "single general-purpose agent loop" in description
    assert "batch_files" not in description
    assert "Do not delegate simple answers" in description
    assert "one known file or symbol lookup" in description
    assert "complete brief for a capable colleague" in description
    assert "has no parent conversation history" in description
    assert "exact paths or resources" in description
    assert "expected output" in description
    assert "`required_tools` defaults to all currently inherited parent tools" in description
    assert "strict subset" in description
    assert "empty list for a tool-free task" in description
    assert "Tools retain the parent permissions and constraints" in description
    assert "constraints=" not in description
    assert "parent remains responsible" in description
    assert "final deliverables" in description
    assert "Pass `budget` as an object" in description
    assert "never pass serialized JSON text" in description

    parameters = tool.parameters["properties"]
    assert "Never pass a serialized JSON string" in parameters["budget"]["description"]


# ── Tool filtering ───────────────────────────────────────────


def test_child_tools_exclude_sub_agent():
    """SubAgentTool must not include itself in the child tool set."""
    llm = AsyncMock()
    dummy = DummyTool()
    parent = {"dummy": dummy, "sub_agent": SubAgentTool(llm=llm, parent_tools={})}
    tool = SubAgentTool(llm=llm, parent_tools=parent)
    resolved = tool._resolve_child_tools()
    assert "sub_agent" not in resolved
    assert "dummy" in resolved


def test_resolve_child_tools_prefers_live_provider():
    """Child toolset follows the parent's live tool map, not the snapshot.

    Tools registered after construction (e.g. MCP web_search merged in via
    register_mcp_tools) must be inherited by child agents.
    """
    llm = AsyncMock()
    snapshot = {"dummy": DummyTool()}
    tool = SubAgentTool(llm=llm, parent_tools=snapshot)

    # Live parent map gains a tool after construction; provider points at it.
    live: dict = {"dummy": DummyTool(), "sub_agent": tool}
    tool.set_tool_provider(lambda: live)
    live["web_search"] = DummyTool()  # simulate late MCP merge (in-place mutation)

    resolved = tool._resolve_child_tools()
    assert "web_search" in resolved  # late tool inherited
    assert "sub_agent" not in resolved  # still excludes itself


def test_resolve_child_tools_honors_an_empty_live_parent_boundary():
    tool = SubAgentTool(llm=AsyncMock(), parent_tools={"dummy": DummyTool()})
    tool.set_tool_provider(lambda: {})

    assert tool._resolve_child_tools() == {}


def test_resolve_child_tools_falls_back_to_snapshot():
    """Without a provider (or if it fails), fall back to the snapshot."""
    llm = AsyncMock()
    tool = SubAgentTool(llm=llm, parent_tools={"dummy": DummyTool()})
    assert "dummy" in tool._resolve_child_tools()  # no provider → snapshot

    tool.set_tool_provider(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert "dummy" in tool._resolve_child_tools()  # provider raised → snapshot


# ── Execution ────────────────────────────────────────────────


async def test_basic_execution():
    """Sub-agent returns the LLM's final text as ToolResult content."""
    llm = _make_llm(text="Analysis complete: revenue up 20%")
    tool = SubAgentTool(llm=llm, parent_tools={})
    result = await tool.execute(task="Analyze revenue data")
    assert result.success is True
    assert "revenue up 20%" in result.content


async def test_forwarded_events_carry_short_title():
    """A provided `title` becomes the SubAgentEvent label; task is unchanged."""
    llm = _make_llm(text="done")
    tool = SubAgentTool(llm=llm, parent_tools={})
    queue = asyncio.Queue()
    tool._event_queue = queue
    tool._parent_tool_call_id = "parent-sub-agent"

    await tool.execute(
        task="围绕商汤科技（SenseTime, 0020.HK）做一个独立研究切片：财务表现与业务结构",
        title="财务表现与业务结构",
    )

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    sub_events = [e for e in events if isinstance(e, SubAgentEvent)]
    assert sub_events
    assert all(e.title == "财务表现与业务结构" for e in sub_events)
    # task_preview still reflects the (long, shared-prefix) task.
    assert all(e.title != e.task_preview for e in sub_events)


async def test_title_falls_back_to_task_preview_when_omitted():
    """Without a title, the label falls back to the task preview (no break)."""
    llm = _make_llm(text="done")
    tool = SubAgentTool(llm=llm, parent_tools={})
    queue = asyncio.Queue()
    tool._event_queue = queue
    tool._parent_tool_call_id = "parent-sub-agent"

    await tool.execute(task="Analyze revenue data for Q3")

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    sub_events = [e for e in events if isinstance(e, SubAgentEvent)]
    assert sub_events
    assert all(e.title == e.task_preview for e in sub_events)


async def test_sub_agent_inherits_parent_system_prompt_constraints():
    """Child system prompt includes finalized parent instructions automatically."""
    captured_messages = None

    async def fake_stream(*, messages, tools, **kwargs):
        nonlocal captured_messages
        captured_messages = messages
        yield StreamEvent(type="text", delta="done")
        yield StreamEvent(type="finish", finish_reason="stop", tool_calls=None)

    llm = AsyncMock()
    llm.generate_stream = fake_stream

    parent_prompt = "Parent constraint: write drafts under draft-a/ only."
    tool = SubAgentTool(llm=llm, parent_tools={})
    tool.set_parent_system_prompt(parent_prompt)

    result = await tool.execute(task="Draft one isolated section")

    assert result.success is True
    assert captured_messages is not None
    child_system_prompt = captured_messages[0].content
    assert "Inherited parent system prompt" in child_system_prompt
    assert parent_prompt in child_system_prompt
    assert "Do not overwrite shared files or final deliverables" in child_system_prompt


def test_agent_wires_system_prompt_into_sub_agent(tmp_path):
    """Agent initialization attaches its finalized system prompt to SubAgentTool."""
    llm = AsyncMock()
    tool = SubAgentTool(llm=llm, parent_tools={})

    Agent(
        llm_client=llm,
        system_prompt="Parent constraint: keep output generic.",
        tools=[tool],
        workspace_dir=str(tmp_path),
    )

    assert tool._parent_system_prompt is not None
    assert "Parent constraint: keep output generic." in tool._parent_system_prompt
    assert "Current Workspace" in tool._parent_system_prompt


def test_sub_agent_prompt_replaces_parent_only_mcp_search_guidance(tmp_path):
    llm = AsyncMock()
    tool = SubAgentTool(llm=llm, parent_tools={})

    agent = Agent(
        llm_client=llm,
        system_prompt="Parent constraint.",
        tools=[tool],
        workspace_dir=str(tmp_path),
        deferred_mcp_loading_enabled=True,
    )

    assert tool._parent_system_prompt is not None
    assert "Use `tool_search`" not in tool._parent_system_prompt
    assert "The parent agent owns deferred MCP discovery" in tool._parent_system_prompt
    assert "tool_search" not in agent._inherited_tools()


async def test_sub_agent_read_ledger_is_local_to_child_context(tmp_path):
    from box_agent.schema import FunctionCall, ToolCall

    path = tmp_path / "reference.md"
    path.write_text("CHILD_EXACT_BODY\n", encoding="utf-8")
    captured_requests: list[list[Message]] = []
    call_count = 0

    async def fake_stream(*, messages, tools, **kwargs):
        nonlocal call_count
        call_count += 1
        captured_requests.append([message.model_copy(deep=True) for message in messages])
        if call_count == 1:
            yield StreamEvent(
                type="finish",
                finish_reason="tool_use",
                tool_calls=[
                    ToolCall(
                        id="child-read",
                        type="function",
                        function=FunctionCall(
                            name="read_file",
                            arguments={"path": "reference.md"},
                        ),
                    )
                ],
            )
            return
        yield StreamEvent(type="text", delta="done")
        yield StreamEvent(type="finish", finish_reason="stop")

    llm = AsyncMock()
    llm.generate_stream = fake_stream
    read_tool = ReadTool(workspace_dir=str(tmp_path))
    sub_agent = SubAgentTool(
        llm=llm,
        parent_tools={"read_file": read_tool},
        workspace_dir=str(tmp_path),
    )
    parent = Agent(
        llm_client=llm,
        system_prompt="parent",
        tools=[read_tool, sub_agent],
        workspace_dir=str(tmp_path),
    )
    parent_read = await read_tool.execute(path="reference.md")
    descriptor = ResourceDescriptor.from_raw_output(parent_read.raw_output)
    assert descriptor is not None
    parent.context_resource_ledger.register_full_source(
        "parent-read",
        descriptor,
        parent_read.content,
    )

    result = await sub_agent.execute(task="Read the reference")

    assert result.success is True
    child_tool_message = next(
        message for message in captured_requests[1] if message.role == "tool"
    )
    assert "CHILD_EXACT_BODY" in child_tool_message.content
    assert "Resource already available" not in child_tool_message.content
    assert parent.context_resource_ledger.source_ids == ("parent-read",)


async def test_invoke_defaults_to_all_live_parent_tools(tmp_path):
    captured = {}

    async def fake_stream(*, messages, tools, **kwargs):
        captured["messages"] = messages
        captured["tools"] = tools
        yield StreamEvent(type="text", delta="done")
        yield StreamEvent(
            type="finish",
            finish_reason="stop",
            tool_calls=None,
            usage=TokenUsage(prompt_tokens=11, completion_tokens=2, total_tokens=13),
        )

    llm = AsyncMock()
    llm.generate_stream = fake_stream
    read_file = ReadTool(workspace_dir=str(tmp_path))
    tool = SubAgentTool(
        llm=llm,
        parent_tools={"read_file": read_file, "web_search": WebSearchTool()},
        workspace_dir=str(tmp_path),
    )
    tool.set_parent_system_prompt("SECRET_PARENT_PROMPT with the full capability catalog")

    result = await tool.invoke({"task": "Inspect the local inputs"})

    assert result.success is True
    assert [candidate.name for candidate in captured["tools"]] == [
        "read_file",
        "web_search",
    ]
    system_prompt = captured["messages"][0].content
    assert "Immutable rules" in system_prompt
    assert "SECRET_PARENT_PROMPT" in system_prompt
    assert "Inherited parent system prompt" in system_prompt
    assert "legacy_general" not in result.raw_output
    assert result.raw_output["capability_source"] == "parent"
    assert "strategy" not in result.raw_output
    assert result.raw_output["requested_tools"] == ["read_file", "web_search"]
    assert result.raw_output["resolved_tools"] == ["read_file", "web_search"]
    assert result.raw_output["defaults_applied"] == [
        "budget.max_steps",
        "budget.max_tool_calls",
        "required_tools",
        "skills",
        "title",
    ]
    assert "## Structured inputs" not in captured["messages"][-1].content
    assert result.raw_output["model_calls"] == 1
    assert result.raw_output["usage"] == {
        "input_tokens": 11,
        "output_tokens": 2,
        "total_tokens": 13,
    }


async def test_explicit_required_tools_limits_child_to_that_subset(tmp_path):
    captured_tools = None

    async def fake_stream(*, messages, tools, **kwargs):
        nonlocal captured_tools
        captured_tools = tools
        yield StreamEvent(type="text", delta="done")
        yield StreamEvent(type="finish", finish_reason="stop", tool_calls=None)

    llm = AsyncMock()
    llm.generate_stream = fake_stream
    tool = SubAgentTool(
        llm=llm,
        parent_tools={
            "read_file": ReadTool(workspace_dir=str(tmp_path)),
            "web_search": WebSearchTool(),
        },
    )

    result = await tool.invoke(
        {"task": "Inspect", "required_tools": ["read_file"]}
    )

    assert result.success is True
    assert [candidate.name for candidate in captured_tools] == ["read_file"]
    assert result.raw_output["requested_tools"] == ["read_file"]
    assert result.raw_output["resolved_tools"] == ["read_file"]


async def test_explicit_empty_required_tools_runs_without_tools():
    captured_tools = None

    async def fake_stream(*, messages, tools, **kwargs):
        nonlocal captured_tools
        captured_tools = tools
        yield StreamEvent(type="text", delta="done")
        yield StreamEvent(type="finish", finish_reason="stop", tool_calls=None)

    llm = AsyncMock()
    llm.generate_stream = fake_stream
    tool = SubAgentTool(llm=llm, parent_tools={"dummy": DummyTool()})

    result = await tool.invoke({"task": "Reason only", "required_tools": []})

    assert result.success is True
    assert captured_tools == []
    assert result.raw_output["requested_tools"] == []
    assert result.raw_output["resolved_tools"] == []


async def test_missing_required_tool_fails_before_child_model_starts():
    llm = AsyncMock()
    tool = SubAgentTool(llm=llm, parent_tools={"dummy": DummyTool()})

    result = await tool.invoke(
        {"task": "Inspect", "required_tools": ["missing_tool"]}
    )

    assert result.success is False
    assert result.raw_output["code"] == "REQUIRED_TOOL_NOT_FOUND"
    assert result.raw_output["tool"] == "missing_tool"
    assert result.raw_output["requested_tools"] == ["missing_tool"]
    assert result.raw_output["resolved_tools"] == []
    llm.generate_stream.assert_not_called()


async def test_invalid_budget_string_returns_object_correction_example():
    llm = AsyncMock()
    tool = SubAgentTool(llm=llm, parent_tools={})

    result = await tool.execute(
        task="Research one dimension",
        budget='{"max_steps": 12, "max_tool_calls": 25}',
    )

    assert result.success is False
    assert result.raw_output["code"] == "INVALID_DELEGATION_SPEC"
    assert result.raw_output["invalid_fields"] == ["budget"]
    assert result.raw_output["field_corrections"]["budget"] == {
        "message": "Pass budget as a JSON object, never as a JSON string.",
        "example": {"max_steps": 12, "max_tool_calls": 25},
    }
    llm.generate_stream.assert_not_called()


async def test_removed_execution_and_constraints_fields_are_rejected_before_llm(tmp_path):
    llm = AsyncMock()
    tool = SubAgentTool(
        llm=llm,
        parent_tools={"write_file": WriteTool(workspace_dir=str(tmp_path))},
        workspace_dir=str(tmp_path),
    )

    for field, value in (
        ("execution", {"strategy": "general_loop"}),
        ("constraints", {"read_only": False}),
        ("capabilities", {"skills": []}),
        ("inputs", {"files": []}),
    ):
        result = await tool.invoke(
            {"task": "Write one research dimension", field: value}
        )
        assert result.success is False
        assert result.raw_output["code"] == "INVALID_DELEGATION_SPEC"
        assert result.raw_output["invalid_fields"] == [field]
    llm.generate_stream.assert_not_called()


async def test_explicit_null_skills_is_rejected_by_schema():
    llm = AsyncMock()
    tool = SubAgentTool(llm=llm, parent_tools={})

    result = await tool.invoke({"task": "Inspect", "skills": None})

    assert result.success is False
    assert result.raw_output["code"] == "INVALID_DELEGATION_SPEC"
    assert "skills" in result.raw_output["invalid_fields"]
    llm.generate_stream.assert_not_called()


def test_sub_agent_schema_rejects_unknown_top_level_fields():
    tool = SubAgentTool(llm=AsyncMock(), parent_tools={})

    assert tool.parameters["additionalProperties"] is False


async def test_unknown_top_level_fields_return_structured_failure():
    llm = AsyncMock()
    tool = SubAgentTool(llm=llm, parent_tools={})

    result = await tool.execute(
        task="Inspect",
        required_tools='["file", "terminal"]',
        capabilities="",
    )

    assert result.success is False
    assert result.raw_output["code"] == "INVALID_DELEGATION_SPEC"
    assert result.raw_output["invalid_fields"] == ["capabilities", "required_tools"]
    assert "Traceback" not in (result.error or "")
    llm.generate.assert_not_called()
    llm.generate_stream.assert_not_called()


async def test_event_context_missing_task_returns_structured_failure():
    llm = AsyncMock()
    tool = SubAgentTool(llm=llm, parent_tools={})

    result = await tool.execute_with_event_context(
        event_queue=asyncio.Queue(),
        parent_tool_call_id="parent-call",
    )

    assert result.success is False
    assert result.raw_output["code"] == "INVALID_DELEGATION_SPEC"
    assert result.raw_output["invalid_fields"] == ["task"]
    assert "Traceback" not in (result.error or "")
    llm.generate.assert_not_called()
    llm.generate_stream.assert_not_called()


async def test_selected_skills_are_loaded_into_new_prompt_only(tmp_path):
    skill_dir = tmp_path / "review-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        """---
name: review-skill
description: Review local material
allowed-tools: [read_file]
---

Follow the REVIEW-SKILL-CONTENT rubric.
""",
        encoding="utf-8",
    )
    loader = SkillLoader(tmp_path)
    loader.discover_skills()
    captured_messages = None

    async def fake_stream(*, messages, tools, **kwargs):
        nonlocal captured_messages
        captured_messages = messages
        yield StreamEvent(type="text", delta="done")
        yield StreamEvent(type="finish", finish_reason="stop", tool_calls=None)

    llm = AsyncMock()
    llm.generate_stream = fake_stream
    tool = SubAgentTool(
        llm=llm,
        parent_tools={"read_file": ReadTool(workspace_dir=str(tmp_path))},
        workspace_dir=str(tmp_path),
    )
    tool.set_skill_provider(lambda: loader)

    result = await tool.execute(
        task="Review the material",
        skills=["review-skill"],
    )

    assert result.success is True
    assert "REVIEW-SKILL-CONTENT" in captured_messages[0].content
    assert result.raw_output["resolved_skills"] == ["review-skill"]


async def test_web_search_tool_emits_reference_event():
    """web_search tool results should surface refs as a structured event."""
    from box_agent.core import run_agent_loop
    from box_agent.schema import FunctionCall, ToolCall

    call_num = 0

    async def fake_stream(*, messages, tools, **kwargs):
        nonlocal call_num
        call_num += 1
        if call_num == 1:
            yield StreamEvent(
                type="finish",
                finish_reason="tool_use",
                tool_calls=[
                    ToolCall(
                        id="search-1",
                        type="function",
                        function=FunctionCall(name="web_search", arguments={"query": "example"}),
                    )
                ],
            )
        else:
            yield StreamEvent(type="text", delta="summary [ref_1]")
            yield StreamEvent(type="finish", finish_reason="stop", tool_calls=None)

    llm = AsyncMock()
    llm.generate_stream = fake_stream

    events = []
    async for event in run_agent_loop(
        llm=llm,
        messages=[Message(role="user", content="search")],
        tools={"web_search": WebSearchTool()},
        max_steps=3,
    ):
        events.append(event)

    web_events = [event for event in events if isinstance(event, WebSearchEvent)]
    assert len(web_events) == 1
    assert web_events[0].tool_call_id == "search-1"
    assert web_events[0].payload["refs"][0]["reference_tag"] == "ref_1"


async def test_sub_agent_forwards_web_search_reference_event():
    """Sub-agent child web_search refs should be forwarded to the parent stream."""
    from box_agent.schema import FunctionCall, ToolCall

    call_num = 0

    async def fake_stream(*, messages, tools, **kwargs):
        nonlocal call_num
        call_num += 1
        if call_num == 1:
            yield StreamEvent(
                type="finish",
                finish_reason="tool_use",
                tool_calls=[
                    ToolCall(
                        id="child-search-1",
                        type="function",
                        function=FunctionCall(name="web_search", arguments={"query": "example"}),
                    )
                ],
            )
        else:
            yield StreamEvent(type="text", delta="child summary [ref_1]")
            yield StreamEvent(type="finish", finish_reason="stop", tool_calls=None)

    llm = AsyncMock()
    llm.generate_stream = fake_stream

    tool = SubAgentTool(llm=llm, parent_tools={"web_search": WebSearchTool()})

    queue = asyncio.Queue()
    tool._event_queue = queue
    tool._parent_tool_call_id = "parent-sub-agent"

    result = await tool.execute(task="search in child")

    forwarded = []
    while not queue.empty():
        forwarded.append(queue.get_nowait())

    assert result.success is True
    web_events = [
        event
        for event in forwarded
        if isinstance(event, SubAgentEvent) and isinstance(event.event, WebSearchEvent)
    ]
    assert len(web_events) == 1
    assert web_events[0].parent_tool_call_id == "parent-sub-agent"
    assert web_events[0].sub_agent_id.startswith("subagent-")
    assert web_events[0].event.payload["refs"][0]["url"] == "https://example.com"


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

    async def boom(*, messages, tools, **kwargs):
        raise RuntimeError("API timeout")
        yield  # make it an async generator  # noqa: E501

    llm.generate_stream = boom
    tool = SubAgentTool(llm=llm, parent_tools={})
    result = await tool.execute(task="Try this")
    # run_agent_loop catches the exception and yields DoneEvent with error as final_content,
    # so SubAgentTool wraps it as a successful result containing the error text.
    # The error is humanized via classify_llm_error: "API timeout" classifies as a
    # timeout, so the friendly Chinese message is surfaced rather than the raw string.
    assert "超时" in result.content


async def test_max_steps_respected():
    """Sub-agent should stop after max_steps even if LLM keeps requesting tools."""
    from box_agent.config import ToolLimitsConfig
    from box_agent.schema import FunctionCall, ToolCall

    call_count = 0

    async def looping_stream(*, messages, tools, **kwargs):
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
        tool_limits=ToolLimitsConfig(
            sub_agent={"general_max_steps": 3, "general_max_tool_calls": 32}
        ),
    )
    result = await tool.execute(task="Loop forever")
    # Should have stopped — call_count should be capped at max_steps
    assert call_count <= 4  # max_steps=3 means 3 LLM calls


async def test_parent_guarded_tool_is_inherited_without_wrapping(tmp_path):
    from box_agent.schema import FunctionCall, ToolCall

    call_num = 0
    inherited_tools = None

    class ParentGuardedWriteTool(Tool):
        calls = 0

        @property
        def name(self):
            return "write_file"

        @property
        def description(self):
            return "parent guarded write"

        @property
        def parameters(self):
            return {"type": "object", "properties": {}}

        async def execute(self, **kwargs):
            self.calls += 1
            return ToolResult(success=False, error="PARENT_POLICY_DENIED")

    async def fake_stream(*, messages, tools, **kwargs):
        nonlocal call_num, inherited_tools
        call_num += 1
        inherited_tools = tools
        if call_num == 1:
            yield StreamEvent(
                type="finish",
                finish_reason="tool_use",
                tool_calls=[
                    ToolCall(
                        id="write-1",
                        type="function",
                        function=FunctionCall(
                            name="write_file",
                            arguments={"path": "../outside.txt", "content": "blocked"},
                        ),
                    )
                ],
            )
        else:
            yield StreamEvent(type="text", delta="write was denied")
            yield StreamEvent(type="finish", finish_reason="stop", tool_calls=None)

    llm = AsyncMock()
    llm.generate_stream = fake_stream
    write_tool = ParentGuardedWriteTool()
    tool = SubAgentTool(
        llm=llm,
        parent_tools={"write_file": write_tool},
        workspace_dir=str(tmp_path),
    )

    result = await tool.execute(task="Write only inside allowed")

    assert result.success is True
    assert inherited_tools == [write_tool]
    assert write_tool.calls == 1
    assert result.raw_output["tool_calls"] == 1


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

    async def fake_stream(*, messages, tools, **kwargs):
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


@pytest.mark.asyncio
async def test_parallel_sub_agent_progress_keeps_parent_tool_call_id():
    class TaskEchoLLM:
        async def generate(self, messages, tools=None):
            return LLMResponse(content=f"done {messages[-1].content}", finish_reason="stop")

        async def generate_stream(self, messages, tools=None, **kwargs):
            task = messages[-1].content
            await asyncio.sleep(0.02 if task == "A" else 0.01)
            yield StreamEvent(type="text", delta=f"done {task}")
            yield StreamEvent(type="finish", finish_reason="stop", tool_calls=None)

    tool = SubAgentTool(llm=TaskEchoLLM(), parent_tools={})
    queue: asyncio.Queue[SubAgentEvent] = asyncio.Queue()

    result_a, result_b = await asyncio.gather(
        tool.execute_with_event_context(
            event_queue=queue,
            parent_tool_call_id="parent-a",
            task="A",
            title="A",
        ),
        tool.execute_with_event_context(
            event_queue=queue,
            parent_tool_call_id="parent-b",
            task="B",
            title="B",
        ),
    )

    assert result_a.success is True
    assert result_b.success is True

    events: list[SubAgentEvent] = []
    while not queue.empty():
        events.append(queue.get_nowait())

    assert events
    assert {event.title for event in events} == {"A", "B"}
    assert {
        event.parent_tool_call_id
        for event in events
        if event.title == "A"
    } == {"parent-a"}
    assert {
        event.parent_tool_call_id
        for event in events
        if event.title == "B"
    } == {"parent-b"}


async def test_parallel_calls_each_inherit_the_same_parent_tool_boundary():
    observed = {}

    class IsolatedLLM:
        async def generate_stream(self, messages, tools=None, **kwargs):
            task_text = messages[-1].content
            key = "read" if "read task" in task_text else "web"
            observed[key] = [tool.name for tool in tools]
            await asyncio.sleep(0.01)
            yield StreamEvent(type="text", delta=f"done {key}")
            yield StreamEvent(type="finish", finish_reason="stop", tool_calls=None)

    tool = SubAgentTool(
        llm=IsolatedLLM(),
        parent_tools={
            "read_file": ReadTool(),
            "web_search": WebSearchTool(),
        },
    )
    result_read, result_web = await asyncio.gather(
        tool.execute(task="read task"),
        tool.execute(task="web task"),
    )

    assert result_read.success is True
    assert result_web.success is True
    assert observed == {
        "read": ["read_file", "web_search"],
        "web": ["read_file", "web_search"],
    }
    assert result_read.raw_output["resolved_tools"] == ["read_file", "web_search"]
    assert result_web.raw_output["resolved_tools"] == ["read_file", "web_search"]


def test_add_workspace_tools_uses_main_context_limit_for_sub_agent(tmp_path) -> None:
    """Main and child loops share the model-derived safe input limit."""
    from box_agent.config import (
        AgentConfig,
        LLMConfig,
        ToolLimitsConfig,
        ToolsConfig,
    )
    from box_agent.tools.setup import add_workspace_tools

    class Config:
        llm = LLMConfig(context_window=220_000, max_output_tokens=20_000)
        tool_limits = ToolLimitsConfig(
            sub_agent={"general_max_steps": 55, "no_progress_steps": 9}
        )
        agent = AgentConfig()
        tools = ToolsConfig(
            enable_bash=False,
            enable_file_tools=False,
            enable_todo=False,
            enable_sub_agent=True,
        )

    tools: list = []
    skill_loader = object()
    add_workspace_tools(
        tools,
        Config(),
        tmp_path,
        allow_full_access=False,
        llm=AsyncMock(),
        skill_loader=skill_loader,
        output=lambda *_: None,
    )

    sub_agent = next(t for t in tools if t.name == "sub_agent")
    assert sub_agent._token_limit == Config.llm.context_token_limit == 180_000
    assert sub_agent.parameters["properties"]["budget"]["default"]["max_steps"] == 55
    assert sub_agent._no_progress_limit == 9
    assert sub_agent._resolve_skill_loader() is skill_loader

"""Test cases for SubAgentTool."""

from __future__ import annotations

import asyncio
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import tiktoken

from box_agent.events import DoneEvent, StopReason, SubAgentEvent, WebSearchEvent
from box_agent.context_resources import ResourceDescriptor
from box_agent.schema import LLMResponse, Message, StreamEvent, TokenUsage
from box_agent.agent import Agent
from box_agent.session_log import SessionLog
from box_agent.tools.base import Tool, ToolResult
from box_agent.tools.file_tools import ReadTool, WriteTool
from box_agent.tools.mcp_loader import MCPTool
from box_agent.tools.model_tool_context import (
    current_model_tool_context,
    reset_model_tool_context,
    set_model_tool_context,
)
from box_agent.tools.permissions import CapabilityPolicy, GrantStore, PermissionEngine
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


class FilesystemNegotiator:
    """Grant or reject a filesystem directory through the shared store."""

    def __init__(self, store: GrantStore, *, grant: bool = True):
        self._store = store
        self._grant = grant
        self.requests: list[dict] = []

    async def negotiate(self, permission_request: dict) -> bool:
        self.requests.append(permission_request)
        if not self._grant:
            return False
        target = Path(permission_request["path"]).expanduser().resolve()
        grant_dir = target if target.is_dir() else target.parent
        self._store.add_filesystem_dir_grant(grant_dir, "prompt")
        return True


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


def test_provider_stale_seconds_defaults_to_none():
    tool = SubAgentTool(llm=AsyncMock(), parent_tools={})
    assert tool._provider_stale_seconds is None


def test_provider_stale_seconds_is_stored_for_child_loop():
    tool = SubAgentTool(llm=AsyncMock(), parent_tools={}, provider_stale_seconds=350.0)
    assert tool._provider_stale_seconds == 350.0


def test_automatic_child_routing_selects_from_host_allowlist():
    class RoutingLLM:
        auto_model_candidates = (
            {
                "model": "model-general",
                "tags": ["general", "code"],
                "abilityLevel": 3,
            },
            {
                "model": "model-html",
                "tags": ["html", "frontend"],
                "abilityLevel": 3,
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
        strategy="general_loop",
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
        strategy="general_loop",
    )

    assert child_llm is llm
    assert diagnostic == {"mode": "inherit", "reason": "no_auto_model_pool"}


def test_each_automatic_child_creation_selects_its_own_model():
    class RoutingLLM:
        auto_model_candidates = (
            {
                "model": "model-light",
                "tags": ["general", "chat", "rewrite", "summary", "fast"],
                "abilityLevel": 1,
            },
            {
                "model": "model-standard",
                "tags": ["office"],
                "abilityLevel": 2,
            },
            {
                "model": "model-advanced",
                "tags": ["analysis", "code", "debug", "reasoning", "vision"],
                "abilityLevel": 3,
            },
        )

        def __init__(self):
            self.bound = []

        def for_model(self, model, *, max_output_tokens=None):
            self.bound.append(model)
            return f"bound:{model}"

    llm = RoutingLLM()
    tool = SubAgentTool(llm=llm, parent_tools={})

    summary_llm, _ = tool._resolve_task_llm(
        task="总结这段文字",
        strategy="general_loop",
    )
    code_llm, _ = tool._resolve_task_llm(
        task="开发并调试这段代码",
        strategy="general_loop",
    )
    rewrite_llm, _ = tool._resolve_task_llm(
        task="润色这句话",
        strategy="general_loop",
    )

    assert summary_llm == "bound:model-light"
    assert code_llm == "bound:model-advanced"
    assert rewrite_llm == "bound:model-light"
    assert llm.bound == ["model-light", "model-advanced", "model-light"]


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
    tool = SubAgentTool(
        llm=llm,
        parent_tools={"read_file": ReadTool()},
    )

    inherited = await tool.execute(task="Use safe defaults")
    explicit = await tool.execute(
        task="Use one tool",
        required_tools=["read_file"],
    )

    assert inherited.success is True
    assert explicit.success is True
    assert routed_tools == [(), ("read_file",)]


def test_default_parallel_safe_is_false():
    """Other tools should have parallel_safe == False by default."""
    dummy = DummyTool()
    assert dummy.parallel_safe is False


def test_schema_exposes_flat_contract_with_safe_defaults():
    llm = AsyncMock()
    tool = SubAgentTool(
        llm=llm,
        parent_tools={
            "read_file": ReadTool(),
            "write_file": WriteTool(),
            "web_search": WebSearchTool(),
        },
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
        "files",
        "write_scope",
        "budget",
    }
    assert input_schema["required"] == ["task"]
    assert properties["required_tools"]["default"] == ["read_file"]
    assert properties["skills"]["default"] == []

    openai_schema = tool.to_openai_schema()
    assert openai_schema["function"]["name"] == "sub_agent"


def test_description_explains_flat_contract_and_derived_policy():
    llm = AsyncMock()
    tool = SubAgentTool(llm=llm, parent_tools={})
    description = tool.description

    assert "independent context, parallel latency, or evidence isolation" in description
    assert "trusted local read tools" in description
    assert "fail-closed runtime policy" in description
    assert "bounded completeness-checked batch fast path" in description
    assert "parent remains responsible" in description
    assert "final deliverables" in description
    assert "exact `write_scope`" in description
    assert "Pass `budget` as an object" in description

    parameters = tool.parameters["properties"]
    assert "Never pass a serialized JSON string" in parameters["budget"]["description"]
    assert "disjoint scopes" in parameters["write_scope"]["description"]
    assert "general agent loop" in parameters["files"]["description"]


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


def test_resolve_child_tools_falls_back_to_snapshot():
    """Without a provider (or if it fails), fall back to the snapshot."""
    llm = AsyncMock()
    tool = SubAgentTool(llm=llm, parent_tools={"dummy": DummyTool()})
    assert "dummy" in tool._resolve_child_tools()  # no provider → snapshot

    tool.set_tool_provider(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert "dummy" in tool._resolve_child_tools()  # provider raised → snapshot


def test_resolve_child_tools_honors_empty_live_boundary():
    tool = SubAgentTool(llm=AsyncMock(), parent_tools={"dummy": DummyTool()})
    tool.set_tool_provider(lambda: {})

    assert tool._resolve_child_tools() == {}


# ── Execution ────────────────────────────────────────────────


async def test_basic_execution():
    """Sub-agent returns the LLM's final text as ToolResult content."""
    llm = _make_llm(text="Analysis complete: revenue up 20%")
    tool = SubAgentTool(llm=llm, parent_tools={})
    result = await tool.execute(task="Analyze revenue data")
    assert result.success is True
    assert "revenue up 20%" in result.content


async def test_one_shot_child_has_independent_replayable_session_log(tmp_path):
    root = tmp_path / "sessions"
    parent_log = SessionLog.create(
        root,
        session_id="parent-session",
        cwd=tmp_path,
    )
    llm = _make_llm(text="child durable result")
    tool = SubAgentTool(llm=llm, parent_tools={}, workspace_dir=str(tmp_path))
    Agent(
        llm_client=llm,
        system_prompt="parent system",
        tools=[tool],
        workspace_dir=str(tmp_path),
        deferred_mcp_loading_enabled=False,
        session_log=parent_log,
    )

    result = await tool.execute(task="Inspect isolated evidence", title="Evidence")

    child_id = result.raw_output["child_session_id"]
    child = SessionLog.open(root, session_id=child_id, cwd=tmp_path)
    assert child.header["parentSession"] == "parent-session"
    assert child.header["origin"] == "subagent"
    assert child.header["delegationDepth"] == 1
    assert any(event["type"] == "subagent/descriptor" for event in child.events)
    assert [message.role for message in child.replay().messages] == [
        "user",
        "assistant",
    ]
    assert child.events[-1]["type"] == "turn/end"
    child.close()
    parent_log.close()


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


def test_sub_agent_does_not_inherit_parent_managed_skill_bodies():
    tool = SubAgentTool(llm=AsyncMock(), parent_tools={})
    parent_prompt = (
        "Stable parent safety constraint.\n\n"
        "## Auto-Loaded Skill Instructions\nAUTO_SKILL_BODY\n\n"
        "## Active Skill Instructions\nACTIVE_SKILL_BODY"
    )

    tool.set_parent_system_prompt(parent_prompt)

    assert tool._parent_system_prompt == "Stable parent safety constraint."


async def test_agent_run_wires_parent_permission_negotiator_into_sub_agent(tmp_path):
    llm = _make_llm()
    tool = SubAgentTool(llm=llm, parent_tools={})
    agent = Agent(
        llm_client=llm,
        system_prompt="parent",
        tools=[tool],
        workspace_dir=str(tmp_path),
        deferred_mcp_loading_enabled=False,
    )
    negotiator = AsyncMock()
    agent.set_permission_negotiator(negotiator)
    agent.messages.append(Message(role="user", content="finish"))

    _ = [event async for event in agent.run_events()]

    assert tool._permission_negotiator is negotiator


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

    result = await sub_agent.execute(
        task="Read the reference",
        required_tools=["read_file"],
    )

    assert result.success is True
    child_tool_message = next(
        message for message in captured_requests[1] if message.role == "tool"
    )
    assert "CHILD_EXACT_BODY" in child_tool_message.content
    assert "Resource already available" not in child_tool_message.content
    assert parent.context_resource_ledger.source_ids == ("parent-read",)


async def test_general_loop_uses_parent_permission_negotiator_and_retries(tmp_path):
    from box_agent.schema import FunctionCall, ToolCall

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    target = downloads / "inventory.txt"
    target.write_text("pdf\nimage\n", encoding="utf-8")

    store = GrantStore()
    engine = PermissionEngine(
        CapabilityPolicy(
            filesystem_scope="session_workspace",
            session_workspace_root=str(workspace),
        ),
        workspace,
        grant_store=store,
    )
    engine._home_dir = tmp_path.resolve()

    class CountingReadTool(ReadTool):
        def __init__(self):
            super().__init__(
                workspace_dir=str(workspace),
                permission_engine=engine,
            )
            self.calls = 0

        async def execute(self, path, offset=None, limit=None):
            self.calls += 1
            return await super().execute(path=path, offset=offset, limit=limit)

    call_count = 0

    async def fake_stream(*, messages, tools, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield StreamEvent(
                type="finish",
                finish_reason="tool_use",
                tool_calls=[
                    ToolCall(
                        id="child-read-downloads",
                        type="function",
                        function=FunctionCall(
                            name="read_file",
                            arguments={"path": str(target)},
                        ),
                    )
                ],
            )
            return
        tool_message = next(message for message in messages if message.role == "tool")
        assert "pdf" in tool_message.content
        yield StreamEvent(type="text", delta="inventory complete")
        yield StreamEvent(type="finish", finish_reason="stop")

    llm = AsyncMock()
    llm.generate_stream = fake_stream
    read_tool = CountingReadTool()
    negotiator = FilesystemNegotiator(store)
    sub_agent = SubAgentTool(
        llm=llm,
        parent_tools={"read_file": read_tool},
        workspace_dir=str(workspace),
    )
    sub_agent.set_permission_negotiator(negotiator)

    result = await sub_agent.execute(
        task="Inventory the Downloads file",
        required_tools=["read_file"],
    )

    assert result.success is True
    assert result.content == "inventory complete"
    assert read_tool.calls == 2
    assert len(negotiator.requests) == 1


async def test_explicit_bash_with_write_scope_uses_parent_permission_boundary(
    tmp_path,
):
    from box_agent.schema import FunctionCall, ToolCall

    class BashTool(Tool):
        def __init__(self):
            self.calls = 0

        @property
        def name(self) -> str:
            return "bash"

        @property
        def description(self) -> str:
            return "Run a command"

        @property
        def parameters(self) -> dict:
            return {
                "type": "object",
                "properties": {"command": {"type": "string"}},
            }

        async def execute(self, command: str) -> ToolResult:
            self.calls += 1
            return ToolResult(success=True, content=command)

    class ApprovingNegotiator:
        def __init__(self):
            self.requests = []

        async def negotiate(self, permission_request: dict) -> bool:
            self.requests.append(permission_request)
            return True

    call_count = 0

    async def fake_stream(*, messages, tools, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield StreamEvent(
                type="finish",
                finish_reason="tool_use",
                tool_calls=[
                    ToolCall(
                        id="child-bash",
                        type="function",
                        function=FunctionCall(
                            name="bash",
                            arguments={"command": "printf asset"},
                        ),
                    )
                ],
            )
            return
        tool_message = next(message for message in messages if message.role == "tool")
        assert "printf asset" in tool_message.content
        yield StreamEvent(type="text", delta="delegation ready")
        yield StreamEvent(type="finish", finish_reason="stop")

    llm = AsyncMock()
    llm.generate_stream = fake_stream
    bash_tool = BashTool()
    negotiator = ApprovingNegotiator()
    tool = SubAgentTool(
        llm=llm,
        parent_tools={
            "bash": bash_tool,
            "write_file": WriteTool(workspace_dir=str(tmp_path)),
        },
        workspace_dir=str(tmp_path),
    )
    tool.set_permission_negotiator(negotiator)

    result = await tool.execute(
        task="Download one asset and write it to the assigned path",
        required_tools=["bash", "write_file"],
        write_scope=["assets/hero.jpg"],
    )

    assert result.success is True
    assert result.raw_output["resolved_tools"] == ["bash", "write_file"]
    assert result.raw_output["constraints"]["write_scope"] == ["assets/hero.jpg"]
    assert bash_tool.calls == 1
    assert len(negotiator.requests) == 1
    assert negotiator.requests[0]["scope"] == "safety"
    assert negotiator.requests[0]["requested_scope"] == "delegated_bash_command"
    assert negotiator.requests[0]["persistent_supported"] is False


async def test_files_with_write_tools_stay_in_general_loop(tmp_path):
    captured = {}

    async def fake_stream(*, messages, tools, **kwargs):
        captured.setdefault(
            "initial_user_content",
            next(message.content for message in messages if message.role == "user"),
        )
        captured["tools"] = tools
        yield StreamEvent(type="text", delta="done")
        yield StreamEvent(type="finish", finish_reason="stop")

    llm = AsyncMock()
    llm.generate_stream = fake_stream
    tool = SubAgentTool(
        llm=llm,
        parent_tools={
            "read_file": ReadTool(workspace_dir=str(tmp_path)),
            "write_file": WriteTool(workspace_dir=str(tmp_path)),
        },
        workspace_dir=str(tmp_path),
    )

    result = await tool.execute(
        task="Read the plan and write one assigned output",
        files=["plan.md"],
        required_tools=["read_file", "write_file"],
        write_scope=["slides/slide_01.html"],
    )

    assert result.success is True
    assert result.raw_output["strategy"] == "general_loop"
    assert [candidate.name for candidate in captured["tools"]] == [
        "read_file",
        "write_file",
    ]
    assert "plan.md" in captured["initial_user_content"]


async def test_general_loop_uses_only_resolved_tools_and_inherits_parent_prompt(tmp_path):
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

    result = await tool.execute(
        task="Inspect the local inputs",
        required_tools=["read_file"],
    )

    assert result.success is True
    assert [candidate.name for candidate in captured["tools"]] == ["read_file"]
    system_prompt = captured["messages"][0].content
    assert "Immutable rules" in system_prompt
    assert "SECRET_PARENT_PROMPT" in system_prompt
    assert "Inherited parent system prompt" in system_prompt
    assert result.raw_output["resolved_tools"] == ["read_file"]
    assert result.raw_output["model_calls"] == 1
    assert result.raw_output["usage"] == {
        "input_tokens": 11,
        "output_tokens": 2,
        "total_tokens": 13,
    }


async def test_omitted_required_tools_exposes_only_trusted_local_readers(tmp_path):
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
            "write_file": WriteTool(workspace_dir=str(tmp_path)),
            "web_search": WebSearchTool(),
        },
        workspace_dir=str(tmp_path),
    )

    result = await tool.execute(task="Inspect safely")

    assert result.success is True
    assert [candidate.name for candidate in captured_tools] == ["read_file"]
    assert result.raw_output["requested_tools"] == ["read_file"]
    assert "required_tools" in result.raw_output["defaults_applied"]


async def test_removed_nested_fields_fail_without_calling_llm():
    llm = AsyncMock()
    tool = SubAgentTool(llm=llm, parent_tools={})

    for field, value in (
        ("execution", {"strategy": "general_loop"}),
        ("capabilities", {"required_tools": ["read_file"]}),
        ("inputs", {"files": []}),
        ("constraints", {"read_only": True}),
    ):
        result = await tool.execute(task="Inspect", **{field: value})

        assert result.success is False
        assert result.raw_output["code"] == "INVALID_DELEGATION_SPEC"
        assert result.raw_output["retryable"] is True
        assert result.raw_output["invalid_fields"] == [field]
        assert "minimal_valid_example" in result.raw_output
        assert result.raw_output["retry_limit"] == 1
    llm.generate.assert_not_called()
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


async def test_write_without_scope_returns_flat_correction(tmp_path):
    llm = AsyncMock()
    tool = SubAgentTool(
        llm=llm,
        parent_tools={"write_file": WriteTool(workspace_dir=str(tmp_path))},
        workspace_dir=str(tmp_path),
    )

    result = await tool.execute(
        task="Write one research dimension",
        required_tools=["write_file"],
    )

    assert result.success is False
    assert result.raw_output["code"] == "INVALID_DELEGATION_SPEC"
    assert result.raw_output["invalid_fields"] == ["write_scope"]
    assert result.raw_output["field_corrections"]["write_scope"]["example"] == [
        "research/dim01.md"
    ]
    llm.generate_stream.assert_not_called()


async def test_explicit_null_capabilities_is_invalid_not_legacy():
    llm = AsyncMock()
    tool = SubAgentTool(llm=llm, parent_tools={})

    result = await tool.execute(task="Inspect", capabilities=None)

    assert result.success is False
    assert result.raw_output["code"] == "INVALID_DELEGATION_SPEC"
    assert "capabilities" in result.raw_output["invalid_fields"]
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
        required_tools=["read_file"],
        skills=["review-skill"],
    )

    assert result.success is True
    assert "REVIEW-SKILL-CONTENT" in captured_messages[0].content
    assert result.raw_output["resolved_skills"] == ["review-skill"]


async def test_capability_state_provider_drives_not_ready_error():
    llm = AsyncMock()
    tool = SubAgentTool(llm=llm, parent_tools={})
    tool.set_capability_state_provider(lambda: "loading")

    result = await tool.execute(
        task="Use the future MCP tool",
        required_tools=["mcp_future_tool"],
    )

    assert result.success is False
    assert result.raw_output["code"] == "REQUIRED_TOOL_NOT_READY"
    assert result.raw_output["pending_source"] == "mcp"
    assert result.raw_output["requested_tools"] == ["mcp_future_tool"]
    assert result.raw_output["resolved_tools"] == []
    assert result.raw_output["denied_tools"][0]["name"] == "mcp_future_tool"
    assert result.raw_output["model_calls"] == 0
    llm.generate_stream.assert_not_called()


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

    result = await tool.execute(
        task="search in child",
        required_tools=["web_search"],
    )

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


async def test_sub_agent_web_extract_uses_child_model_context(monkeypatch):
    """Child web extraction must not inherit the surrounding parent model."""
    from box_agent.schema import FunctionCall, ToolCall

    class FakeSession:
        arguments = None

        async def call_tool(self, name, arguments):
            assert name == "web_extract"
            self.arguments = arguments
            return SimpleNamespace(
                content=[SimpleNamespace(text="page content")],
                isError=False,
            )

    class ChildLLM:
        model = "child-model"
        max_output_tokens = 222

        def __init__(self):
            self.calls = 0

        async def generate_stream(self, *, messages, tools, **kwargs):
            self.calls += 1
            if self.calls == 1:
                yield StreamEvent(
                    type="finish",
                    finish_reason="tool_use",
                    tool_calls=[
                        ToolCall(
                            id="child-extract-1",
                            type="function",
                            function=FunctionCall(
                                name="web_extract",
                                arguments={"url": "https://example.com"},
                            ),
                        )
                    ],
                )
                return
            yield StreamEvent(type="text", delta="child summary")
            yield StreamEvent(type="finish", finish_reason="stop", tool_calls=None)

    session = FakeSession()
    web_extract = MCPTool(
        name="web_extract",
        description="extract",
        parameters={
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
        session=session,
        server_name="box-agent-web-extract",
    )
    child_llm = ChildLLM()
    tool = SubAgentTool(
        llm=AsyncMock(),
        parent_tools={"web_extract": web_extract},
    )
    monkeypatch.setattr(
        tool,
        "_resolve_task_llm",
        lambda **kwargs: (child_llm, {"mode": "test"}),
    )

    parent_token = set_model_tool_context(
        model="parent-model",
        max_output_tokens=111,
    )
    try:
        result = await tool.execute(
            task="Extract and summarize the page",
            required_tools=["web_extract"],
        )
        restored_context = current_model_tool_context()
        assert restored_context is not None
        assert restored_context.model == "parent-model"
        assert restored_context.max_output_tokens == 111
    finally:
        reset_model_tool_context(parent_token)

    assert result.success is True
    assert session.arguments == {
        "url": "https://example.com",
        "model": "child-model",
        "max_output_tokens": 222,
    }


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

    class ReadDummy(DummyTool):
        @property
        def name(self) -> str:
            return "read_file"

    dummy = ReadDummy()
    tool = SubAgentTool(
        llm=llm,
        parent_tools={"read_file": dummy},
    )
    result = await tool.execute(
        task="Loop forever",
        required_tools=["read_file"],
        budget={"max_steps": 3, "max_tool_calls": 3},
    )
    # Should have stopped — call_count should be capped at max_steps
    assert call_count <= 4  # max_steps=3 means 3 LLM calls


async def test_batch_files_reads_twenty_files_once_and_calls_generate_once(tmp_path):
    paths = []
    for index in range(20):
        path = tmp_path / f"project-{index:02d}.md"
        path.write_text(f"# Project {index}\nScore material {index}\n", encoding="utf-8")
        paths.append(path.name)

    class CountingReadTool(ReadTool):
        def __init__(self):
            super().__init__(workspace_dir=str(tmp_path))
            self.calls = []

        async def execute(self, path, offset=None, limit=None):
            self.calls.append(path)
            return await super().execute(path=path, offset=offset, limit=limit)

    class BatchLLM:
        def __init__(self):
            self.generate_calls = 0
            self.stream_calls = 0
            self.messages = None
            self.tools = "unset"
            self.generate_kwargs = None

        async def generate(self, messages, tools=None, **kwargs):
            self.generate_calls += 1
            self.messages = messages
            self.tools = tools
            self.generate_kwargs = kwargs
            encoding = tiktoken.get_encoding("cl100k_base")
            prompt_tokens = sum(
                len(encoding.encode(str(message.content))) for message in messages
            )
            return LLMResponse(
                content="ranked all 20 projects",
                finish_reason="stop",
                usage=TokenUsage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=100,
                    total_tokens=prompt_tokens + 100,
                ),
            )

        async def generate_stream(self, messages, tools=None, **kwargs):
            self.stream_calls += 1
            raise AssertionError("batch_files must not enter run_agent_loop")
            yield

    llm = BatchLLM()
    read_tool = CountingReadTool()
    tool = SubAgentTool(
        llm=llm,
        parent_tools={"read_file": read_tool},
        workspace_dir=str(tmp_path),
    )

    started = perf_counter()
    result = await tool.execute(
        task="Compare every project and rank them",
        files=list(reversed(paths)),
    )
    elapsed = perf_counter() - started

    assert result.success is True
    assert result.content == "ranked all 20 projects"
    assert sorted(read_tool.calls) == sorted(paths)
    assert len(read_tool.calls) == 20
    assert llm.generate_calls == 1
    assert llm.stream_calls == 0
    assert llm.tools is None
    assert "session_id" not in llm.generate_kwargs
    assert llm.generate_kwargs["call_kind"] == "subagent_step"
    assert "<<<UNTRUSTED_FILE" in llm.messages[-1].content
    assert llm.messages[-1].content.count("<<<UNTRUSTED_FILE") == 20
    assert result.raw_output["model_calls"] == 1
    assert result.raw_output["tool_calls"] == 20
    assert result.raw_output["resolved_tools"] == ["read_file"]
    assert result.raw_output["usage"]["input_tokens"] <= int(3_353_714 * 0.10)
    assert elapsed < 60


async def test_batch_files_uses_parent_permission_negotiator_and_retries(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    target = downloads / "inventory.txt"
    target.write_text("pdf\nimage\n", encoding="utf-8")

    store = GrantStore()
    engine = PermissionEngine(
        CapabilityPolicy(
            filesystem_scope="session_workspace",
            session_workspace_root=str(workspace),
        ),
        workspace,
        grant_store=store,
    )
    engine._home_dir = tmp_path.resolve()

    class CountingReadTool(ReadTool):
        def __init__(self):
            super().__init__(
                workspace_dir=str(workspace),
                permission_engine=engine,
            )
            self.calls = 0

        async def execute(self, path, offset=None, limit=None):
            self.calls += 1
            return await super().execute(path=path, offset=offset, limit=limit)

    class BatchLLM:
        async def generate(self, messages, tools=None, **kwargs):
            assert "pdf" in messages[-1].content
            return LLMResponse(content="inventory complete", finish_reason="stop")

        async def generate_stream(self, messages, tools=None, **kwargs):
            raise AssertionError("batch_files must not enter run_agent_loop")
            yield

    read_tool = CountingReadTool()
    negotiator = FilesystemNegotiator(store)
    sub_agent = SubAgentTool(
        llm=BatchLLM(),
        parent_tools={"read_file": read_tool},
        workspace_dir=str(workspace),
    )
    sub_agent.set_permission_negotiator(negotiator)

    result = await sub_agent.execute(
        task="Inventory Downloads",
        files=[str(target)],
    )

    assert result.success is True
    assert result.content == "inventory complete"
    assert read_tool.calls == 2
    assert len(negotiator.requests) == 1


async def test_batch_files_keeps_permission_denial_fail_closed(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    target = downloads / "inventory.txt"
    target.write_text("private\n", encoding="utf-8")

    store = GrantStore()
    engine = PermissionEngine(
        CapabilityPolicy(
            filesystem_scope="session_workspace",
            session_workspace_root=str(workspace),
        ),
        workspace,
        grant_store=store,
    )
    engine._home_dir = tmp_path.resolve()
    read_tool = ReadTool(
        workspace_dir=str(workspace),
        permission_engine=engine,
    )
    llm = AsyncMock()
    sub_agent = SubAgentTool(
        llm=llm,
        parent_tools={"read_file": read_tool},
        workspace_dir=str(workspace),
    )
    negotiator = FilesystemNegotiator(store, grant=False)
    sub_agent.set_permission_negotiator(negotiator)

    result = await sub_agent.execute(
        task="Inventory Downloads",
        files=[str(target)],
    )

    assert result.success is False
    assert result.raw_output["code"] == "BATCH_FILES_PREFETCH_FAILED"
    assert len(negotiator.requests) == 1
    llm.generate.assert_not_called()


async def test_batch_files_prefetch_propagates_cancellation():
    started = asyncio.Event()

    class CancellableReadTool(Tool):
        @property
        def name(self):
            return "read_file"

        @property
        def description(self):
            return "test read"

        @property
        def parameters(self):
            return {"type": "object", "properties": {"path": {"type": "string"}}}

        async def execute(self, path):
            started.set()
            await asyncio.Event().wait()

    llm = AsyncMock()
    tool = SubAgentTool(
        llm=llm,
        parent_tools={"read_file": CancellableReadTool()},
    )

    execution = asyncio.create_task(
        tool.execute(
            task="Summarize",
            files=["one.md"],
        )
    )
    await started.wait()
    execution.cancel()

    with pytest.raises(asyncio.CancelledError):
        await execution
    llm.generate.assert_not_called()


async def test_batch_files_uses_configurable_synthesis_timeout():
    class CompleteReadTool(Tool):
        @property
        def name(self):
            return "read_file"

        @property
        def description(self):
            return "test read"

        @property
        def parameters(self):
            return {"type": "object", "properties": {"path": {"type": "string"}}}

        async def execute(self, path):
            return ToolResult(
                success=True,
                content="body",
                raw_output={
                    "source_char_count": 4,
                    "selected_char_count": 4,
                    "selected_line_count": 1,
                    "truncated": False,
                },
            )

    class HangingLLM:
        async def generate(self, **kwargs):
            await asyncio.Event().wait()

    tool = SubAgentTool(
        llm=HangingLLM(),
        parent_tools={"read_file": CompleteReadTool()},
        batch_synthesis_timeout_seconds=0.01,
    )

    result = await tool.execute(
        task="Summarize",
        files=["one.md"],
    )

    assert result.success is False
    assert result.raw_output["code"] == "BATCH_SYNTHESIS_TIMEOUT"
    assert result.raw_output["timeout_seconds"] == 0.01
    assert "configured 0.01 second runtime limit" in result.raw_output["message"]


@pytest.mark.parametrize(
    ("content", "raw_output", "expected_code"),
    [
        (
            "normal body",
            None,
            "READ_COMPLETENESS_UNVERIFIED",
        ),
        (
            "body ... [Content truncated: 40000 tokens -> ~32000 tokens limit] ...",
            None,
            "FILE_CONTENT_TRUNCATED",
        ),
        (
            "large body",
            {
                "source_char_count": 64_001,
                "selected_char_count": 64_001,
                "selected_line_count": 1,
                "truncated": False,
            },
            "FILE_TOO_LARGE",
        ),
        (
            "truncated body",
            {
                "source_char_count": 10,
                "selected_char_count": 10,
                "selected_line_count": 1,
                "truncated": True,
            },
            "FILE_CONTENT_TRUNCATED",
        ),
    ],
)
async def test_batch_files_rejects_unproven_or_incomplete_reads_before_model(
    content,
    raw_output,
    expected_code,
):
    class UnsafeReadTool(Tool):
        @property
        def name(self):
            return "read_file"

        @property
        def description(self):
            return "test read"

        @property
        def parameters(self):
            return {"type": "object", "properties": {"path": {"type": "string"}}}

        async def execute(self, path):
            return ToolResult(success=True, content=content, raw_output=raw_output)

    llm = AsyncMock()
    tool = SubAgentTool(llm=llm, parent_tools={"read_file": UnsafeReadTool()})

    result = await tool.execute(
        task="Summarize",
        files=["one.md"],
    )

    assert result.success is False
    assert result.raw_output["type"] == "sub_agent_delegation_error"
    assert result.raw_output["code"] == "BATCH_FILES_PREFETCH_FAILED"
    assert result.raw_output["failures"][0]["code"] == expected_code
    assert result.raw_output["model_calls"] == 0
    llm.generate.assert_not_called()
    llm.generate_stream.assert_not_called()


async def test_batch_files_rejects_aggregate_over_200k_before_model():
    class LargeCompleteReadTool(Tool):
        @property
        def name(self):
            return "read_file"

        @property
        def description(self):
            return "test read"

        @property
        def parameters(self):
            return {"type": "object", "properties": {"path": {"type": "string"}}}

        async def execute(self, path):
            content = "x" * 51_000
            return ToolResult(
                success=True,
                content=content,
                raw_output={
                    "source_char_count": len(content),
                    "selected_char_count": len(content),
                    "selected_line_count": 1,
                    "truncated": False,
                },
            )

    llm = AsyncMock()
    tool = SubAgentTool(
        llm=llm,
        parent_tools={"read_file": LargeCompleteReadTool()},
    )

    result = await tool.execute(
        task="Summarize all files",
        files=["a.md", "b.md", "c.md", "d.md"],
    )

    assert result.success is False
    assert result.raw_output["failures"] == [
        {
            "path": "*",
            "code": "AGGREGATE_CONTENT_TOO_LARGE",
            "source_char_count": 204_000,
            "limit": 200_000,
            "retryable": False,
        }
    ]
    assert result.raw_output["model_calls"] == 0
    llm.generate.assert_not_called()


async def test_write_scope_is_enforced_before_live_write_tool(tmp_path):
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
    write_tool = WriteTool(workspace_dir=str(tmp_path))
    tool = SubAgentTool(
        llm=llm,
        parent_tools={"write_file": write_tool},
        workspace_dir=str(tmp_path),
    )

    result = await tool.execute(
        task="Write only inside allowed",
        required_tools=["write_file"],
        write_scope=["allowed"],
    )

    assert result.success is True
    assert not (tmp_path.parent / "outside.txt").exists()
    assert result.raw_output["tool_calls"] == 1


async def test_write_scope_accepts_absolute_path_under_file_tool_workspace(tmp_path):
    from box_agent.schema import FunctionCall, ToolCall

    session_root = tmp_path / "session"
    output_root = session_root / "output"
    target = output_root / "ppt_decks" / "demo" / "slides" / "slide_01.html"
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
                        id="write-absolute",
                        type="function",
                        function=FunctionCall(
                            name="write_file",
                            arguments={"path": str(target), "content": "<html></html>"},
                        ),
                    )
                ],
            )
            return
        yield StreamEvent(type="text", delta="absolute write completed")
        yield StreamEvent(type="finish", finish_reason="stop", tool_calls=None)

    llm = AsyncMock()
    llm.generate_stream = fake_stream
    tool = SubAgentTool(
        llm=llm,
        parent_tools={
            "write_file": WriteTool(
                workspace_dir=str(session_root),
                relative_root_dir=str(output_root),
            )
        },
        workspace_dir=str(session_root),
    )

    result = await tool.execute(
        task="Write the assigned slide",
        required_tools=["write_file"],
        write_scope=["ppt_decks/demo/slides/slide_01.html"],
    )

    assert result.success is True
    assert target.read_text() == "<html></html>"


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


async def test_parallel_new_style_calls_do_not_leak_resolved_tools():
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
        tool.execute(
            task="read task",
            required_tools=["read_file"],
        ),
        tool.execute(
            task="web task",
            required_tools=["web_search"],
        ),
    )

    assert result_read.success is True
    assert result_web.success is True
    assert observed == {"read": ["read_file"], "web": ["web_search"]}
    assert result_read.raw_output["resolved_tools"] == ["read_file"]
    assert result_web.raw_output["resolved_tools"] == ["web_search"]


def test_add_workspace_tools_wires_sub_agent_token_limit(tmp_path) -> None:
    """Sub-agent config and live capability providers flow through setup."""
    from box_agent.config import AgentConfig, ToolLimitsConfig, ToolsConfig
    from box_agent.tools.setup import add_workspace_tools

    class Config:
        tool_limits = ToolLimitsConfig(
            sub_agent={"general_max_steps": 55, "no_progress_steps": 9}
        )
        agent = AgentConfig(
            sub_agent_token_limit=12345,
            sub_agent_batch_synthesis_timeout_seconds=234.5,
        )
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
        capability_state_provider=lambda: "loading",
        output=lambda *_: None,
    )

    sub_agent = next(t for t in tools if t.name == "sub_agent")
    assert sub_agent._token_limit == 12345
    assert sub_agent._tool_limits.sub_agent.general_max_steps == 55
    assert sub_agent._no_progress_limit == 9
    assert sub_agent._batch_synthesis_timeout_seconds == 234.5
    assert sub_agent._resolve_skill_loader() is skill_loader
    assert sub_agent._resolve_capability_state() == "loading"

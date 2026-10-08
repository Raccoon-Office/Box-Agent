"""Identical model proposals only share execution with a trusted opt-in."""

from types import SimpleNamespace

import pytest

from box_agent.core import run_agent_loop
from box_agent.events import ToolCallResult
from box_agent.schema import FunctionCall, Message, StreamEvent, ToolCall
from box_agent.tools.base import Tool, ToolResult
from box_agent.tools.engine.preparation import prepare_tools
from box_agent.tools.file_tools import AppendTool
from box_agent.tools.mcp_loader import MCPTool


class RepeatedCalls:
    def __init__(self, name, arguments, *, batches=1, mutate=None):
        self.name, self.arguments = name, arguments
        self.batches, self.mutate, self.requests = batches, mutate, 0

    async def generate_stream(self, **kwargs):
        self.requests += 1
        if self.requests <= self.batches:
            if self.mutate:
                self.mutate()
            yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[
                ToolCall(id=f"batch-{self.requests}-{index}", type="function",
                         function=FunctionCall(name=self.name, arguments=self.arguments))
                for index in range(2)
            ])
        else:
            yield StreamEvent(type="text", delta="Finished.")
            yield StreamEvent(type="finish", finish_reason="stop")


class RecordingTool(Tool):
    name = "record"
    description = "Record one invocation."
    parameters = {"type": "object", "properties": {"value": {"type": "string"}}}

    def __init__(self, *, fail_first=False):
        self.calls = 0
        self.fail_first = fail_first

    async def execute(self, value):
        self.calls += 1
        if self.fail_first and self.calls == 1:
            return ToolResult(success=False, error="first attempt failed")
        return ToolResult(success=True, content="recorded")


async def run(tool, *, arguments=None, batches=1, mutate=None, **options):
    messages = [Message(role="system", content="test"), Message(role="user", content="Execute requests.")]
    events = [event async for event in run_agent_loop(
        llm=RepeatedCalls(tool.name, arguments if arguments is not None else {"value": "same"},
                          batches=batches, mutate=mutate),
        tools={tool.name: tool}, messages=messages, max_steps=batches + 1, **options,
    )]
    results = [event for event in events if isinstance(event, ToolCallResult)]
    assert [m.tool_call_id for m in messages if m.role == "tool"] == [r.tool_call_id for r in results]
    return results


@pytest.mark.asyncio
@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.parametrize("opt_in", [False, True, "true"])
async def test_identical_calls_require_boolean_opt_in_to_share_execution(parallel, opt_in):
    tool = RecordingTool()
    tool.parallel_safe = parallel
    tool.deduplicate_within_batch = opt_in
    results = await run(tool)
    assert tool.calls == (1 if opt_in is True else 2)
    assert len(results) == 2
    assert all(r.success for r in results)
    assert [r.user_visible for r in results] == [True, opt_in is not True]


@pytest.mark.asyncio
async def test_identical_append_requests_both_change_the_file(tmp_path):
    tool = AppendTool(workspace_dir=str(tmp_path))
    results = await run(tool, arguments={"path": "record.txt", "content": "entry\n"},
                        workspace_dir=str(tmp_path))
    assert all(r.success for r in results)
    assert (tmp_path / "record.txt").read_text(encoding="utf-8") == "entry\nentry\n"


@pytest.mark.asyncio
async def test_failure_does_not_suppress_a_separate_default_call():
    tool = RecordingTool(fail_first=True)
    results = await run(tool)
    assert tool.calls == 2
    assert [r.success for r in results] == [False, True]
    assert all(r.user_visible for r in results)


@pytest.mark.asyncio
async def test_opted_in_failure_is_shared_only_within_its_batch():
    tool = RecordingTool(fail_first=True)
    tool.deduplicate_within_batch = True
    results = await run(tool, batches=2)
    assert tool.calls == 2
    assert [r.success for r in results] == [False, False, True, True]
    assert "already failed" in results[1].error


@pytest.mark.asyncio
@pytest.mark.parametrize("opt_in", [False, True])
async def test_direct_budget_charges_only_admitted_executions(opt_in):
    tool = RecordingTool()
    tool.deduplicate_within_batch = opt_in
    results = await run(tool, max_tool_calls=1)
    assert tool.calls == 1
    assert [r.success for r in results] == [True, opt_in]
    if not opt_in:
        assert "Total tool call budget reached" in results[1].error


@pytest.mark.asyncio
@pytest.mark.parametrize("initial,replacement", [(False, True), (True, False), (True, 1)])
async def test_changing_opt_in_after_request_rejects_both_calls(initial, replacement):
    tool = RecordingTool()
    tool.deduplicate_within_batch = initial
    results = await run(tool, mutate=lambda: setattr(tool, "deduplicate_within_batch", replacement))
    assert tool.calls == 0
    assert len(results) == 2
    assert all(not r.success and "changed after it was offered" in r.error for r in results)
    assert all("Duplicate tool call" not in (r.error or "") for r in results)


@pytest.mark.asyncio
async def test_mcp_parallel_and_read_only_annotations_do_not_enable_merging():
    class Session:
        calls = 0

        async def call_tool(self, *args, **kwargs):
            self.calls += 1
            return SimpleNamespace(isError=False, content=[SimpleNamespace(type="text", text="ok")])

    session = Session()
    tool = MCPTool("remote_read", "Read remote data", {"type": "object", "properties": {}},
                   session, server_name="fixture")
    tool.parallel_safe = True
    tool.annotations = {"readOnlyHint": True, "idempotentHint": True}
    results = await run(tool)
    assert session.calls == 2
    assert all(r.success and r.user_visible for r in results)


def test_opt_in_never_bypasses_mcp_generation_validation():
    tool = MCPTool("remote_read", "Read", {"type": "object", "properties": {}},
                   object(), server_name="fixture")
    tool.deduplicate_within_batch = True
    prepared = prepare_tools([tool])
    tool._mcp_generation += 1
    assert not prepared.allows_batch_deduplication(tool.name)

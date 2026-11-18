"""Verify time-based child handoff through the real parent and child loops."""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict

import pytest

from box_agent.core import run_agent_loop
from box_agent.events import ProgressEvent, SubAgentEvent, ToolCallResult
from box_agent.schema import FunctionCall, Message, StreamEvent, ToolCall
from box_agent.tools.engine import scheduler
from box_agent.tools.engine.engine import DefaultToolEngine
from box_agent.tools.file_tools import WriteTool
from box_agent.tools.mcp_tool_catalog import MCPToolCatalog
from box_agent.tools.mcp_tool_search import MCPToolExposureManager, ToolSearchTool
from box_agent.tools.sub_agent_tool import SubAgentTool


def invoke(call_id, name, **arguments):
    return ToolCall(
        id=call_id, type="function", function=FunctionCall(name=name, arguments=arguments),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ending, structured, stable_tools",
    [("wrapup", True, False), ("timeout", True, True), ("cancel", True, False), ("timeout", False, False)],
)
async def test_child_deadline_preserves_committed_work_and_only_resumes_pending_items(
    tmp_path, monkeypatch, ending, structured, stable_tools,
):
    now = 0.0
    stopped = False
    blocked = asyncio.Event()
    sibling_finished = asyncio.Event()
    unblock = asyncio.Event()
    wrapping_up = asyncio.Event()
    resumed_items = []
    child_attempt_closed = False
    initial_items = [
        {"id": "first", "task": "Produce the first file."},
        {"id": "second", "task": "Produce the second file."},
    ]
    monkeypatch.setattr(scheduler, "perf_counter", lambda: now)
    invoke_tool = scheduler.ToolEngine._invoke_with_optional_events

    async def observe_completion(self, request, *args, **kwargs):
        if request.call_id == "sibling":
            task = asyncio.current_task()
            assert task is not None
            task.add_done_callback(lambda _: sibling_finished.set())
        return await invoke_tool(self, request, *args, **kwargs)

    monkeypatch.setattr(scheduler.ToolEngine, "_invoke_with_optional_events", observe_completion)

    class ChildLLM:
        async def generate_stream(self, messages, tools=None, **kwargs):
            nonlocal child_attempt_closed
            task = next(msg.content for msg in messages if msg.role == "user")
            if "Independent sibling" in task:
                yield StreamEvent(type="text", delta="Sibling finished.")
            elif "Previously checkpointed results" in task:
                if not any(msg.tool_call_id == "resume-write" for msg in messages):
                    section = task.split("## Assigned work items", 1)[1]
                    items, _ = json.JSONDecoder().raw_decode(section[section.index("["):])
                    resumed_items.extend(items)
                    yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[
                        invoke(
                            "resume-write", "write_file", path="output/second.txt", content="second completed",
                        ),
                    ])
                    return
                yield StreamEvent(type="text", delta="Completed only the second item.")
            else:
                done = {msg.tool_call_id for msg in messages if msg.role == "tool"}
                if "write-first" not in done:
                    yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[
                        invoke("write-first", "write_file", path="output/first.txt", content="first retained"),
                    ])
                    return
                if structured and "save-first" not in done:
                    yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[
                        invoke(
                            "save-first", "sub_agent_checkpoint", item_id="first",
                            summary="First result is in output/first.txt.",
                        ),
                    ])
                    return
                blocked.set()
                try:
                    await unblock.wait()
                    yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[
                        invoke("write-second", "write_file", path="output/second.txt", content="late work"),
                    ])
                    return
                finally:
                    child_attempt_closed = True
            yield StreamEvent(type="finish", finish_reason="stop")

    class ParentLLM:
        calls = 0

        async def generate_stream(self, messages, tools=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[
                    invoke(
                        "slow", "sub_agent", task="Produce two files.",
                        required_tools=["write_file"], write_scope=["output"],
                        **({"work_items": initial_items} if structured else {}),
                    ),
                    invoke("sibling", "sub_agent", task="Independent sibling", required_tools=[]),
                ])
                return
            if self.calls == 2:
                assert ending != "cancel", "a cancelled turn cannot start another model step"
                result = next(msg.content for msg in messages if msg.tool_call_id == "slow")
                payload = json.loads(result.removeprefix("Error: "))
                assert any(file["path"] == "output/first.txt" for file in payload["artifacts"])
                if structured:
                    assert payload["remaining_item_ids"] == ["second"]
                    assert [item["id"] for item in payload["completed_items"]] == ["first"]
                    reference = payload["resume_from"]
                    yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[
                        invoke(
                            "widen-scope", "sub_agent", task="Continue pending items",
                            resume_from=reference, write_scope=["other"],
                        ),
                        invoke("continue", "sub_agent", task="Continue pending items", resume_from=reference),
                        invoke("duplicate", "sub_agent", task="Continue pending items", resume_from=reference),
                    ])
                    return
                assert payload["remaining_item_ids"] == []
                assert "resume_from" not in payload
            yield StreamEvent(type="text", delta="The work has been assembled.")
            yield StreamEvent(type="finish", finish_reason="stop")

    llm = ParentLLM()
    parent_tools = {"write_file": WriteTool(workspace_dir=str(tmp_path))}
    tool = SubAgentTool(
        llm=ChildLLM(), parent_tools=parent_tools,
        workspace_dir=str(tmp_path),
    )
    if stable_tools:
        catalog, active = MCPToolCatalog(), OrderedDict()
        manager = MCPToolExposureManager(catalog, active)
        manager.enable_stable_prefix(ToolSearchTool(catalog, active))
        DefaultToolEngine(tools=parent_tools, tool_exposure=manager).prepare_tools()
        tool.set_tool_exposure_provider(lambda: manager)

    async def collect():
        events = []
        async for event in run_agent_loop(
            llm=llm, tools={"sub_agent": tool},
            messages=[Message(role="user", content="Produce the assigned files.")],
            workspace_dir=str(tmp_path), parallel_tool_timeout_seconds=1800,
            is_cancelled=lambda: stopped, max_steps=5,
        ):
            events.append(event)
            if isinstance(event, SubAgentEvent) and isinstance(event.event, ProgressEvent):
                if "wrapping up" in event.event.content:
                    wrapping_up.set()
        return events

    task = asyncio.create_task(collect())
    try:
        await asyncio.wait_for(blocked.wait(), timeout=5)
        await asyncio.wait_for(sibling_finished.wait(), timeout=5)
        assert (tmp_path / "output" / "first.txt").read_text() == "first retained"
        now = 1500
        await asyncio.wait_for(wrapping_up.wait(), timeout=5)
        if ending == "wrapup":
            unblock.set()
        else:
            stopped = ending == "cancel"
            now = 1800
        events = await asyncio.wait_for(task, timeout=5)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    notices = [
        event for event in events
        if isinstance(event, SubAgentEvent) and isinstance(event.event, ProgressEvent)
        and "wrapping up" in event.event.content
    ]
    assert [event.parent_tool_call_id for event in notices] == ["slow"]
    assert child_attempt_closed
    second_file = tmp_path / "output" / "second.txt"
    results = {event.tool_call_id: event for event in events if isinstance(event, ToolCallResult)}
    assert results["sibling"].success
    assert not results["slow"].success
    if ending == "cancel":
        assert llm.calls == 1
        assert "cancelled" in results["slow"].error
        assert not resumed_items
        assert not second_file.exists()
        return
    assert results["slow"].raw_output["code"] == f"SUB_AGENT_{ending.upper()}"
    if not structured:
        assert not resumed_items
        assert not second_file.exists()
        return
    assert results["continue"].success
    assert results["widen-scope"].raw_output["code"] == "INVALID_DELEGATION_SPEC"
    assert results["duplicate"].raw_output["code"] == "SUB_AGENT_HANDOFF_UNAVAILABLE"
    assert resumed_items == [initial_items[1]]
    assert second_file.read_text() == "second completed"
    assert (tmp_path / "output" / "first.txt").read_text() == "first retained"

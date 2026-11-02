"""Output presentation failures must not prevent the next Agent model call."""

from __future__ import annotations

import asyncio
import json
import tracemalloc
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from box_agent.agent_run import AgentRunHandle
from box_agent.api import EventEnvelope
from box_agent.events import DoneEvent, ToolCallResult
from box_agent.kernel.tool_result_pipeline import ToolResultPipelineInput, process_tool_result
from box_agent.runtime import run_agent_loop
from box_agent.schema import FunctionCall, Message, StreamEvent, ToolCall
from box_agent.session_trace import SessionTraceWriter, reset_session_trace_writer, set_session_trace_writer
from box_agent.tool_result_storage import (
    MAX_INLINE_OUTPUT_BYTES, MAX_RAW_OUTPUT_BYTES, MAX_TOOL_RESULT_BYTES, ToolResultStorage,
)
from box_agent.tools.base import Tool, ToolResult
from box_agent.tools.file.read_tool import ReadTool
from box_agent.tools.plan_tool import PlanReadTool, PlanStore, PlanWriteTool
from box_agent.tools.todo_tool import TodoReadTool, TodoStore, TodoWriteTool
from box_agent.tools.output_capture import OUTPUT_REPLY_BYTES, OutputCapture


def _pipeline(tmp_path, result, **kwargs):
    return ToolResultPipelineInput(
        messages=[], tool_call_id="python-1", tool_name="execute_code", arguments={},
        result=result, visible_content=result.content, visible_error=result.error,
        result_storage=ToolResultStorage(tmp_path), session_id="session-1", **kwargs,
    )


@pytest.mark.parametrize("success", [False, True])
def test_oversized_results_are_bounded_for_model_event_and_trace(tmp_path, success):
    text = "HEAD\n" + "\\x00中文\n" * 500_000 + "TAIL\n"
    result = ToolResult(success=success, content=text, error=None if success else text)
    writer = SessionTraceWriter(session_id="s", acp_session_id="a", trace_dir=tmp_path / "traces", enabled=True)
    token = set_session_trace_writer(writer, turn_id="t")
    try:
        outcome = process_tool_result(_pipeline(tmp_path / "results", result))
    finally:
        reset_session_trace_writer(token)
    event = outcome.events[0]
    assert event.success is success
    assert len(event.content.encode("utf-8")) < OUTPUT_REPLY_BYTES
    assert len(outcome.model_content.encode("utf-8")) < OUTPUT_REPLY_BYTES
    assert "Full output saved to:" in outcome.model_content
    assert "HEAD" in outcome.model_content and "TAIL" in outcome.model_content
    encoded = json.dumps(EventEnvelope("r", "r:1", 1, event).to_dict(), ensure_ascii=False).encode("utf-8")
    assert len(encoded) < 4 * 1024 * 1024
    assert writer.file_path.stat().st_size < 1024 * 1024
    files = list((tmp_path / "results").rglob("*.txt"))
    assert files
    saved = [path.read_text(encoding="utf-8") for path in files]
    assert text in saved or f"Error: {text}" in saved


def test_existing_output_receipt_is_shared_without_duplicate_files(tmp_path):
    capture = OutputCapture(tmp_path / "outputs")
    capture.append("HEAD\n" + "x" * 200_000 + "\nTAIL")
    receipt = capture.finish()
    result = ToolResult(success=True, content=receipt, model_context=receipt)
    outcome = process_tool_result(_pipeline(tmp_path / "results", result))
    assert outcome.model_content == receipt
    assert outcome.events[0].content == receipt
    assert len(list(tmp_path.rglob("*.txt"))) == 1


def test_final_fallback_persists_identical_content_and_model_context_only_once(tmp_path):
    text = "x" * (MAX_INLINE_OUTPUT_BYTES + 1)
    result = ToolResult(success=True, content=text, model_context=text)
    outcome = process_tool_result(_pipeline(tmp_path, result))
    assert outcome.model_content == outcome.events[0].content
    assert len(list(tmp_path.rglob("*.txt"))) == 1


@pytest.mark.parametrize("raw,complete", [
    ({"stdout": "x" * 5_000_000}, True), ({"x": float("nan")}, False),
    ({"x": object()}, False), ({"x": "\ud800"}, True),
])
def test_oversized_or_invalid_metadata_does_not_break_event_serialization(tmp_path, raw, complete):
    outcome = process_tool_result(_pipeline(tmp_path, ToolResult(success=True, content="done", raw_output=raw)))
    event = outcome.events[0]
    reference = event.raw_output["_persisted_output"]
    assert reference["file_complete"] is complete
    assert reference["output_truncated"] is True
    if complete:
        assert json.loads(Path(reference["path"]).read_text(encoding="utf-8")) == raw
        assert reference["path"] in outcome.model_content
    else:
        assert "cannot be recovered" in event.content
        assert "cannot be recovered" in outcome.model_content
    json.dumps(EventEnvelope("r", "r:1", 1, event).to_dict(), allow_nan=False, ensure_ascii=False).encode("utf-8")


def test_malformed_unicode_in_custom_kernel_output_remains_deliverable(tmp_path):
    result = ToolResult(success=True, content="before\ud800after")
    outcome = process_tool_result(_pipeline(tmp_path, result))
    assert "before" in outcome.model_content and "after" in outcome.model_content
    json.dumps(EventEnvelope("r", "r:1", 1, outcome.events[0]).to_dict(), ensure_ascii=False).encode("utf-8")


def test_normal_execute_code_metadata_and_small_output_are_unchanged(tmp_path):
    raw = {"status": "complete", "size": 123}
    outcome = process_tool_result(_pipeline(tmp_path, ToolResult(success=True, content="hello", raw_output=raw)))
    assert outcome.events[0].raw_output == raw
    assert outcome.model_content == outcome.events[0].content == "hello"
    assert not list(tmp_path.rglob("*.txt"))


@pytest.mark.parametrize("tool_name", ["execute_code", "bash", "mcp_query", "task"])
def test_failed_output_history_preparation_is_nonfatal_for_all_tools(tmp_path, monkeypatch, tool_name):
    def fail(*args, **kwargs):
        raise OSError("output storage failed")

    monkeypatch.setattr(ToolResultStorage, "process_message", fail)
    request = replace(_pipeline(tmp_path, ToolResult(success=True, content="hello")), tool_name=tool_name)
    outcome = process_tool_result(request)
    assert outcome.events[0].success
    assert "bounded output retained" in outcome.model_content
    assert "hello" in outcome.model_content


def test_durable_commit_failure_keeps_its_existing_semantics(tmp_path):
    def fail_commit(message, event, step):
        raise RuntimeError("durable commit failed")

    with pytest.raises(RuntimeError, match="durable commit failed"):
        process_tool_result(_pipeline(tmp_path, ToolResult(success=True, content="done"), commit_result=fail_commit))


def test_cancellation_is_not_converted_to_an_output_notice(tmp_path, monkeypatch):
    def cancel(*args, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(ToolResultStorage, "process_message", cancel)
    with pytest.raises(asyncio.CancelledError):
        process_tool_result(_pipeline(tmp_path, ToolResult(success=True, content="done")))


def test_raw_json_is_complete_and_small_status_fields_keep_their_types(tmp_path):
    raw = {
        "type": "query_result", "exit_code": 0, "path": "report.json",
        "rows": [{"name": "中文\n\"\\", "payload": "x" * 1024}] * 500,
        "nested": {"tail": [1, None, True]}, "_persisted_output": "original field",
    }
    result = ToolResult(success=True, content="500 records", raw_output=raw)
    request = replace(_pipeline(tmp_path, result), tool_name="mcp_query")
    outcome = process_tool_result(request)
    event = outcome.events[0]
    reference = event.raw_output["_persisted_output"]
    assert json.loads(Path(reference["path"]).read_text(encoding="utf-8")) == raw
    assert event.raw_output["type"] == "query_result"
    assert event.raw_output["exit_code"] == 0
    assert event.raw_output["path"] == "report.json"
    assert event.raw_output["nested"] == raw["nested"]
    assert "rows" not in event.raw_output  # Unknown arrays are not recursively rewritten.
    assert reference["path"] in outcome.model_content
    request.result_storage.enforce_fresh_budget(request.messages, tools={})
    assert reference["path"] in request.messages[-1].content
    assert result.raw_output == raw  # Internal tool data is never mutated.


@pytest.mark.parametrize("save_fails", [False, True])
@pytest.mark.parametrize("read_back", [False, True])
async def test_large_plan_keeps_host_fields_and_steps_after_shared_delivery(tmp_path, monkeypatch, save_fails, read_back):
    store = PlanStore()
    tool = PlanWriteTool(store)
    result = await tool.execute(
        action="set", title="Migration plan", objective="Preserve existing behavior",
        steps=[{"title": f"Step {index}", "details": "HEAD" + "x" * 3000 + "TAIL"} for index in range(24)],
        verification=["Verify the migration"],
    )
    if read_back:
        tool = PlanReadTool(store)
        result = await tool.execute()
    original = result.raw_output
    assert len(json.dumps(original).encode()) > MAX_RAW_OUTPUT_BYTES
    if save_fails:
        original_open = Path.open

        def fail_json(path, *args, **kwargs):
            if path.suffix == ".json":
                raise OSError("disk full")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", fail_json)
    request = replace(_pipeline(tmp_path, result), tool_name=tool.name, tool=tool)
    outcome = process_tool_result(request)
    event = outcome.events[0]
    raw = event.raw_output
    assert event.success
    assert raw["type"] == "plan_snapshot" and raw.get("action") == original.get("action")
    assert raw["summary"] == original["summary"]
    plan = raw["plan"]
    assert plan["id"] == original["plan"]["id"]
    assert plan["title"] == "Migration plan"
    assert plan["objective"] == "Preserve existing behavior"
    assert plan["verification"] == ["Verify the migration"]
    assert len(plan["steps"]) == 24
    for actual, expected in zip(plan["steps"], original["plan"]["steps"]):
        assert actual["id"] == expected["id"]
        assert actual["title"] == expected["title"]
        assert actual["details"].startswith("HEAD") and actual["details"].endswith("TAIL")
        if actual["details"] != expected["details"]:
            assert "omitted" in actual["details"]
    assert any("omitted" in step["details"] for step in plan["steps"])
    reference = raw["_persisted_output"]
    assert reference["file_complete"] is not save_fails
    if save_fails:
        assert "cannot be recovered" in outcome.model_content
        assert "Full JSON saved" not in outcome.model_content
    else:
        assert json.loads(Path(reference["path"]).read_text(encoding="utf-8")) == original
    assert len(json.dumps(raw).encode()) <= MAX_RAW_OUTPUT_BYTES
    assert len(json.dumps(EventEnvelope("r", "r:1", 1, event).to_dict()).encode()) < MAX_TOOL_RESULT_BYTES
    assert result.raw_output is original and len(original["plan"]["steps"][0]["details"]) > 3000


async def test_plan_preview_accepts_restored_plan_without_optional_fields(tmp_path):
    store = PlanStore()
    store.restore({"id": "42", "title": "Restored plan", "status": "active"})
    result = await PlanReadTool(store).execute()
    assert result.success
    assert result.raw_output_preview["plan"]["id"] == "42"
    assert result.raw_output_preview["plan"]["steps"] == []


@pytest.mark.parametrize("read_back", [False, True])
async def test_todo_owns_its_large_snapshot_preview_and_keeps_active_work(tmp_path, read_back):
    store = TodoStore()
    tool = TodoWriteTool(store)
    result = await tool.execute(action="set", todos=[
        {"task": "中文" * 2000, "status": "in_progress" if index == 49 else "pending"}
        for index in range(50)
    ])
    if read_back:
        tool = TodoReadTool(store)
        result = await tool.execute()
    raw = result.raw_output
    request = replace(_pipeline(tmp_path, result), tool_name=tool.name, tool=tool)
    outcome = process_tool_result(request)
    projected = outcome.events[0].raw_output
    assert projected["summary"] == raw["summary"]
    assert isinstance(projected["items"], list) and 0 < len(projected["items"]) <= 64
    assert projected["items"][0]["id"] == raw["items"][49]["id"]
    assert projected["items"][0]["status"] == "in_progress"
    assert len(projected["items"][0]["task"]) <= 500
    assert projected["omitted_items"] == 50 - len(projected["items"])
    assert len(json.dumps(projected, ensure_ascii=False).encode()) <= MAX_RAW_OUTPUT_BYTES
    assert json.loads(Path(projected["_persisted_output"]["path"]).read_text(encoding="utf-8")) == raw
    assert "raw_output_preview" not in result.model_dump()


def test_invalid_tool_preview_falls_back_without_mutating_unknown_fields(tmp_path):
    raw = {"status": "done", "rows": ["x" * 100_000]}
    result = ToolResult(success=True, content="done", raw_output=raw,
                        raw_output_preview={"rows": {"changed_type": "x" * 100_000}})
    outcome = process_tool_result(_pipeline(tmp_path, result))
    projected = outcome.events[0].raw_output
    assert projected["status"] == "done"
    assert "rows" not in projected
    assert json.loads(Path(projected["_persisted_output"]["path"]).read_text(encoding="utf-8")) == raw


def test_many_small_structured_fields_obey_an_aggregate_budget(tmp_path):
    raw = {f"item_{n}": "x" * 3000 for n in range(100)}
    outcome = process_tool_result(_pipeline(tmp_path, ToolResult(success=True, content="done", raw_output=raw)))
    projected = outcome.events[0].raw_output
    assert len(json.dumps(projected, ensure_ascii=False).encode("utf-8")) < MAX_RAW_OUTPUT_BYTES
    path = projected["_persisted_output"]["path"]
    assert json.loads(Path(path).read_text(encoding="utf-8")) == raw


def test_small_status_fields_after_large_containers_are_preserved(tmp_path):
    raw = {f"rows_{index}": ["x" * 2000] for index in range(100)}
    raw["status"] = "ready"
    raw["path"] = "report.json"
    outcome = process_tool_result(_pipeline(tmp_path, ToolResult(success=True, content="done", raw_output=raw)))
    projected = outcome.events[0].raw_output
    assert projected["status"] == "ready"
    assert projected["path"] == "report.json"
    assert len(json.dumps(projected).encode()) <= MAX_RAW_OUTPUT_BYTES


@pytest.mark.parametrize("failure", ["open", "write", "close", "cycle"])
def test_raw_storage_failures_report_partial_output_without_losing_status(tmp_path, monkeypatch, failure):
    raw = {"status": "done", "data": "x" * 100_000}
    if failure == "cycle":
        raw["cycle"] = raw
    else:
        original_open = Path.open

        class FailingStream:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def write(self, chunk):
                if failure == "write":
                    raise OSError("disk full")
                return self.stream.write(chunk)

            def __exit__(self, *args):
                self.stream.close()
                if failure == "close":
                    raise OSError("flush failed")

        def fail_json(path, *args, **kwargs):
            if path.suffix != ".json":
                return original_open(path, *args, **kwargs)
            if failure == "open":
                raise PermissionError("cannot create file")
            return FailingStream(original_open(path, *args, **kwargs))

        monkeypatch.setattr(Path, "open", fail_json)
    outcome = process_tool_result(_pipeline(tmp_path, ToolResult(success=True, content="done", raw_output=raw)))
    projected = outcome.events[0].raw_output
    assert projected["status"] == "done"
    assert projected["_persisted_output"]["file_complete"] is False
    assert "path" not in projected["_persisted_output"]
    assert "cannot be recovered" in outcome.model_content
    assert "Full JSON saved" not in outcome.model_content
    json.dumps(EventEnvelope("r", "r:1", 1, outcome.events[0]).to_dict(), allow_nan=False)


def test_complete_unicode_read_page_remains_verbatim(tmp_path):
    page = "😀" * 100_000
    result = ToolResult(success=True, content=page, raw_output={"truncated": False, "next_offset": None})
    request = replace(
        _pipeline(tmp_path, result), tool_name="read_file",
        tool=ReadTool(workspace_dir=str(tmp_path)),
    )
    outcome = process_tool_result(request)
    assert outcome.model_content == outcome.events[0].content == page
    assert outcome.events[0].raw_output == result.raw_output
    assert not list(tmp_path.rglob("*.txt"))


def test_semantic_model_context_and_small_visible_result_are_preserved(tmp_path):
    text = "a" * 100_000
    result = ToolResult(success=True, content=text, model_context="Seven matching files.")
    outcome = process_tool_result(_pipeline(tmp_path, result))
    assert outcome.model_content == result.model_context
    assert outcome.events[0].content == text
    assert not list(tmp_path.rglob("*.txt"))


def test_escaping_and_combined_fields_trigger_the_final_event_guard(tmp_path):
    text = "\x00" * 300_000  # Each field is below 512 KiB; JSON escaping expands it 6x.
    raw = {"status": "failed", "detail": "a" * 50_000}
    result = ToolResult(success=False, content=text, error=text, raw_output=raw)
    request = replace(_pipeline(tmp_path, result), result_storage=ToolResultStorage(tmp_path, default_result_limit=10_000_000))
    outcome = process_tool_result(request)
    encoded = json.dumps(EventEnvelope("r", "r:1", 1, outcome.events[0]).to_dict()).encode("utf-8")
    assert len(encoded) < MAX_TOOL_RESULT_BYTES
    assert "Full output saved to:" in outcome.events[0].content
    assert text in [path.read_text(encoding="utf-8") for path in tmp_path.rglob("*.txt")]


def test_inline_images_stay_private_when_other_raw_fields_are_externalized(tmp_path):
    blocks = [{"type": "input_image", "data": "PRIVATE_IMAGE", "media_type": "image/png"}]
    raw = {"rows": ["x" * 100_000], "mcp_inline_images": [{"data": "PRIVATE_IMAGE", "mime_type": "image/png"}]}
    result = ToolResult(success=True, content="image", raw_output=raw, image_content=blocks)
    committed = []
    outcome = process_tool_result(_pipeline(tmp_path, result, commit_result=lambda m, e, s: committed.append(e)))
    saved = Path(outcome.events[0].raw_output["_persisted_output"]["path"]).read_text(encoding="utf-8")
    assert "PRIVATE_IMAGE" not in saved
    assert committed[0].image_content == blocks


@pytest.mark.parametrize("structured", [False, True])
def test_persisting_large_output_does_not_allocate_a_second_complete_copy(tmp_path, structured):
    text = "x" * (12 * 1024 * 1024)
    result = (
        ToolResult(success=True, content="done", raw_output={"data": text})
        if structured else ToolResult(success=True, content=text)
    )
    tracemalloc.start()
    try:
        outcome = process_tool_result(_pipeline(tmp_path, result))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 3 * 1024 * 1024
    if structured:
        path = Path(outcome.events[0].raw_output["_persisted_output"]["path"])
        assert json.loads(path.read_text(encoding="utf-8")) == result.raw_output
    else:
        paths = list(tmp_path.rglob("*.txt"))
        assert len(paths) == 1
        assert paths[0].read_text(encoding="utf-8") == text


def test_model_side_persistence_preserves_original_line_endings(tmp_path):
    text = "first\r\nsecond\n" * 10_000
    outcome = process_tool_result(_pipeline(tmp_path, ToolResult(success=True, content=text)))
    # The model projection strips outer whitespace; the host text stays exact.
    assert outcome.events[0].content == text
    assert next(tmp_path.rglob("*.txt")).read_bytes() == text.strip().encode("utf-8")


class _LargeOutputTool(Tool):
    def __init__(self, tool_name, result_kind):
        self._name = tool_name
        self.result_kind = result_kind
        self.calls = 0

    @property
    def name(self):
        return self._name

    @property
    def description(self):
        return "Return a large diagnostic once."

    @property
    def parameters(self):
        return {"type": "object", "properties": {}}

    async def execute(self):
        self.calls += 1
        text = "HEAD\n" + "\\x00" * 1_200_000 + "\nTAIL"
        if self.result_kind == "raw":
            return ToolResult(success=True, content="HEAD\nSee structured output.\nTAIL", raw_output={"status": "done", "data": text})
        if self.result_kind == "error":
            return ToolResult(success=False, content="", error=text)
        return ToolResult(success=True, content=text)


class _InspectingLLM:
    def __init__(self, tool_name):
        self.calls = 0
        self.tool_reply = None
        self.tool_name = tool_name

    async def generate(self, **kwargs):
        raise AssertionError("No context summarization should be needed")

    async def generate_stream(self, messages, tools=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(type="finish", finish_reason="tool", tool_calls=[
                ToolCall(id="python-1", type="function", function=FunctionCall(name=self.tool_name, arguments={})),
            ])
            return
        self.tool_reply = next(message.content for message in reversed(messages) if message.role == "tool")
        assert len(self.tool_reply.encode("utf-8")) < OUTPUT_REPLY_BYTES
        yield StreamEvent(type="text", delta="Analysis continued")
        yield StreamEvent(type="finish", finish_reason="stop")


@pytest.mark.parametrize("failure", [None, "file", "history", "trace", "delivery", "metadata"])
@pytest.mark.parametrize("tool_name", ["execute_code", "bash", "mcp_query", "task"])
@pytest.mark.parametrize("result_kind", ["content", "error", "raw"])
async def test_agent_loop_and_event_delivery_continue_after_output_handling_failures(
    tmp_path, monkeypatch, failure, tool_name, result_kind,
):
    if failure in ("file", "trace"):
        path_open = Path.open

        def fail_file(path, *args, **kwargs):
            if failure == "file" and path.suffix in (".txt", ".json"):
                raise PermissionError("cannot save stdout")
            return path_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", fail_file)
    if failure == "history":
        def fail_history(*args, **kwargs):
            raise OSError("output history unavailable")

        monkeypatch.setattr(ToolResultStorage, "process_message", fail_history)
    if failure == "delivery":
        def fail_delivery(*args, **kwargs):
            raise RuntimeError("output formatter unavailable")

        monkeypatch.setattr(ToolResultStorage, "prepare_delivery", fail_delivery)
    if failure == "metadata":
        from box_agent.tools.engine import engine

        def with_large_metadata(request):
            return process_tool_result(replace(request, policy_decision={"detail": "x" * (5 * 1024 * 1024)}))

        monkeypatch.setattr(engine, "process_tool_result", with_large_metadata)
    llm = _InspectingLLM(tool_name)
    tool = _LargeOutputTool(tool_name, result_kind)
    writer = SessionTraceWriter(session_id="s", acp_session_id="a", trace_dir=tmp_path / "traces", enabled=True)
    if failure == "trace":
        writer.trace_dir.write_text("not a directory", encoding="utf-8")
    token = set_session_trace_writer(writer, turn_id="t")
    handle = AgentRunHandle.for_run(
        state=SimpleNamespace(), run_id="r",
        events_factory=lambda: run_agent_loop(
            llm=llm, messages=[Message(role="user", content="Inspect Python output")],
            tools={tool.name: tool}, max_steps=3, workspace_dir=str(tmp_path),
            tool_result_storage=ToolResultStorage(tmp_path / "results"),
        ),
    )
    try:
        events = [envelope.payload async for envelope in handle.events()]
        result = await handle.result(consume_events=False)
    finally:
        await handle.aclose()
        reset_session_trace_writer(token)
    assert result.status == "completed", result.error
    assert any(isinstance(event, DoneEvent) for event in events)
    assert next(event for event in events if isinstance(event, ToolCallResult)).success is (result_kind != "error")
    assert llm.calls == 2
    assert tool.calls == 1
    assert "HEAD" in llm.tool_reply and "TAIL" in llm.tool_reply
    if failure == "file":
        assert ("Full structured output unavailable" if result_kind == "raw" else "Full output unavailable") in llm.tool_reply
        assert "Full output saved to:" not in llm.tool_reply
        assert "Full JSON saved to:" not in llm.tool_reply
    elif failure == "history":
        assert "bounded output retained" in llm.tool_reply
    elif failure in ("delivery", "metadata"):
        assert "Output delivery failed" in llm.tool_reply
        assert ("output formatter unavailable" if failure == "delivery" else "oversized or cannot be serialized") in llm.tool_reply
        event = next(event for event in events if isinstance(event, ToolCallResult))
        assert event.policy_decision is None
        assert event.raw_output is None
        assert writer.file_path.stat().st_size < MAX_TOOL_RESULT_BYTES
    else:
        assert ("Full JSON saved to:" if result_kind == "raw" else "Full output saved to:") in llm.tool_reply


@pytest.mark.parametrize("metadata", [
    {"policy_decision": {"reason": float("nan")}},
    {"policy_decision": {"reason": object()}},
    {"policy_decision": {"reason": "\ud800"}},
    {"server_name": "x" * (5 * 1024 * 1024)},
], ids=["nan", "unsupported-object", "invalid-unicode", "oversized-server-name"])
def test_invalid_event_metadata_becomes_a_small_serializable_tool_reply(tmp_path, metadata):
    request = replace(_pipeline(tmp_path, ToolResult(success=False, error="original tool failure")), **metadata)
    outcome = process_tool_result(request)
    event = outcome.events[0]
    assert not event.success
    assert "original tool failure" in outcome.model_content
    assert "Output delivery failed" in outcome.model_content
    assert event.policy_decision is None
    assert len(json.dumps(EventEnvelope("r", "r:1", 1, event).to_dict(), ensure_ascii=False, allow_nan=False).encode()) < MAX_TOOL_RESULT_BYTES
    assert outcome.tool_message.tool_call_id == request.tool_call_id


def test_failure_reply_survives_cached_history_and_does_not_retry_storage(tmp_path, monkeypatch):
    storage = ToolResultStorage(tmp_path)
    request = replace(_pipeline(tmp_path, ToolResult(success=True, content="HEAD" + "x" * 100_000 + "TAIL")), result_storage=storage)
    # Model-side persistence succeeds first, then all final formatting/storage
    # fails. The failure reply must replace the cached success receipt.
    def fail_delivery(*args, **kwargs):
        monkeypatch.setattr(Path, "open", lambda *args, **kwargs: pytest.fail("fallback must not write files"))
        raise OSError("formatter storage failed")

    monkeypatch.setattr(storage, "prepare_delivery", fail_delivery)
    outcome = process_tool_result(request)
    assert "formatter storage failed" in outcome.model_content
    assert outcome.events[0].success is True
    storage.enforce_fresh_budget(request.messages, tools={})
    assert request.messages[-1].content == outcome.model_content


def test_delivery_cancellation_is_not_converted_into_an_output_failure(tmp_path, monkeypatch):
    def cancel(*args, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(ToolResultStorage, "prepare_delivery", cancel)
    request = _pipeline(tmp_path, ToolResult(success=True, content="done"))
    with pytest.raises(asyncio.CancelledError):
        process_tool_result(request)
    assert request.messages == []


def test_final_size_checker_failure_uses_a_bounded_reply_without_calling_it_again(tmp_path, monkeypatch):
    from box_agent.tools.engine import results

    calls = []

    def fail_check(*args):
        calls.append(1)
        raise RuntimeError("event size checker unavailable")

    monkeypatch.setattr(results, "_json_fits", fail_check)
    outcome = process_tool_result(_pipeline(tmp_path, ToolResult(success=True, content="done")))
    assert len(calls) == 1
    assert "event size checker unavailable" in outcome.model_content
    encoded = json.dumps(EventEnvelope("r", "r:1", 1, outcome.events[0]).to_dict(), allow_nan=False).encode()
    assert len(encoded) < MAX_TOOL_RESULT_BYTES

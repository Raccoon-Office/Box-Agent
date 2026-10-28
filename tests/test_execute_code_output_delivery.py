"""Output presentation failures must not prevent the next Agent model call."""

from __future__ import annotations

import json
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
from box_agent.tool_result_storage import ToolResultStorage
from box_agent.tools.base import Tool, ToolResult
from box_agent.tools.jupyter_output import OUTPUT_REPLY_BYTES, OutputCapture


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
    assert all(path.read_text(encoding="utf-8") == text for path in files)


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
    text = "x" * 200_000
    result = ToolResult(success=True, content=text, model_context=text)
    outcome = process_tool_result(_pipeline(tmp_path, result))
    assert outcome.model_content == outcome.events[0].content
    assert len(list(tmp_path.rglob("*.txt"))) == 1


@pytest.mark.parametrize("raw", [
    {"stdout": "x" * 5_000_000}, {"x": float("nan")}, {"x": object()}, {"x": "\ud800"},
])
def test_oversized_or_invalid_execute_code_metadata_does_not_break_event_serialization(tmp_path, raw):
    outcome = process_tool_result(_pipeline(tmp_path, ToolResult(success=True, content="done", raw_output=raw)))
    event = outcome.events[0]
    assert event.raw_output is None
    assert "metadata omitted" in event.content
    assert "metadata omitted" in outcome.model_content
    json.dumps(EventEnvelope("r", "r:1", 1, event).to_dict(), allow_nan=False)


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


def test_failed_output_history_preparation_is_local_to_execute_code(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("output storage failed")

    monkeypatch.setattr(ToolResultStorage, "process_message", fail)
    outcome = process_tool_result(_pipeline(tmp_path, ToolResult(success=True, content="hello")))
    assert outcome.events[0].success
    assert "bounded output retained" in outcome.model_content
    assert "hello" in outcome.model_content
    pipeline_input = _pipeline(tmp_path, ToolResult(success=True, content="other tool"))
    pipeline_input = replace(pipeline_input, tool_name="another_tool")
    with pytest.raises(OSError, match="output storage failed"):
        process_tool_result(pipeline_input)


def test_execute_code_durable_commit_failure_keeps_its_existing_semantics(tmp_path):
    def fail_commit(message, event, step):
        raise RuntimeError("durable commit failed")

    with pytest.raises(RuntimeError, match="durable commit failed"):
        process_tool_result(_pipeline(tmp_path, ToolResult(success=True, content="done"), commit_result=fail_commit))


class _LargePythonTool(Tool):
    @property
    def name(self):
        return "execute_code"

    @property
    def description(self):
        return "Return a large diagnostic once."

    @property
    def parameters(self):
        return {"type": "object", "properties": {}}

    async def execute(self):
        return ToolResult(success=True, content="HEAD\n" + "\\x00" * 1_200_000 + "\nTAIL")


class _InspectingLLM:
    def __init__(self):
        self.calls = 0
        self.tool_reply = None

    async def generate(self, **kwargs):
        raise AssertionError("No context summarization should be needed")

    async def generate_stream(self, messages, tools=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(type="finish", finish_reason="tool", tool_calls=[
                ToolCall(id="python-1", type="function", function=FunctionCall(name="execute_code", arguments={})),
            ])
            return
        self.tool_reply = next(message.content for message in reversed(messages) if message.role == "tool")
        assert len(self.tool_reply.encode("utf-8")) < OUTPUT_REPLY_BYTES
        yield StreamEvent(type="text", delta="Analysis continued")
        yield StreamEvent(type="finish", finish_reason="stop")


@pytest.mark.parametrize("failure", [None, "file", "history", "trace"])
async def test_agent_loop_and_event_delivery_continue_after_output_handling_failures(
    tmp_path, monkeypatch, failure,
):
    if failure in ("file", "trace"):
        path_open = Path.open

        def fail_file(path, *args, **kwargs):
            if failure == "file" and path.suffix == ".txt":
                raise PermissionError("cannot save stdout")
            return path_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", fail_file)
    if failure == "history":
        def fail_history(*args, **kwargs):
            raise OSError("output history unavailable")

        monkeypatch.setattr(ToolResultStorage, "process_message", fail_history)
    llm = _InspectingLLM()
    tool = _LargePythonTool()
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
    assert next(event for event in events if isinstance(event, ToolCallResult)).success
    assert llm.calls == 2
    assert "HEAD" in llm.tool_reply and "TAIL" in llm.tool_reply
    if failure == "file":
        assert "Full output unavailable" in llm.tool_reply
        assert "Full output saved to:" not in llm.tool_reply
    elif failure == "history":
        assert "bounded output retained" in llm.tool_reply
    else:
        assert "Full output saved to:" in llm.tool_reply

"""Request histories, durable projections and host replay across tool-result edits."""

import asyncio
import json
from contextlib import aclosing, closing, nullcontext
from pathlib import Path

import pytest

from box_agent.events import DoneEvent, ErrorEvent, StopReason, SummarizationEvent, ToolCallResult
from box_agent.runtime import run_agent_loop
from box_agent.schema import FunctionCall, Message, StreamEvent, TokenUsage, ToolCall
from box_agent.session_log import SessionLog
from box_agent.tool_result_storage import ToolResultStorage
from box_agent.tools.base import Tool, ToolResult


class ReportTool(Tool):
    name = "report"
    description = "Read a named test report."
    parallel_safe = True
    parameters = {"type": "object", "properties": {
        "label": {"type": "string"}, "size": {"type": "integer"},
    }}

    async def execute(self, label, size):
        return ToolResult(success=True, content=label * size)


def report(call_id, label, size):
    return ToolCall(id=call_id, type="function", function=FunctionCall(
        name="report", arguments={"label": label, "size": size},
    ))


def snapshot(messages):
    return tuple(message.model_dump_json().encode("utf-8") for message in messages)


def tool_bodies(request, call_id):
    messages = map(json.loads, request)
    return [m["content"] for m in messages if m["role"] == "tool" and m["tool_call_id"] == call_id]


class RecordingModel:
    def __init__(self, *batches, log=None):
        self.batches = iter(batches)
        self.requests = []
        self.persisted_counts = []
        self.log = log

    def estimate_text_tokens(self, text):
        return len(text.encode("utf-8")) // 3

    async def generate_stream(self, messages, **kwargs):
        if kwargs.get("call_kind") == "context_summary":
            yield StreamEvent(type="text", delta="<summary>Reports inspected.</summary>")
        else:
            self.requests.append(snapshot(messages))
            if self.log is not None:
                records = map(json.loads, self.log.path.read_bytes().splitlines())
                self.persisted_counts.append(sum(
                    bool(record.get("data", {}).get("edit", {}).get("persistedPath")) for record in records
                ))
            calls = next(self.batches, None)
            if calls:
                yield StreamEvent(type="finish", finish_reason="tool_calls", tool_calls=calls)
                return
            yield StreamEvent(type="text", delta="Finished.")
        yield StreamEvent(type="finish", finish_reason="stop")


def initial_history():
    return [Message(role="system", content="System."), Message(role="user", content="Inspect the reports.")]


def run(model, history, tool, storage, log, **options):
    return run_agent_loop(
        llm=model, messages=history, tools={tool.name: tool}, tool_result_storage=storage,
        session_log=log, session_turn=options.pop("session_turn", 1), session_id="reports",
        max_steps=options.pop("max_steps", 4), completion_checks_enabled=False,
        artifact_detection_enabled=False, **options,
    )


def assert_done(events, reason=StopReason.END_TURN):
    assert not [event.message for event in events if isinstance(event, ErrorEvent)]
    assert [event.stop_reason for event in events if isinstance(event, DoneEvent)] == [reason]


@pytest.mark.parametrize("logged", [True, False], ids=["session-log", "sdk"])
@pytest.mark.parametrize("limit", [3_000, 8_000], ids=["omitted-round", "retained-round"])
async def test_compacted_parallel_tool_round_has_a_stable_prefix_on_the_next_request(tmp_path, caplog, logged, limit):
    class MeasuredFirstCall(RecordingModel):
        async def generate_stream(self, messages, **kwargs):
            async for event in super().generate_stream(messages, **kwargs):
                if event.tool_calls and len(self.requests) == 1:
                    event.usage = TokenUsage(total_tokens=9_000)
                yield event

    batch = [report(f"report-{label}", label, 9_500) for label in "ABC"]
    history = initial_history()
    storage = ToolResultStorage(tmp_path, aggregate_budget=16_000)
    with (closing(SessionLog.create(tmp_path, session_id="reports", cwd=tmp_path)) if logged else nullcontext()) as log:
        model = MeasuredFirstCall(batch, [report("small", "D", 8)], log=log)
        events = [event async for event in run(model, history, ReportTool(), storage, log, token_limit=limit)]
        assert_done(events)
        assert len([event for event in events if isinstance(event, SummarizationEvent)]) == 1
        assert len(model.requests) == 3
        _, first, second = model.requests
        for call in batch:
            bodies = tool_bodies(first, call.id)
            if limit == 3_000:
                assert bodies == []
            elif call.id == "report-C":
                assert bodies == ["C" * 9_500]
            else:
                assert len(bodies) == 1 and bodies[0].startswith("<persisted-output>")
            assert tool_bodies(second, call.id) == bodies
        assert second[:len(first)] == first
        if logged:
            assert model.persisted_counts == [0, 2, 2]
            assert snapshot(log.replay().messages) == snapshot(history[1:])
        else:
            assert {p.read_text() for p in tmp_path.rglob("*.txt")} == {"A" * 9_500, "B" * 9_500}
    assert "session_log/surface_diverged" not in caplog.text


async def test_fresh_budget_commits_before_request_and_acp_replays_each_original_once(tmp_path, caplog):
    from tests.test_acp_history_edit import adapter

    batch = [report(label, label, 6_000) for label in "AB"]
    storage = ToolResultStorage(tmp_path, aggregate_budget=9_000)
    queue = asyncio.Queue()
    async with aclosing(adapter(tmp_path, RecordingModel())) as host:
        session = await host.new_session(str(tmp_path), session_id="reports")
        log = host._sessions[session.session_id].agent.session_log
        history = initial_history()
        model = RecordingModel(batch, [report("small", "s", 1)], log=log)
        events = []
        async for event in run(model, history, ReportTool(), storage, log, inject_queue=queue):
            events.append(event)
            if isinstance(event, ToolCallResult) and event.tool_call_id == "B":
                queue.put_nowait("Read the follow-up too.")
        assert_done(events)
        assert len(model.requests) == 3
        for previous, current in zip(model.requests, model.requests[1:]):
            assert current[:len(previous)] == previous
        assert "Read the follow-up too." in model.requests[1][-1].decode()
        assert model.persisted_counts == [0, 1, 1]
        edit, = [e for e in log.events if e["data"].get("edit", {}).get("persistedPath")]
        original, = [e for e in log.events if e["seq"] in edit["sourceEventSeqs"]]
        assert edit["surfaceOp"] == {"op": "replace", "start": original["seq"], "end": original["seq"]}
        details = edit["data"]["edit"]
        assert details["reason"] == "tool_result_budget"
        assert details["originalChars"] == 6_000
        assert Path(details["persistedPath"]).read_text() == "A" * 6_000
        preview, = tool_bodies(model.requests[1], "A")
        assert preview.startswith("<persisted-output>")
        assert [(m.tool_call_id, m.content) for _, m, _ in log.history_messages() if m.role == "tool"] == [
            ("A", "A" * 6_000), ("B", "B" * 6_000), ("small", "s"),
        ]
        completed = []

        async def collect(session_id, update, **meta):
            record = update.model_dump(by_alias=True, exclude_none=True)
            if record.get("sessionUpdate") == "tool_call_update" and record.get("status") == "completed":
                completed.append((record["toolCallId"], record["content"][0]["content"]["text"]))

        host._conn.session_update = collect
        await host.load_session(str(tmp_path), session.session_id)
        assert completed == [("A", "A" * 6_000), ("B", "B" * 6_000), ("small", "s")]
    assert "session_log/surface_diverged" not in caplog.text


@pytest.mark.parametrize(("stop", "reason"), [
    ("limit", StopReason.MAX_STEPS), ("cancel", StopReason.CANCELLED), ("tool", StopReason.WAITING_FOR_USER),
])
async def test_reopened_turn_budgets_results_and_preserves_sent_prefix(tmp_path, stop, reason):
    first_batch = [report("old", "X", 6_000)]
    second_batch = [report("pending", "Y", 6_000)]
    tool = ReportTool()
    history = initial_history()
    storage = ToolResultStorage(tmp_path, aggregate_budget=3_000)
    with closing(SessionLog.create(tmp_path, session_id="reports", cwd=tmp_path)) as log:
        before = RecordingModel(first_batch, second_batch)
        cancelled = False
        events = []
        async for event in run(before, history, tool, storage, log,
                               max_steps=2 if stop == "limit" else 4, is_cancelled=lambda: cancelled):
            events.append(event)
            if isinstance(event, ToolCallResult):
                if stop == "tool" and event.tool_call_id == "old":
                    tool.ends_turn_on_success = True
                if stop == "cancel" and event.tool_call_id == "pending":
                    cancelled = True
        assert_done(events, reason)
        assert len(before.requests) == (3 if stop == "limit" else 2)
        assert tool_bodies(before.requests[1], "pending") == []
        assert tool_bodies(before.requests[-1], "old")[0].startswith("<persisted-output>")
        if stop == "limit":
            assert tool_bodies(before.requests[-1], "pending")[0].startswith("<persisted-output>")
        else:
            assert history[-1].content == "Y" * 6_000
        log.flush()

    with closing(SessionLog.open(tmp_path, session_id="reports", cwd=tmp_path)) as reopened:
        reopened.prepare_resume()
        restored = [history[0], *reopened.replay().messages, Message(role="user", content="Continue.")]
        after = RecordingModel()
        fresh_store = ToolResultStorage(tmp_path, aggregate_budget=3_000)
        events = [event async for event in run(after, restored, tool, fresh_store, reopened, session_turn=2)]
        assert_done(events)
        first, = after.requests
        assert first[:len(before.requests[-1])] == before.requests[-1]
        assert tool_bodies(first, "pending")[0].startswith("<persisted-output>")
        assert snapshot(reopened.replay().messages) == snapshot(restored[1:])


@pytest.mark.parametrize("logged", [True, False], ids=["session-log", "sdk"])
@pytest.mark.parametrize("first_send", ["storage_unavailable", "larger_budget"])
async def test_resume_preserves_already_sent_inline_result_when_no_response_was_committed(tmp_path, first_send, logged):
    output_root = tmp_path / "outputs"
    if first_send == "storage_unavailable":
        # A regular file cannot hold the tool-results directory.
        output_root.write_text("temporarily unavailable")
    initial_budget = 3_000 if first_send == "storage_unavailable" else 9_000
    storage = ToolResultStorage(output_root, aggregate_budget=initial_budget)
    history = initial_history()
    cancelled = False

    class InterruptedModel(RecordingModel):
        async def generate_stream(self, messages, **kwargs):
            nonlocal cancelled
            if self.requests:
                self.requests.append(snapshot(messages))
                cancelled = True
                yield StreamEvent(type="text", delta="uncommitted reply")
                return
            async for event in super().generate_stream(messages, **kwargs):
                yield event

    before = InterruptedModel([report("sent", "X", 6_000)])
    with (closing(SessionLog.create(tmp_path, session_id="reports", cwd=tmp_path)) if logged else nullcontext()) as log:
        events = [event async for event in run(before, history, ReportTool(), storage, log,
                                               is_cancelled=lambda: cancelled)]
        assert_done(events, StopReason.CANCELLED)
        assert len(before.requests) == 2
        sent = before.requests[-1]
        assert tool_bodies(sent, "sent") == ["X" * 6_000]
        assert history[-1].role == "tool"

    if first_send == "storage_unavailable":
        output_root.unlink()
    with (closing(SessionLog.open(tmp_path, session_id="reports", cwd=tmp_path)) if logged else nullcontext()) as reopened:
        if logged:
            reopened.prepare_resume()
            history = [history[0], *reopened.replay().messages]
        history.append(Message(role="user", content="Continue."))
        after = RecordingModel()
        storage = ToolResultStorage(output_root, aggregate_budget=3_000)
        events = [event async for event in run(after, history, ReportTool(), storage, reopened, session_turn=2)]
        assert_done(events)
        resumed, = after.requests
        assert resumed[:len(sent)] == sent
        if logged:
            assert tool_bodies(snapshot(reopened.replay().messages), "sent") == ["X" * 6_000]


async def test_reused_call_id_budgets_both_results_and_keeps_both_files(tmp_path):
    storage = ToolResultStorage(tmp_path, aggregate_budget=3_000)
    model = RecordingModel([report("same", "X", 6_000)], [report("same", "Y", 7_000)])
    with closing(SessionLog.create(tmp_path, session_id="reports", cwd=tmp_path)) as log:
        events = [event async for event in run(model, initial_history(), ReportTool(), storage, log)]
        assert_done(events)
        previous, current = model.requests[1:]
        assert current[:len(previous)] == previous
        original, newer = tool_bodies(current, "same")
        assert original.startswith("<persisted-output>") and "X" * 20 in original
        assert newer.startswith("<persisted-output>") and "Y" * 20 in newer
        edits = [e["data"]["edit"] for e in log.events if e["data"].get("edit", {}).get("persistedPath")]
        assert [Path(e["persistedPath"]).read_text() for e in edits] == ["X" * 6_000, "Y" * 7_000]


async def test_delivery_failure_notice_survives_next_step_and_log_replay(tmp_path, monkeypatch):
    storage = ToolResultStorage(tmp_path, default_result_limit=1_000, aggregate_budget=100)
    original_delivery = storage.prepare_delivery

    def fail_first(**kwargs):
        if kwargs["tool_call_id"] == "failed":
            raise OSError("formatter unavailable")
        return original_delivery(**kwargs)

    monkeypatch.setattr(storage, "prepare_delivery", fail_first)
    model = RecordingModel([report("failed", "X", 6_000)], [report("small", "s", 1)])
    with closing(SessionLog.create(tmp_path, session_id="reports", cwd=tmp_path)) as log:
        events = [event async for event in run(model, initial_history(), ReportTool(), storage, log)]
        assert_done(events)
        _, first, next_request = model.requests
        notice, = tool_bodies(first, "failed")
        assert notice.startswith("Output delivery failed") and "formatter unavailable" in notice
        assert tool_bodies(next_request, "failed") == [notice]
        assert tool_bodies(snapshot(log.replay().messages), "failed") == [notice]


async def test_workflow_text_in_midturn_updates_survives_fresh_edits_compaction_and_reopen(tmp_path, caplog):
    from box_agent.kernel.context_engine import _maybe_summarize
    from box_agent.kernel.tool_messages import ToolMessageCommitter
    from box_agent.loop_guards import format_injected_message, format_runtime_context_update

    marker = "CONTROLLED_PRESENTATION_STAGE=3 [BOX_AGENT_EXTERNAL_SKILL_CHECKPOINT]"
    injected = Message(role="user", content=format_injected_message(marker))
    runtime = Message(role="user", source="runtime", content=format_runtime_context_update(marker))
    history = [*initial_history(), *(Message(role="assistant", content=f"round {i}") for i in range(7)),
               injected, runtime, Message(role="assistant", content="", tool_calls=[report("edited", "A", 6000)]),
               Message(role="tool", name="report", tool_call_id="edited", content="A" * 6_000)]
    with closing(SessionLog.create(tmp_path, session_id="workflow-text", cwd=tmp_path)) as log:
        for _ in range(2):
            log.append_unlogged_messages(history[1:], turn=1, step=1)
            log.flush()
            assert log.replay().messages == history[1:]
        committer = ToolMessageCommitter(history, log, turn=1)
        storage = ToolResultStorage(tmp_path, aggregate_budget=3_000)
        committer.apply_fresh_budget(storage, tools={"report": ReportTool()}, session_id="workflow-text", step=2)
        assert history[-1].content.startswith("<persisted-output>")
        assert log.replay().messages == history[1:]
        edits = [e for e in log.events if e["data"].get("edit", {}).get("persistedPath")]
        assert len(edits) == 1 and edits[0]["surfaceOp"]["start"] == edits[0]["surfaceOp"]["end"]
        result = await _maybe_summarize(RecordingModel(), history, 20_000, 0, False, force=True, session_log=log)
        assert result.mode == "summary"
        assert injected in result.messages and runtime in result.messages
        log.commit_compaction(result.plan, turn=1, step=2)
        log.append("compaction/end", {"format": 2, "mode": "summary"})
        log.flush()
    with closing(SessionLog.open(tmp_path, session_id="workflow-text", cwd=tmp_path)) as log:
        assert log.replay().messages == result.messages[1:]
        original = [m for _, m, _ in log.history_messages()]
        assert injected in original and runtime in original and original[-1].content == "A" * 6_000
    assert "surface_diverged" not in caplog.text

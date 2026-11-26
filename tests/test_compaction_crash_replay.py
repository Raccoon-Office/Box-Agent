"""Reopen each complete JSONL prefix of real kernel commits in a 128k window."""
from contextlib import closing

import pytest

from box_agent.events import DoneEvent, StopReason
from box_agent.kernel.tool_messages import validate_tool_history
from box_agent.runtime import run_agent_loop
from box_agent.schema import FunctionCall, Message, StreamEvent, TokenUsage, ToolCall
from box_agent.session_log import SessionLog
from box_agent.skill_runtime import SkillRuntime
from box_agent.tools.skill_tool import GetSkillTool
from tests.test_compaction_resource_retention import skill_context
from tests.test_session_log_images import reference


class RecordingProvider:
    context_window = 128_000
    max_output_tokens = 2_048

    def __init__(self, mode):
        self.mode = mode
        self.requests = []

    async def generate_stream(self, messages, *, call_kind="", **kwargs):
        if call_kind == "context_summary":
            text = "<summary>Reviewed the figures.</summary>" if self.mode == "summary" else "invalid format"
        else:
            self.requests.append([m.model_dump() for m in messages])
            text = "Figures checked."
        yield StreamEvent(type="text", delta=text)
        yield StreamEvent(type="finish", finish_reason="stop")


def add_read(history, name, content, arguments):
    call = ToolCall(id=f"read-{len(history)}", type="function", function=FunctionCall(name=name, arguments=arguments))
    history.extend([Message(role="assistant", content="", tool_calls=[call]),
                    Message(role="tool", name=name, tool_call_id=call.id, content=content)])


@pytest.mark.parametrize("mode", ["summary", "fallback"])
async def test_every_compaction_prefix_keeps_current_request_and_reopens_ui_history(tmp_path, mode):
    runtime, context, history = skill_context(tmp_path)
    root = tmp_path / "original"
    provider = RecordingProvider(mode)
    current = "Check the final figures."
    history.append(Message(role="user", source="runtime", name="current_date", content="2026-11-26"))
    history.extend(Message(role="assistant", content=f"Old round {i}.") for i in range(6))
    history.append(Message(role="user", content=current))
    for name in ("analysis", "slides"):
        context.prepare_request(history, budget_chars=50_000)
        read = context.read(name)
        assert read.success
        add_read(history, "get_skill", read.model_context, {"skill_name": name})
    add_read(history, "inspect_images", "Chart inspected.", {})
    picture = history[-1]
    history[-2].usage = TokenUsage(total_tokens=110_000)
    tool = GetSkillTool(runtime.loader)

    async def run(log, turn):
        events = [e async for e in run_agent_loop(
            llm=provider, tools={tool.name: tool}, skill_engine=runtime, messages=history, session_log=log,
            session_turn=turn, token_limit=64_000, max_steps=1,
        )]
        assert any(isinstance(e, DoneEvent) and e.stop_reason == StopReason.END_TURN for e in events), events

    with closing(SessionLog.create(root, session_id=mode, cwd=tmp_path)) as log:
        log.queue_message_images(picture, [reference("chart", source="image_inspect")])
        await run(log, 1)
        start, end = [e for e in log.events if e["type"].startswith("compaction/")]
        assert end["data"]["mode"] == mode
        delta = log.events[start["seq"] + 1:end["seq"]]
        edits = [e for e in delta if isinstance(e.get("surfaceOp"), dict) and e["data"].get("edit")]
        appends = [e for e in delta if e.get("surfaceOp") == "append"]
        head, = [e for e in delta if e["data"].get("compaction_node") == mode + "_head"]
        assert len(edits) == (2 if mode == "summary" else 0)
        assert (len(appends) >= 2 if mode == "summary" else len(appends) == 1)
        assert [e["seq"] for e in delta if "surfaceOp" in e] == [e["seq"] for e in [*edits, head, *appends]]
        assert all(e["data"]["compaction_node"] == "appended" for e in appends)
        expected_ui = [m.content for _, m, _ in log.history_messages(events=log.events[:start["seq"]])]
        lines = log.path.read_bytes().splitlines(keepends=True)
        relative_path = log.path.relative_to(root)
        after = log.replay().messages
        if mode == "summary":
            summary = after[0]
            skills, = [m for m in after if m.name == "invoked_skills"]
            assert summary.model_content() == summary.content and skills.model_content() == skills.content
        assert [m.model_dump() for m in after] == [m.model_dump() for m in history[1:]]
        assert log.replay().images[0]["active"] and log.replay().images[0]["messageSeq"] == picture._surface_seq

    with closing(SessionLog.open(root, session_id=mode, cwd=tmp_path)) as log:
        after_reopen = log.replay().messages
        assert [m.model_dump() for m in after_reopen] == [m.model_dump() for m in after]
        runtime = SkillRuntime(runtime.loader, session_log=log, append_only_context=True)
        runtime.restore_records(log.replay().skills)
        history[1:] = [*after_reopen, Message(role="user", content="Continue.")]
        await run(log, 2)
        first, second = provider.requests
        assert second[:len(first)] == first

    for seq in range(start["seq"], end["seq"] + 1):
        snapshot = tmp_path / f"crash-{seq}"
        path = snapshot / relative_path
        path.parent.mkdir(parents=True)
        path.write_bytes(b"".join(lines[:seq + 2]))  # JSONL header precedes event zero.
        with closing(SessionLog.open(snapshot, session_id=mode, cwd=tmp_path)) as recovered:
            surface = recovered.replay().messages
            validate_tool_history(surface)
            assert any(m.content == current and m.source == "user" for m in surface)
            later = Message(role="user", content=f"Continue after {seq}.")
            recovered.append_unlogged_messages([*surface, later], turn=2, step=1)
            recovered.flush()
            assert [m.content for _, m, _ in recovered.history_messages()] == [*expected_ui, later.content]

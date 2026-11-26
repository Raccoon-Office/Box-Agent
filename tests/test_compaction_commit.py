"""The shared kernel publishes a cut only against the history that was summarized."""
from contextlib import closing

import pytest

from box_agent.events import DoneEvent, StopReason
from box_agent.runtime import run_agent_loop
from box_agent.schema import Message, StreamEvent, TokenUsage
from box_agent.session_log import SessionLog, SessionLogReplayError


class Provider:
    context_window = 128_000
    max_output_tokens = 2_048

    def __init__(self, on_summary=None):
        self.on_summary = on_summary

    async def generate(self, *args, **kwargs):
        pytest.fail("Summary requests must stream")

    async def generate_stream(self, messages, *, call_kind="", **kwargs):
        if call_kind == "context_summary":
            if self.on_summary:
                self.on_summary()
            text = "<summary>Earlier work.</summary>"
        else:
            text = "Done."
        yield StreamEvent(type="text", delta=text)
        yield StreamEvent(type="finish", finish_reason="stop")


def history():
    return [
        Message(role="system", content="system"), Message(role="user", content="first"),
        *(Message(role="assistant", content=f"round {i}") for i in range(6)),
        Message(role="assistant", content="last round", usage=TokenUsage(total_tokens=100_000)),
        Message(role="user", content="latest"),
    ]


def run(log, messages, llm):
    return run_agent_loop(llm=llm, tools={}, messages=messages, session_log=log, session_turn=1,
                          token_limit=64_000, max_steps=1)


async def test_kernel_records_summary_metadata_once_and_replays_the_committed_cut(tmp_path):
    messages = history()
    with closing(SessionLog.create(tmp_path, session_id="cut", cwd=tmp_path)) as log:
        events = [event async for event in run(log, messages, Provider())]
        assert any(isinstance(e, DoneEvent) and e.stop_reason == StopReason.END_TURN for e in events)
        start, end = [e for e in log.events if e["type"].startswith("compaction/")]
        assert (start["type"], end["type"]) == ("compaction/start", "compaction/end")
        assert start["data"]["format"] == end["data"]["format"] == 2
        before = log.replay(through_seq=start["seq"]).messages
        after = log.replay(through_seq=end["seq"]).messages
        header, = [e for e in log.events[start["seq"] + 1:end["seq"]] if "surfaceOp" in e]
        cut = next(i for i, m in enumerate(before) if m._surface_seq == end["data"]["cutSeq"])
        assert header["surfaceOp"] == {"op": "replace", "start": before[0]._surface_seq,
                                      "end": before[cut - 1]._surface_seq}
        assert header["sourceEventSeqs"] == [m._surface_seq for m in before[:cut]]
        assert [m._surface_seq for m in after[1:]] == [m._surface_seq for m in before[cut:]]
        assert all(m.usage is None for m in after)
        assert after[0].compaction_node == "summary_head" and after[0].summary_body == "Earlier work."
        assert end["data"]["summaryInput"] == [before[0]._surface_seq, before[-1]._surface_seq]
        assert end["data"]["summaryOmitted"] == []
        assert [m.model_dump() for m in messages[1:]] == [m.model_dump() for m in log.replay().messages]


async def test_kernel_rejects_a_cut_when_the_durable_surface_changes_during_summary(tmp_path):
    messages = history()
    with closing(SessionLog.create(tmp_path, session_id="stale-cut", cwd=tmp_path)) as log:
        def new_input():
            log.append("user/message", {"role": "user", "content": "new input"}, surface_op="append")

        with pytest.raises(SessionLogReplayError, match="surface"):
            _ = [event async for event in run(log, messages, Provider(new_input))]
        assert log.replay().messages[-1].content == "new input"
        assert all(e.get("surfaceOp") in (None, "append") for e in log.events)
        assert log.events[-1]["type"] == "compaction/end" and log.events[-1]["data"]["stage"] == "commit"


async def test_rapid_refill_closes_the_attempt_before_a_summary_or_surface_change(tmp_path):
    from box_agent.context_input import DefaultContextEngine
    from box_agent.kernel.context_types import ContextCompactionState

    messages = history()
    original = [m.model_dump() for m in messages[1:]]
    context = DefaultContextEngine(compaction_state=ContextCompactionState(step=3, last_compaction_step=3,
                                                                          rapid_refills=2))
    with closing(SessionLog.create(tmp_path, session_id="rapid", cwd=tmp_path)) as log:
        events = [e async for e in run_agent_loop(
            llm=Provider(on_summary=lambda: pytest.fail("unexpected summary call")), messages=messages,
            tools={}, context_engine=context, session_log=log, session_turn=1, token_limit=64_000, max_steps=1,
        )]
        assert events[-1].stop_reason == StopReason.ERROR
        start, end = [e for e in log.events if e["type"].startswith("compaction/")]
        assert (start["type"], end["type"]) == ("compaction/start", "compaction/end")
        assert end["data"]["stage"] == "thrashing" and end["data"]["error"]
        assert [m.model_dump() for m in log.replay().messages] == original


@pytest.mark.parametrize("same_route", [True, False])
async def test_rebuilt_history_projection_keeps_log_positions_for_the_summary_route(tmp_path, same_route):
    from box_agent.context_input import DefaultContextEngine

    class RebuildingContext(DefaultContextEngine):
        def project_history(self, messages):
            return [Message.model_validate({**m.model_dump(), "source": m.source}) for m in messages]

    with closing(SessionLog.create(tmp_path, session_id="custom-context", cwd=tmp_path)) as log:
        main, summarizer = Provider(), Provider()
        main.model, summarizer.model = "main", "main" if same_route else "summary"
        events = [e async for e in run_agent_loop(
            llm=main, summary_llm=summarizer, messages=history(), context_engine=RebuildingContext(),
            tools={}, session_log=log, session_turn=1, token_limit=64_000, max_steps=1,
        )]
        assert events[-1].stop_reason == StopReason.END_TURN
        start, end = [e for e in log.events if e["type"].startswith("compaction/")]
        before = log.replay(through_seq=start["seq"]).messages
        assert end["data"]["summaryInput"] == [before[0]._surface_seq, before[-1]._surface_seq]

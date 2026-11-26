from contextlib import closing
from types import SimpleNamespace

import pytest

from box_agent.schema import Message, TokenUsage
from box_agent.session_log import SessionLog, SessionLogCorrupted


def make_plan(messages, **options):
    seqs = tuple(message._surface_seq for message in messages)
    head = Message(role="user", source="runtime", content="Summary", summary_body="Summary",
                   compaction_node="summary_head")
    return SimpleNamespace(**{
        "source_seqs": seqs, "head": head, "cut_index": 2, "cut_seq": seqs[1],
        "rewrites": {}, "appends": [], **options,
    })


def test_compaction_preserves_retained_events_and_expires_old_usage(tmp_path):
    messages = [
        Message(role="user", content="original"),
        Message(role="assistant", content="previous response",
                usage=TokenUsage(prompt_tokens=60_000), request_only_input_tokens=20),
        Message(role="tool", content="skill body", name="get_skill", tool_call_id="skill"),
        Message(role="user", content="latest"),
    ]
    with closing(SessionLog.create(tmp_path, session_id="retained", cwd=tmp_path)) as log:
        image = {"imageId": "example", "source": "image_inspect"}
        log.queue_message_images(messages[2], [image])
        originals = log.append_unlogged_messages(messages, turn=1, step=1)
        seqs = [event["seq"] for event in originals]
        assert [msg._surface_seq for msg in messages] == seqs

        plan = make_plan(messages, rewrites={
            seqs[2]: messages[2].model_copy(update={"content": "read receipt"}),
        }, appends=[Message(role="user", source="runtime", content="notice",
                            compaction_node="appended", omitted_ranges=[(0, 0)])])
        log.append("compaction/start", {"format": 2})
        log.commit_compaction(plan, turn=1, step=2)
        rewrite, head, addition = log.events[-3:]
        assert rewrite["surfaceOp"] == {"op": "replace", "start": seqs[2], "end": seqs[2]}
        assert rewrite["sourceEventSeqs"] == [seqs[2]]
        assert rewrite["data"]["images"] == [image]
        assert rewrite["data"]["edit"]
        assert head["surfaceOp"] == {"op": "replace", "start": seqs[0], "end": seqs[0]}
        assert head["sourceEventSeqs"] == [seqs[0]]
        assert addition["surfaceOp"] == "append"

        restored = log.replay().messages
        assert [msg._surface_seq for msg in restored] == [
            head["seq"], seqs[1], rewrite["seq"], seqs[3], addition["seq"],
        ]
        assert restored[1].usage is None and restored[1].request_only_input_tokens == 0
        assert log.replay(through_seq=rewrite["seq"]).messages[1].usage.prompt_tokens == 60_000
        assert restored[0].model_content() == restored[0].summary_body == "Summary"
        assert restored[-1].omitted_ranges == [(0, 0)]
        assert log.replay().images[0]["active"]
        next_response = Message(role="assistant", content="later", usage=TokenUsage(prompt_tokens=150))
        [followup] = log.append_unlogged_messages([*restored, next_response], turn=2, step=1)
        assert followup["surfaceOp"] == "append"
        assert [msg.content for _, msg, _ in log.history_messages()] == [
            msg.content for msg in [*messages, next_response]
        ]
        log.flush()
    with closing(SessionLog.open(tmp_path, session_id="retained", cwd=tmp_path)) as log:
        surface = log.replay().messages
        assert [msg.model_dump() for msg in surface] == [msg.model_dump() for msg in [*restored, next_response]]
        assert surface[-1].usage.prompt_tokens == 150


@pytest.mark.parametrize("invalid", ["stale", "cut", "cut_seq", "rewrite_prefix", "rewrite_role", "head", "append"])
def test_invalid_compaction_leaves_surface_and_log_intact(tmp_path, invalid):
    messages = [Message(role="user", content=str(i)) for i in range(3)]
    with closing(SessionLog.create(tmp_path, session_id="preflight", cwd=tmp_path)) as log:
        original_events = log.append_unlogged_messages(messages, turn=1, step=1)
        plan = make_plan(messages)
        replacements = {
            "stale": {"source_seqs": (5, 1, 2)}, "cut": {"cut_index": 1}, "cut_seq": {"cut_seq": 2},
            "rewrite_prefix": {"rewrites": {0: messages[0]}},
            "rewrite_role": {"rewrites": {1: messages[1], 2: Message(role="tool", content="bad")}},
            "head": {"head": messages[0]}, "append": {"appends": [messages[1]]},
        }
        for key, value in replacements[invalid].items():
            setattr(plan, key, value)
        with pytest.raises(SessionLogCorrupted):
            log.commit_compaction(plan, turn=1, step=2)
        assert list(log.events) == original_events
        assert log.replay().messages == messages


def test_history_hides_legacy_compaction_copies_and_shows_new_format_turns(tmp_path):
    with closing(SessionLog.create(tmp_path, session_id="history", cwd=tmp_path)) as log:
        def user(text):
            log.append("user/message", {"role": "user", "content": text}, surface_op="append")

        user("first")
        log.append("compaction/start", {})
        log.append("compaction/summary", {"mode": "summary"})
        user("legacy copy")
        log.append("compaction/end", {})
        log.append("compaction/start", {"format": 2})
        user("after interrupted compaction")
        assert [msg.content for _, msg, _ in log.history_messages()] == ["first", "after interrupted compaction"]


def test_compaction_metadata_is_excluded_from_request_traces_and_desktop_memory_budget():
    from box_agent.desktop_memory.visibility import DesktopInputGuard
    from box_agent.llm.llm_wrapper import _trace_request_message

    rendered = Message(role="user", source="runtime", content="Visible summary")
    head = rendered.model_copy(update={
        "compaction_node": "fallback_head", "summary_body": "saved summary " * 1000,
        "earlier_user_messages": ["older user original"], "summary_visible_from": 1,
        "summary_latest_full": True, "fallback_user_count": 1, "omitted_ranges": [(1, 8)],
    })
    assert _trace_request_message(head) == _trace_request_message(rendered)
    guard = DesktopInputGuard(lambda *_: None, lambda _: "")
    assert guard._input_tokens([head], []) == guard._input_tokens([rendered], [])


@pytest.mark.parametrize("kind", ["current_date", "tool_descriptions"])
def test_first_new_compaction_cuts_after_a_legacy_runtime_append(tmp_path, kind):
    from box_agent.kernel.context_engine import _select_recent_messages

    runtime = Message(role="user", source="runtime", name=kind, content="host reference",
                      runtime_kind="tool_descriptions" if kind == "tool_descriptions" else None)
    user = Message(role="user", content="previous request")
    answer = Message(role="assistant", content="previous answer")
    head = Message(role="user", source="runtime", content="legacy summary", earlier_user_messages=["older"])
    # Reproduce the old writer: dates and descriptions were skipped during
    # selection and re-appended by the projector, possibly with surfaceDerived.
    with closing(SessionLog.create(tmp_path, session_id="legacy", cwd=tmp_path)) as log:
        log.append_unlogged_messages([runtime, user, answer], turn=1, step=1)
        log.append("compaction/start", {})
        log.append("compaction/summary", {"message": head.model_dump()})
        log.replace_surface([head, user, answer, runtime], turn=1, step=2)
        log.append("compaction/end", {})
        new_turn = [user.model_copy(update={"content": "next request"}), runtime.model_copy(), answer]
        [current, _, _] = log.append_unlogged_messages(
            [*log.replay().messages, *new_turn], turn=2, step=1,
        )
        log.flush()
    with closing(SessionLog.open(tmp_path, session_id="legacy", cwd=tmp_path)) as log:
        replayed = log.replay().messages
        latest = replayed[-3:]
        assert replayed[3]._legacy_compaction_append and not latest[1]._legacy_compaction_append
        assert replayed[3].model_dump() == latest[1].model_dump()
        system = Message(role="system", content="system")
        retained, _ = _select_recent_messages([system, *replayed])
        assert [m._surface_seq for m in retained] == [current["seq"], current["seq"] + 1, current["seq"] + 2]

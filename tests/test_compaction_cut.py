"""The compacted request has one prefix replacement and a contiguous original tail."""

from box_agent.kernel import context_engine as engine
from box_agent.schema import Message, StreamEvent


class SummaryProvider:
    context_window = 64_000

    async def generate_stream(self, **kwargs):
        yield StreamEvent(type="text", delta="<summary>Earlier work.</summary>")
        yield StreamEvent(type="finish", finish_reason="stop")


async def test_summary_keeps_normal_runtime_messages_at_their_original_cut_positions():
    system = Message(role="system", content="system")
    old = Message(role="user", content="first task")
    boundary = Message(role="user", source="runtime", content="previously restored",
                       compaction_node="appended")
    ordinary = [
        Message(role="user", source="runtime", name="current_date", content="today"),
        Message(role="user", source="runtime", name="tool_descriptions", content="schema"),
    ]
    user = Message(role="user", content=engine._SUMMARY_MARKER + "\nA literal quotation.")
    history = [system, old, boundary, ordinary[0], user, ordinary[1], Message(role="assistant", content="done")]
    for seq, message in enumerate(history[1:], 10):
        message._surface_seq = seq
    result = await engine._maybe_summarize(SummaryProvider(), history, 50_000, 0, False, force=True)
    assert result.plan.cut_index == 3 and result.plan.cut_seq == 12
    assert result.plan.source_seqs == tuple(range(10, 16))
    assert result.messages == [system, result.plan.head, *history[3:]]
    assert all(left is right for left, right in zip(result.messages[2:], history[3:], strict=True))
    assert result.plan.rewrites == {} and result.plan.appends == []
    assert result.plan.head.summary_body == "Earlier work."
    assert result.plan.head.earlier_user_messages == ["first task"]
    assert engine._is_real_user_message(user)
    assert result.summary_input == (10, 15) and result.summary_omitted == []


async def test_only_assistant_round_is_kept_if_it_fits_and_dropped_only_under_target_pressure():
    history = [Message(role="system", content="system"),
               Message(role="assistant", content="材料" * 12_000),
               Message(role="user", content="continue")]
    fits = await engine._maybe_summarize(SummaryProvider(), history, 52_000, 0, False, force=True)
    assert fits.plan is None and fits.messages is None and not fits.blocked
    tight = await engine._maybe_summarize(SummaryProvider(), history, 52_000, 0, False, force=True,
                                         request_headroom_tokens=42_000)
    assert tight.plan.cut_index == 2 and tight.degradation["droppedRounds"] == 1
    assert tight.messages[2:] == history[2:]


def test_normal_runtime_reference_is_charged_to_recent_tail_allowance():
    runtime = Message(role="user", source="runtime", name="tool_descriptions", content="模式" * 11_000)
    messages = [Message(role="system", content="system"), Message(role="user", content="task"),
                Message(role="assistant", content="early"), runtime,
                Message(role="assistant", content="newest")]
    assert engine._select_recent_messages(messages) == ([messages[-1]], {4})


async def test_compacted_preview_ignores_old_usage_and_accepts_the_next_response_usage():
    from box_agent.schema import TokenUsage

    old = Message(role="assistant", content="old", usage=TokenUsage(prompt_tokens=49_000))
    history = [Message(role="system", content="system"), Message(role="user", content="request"), old]
    result = await engine._maybe_summarize(SummaryProvider(), history, 52_000, 0, False, force=True)
    assert result.messages[-1] is old
    cost, source = engine._estimate_context_from_latest_response(result.messages, None, api_prompt_tokens=49_000)
    assert cost == result.estimated_after and source == "fallback"
    latest = Message(role="assistant", content="answer", usage=TokenUsage(prompt_tokens=813))
    assert engine._estimate_context_from_latest_response([*result.messages, latest], None) == (813, "usage")


async def test_summary_truncation_records_only_seqs_removed_from_the_provider_request():
    from tests.test_context_compaction_recovery import _ContextOverflow, _Summary, _history

    provider = _Summary([_ContextOverflow("too long")])
    provider.context_window = 65_536
    messages = _history(rounds=10, size=60)
    for index, message in enumerate(messages[1:], 1):
        message._surface_seq = 2 * index
    result = await engine._maybe_summarize(provider, messages, 52_000, 0, False, force=True)
    assert result.head_truncated and result.summary_retries == 1
    seen = {m._surface_seq for m in provider.requests[-1] if m._surface_seq is not None}
    assert result.summary_input == (min(seen), max(seen))
    removed = {m._surface_seq for m in messages[1:]} - seen
    assert removed and result.summary_omitted == [(seq, seq) for seq in sorted(removed)]


async def test_identity_projection_copies_keep_durable_user_text_and_do_not_rewrite_the_tail():
    history = [Message(role="system", content="system"), Message(role="user", content="first"),
               Message(role="assistant", content="acknowledged"), Message(role="user", content="current")]
    for seq, message in enumerate(history[1:]):
        message._surface_seq = seq
    effective = [*history[:-1], history[-1].model_copy(update={"content": "request-only overlay"})]

    result = await engine._maybe_summarize(
        SummaryProvider(), effective, 52_000, 0, False, force=True, durable_history=tuple(history),
        compacted_history_projector=lambda messages: [m.model_copy() for m in messages],
    )
    assert result.plan.rewrites == {}
    assert result.messages[-1] is history[-1]

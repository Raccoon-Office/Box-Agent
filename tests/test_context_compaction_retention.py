"""Compaction keeps complete API rounds and durably carries original user text."""

import pytest

from box_agent.kernel import context_engine
from box_agent.kernel.loop import _auto_match_memory_for_latest_prompt, _latest_user_text
from box_agent.llm.token_estimator import estimate_text_tokens
from box_agent.schema import FunctionCall, Message, StreamEvent, ToolCall
from box_agent.session_log import SessionLog


def _tool_round(index, result="result"):
    call = ToolCall(
        id=f"call-{index}",
        type="function",
        function=FunctionCall(name="read_file", arguments={"path": f"sample-{index}"}),
    )
    return [
        Message(role="assistant", content="", tool_calls=[call]),
        Message(role="tool", name="read_file", tool_call_id=call.id, content=result),
    ]


def test_recent_window_counts_assistant_rounds_and_charges_tokens():
    history = [Message(role="system", content="system")]
    for index in range(7):
        history.extend([Message(role="user", content=f"request {index}"), *_tool_round(index)])
    retained, indices = context_engine._select_recent_messages(history)
    assert retained == history[-15:]
    assert indices == set(range(len(history) - 15, len(history)))

    history = [history[0], history[1], *_tool_round(0, "汉" * 8_000),
               *_tool_round(1, "字" * 8_000)]
    retained, _ = context_engine._select_recent_messages(history)
    assert retained == [history[1], *history[-2:]]
    assert context_engine._fallback_context_estimate(retained, None) <= 15_000


class _SummaryProvider:
    async def generate_stream(self, **kwargs):
        yield StreamEvent(type="text", delta="<summary>Earlier conversation.</summary>")
        yield StreamEvent(type="finish", finish_reason="stop")


@pytest.mark.asyncio
async def test_oversized_recent_round_preserves_call_and_all_result_ids():
    user = Message(role="user", content="Please continue.")
    assistant, result = _tool_round(0, "汉" * 20_000)
    extra_assistant, extra_result = _tool_round(1, "字" * 400)
    assistant.tool_calls.extend(extra_assistant.tool_calls)
    older_round = Message(role="assistant", content="Previous answer.")
    messages = [Message(role="system", content="system"), older_round, user,
                assistant, result, extra_result]

    recent, _ = context_engine._select_recent_messages(messages)
    assert recent == [user, assistant, result, extra_result]
    outcome = await context_engine._maybe_summarize(
        _SummaryProvider(), messages, 20_000, 0, False, force=True,
    )
    assert outcome.mode == "summary"
    assert user in outcome.messages
    kept = next(message for message in outcome.messages if message.tool_calls)
    assert kept.tool_calls == assistant.tool_calls
    kept_results = [message for message in outcome.messages if message.role == "tool"]
    assert [message.tool_call_id for message in kept_results] == [result.tool_call_id, extra_result.tool_call_id]
    assert all("Tool result compacted" in message.content for message in kept_results)


def test_earlier_user_token_budget_truncates_middle_and_omits_oldest_text():
    texts = [f"start {index} " + "汉" * 3_000 + f" end {index}" for index in range(8)]
    message = context_engine._earlier_user_message(texts, estimate_text_tokens)
    assert message.earlier_user_messages == texts
    assert "[4 earlier user messages are left out here; see the summary.]" in message.content
    assert "start 3" not in message.content
    assert "start 4" in message.content and "end 7" in message.content
    assert message.content.count(context_engine._EARLIER_USER_MIDDLE) == 4
    for text in texts:
        truncated = context_engine._bound_earlier_user_text(text, estimate_text_tokens)
        assert estimate_text_tokens(truncated) <= 2_000
    assert estimate_text_tokens(message.content) <= 10_000


@pytest.mark.asyncio
async def test_three_compactions_keep_original_user_text_across_session_replay(tmp_path):
    system = Message(role="system", content="system")
    messages = [system]
    root = tmp_path / "sessions"
    session_id = "retained-original-text"
    originals: list[str] = []
    session = SessionLog.create(root, session_id=session_id, cwd=tmp_path)
    for batch in range(3):
        try:
            for index in range(8):
                text = f"Request {batch}-{index}\n  unchanged text. "
                originals.append(text)
                messages.extend([
                    Message(role="user", content="host guidance" if index == 0 else text,
                            input_facts={"blocks": [{"type": "text", "text": text}]} if index == 0 else None),
                    Message(role="assistant", content=f"Answer {batch}-{index}"),
                ])
            messages.insert(-1, Message(role="user", source="runtime", content="Injected runtime guidance"))
            outcome = await context_engine._maybe_summarize(
                _SummaryProvider(), messages, 14_000, 0, False, force=True,
                allow_llm_summary=batch != 1,
            )
            assert outcome.mode == ("fallback" if batch == 1 else "summary")
            assert outcome.messages[0] is system
            original_section = outcome.messages[1]
            assert original_section.source == "runtime"
            assert context_engine._is_summary_marker(outcome.messages[2])
            tail = [message.content for message in outcome.messages if context_engine._is_real_user_message(message)]
            assert original_section.earlier_user_messages + tail == originals
            assert all(text in original_section.content for text in original_section.earlier_user_messages)
            assert "host guidance" not in original_section.content
            assert "Injected runtime guidance" not in original_section.content
            assert _latest_user_text(outcome.messages) == originals[-1]
            session.replace_surface(outcome.messages[1:], turn=batch + 1, step=1)
            session.flush()
        finally:
            session.close()
        session = SessionLog.open(root, session_id=session_id, cwd=tmp_path)
        messages = [system, *session.replay().messages]
        assert messages[1].earlier_user_messages == original_section.earlier_user_messages
    session.close()


@pytest.mark.asyncio
async def test_earlier_user_archive_does_not_drive_memory_recall():
    class Memory:
        def auto_match_context(self, user_text):
            assert user_text == "Current question"
            return []

    archive = context_engine._earlier_user_message(["Prior request"], estimate_text_tokens)
    assert _latest_user_text([archive]) == ""
    await _auto_match_memory_for_latest_prompt(
        [Message(role="user", content="Current question"), archive], Memory(),
    )


@pytest.mark.asyncio
async def test_runtime_state_gives_all_large_sections_a_fair_share():
    class State:
        def __init__(self, label):
            self.label = label

        def compaction_state(self):
            return self.label, f"{self.label} beginning. " + "x" * 20_000 + f" {self.label} ending."

    state = await context_engine._restore_runtime_state(
        [], {label: State(label) for label in ("Todo", "Goal", "Plan")},
    )
    sections = state.content.split("\n\n## ")
    assert [section.splitlines()[0] for section in sections[1:]] == ["Goal", "Plan", "Todo"]
    assert len(state.content) <= 12_000
    assert all(3_900 < len(section) < 4_100 for section in sections[1:])
    for section in sections[1:]:
        label = section.splitlines()[0]
        assert f"{label} beginning." in section and f"{label} ending." in section

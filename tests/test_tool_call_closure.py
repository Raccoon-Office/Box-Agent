"""Regression tests for tool_call/tool_result closure invariants.

Ensures that interrupted, cancelled, or otherwise abnormally-terminated
tool executions still leave the model-facing message history in a state
where every ``assistant.tool_calls[i].id`` is followed by a matching
``role:"tool"`` message — the precondition both OpenAI and Anthropic
APIs enforce on the next request.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from box_agent.core import _sanitize_dangling_tool_calls
from box_agent.correction import CorrectionNotice
from box_agent.kernel.context_engine import _maybe_summarize
from box_agent.kernel.tool_messages import _LEGACY_INTERRUPTED_TOOL_STUB, validate_tool_history
from box_agent.schema import FunctionCall, Message, ToolCall


def _mk_tool_call(tc_id: str, name: str = "noop") -> ToolCall:
    return ToolCall(id=tc_id, type="function", function=FunctionCall(name=name, arguments={}))


def test_sanitize_no_op_when_balanced() -> None:
    messages = [
        Message(role="user", content="hi"),
        Message(
            role="assistant",
            content="",
            tool_calls=[_mk_tool_call("call_a"), _mk_tool_call("call_b")],
        ),
        Message(role="tool", content="ok", tool_call_id="call_a", name="noop"),
        Message(role="tool", content="ok", tool_call_id="call_b", name="noop"),
    ]
    assert _sanitize_dangling_tool_calls(messages) == 0
    assert len(messages) == 4


def test_sanitize_synthesizes_missing_tool_replies() -> None:
    messages = [
        Message(
            role="assistant",
            content="",
            tool_calls=[
                _mk_tool_call("call_a"),
                _mk_tool_call("call_b"),
                _mk_tool_call("call_c"),
                _mk_tool_call("call_d"),
            ],
        ),
        # Only call_a has a response — simulating the SIGKILL-during-gather case
        Message(role="tool", content="ok", tool_call_id="call_a", name="noop"),
    ]
    synthesized = _sanitize_dangling_tool_calls(messages)
    assert synthesized == 3
    tool_ids = [m.tool_call_id for m in messages if m.role == "tool"]
    assert tool_ids == ["call_a", "call_b", "call_c", "call_d"]
    # Stub bodies must be non-empty so providers don't reject the message
    for m in messages:
        if m.role == "tool" and m.tool_call_id != "call_a":
            assert m.content.strip()


def test_sanitize_handles_multiple_assistant_turns() -> None:
    messages = [
        Message(role="assistant", content="", tool_calls=[_mk_tool_call("call_1")]),
        Message(role="tool", content="ok", tool_call_id="call_1", name="noop"),
        Message(role="assistant", content="", tool_calls=[_mk_tool_call("call_2")]),
        # call_2 has no response — interrupted
        Message(role="user", content="follow-up"),
    ]
    synthesized = _sanitize_dangling_tool_calls(messages)
    assert synthesized == 1
    # call_2 stub must be inserted before the user follow-up
    assistant_2_idx = next(
        i for i, m in enumerate(messages)
        if m.role == "assistant" and m.tool_calls and m.tool_calls[0].id == "call_2"
    )
    assert messages[assistant_2_idx + 1].role == "tool"
    assert messages[assistant_2_idx + 1].tool_call_id == "call_2"


def _split_batch(*, source="runtime", with_placeholder=False):
    assistant = Message(
        role="assistant", content="", tool_calls=[_mk_tool_call("a"), _mk_tool_call("b")],
    )
    result_a = Message(role="tool", content="first result", tool_call_id="a")
    result_b = Message(role="tool", content="second result", tool_call_id="b")
    legacy_notice = Message(
        role="user", source=source,
        content=CorrectionNotice("", "tool", "inspect_images", "verification_fixture", "timeout").prompt(),
    )
    placeholder = Message(role="tool", content=_LEGACY_INTERRUPTED_TOOL_STUB, tool_call_id="b")
    return [assistant, result_a, *([placeholder] if with_placeholder else []), legacy_notice, result_b]


@pytest.mark.parametrize("with_placeholder", [False, True])
def test_restore_legacy_correction_batch_preserves_real_results(with_placeholder):
    followup = Message(role="user", content="continue")
    messages = [*_split_batch(with_placeholder=with_placeholder), followup]
    assert _sanitize_dangling_tool_calls(messages) == 0
    assert [message.content for message in messages[1:]] == ["first result", "second result", "continue"]
    assert messages[-1] is followup
    validate_tool_history(messages)
    before = list(messages)
    assert _sanitize_dangling_tool_calls(messages) == 0
    assert messages == before


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["user", "runtime"])
async def test_rewind_rejects_split_batches_except_known_legacy_notices(tmp_path, source):
    from box_agent.agent import Agent
    from box_agent.session_history import revision_request, revise_agent_history
    from box_agent.session_log import SessionLog

    log = SessionLog.create(tmp_path / "sessions", session_id="rewind", cwd=tmp_path)
    agent = Agent(llm_client=SimpleNamespace(), system_prompt="system", tools=[],
                  workspace_dir=str(tmp_path), session_log=log, deferred_mcp_loading_enabled=False)
    try:
        history = [Message(role="user", content="first turn"), *_split_batch(source=source)]
        log.append_unlogged_messages(history, turn=1, step=1)
        log.append("turn/binding", {"turn_id": "second"})
        revision = revision_request(log.events, target_turn_id="second", turn_id="edit",
                                    expected_revision_id="initial", fingerprint="fixture")
        if source == "user":
            with pytest.raises(ValueError, match="HISTORY_TOOL_PAIR_INVALID"):
                await revise_agent_history(agent, revision)
            assert all(event["type"] != "history/revise" for event in log.events)
            assert log.replay().messages == history
        else:
            await revise_agent_history(agent, revision)
            restored = log.replay().messages
            assert [message.content for message in restored if message.role == "tool"] == [
                "first result", "second result",
            ]
            assert agent.messages[1:] == restored
            validate_tool_history(restored)
    finally:
        log.close()


@pytest.mark.asyncio
async def test_compaction_blocks_a_split_tool_batch_before_summary():
    messages = [
        Message(role="system", content="system"),
        Message(role="user", content="first turn"),
        *_split_batch(source="user"),
        Message(role="assistant", content="done"),
    ]
    outcome = await _maybe_summarize(
        SimpleNamespace(), messages, token_limit=500, api_total_tokens=0, skip_check=False, force=True,
    )
    assert outcome.blocked
    assert outcome.summary_calls == 0
    assert outcome.messages is None
    assert "Missing tool results" in outcome.error

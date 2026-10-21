"""Message identities remain additive across ordinary and exceptional outputs."""

import pytest

from box_agent.acp.message_display import MessageDisplay
from box_agent.events import ContentEvent, DoneEvent, LLMOutputEvent, StopReason
from box_agent.injections import InjectionKind, MessageInjection


def test_final_receipt_matches_stream_without_output_snapshot():
    display = MessageDisplay()
    first = display.observe(ContentEvent(content="Hello ", _streaming=True))
    assert display.observe(ContentEvent(content="world", _streaming=True)) == first
    display.observe(DoneEvent(stop_reason=StopReason.END_TURN, final_content="Hello world"))
    assert display.receipt(completed=True, duration_ms=0)["finalMessageIds"] == [first["messageId"]]


def test_snapshot_only_continuation_preserves_answer_and_changes_output_identity():
    display = MessageDisplay()
    first = display.observe(LLMOutputEvent(step=1, content="Hello wor"))
    display.observe(MessageInjection(InjectionKind.TEXT_CONTINUATION, "continue").apply([]))
    second = display.observe(LLMOutputEvent(step=2, content="ld"))
    assert first["messageId"] == second["messageId"]
    assert first["outputId"] != second["outputId"]
    display.observe(DoneEvent(stop_reason=StopReason.END_TURN, final_content="ld"))
    assert display.receipt(completed=True, duration_ms=2)["finalMessageIds"] == [second["messageId"]]


@pytest.mark.parametrize("kind", [InjectionKind.BUDGET, InjectionKind.NO_PROGRESS])
def test_runtime_advice_preserves_pending_text_continuation(kind):
    display = MessageDisplay()
    first = display.observe(LLMOutputEvent(step=1, content="Hello wor"))
    display.observe(MessageInjection(InjectionKind.TEXT_CONTINUATION, "continue").apply([]))
    display.observe(MessageInjection(kind, "wrap up soon").apply([]))
    second = display.observe(LLMOutputEvent(step=2, content="ld"))
    assert first["messageId"] == second["messageId"]
    assert first["outputId"] != second["outputId"]


@pytest.mark.parametrize("kind", [
    InjectionKind.USER_SUPPLEMENT,
    InjectionKind.TASK_CONTINUATION,
    InjectionKind.OUTPUT_RECOVERY,
    InjectionKind.STREAM_RECOVERY,
    InjectionKind.REQUEST_RECOVERY,
])
def test_new_task_or_recovery_cancels_pending_text_continuation(kind):
    display = MessageDisplay()
    first = display.observe(LLMOutputEvent(step=1, content="Hello wor"))
    display.observe(MessageInjection(InjectionKind.TEXT_CONTINUATION, "continue").apply([]))
    display.observe(MessageInjection(kind, "new answer").apply([]))
    second = display.observe(LLMOutputEvent(step=2, content="Replacement"))
    assert first["messageId"] != second["messageId"]


@pytest.mark.parametrize("reason", [StopReason.CANCELLED, StopReason.ERROR, StopReason.MAX_TOKENS])
def test_non_successful_done_cannot_classify_partial_text(reason):
    display = MessageDisplay()
    display.observe(ContentEvent(content="partial", _streaming=True))
    display.observe(LLMOutputEvent(step=1, content="partial"))
    display.observe(DoneEvent(stop_reason=reason, final_content="partial"))
    assert display.receipt(completed=True, duration_ms=1)["finalMessageIds"] == []


def test_tool_preamble_is_distinct_from_snapshot_only_final_answer():
    display = MessageDisplay()
    preamble = display.observe(ContentEvent(content="Checking", _streaming=True))
    display.observe(LLMOutputEvent(step=1, content="Checking", tool_calls=[{"name": "read_file"}]))
    final = display.observe(LLMOutputEvent(step=2, content="Answer"))
    assert preamble["messageId"] != final["messageId"]
    display.observe(DoneEvent(stop_reason=StopReason.END_TURN, final_content="Answer"))
    assert display.receipt(completed=True, duration_ms=1)["finalMessageIds"] == [final["messageId"]]

"""Kernel ownership of final tool replies and the durable call record."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..events import ToolCallResult
from ..schema import Message

if TYPE_CHECKING:
    from .ports import SessionStorePort
    from ..tools.engine.call_contracts import ToolCallRecord


def session_log_messages(messages: list[Message]) -> list[Message]:
    """Use the same durable conversation at request, response and tool commits."""
    return messages[1:] if messages and messages[0].role == "system" else messages


class ToolMessageCommitter:
    def __init__(self, messages: list[Message], session_log: SessionStorePort | None, turn: int | None):
        self.messages = messages
        self.session_log = session_log
        self.turn = turn
        self._parallel_calls_pending = False

    def record_call(self, call: ToolCallRecord, step: int) -> None:
        if self.session_log is None or self.turn is None:
            return
        self.session_log.append("tool/call", {
            "turn": self.turn, "step": step, "callId": call.call_id,
            "name": call.name, "arguments": call.arguments,
            **({"origin": "runtime"} if call.origin == "runtime" else {}),
        })
        if call.parallel:
            self._parallel_calls_pending = True
        else:
            # Serial calls flush immediately; the parallel batch flushes once
            # after all calls are recorded and before starting any of them.
            self.session_log.flush()

    def flush_calls(self) -> None:
        if self._parallel_calls_pending and self.session_log is not None:
            self.session_log.flush()
            self._parallel_calls_pending = False

    def commit_result(self, message: Message, event: ToolCallResult, step: int) -> None:
        self.messages.append(message)
        if self.session_log is None or self.turn is None:
            return
        self.session_log.append_unlogged_messages(
            session_log_messages(self.messages), turn=self.turn, step=step,
            tool_result_metadata={event.tool_call_id: {
                "success": event.success, "content": event.content,
                "error": event.error, "rawOutput": event.raw_output,
                "policyDecision": event.policy_decision,
                **({"stateUpdates": message._commit_state_updates}
                   if message._commit_state_updates is not None else {}),
                **({"origin": "runtime"} if event.origin == "runtime" else {}),
            }},
        )


_INTERRUPTED_TOOL_STUB = (
    "[Tool execution interrupted — no result available. "
    "The previous run was terminated before this tool produced output.]"
)

def _sanitize_dangling_tool_calls(messages: list[Message]) -> int:
    """Synthesize stub tool replies for any assistant.tool_calls lacking a response.

    Heals message histories where a previous turn's tool execution was
    interrupted (process crash, SIGKILL, mid-flight cancellation that skipped
    the result-append path) before every tool response was recorded. Without
    this, the next LLM request would fail with the OpenAI/Anthropic protocol
    error ``assistant message with tool_calls must be followed by tool
    messages``. Returns count of synthesized stubs.
    """
    synthesized = 0
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.role != "assistant" or not msg.tool_calls:
            i += 1
            continue
        seen_ids: set[str] = set()
        j = i + 1
        while j < len(messages) and messages[j].role == "tool":
            if messages[j].tool_call_id:
                seen_ids.add(messages[j].tool_call_id)
            j += 1
        insert_at = j
        for tc in msg.tool_calls:
            if tc.id and tc.id not in seen_ids:
                messages.insert(
                    insert_at,
                    Message(
                        role="tool",
                        content=_INTERRUPTED_TOOL_STUB,
                        tool_call_id=tc.id,
                        name=tc.function.name,
                    ),
                )
                insert_at += 1
                synthesized += 1
        i = insert_at if insert_at > i else i + 1
    return synthesized

def _cleanup_incomplete_messages(messages: list[Message]) -> int:
    """Remove trailing incomplete assistant + tool messages. Returns removed count.

    Called from abort paths (cancel / max_tokens / error / no-output) to leave
    the message list in a state safe to resend to the LLM on the next turn.

    A trailing assistant turn is considered *incomplete* when:
      - It has ``tool_calls`` but the number of trailing tool messages does
        not match (some tool responses are missing).
      - Its content is empty AND it has no tool_calls (an LLM that was cut
        off before emitting anything).

    A trailing assistant turn that has no tool_calls AND has content is
    treated as complete and left in place — deleting it would discard a
    fully-formed answer the LLM already produced.
    """
    last_assistant_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].role == "assistant":
            last_assistant_idx = i
            break
    if last_assistant_idx == -1:
        return 0

    last = messages[last_assistant_idx]
    trailing_tool_count = len(messages) - last_assistant_idx - 1

    expected_tool_count = len(last.tool_calls or [])
    has_content = bool(last.content) or bool(last.thinking)

    is_incomplete = False
    if expected_tool_count > 0:
        # tool_calls present — incomplete unless every call has a tool response
        if trailing_tool_count < expected_tool_count:
            is_incomplete = True
    elif not has_content:
        # Empty assistant turn with no tool_calls → cut off before output
        is_incomplete = True

    if not is_incomplete:
        return 0

    removed = len(messages) - last_assistant_idx
    del messages[last_assistant_idx:]
    return removed

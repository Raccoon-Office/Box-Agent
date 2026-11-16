"""Kernel ownership of final tool replies and the durable call record."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING

from ..correction import CORRECTION_NOTICE_PREFIX
from ..events import ToolCallResult
from ..schema import Message, ToolCall

if TYPE_CHECKING:
    from .ports import SessionStorePort
    from ..tools.engine.call_contracts import ToolCallRecord


def session_log_messages(messages: list[Message]) -> list[Message]:
    """Use the same durable conversation at request, response and tool commits."""
    return messages[1:] if messages and messages[0].role == "system" else messages


class ToolMessageCommitter:
    def __init__(self, messages: list[Message], session_log: SessionStorePort | None, turn: int | None,
                 image_context=None):
        self.messages = messages
        self.session_log = session_log
        self.turn = turn
        self._parallel_calls_pending = False
        self.image_context = image_context

    def record_call(self, call: ToolCallRecord, step: int) -> None:
        if self.session_log is None or self.turn is None:
            return
        self.session_log.append("tool/call", {
            "turn": self.turn, "step": step, "callId": call.call_id,
            "name": call.name, "arguments": call.arguments,
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
        if self.image_context is not None and event.image_content:
            self.image_context.register(message, event.image_content,
                                        source=event.image_source or "tool",
                                        eligible=event.image_context_eligible)
        self.messages.append(message)
        if self.session_log is None or self.turn is None:
            return
        self.session_log.append_unlogged_messages(
            session_log_messages(self.messages), turn=self.turn, step=step,
            tool_result_metadata={event.tool_call_id: {
                "success": event.success, "content": event.content,
                "error": event.error, "rawOutput": event.raw_output,
                "policyDecision": event.policy_decision,
            }},
        )
        if event.image_content:
            self.session_log.flush()


_LEGACY_INTERRUPTED_TOOL_STUB = (
    "[Tool execution interrupted — no result available. "
    "The previous run was terminated before this tool produced output.]"
)
_INTERRUPTED_TOOL_STUB = (
    "[Tool execution interrupted — no result recorded. "
    "The tool may have taken effect. Verify its state before retrying.]"
)


class InvalidToolHistory(ValueError):
    """The history cannot be sent with unambiguous tool call/result pairs."""


def valid_tool_call_ids(calls: Sequence[ToolCall]) -> bool:
    ids = {call.id for call in calls}
    return len(ids) == len(calls) and all(call_id.strip() for call_id in ids)


def tool_message_groups(messages: Sequence[Message], start: int = 0) -> Iterator[range]:
    """Yield atomic history groups, requiring one contiguous result per call."""
    index = start
    while index < len(messages):
        message = messages[index]
        if message.role == "tool":
            raise InvalidToolHistory(f"Unexpected tool result at message {index}")
        calls = message.tool_calls or ()
        end = index + 1
        if calls:
            if message.role != "assistant" or not valid_tool_call_ids(calls):
                raise InvalidToolHistory(f"Invalid tool calls at message {index}")
            pending = {call.id for call in calls}
            while pending and end < len(messages) and messages[end].role == "tool":
                call_id = messages[end].tool_call_id
                if call_id not in pending:
                    raise InvalidToolHistory(f"Unmatched or duplicate tool result at message {end}")
                pending.remove(call_id)
                end += 1
            if pending:
                raise InvalidToolHistory(f"Missing tool results for message {index}")
        yield range(index, end)
        index = end


def validate_tool_history(messages: Sequence[Message]) -> None:
    for _ in tool_message_groups(messages):
        pass


def _legacy_correction_notice(message: Message) -> bool:
    return (
        message.role == "user" and message.source == "runtime"
        and isinstance(message.content, str) and message.content.startswith(CORRECTION_NOTICE_PREFIX)
        and "with memory_write_correction and this verification_id." in message.content
        and "Evidence data: " in message.content
    )


def is_interrupted_tool_reply(message: Message) -> bool:
    return message.role == "tool" and message.content in (_INTERRUPTED_TOOL_STUB, _LEGACY_INTERRUPTED_TOOL_STUB)


def _repair_tool_batch(messages: list[Message], index: int, *, fill_missing: bool) -> tuple[int, int]:
    calls = messages[index].tool_calls or ()
    end = index + 1
    if not valid_tool_call_ids(calls):
        return end, 0
    expected = {call.id for call in calls}
    results: dict[str, Message] = {}
    while end < len(messages):
        result = messages[end]
        if _legacy_correction_notice(result):
            # Old runtimes persisted an ephemeral receipt inside the batch.
            end += 1
            continue
        call_id = result.tool_call_id
        if result.role != "tool" or call_id not in expected:
            break
        previous = results.get(call_id)
        if previous is None or is_interrupted_tool_reply(previous):
            results[call_id] = result
        elif not is_interrupted_tool_reply(result):
            # Two real outcomes for one ID are ambiguous. Leave the second
            # result for strict validation rather than guessing which to keep.
            break
        end += 1

    replies = list(results.values())
    if fill_missing:
        replies.extend(
            Message(role="tool", content=_INTERRUPTED_TOOL_STUB,
                    tool_call_id=call.id, name=call.function.name)
            for call in calls if call.id not in results
        )
    messages[index + 1:end] = replies
    return index + 1 + len(replies), len(replies) - len(results)


def _sanitize_dangling_tool_calls(messages: list[Message], *, fill_missing: bool = True) -> int:
    """Repair interrupted batches and the known legacy correction notice."""
    synthesized = 0
    index = 0
    while index < len(messages):
        message = messages[index]
        if message.role == "assistant" and message.tool_calls:
            index, added = _repair_tool_batch(messages, index, fill_missing=fill_missing)
            synthesized += added
        else:
            index += 1
    return synthesized


def _cleanup_incomplete_messages(messages: list[Message]) -> int:
    """Close an interrupted tool batch or remove an empty assistant message."""
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.role != "assistant":
            continue
        if message.tool_calls:
            _repair_tool_batch(messages, index, fill_missing=True)
        elif not message.content and not message.thinking:
            del messages[index]
            return 1
        break
    return 0

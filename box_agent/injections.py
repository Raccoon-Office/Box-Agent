"""Typed entry point for messages injected into an active conversation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Any
from uuid import uuid4

from .events import InjectedMessageEvent
from .loop_guards import format_injected_message, format_runtime_context_update
from .schema import Message


class InjectionKind(Enum):
    USER_SUPPLEMENT = "user_supplement"
    RUNTIME_STATE = "runtime_state"
    PLAN = "plan"
    BUDGET = "budget"
    NO_PROGRESS = "no_progress"
    TOOL_FEEDBACK = "tool_feedback"
    OUTPUT_RECOVERY = "output_recovery"
    FINAL_RESPONSE = "final_response"
    STREAM_RECOVERY = "stream_recovery"
    REQUEST_RECOVERY = "request_recovery"
    TEXT_CONTINUATION = "text_continuation"
    TASK_CONTINUATION = "task_continuation"


def _self_contained(content: str) -> str:
    return content


# Recovery and continuation producers already supply their complete framing.
_PROMPT_FORMATTERS = {
    InjectionKind.USER_SUPPLEMENT: format_injected_message,
    InjectionKind.RUNTIME_STATE: format_runtime_context_update,
    InjectionKind.PLAN: format_runtime_context_update,
    InjectionKind.BUDGET: format_runtime_context_update,
    InjectionKind.NO_PROGRESS: format_runtime_context_update,
    InjectionKind.TOOL_FEEDBACK: format_runtime_context_update,
    InjectionKind.OUTPUT_RECOVERY: format_runtime_context_update,
    InjectionKind.FINAL_RESPONSE: format_runtime_context_update,
    InjectionKind.STREAM_RECOVERY: _self_contained,
    InjectionKind.REQUEST_RECOVERY: _self_contained,
    InjectionKind.TEXT_CONTINUATION: _self_contained,
    InjectionKind.TASK_CONTINUATION: _self_contained,
}


@dataclass(frozen=True)
class MessageInjection:
    kind: InjectionKind
    content: str
    injection_id: str | None = None
    user_visible: bool | None = None
    source: str | None = None

    def __post_init__(self) -> None:
        expected = "user" if self.kind is InjectionKind.USER_SUPPLEMENT else "runtime"
        if self.source is not None and self.source != expected:
            raise ValueError("injection source does not match its type")
        object.__setattr__(self, "source", expected)

    def apply(self, messages: list[Message]) -> InjectedMessageEvent:
        """Append the model message and return its matching host event."""
        formatted = _PROMPT_FORMATTERS[self.kind](self.content)
        from_user = self.kind is InjectionKind.USER_SUPPLEMENT
        messages.append(Message(
            role="user", source=self.source, content=formatted,
        ))
        return InjectedMessageEvent(
            content=self.content,
            injection_id=self.injection_id,
            user_visible=from_user if self.user_visible is None else self.user_visible,
        )


def parse_queued_injection(item: object) -> MessageInjection | None:
    """Accept the existing string/dict queue interface with its original defaults."""
    if isinstance(item, MessageInjection):
        return item if item.content else None
    kind = InjectionKind.USER_SUPPLEMENT
    injection_id = None
    user_visible = True
    if isinstance(item, dict):
        content = str(item.get("content") or "")
        if isinstance(item.get("id"), str):
            injection_id = item["id"]
        if isinstance(item.get("user_visible"), bool):
            user_visible = item["user_visible"]
        if item.get("source") == "runtime":
            kind = InjectionKind.RUNTIME_STATE
    else:
        content = str(item)
    if not content:
        return None
    return MessageInjection(kind, content, injection_id, user_visible)


class InjectionStatus(Enum):
    PENDING = "pending"
    TAKEN = "taken"
    INJECTED = "injected"
    CANCELLED = "cancelled"
    DISCARDED = "discarded"


class InjectionManager(asyncio.Queue[Any]):
    """Run-scoped injection lifecycle with the legacy asyncio queue interface.

    Access on the owning event loop. Session Log owns durable history; these
    receipts are transient and scoped to ``run_id``.
    """

    def __init__(self) -> None:
        super().__init__()
        self.run_id = str(uuid4())
        self._statuses: dict[str, InjectionStatus] = {}
        self._run_finished = False

    def status(self, injection_id: str) -> InjectionStatus | None:
        return self._statuses.get(injection_id)

    def submit(self, item: object) -> bool:
        """Return false for an empty message or a duplicate within this run."""
        injection = parse_queued_injection(item)
        if injection is None:
            return False
        if injection.kind not in _PROMPT_FORMATTERS:
            raise ValueError("unknown injection type")
        if self._run_finished:
            self.begin_run()
        identifier = injection.injection_id
        if identifier is not None and self.status(identifier) in {
            InjectionStatus.PENDING, InjectionStatus.TAKEN, InjectionStatus.INJECTED,
        }:
            return False
        super().put_nowait(dict(item) if isinstance(item, dict) else item)
        if identifier is not None:
            self._statuses[identifier] = InjectionStatus.PENDING
        return True

    def put_nowait(self, item: Any) -> None:
        self.submit(item)

    def get_nowait(self) -> Any:
        item = super().get_nowait()
        injection = parse_queued_injection(item)
        if injection is not None and injection.injection_id is not None:
            self._statuses[injection.injection_id] = InjectionStatus.TAKEN
        return item

    def cancel(self, injection_id: str) -> bool:
        """Cancel pending input without forgetting already consumed IDs."""
        if self.status(injection_id) is not InjectionStatus.PENDING:
            return False
        kept = []
        removed = False
        while not self.empty():
            item = super().get_nowait()
            injection = parse_queued_injection(item)
            if injection is not None and injection.injection_id == injection_id:
                removed = True
                self.task_done()
            else:
                kept.append(item)
        for item in kept:
            super().put_nowait(item)
            self.task_done()
        if removed:
            self._statuses[injection_id] = InjectionStatus.CANCELLED
        return removed

    def discard_pending(self) -> list[Any]:
        discarded = []
        while not self.empty():
            item = self.get_nowait()
            self.task_done()
            injection = parse_queued_injection(item)
            if injection is not None and injection.injection_id is not None:
                self._statuses[injection.injection_id] = InjectionStatus.DISCARDED
            discarded.append(item)
        return discarded

    def begin_run(self, *, discard_pending: bool = False) -> list[Any]:
        """Start a new scope, optionally dropping stale input at a host boundary."""
        discarded = self.discard_pending() if discard_pending else []
        self._statuses = {
            identifier: status for identifier, status in self._statuses.items()
            if status is InjectionStatus.PENDING
        }
        self.run_id = str(uuid4())
        self._run_finished = False
        return discarded

    def end_run(self) -> None:
        """Retain final receipts until the next run or prequeued submission."""
        self.discard_pending()
        self._run_finished = True

    def apply_next(self, messages: list[Message]) -> tuple[MessageInjection, InjectedMessageEvent]:
        injection = parse_queued_injection(self.get_nowait())
        assert injection is not None
        try:
            event = injection.apply(messages)
        except BaseException:
            if injection.injection_id is not None:
                self._statuses[injection.injection_id] = InjectionStatus.DISCARDED
            raise
        finally:
            self.task_done()
        if injection.injection_id is not None:
            self._statuses[injection.injection_id] = InjectionStatus.INJECTED
        return injection, event

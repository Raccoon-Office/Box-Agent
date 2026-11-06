"""Typed entry point for messages injected into an active conversation."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import json
from typing import Any
from uuid import uuid4

from .events import InjectedMessageEvent
from .loop_guards import format_injected_message, format_runtime_context_update
from .schema import Message
from .input_context import project_input_context


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
    input_context: dict[str, Any] | None = None
    input_facts: dict[str, Any] | None = None
    input_images: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        expected = "user" if self.kind is InjectionKind.USER_SUPPLEMENT else "runtime"
        if self.source is not None and self.source != expected:
            raise ValueError("injection source does not match its type")
        object.__setattr__(self, "source", expected)

    def apply(self, messages: list[Message]) -> InjectedMessageEvent:
        """Append the model message and return its matching host event."""
        formatted = (self.content if self.input_facts is not None else
                     _PROMPT_FORMATTERS[self.kind](project_input_context(self.content, self.input_context or {})))
        from_user = self.kind is InjectionKind.USER_SUPPLEMENT
        messages.append(Message(
            role="user", source=self.source, content=formatted, input_facts=deepcopy(self.input_facts),
        ))
        return InjectedMessageEvent(
            content=self.content,
            # 透传已有语义，不改变注入的正文、来源或用户可见性。
            kind=self.kind.value,
            injection_id=self.injection_id,
            user_visible=from_user if self.user_visible is None else self.user_visible,
        )


def parse_queued_injection(item: object) -> MessageInjection | None:
    """Accept the existing string/dict queue interface with its original defaults."""
    if isinstance(item, MessageInjection):
        return item if item.content or item.input_facts or (item.input_context or {}).get("files") else None
    kind = InjectionKind.USER_SUPPLEMENT
    injection_id = None
    user_visible = True
    input_context = None
    input_facts = None
    input_images = ()
    if isinstance(item, dict):
        content = str(item.get("content") or "")
        if isinstance(item.get("id"), str):
            injection_id = item["id"]
        if isinstance(item.get("user_visible"), bool):
            user_visible = item["user_visible"]
        if item.get("source") == "runtime":
            kind = InjectionKind.RUNTIME_STATE
        input_context = item.get("input_context")
        input_facts = item.get("input_facts")
        input_images = tuple(item.get("input_images", ()))
    else:
        content = str(item)
    if not content and not input_facts and not (input_context or {}).get("files"):
        return None
    return MessageInjection(kind, content, injection_id, user_visible,
                            input_context=input_context, input_facts=input_facts, input_images=input_images)


class InjectionStatus(Enum):
    PENDING = "pending"
    TAKEN = "taken"
    INJECTED = "injected"
    CANCELLED = "cancelled"
    DISCARDED = "discarded"


def submit_user_input(value, *, agent, queue, active=True):
    """Prepare once, with receipt lookup before touching mutable resources."""
    from .api.input import UserInput
    from .input_preparation import prepare_user_input

    value = UserInput.text(value) if isinstance(value, str) else value
    if not isinstance(value, UserInput):
        raise TypeError("input must be a string or UserInput")
    identifier = value.input_id or str(uuid4())
    fingerprint = value.fingerprint()
    receipt = queue.receipt(identifier, fingerprint)
    if receipt is None:
        store = agent.session_log
        for _, message, _ in store.history_messages() if store is not None else ():
            record = message.input_facts
            if record and record.get("kind") == "user_supplement" and record["id"] == identifier:
                if record["fingerprint"] != fingerprint:
                    raise ValueError("INJECTION_IDEMPOTENCY_CONFLICT")
                receipt = InjectionStatus.INJECTED
                break
    if receipt is not None:
        return {"ok": True, "injectionId": identifier, "deduplicated": True, "status": receipt.value}
    if not active:
        raise ValueError("no_active_turn")
    if not value.user_text and not any(block["type"] != "text" for block in value.blocks):
        raise ValueError("empty_text")
    record, images = prepare_user_input(value, agent.workspace_dir,
                                       getattr(agent, "_input_permission_engine", None))
    record.update(id=identifier, kind="user_supplement")
    queue.submit(MessageInjection(InjectionKind.USER_SUPPLEMENT, value.user_text,
                                  identifier, input_facts=record, input_images=tuple(images)))
    return {"ok": True, "injectionId": identifier, "status": "pending"}


class InjectionManager(asyncio.Queue[Any]):
    """Run-scoped injection lifecycle with the legacy asyncio queue interface.

    Access on the owning event loop. Session Log owns durable history; these
    receipts are transient and scoped to ``run_id``.
    """

    def __init__(self) -> None:
        super().__init__()
        self.run_id = str(uuid4())
        self._statuses: dict[str, InjectionStatus] = {}
        self._inputs: dict[str, str] = {}
        self._run_finished = False
        self.prepare_input = None

    def status(self, injection_id: str) -> InjectionStatus | None:
        return self._statuses.get(injection_id)

    def terminal_receipts(self):
        return [(identifier, status.value) for identifier, status in self._statuses.items()
                if status in {InjectionStatus.CANCELLED, InjectionStatus.DISCARDED}]

    def receipt(self, injection_id: str, fingerprint: str) -> InjectionStatus | None:
        status = self.status(injection_id)
        if status is not None and self._inputs.get(injection_id) != fingerprint:
            raise ValueError("INJECTION_IDEMPOTENCY_CONFLICT")
        return status

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
        fingerprint = sha256(json.dumps([
            injection.kind.value, injection.content, injection.user_visible,
            injection.source, injection.input_context,
        ], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if injection.input_facts is not None:
            fingerprint = injection.input_facts["fingerprint"]
        elif (injection.kind is InjectionKind.USER_SUPPLEMENT and not injection.input_context
              and injection.user_visible is not False):
            from .api.input import UserInput
            fingerprint = UserInput.text(injection.content).fingerprint()
        if identifier is not None and self.status(identifier) is not None:
            if fingerprint != self._inputs[identifier]:
                raise ValueError("INJECTION_IDEMPOTENCY_CONFLICT")
            return False
        super().put_nowait(deepcopy(item))
        if identifier is not None:
            self._statuses[identifier] = InjectionStatus.PENDING
            self._inputs[identifier] = fingerprint
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
        if self.status(injection_id) is InjectionStatus.TAKEN:
            self._statuses[injection_id] = InjectionStatus.CANCELLED
            return True
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
        self._inputs = {identifier: self._inputs[identifier] for identifier in self._statuses}
        self.run_id = str(uuid4())
        self._run_finished = False
        return discarded

    def end_run(self) -> None:
        """Retain final receipts until the next run or prequeued submission."""
        self.discard_pending()
        self._run_finished = True

    def apply_next(self, messages: list[Message], *, commit=None) -> tuple[MessageInjection, InjectedMessageEvent]:
        injection = parse_queued_injection(self.get_nowait())
        assert injection is not None
        original_length = len(messages)
        try:
            event = injection.apply(messages)
            if commit is not None:
                commit()
        except BaseException:
            del messages[original_length:]
            if injection.injection_id is not None:
                self._statuses[injection.injection_id] = InjectionStatus.DISCARDED
            raise
        finally:
            self.task_done()
        if injection.injection_id is not None:
            self._statuses[injection.injection_id] = InjectionStatus.INJECTED
        return injection, event

    async def aapply_next(self, messages, *, commit=None, cancelled=lambda: False):
        injection = parse_queued_injection(self.get_nowait())
        assert injection is not None
        temporary = []
        length = len(messages)
        accept_input = None
        try:
            event = injection.apply(temporary)
            if self.prepare_input is not None:
                accept_input = await self.prepare_input(temporary[0], injection)
            if cancelled() or self.status(injection.injection_id) is InjectionStatus.CANCELLED:
                status = InjectionStatus.CANCELLED
            else:
                # No await between the cancellation check, image adoption and commit.
                if accept_input is not None:
                    accept_input()
                messages.extend(temporary)
                if commit is not None:
                    commit()
                status = InjectionStatus.INJECTED
        except BaseException:
            del messages[length:]
            if injection.injection_id is not None:
                self._statuses[injection.injection_id] = InjectionStatus.DISCARDED
            raise
        finally:
            self.task_done()
        if injection.injection_id is not None:
            self._statuses[injection.injection_id] = status
        if status is not InjectionStatus.INJECTED:
            from dataclasses import replace
            event = replace(event, status=status.value)
        return injection, event

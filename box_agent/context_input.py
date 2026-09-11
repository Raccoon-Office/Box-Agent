"""Run-scoped assembly of model input over borrowed session capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .kernel.context_engine import (
    _fallback_context_estimate,
    request_input_tokens, skill_reference_budget_chars,
)
from .schema import Message
from .skill_context import (
    SkillReferenceContext, effective_system_prompt, project_legacy_system, verified_system_suffixes,
)
from .tools.base import ToolResult
from .tools.engine.contracts import PreparedTools


@dataclass(frozen=True, slots=True)
class PreparedContext:
    """One request projection; durable history and tool admission stay external."""

    messages: list[Message]
    context_messages: list[Message]
    references: tuple[dict[str, Any], ...] = ()
    input_tokens: int = 0
    request_only_input_tokens: int = 0
    blocked_reason: str | None = None
    on_committed: Callable[[], None] | None = None
    budget_blocked: bool = False
    on_response: Callable[[], None] | None = None


class DefaultContextEngine:
    """Own request coverage and budgets, borrowing the final resolved services.

    The factory takes no services. Composition binds this run only after
    plugin replacement and Skill/tool source validation have completed.
    """

    def __init__(self, *, system_prompt_projector: Callable[
        [str, str | tuple[str, ...]], str,
    ] = effective_system_prompt) -> None:
        self._system_prompt_projector = system_prompt_projector
        self.references: SkillReferenceContext | None = None
        self.prepared_tools: PreparedTools | None = None
        self._history: list[Message] = []
        self._extra_messages: tuple[Message, ...] = ()
        self._token_limit = 113400
        self._output_tokens = 0
        self._request_reference_tokens = 0
        self._transient_tokens = 0
        self._transient_reserve_tokens = 0
        self._pending_followup_blocks: list[dict[str, Any]] = []

    def configure_run(self, *, skill_engine: Any = None, session_store: Any = None) -> None:
        self.references = (SkillReferenceContext(skill_engine, session_store=session_store)
                           if skill_engine is not None else None)
        self.prepared_tools = None
        self._history = []

    def bind_history(self, messages: list[Message]) -> None:
        """Observe exact tool history before Kernel may compact it; never edit it."""
        self._history = messages
        if self.references is not None:
            self.references.bind_history(messages)

    def project_history(self, messages: list[Message]) -> list[Message]:
        """Effective rules for budgeting/compaction, without reference delivery."""
        suffix = verified_system_suffixes(self.references.runtime) if self.references is not None else ()
        return project_legacy_system(messages, suffix, self._system_prompt_projector)

    @property
    def tool_reader(self):
        return self._read_reference if self.references is not None else None

    def reserve_followup(self, blocks: list[dict[str, Any]]) -> None:
        """Reserve already accepted request-only material during a serial batch."""
        self._pending_followup_blocks.extend(blocks)

    def _can_page(self, names: tuple[str, ...]) -> bool:
        from .tools.skill_tool import GetSkillTool

        assert self.prepared_tools is not None
        for name, target in self.prepared_tools.targets.items():
            if not isinstance(target, GetSkillTool) or self.prepared_tools.validate_call(name) is not None:
                continue
            if all(target.check_access(skill_name) is None for skill_name in names):
                return True
        return False

    def _read_reference(self, name: str, **arguments: Any) -> ToolResult:
        assert self.references is not None
        self.references.bind_history([*self._history, *self._extra_messages])
        # Tool arguments and earlier committed replies may have been added
        # since preparation. Keep the exact offered schema/admission snapshot.
        calls = next((message.tool_calls for message in reversed(self._history)
                      if message.role == "assistant" and message.tool_calls), ())
        committed = {message.tool_call_id for message in self._history if message.role == "tool"}
        envelopes = [Message(role="tool", name=call.function.name, tool_call_id=call.id, content="")
                     for call in calls if call.id not in committed]
        definitions = self.prepared_tools.definitions if self.prepared_tools is not None else ()
        pending = ([Message(role="user", content=list(self._pending_followup_blocks))]
                   if self._pending_followup_blocks else [])
        available = skill_reference_budget_chars(
            self.project_history([*self._history, *envelopes, *self._extra_messages, *pending]), definitions,
            self._token_limit - self._transient_reserve_tokens, self._output_tokens,
        )
        return self.references.read(name, **arguments, budget_chars=max(
            0, available - self._request_reference_tokens * 4,
        ))

    def prepare_request(
        self, messages: list[Message], *, prepared_tools: PreparedTools,
        token_limit: int, output_tokens: int = 0,
        extra_messages: tuple[Message, ...] = (),
        transient_message: Message | None = None, transient_tokens: int = 0,
    ) -> PreparedContext:
        """Project ordinary reference material using the offered tools unchanged."""
        self.bind_history(messages)
        self._pending_followup_blocks = []
        self.prepared_tools = prepared_tools
        self._extra_messages = (*extra_messages, *((transient_message,) if transient_message is not None else ()))
        self._token_limit, self._output_tokens = token_limit, output_tokens
        self._transient_tokens = (max(transient_tokens, _fallback_context_estimate([transient_message], {}))
                                  if transient_message is not None else 0)
        self._transient_reserve_tokens = (max(
            0, self._transient_tokens - _fallback_context_estimate([transient_message], {}),
        ) if transient_message is not None else 0)
        context_messages = self.project_history([*messages, *extra_messages])
        references: tuple[dict[str, Any], ...] = ()
        self._request_reference_tokens = 0
        blocked_reason = None
        budget_blocked = False
        on_committed = None
        on_response = None
        full_request = ([*context_messages, transient_message]
                        if transient_message is not None else context_messages)
        base_input_tokens = (request_input_tokens(full_request, prepared_tools.definitions)
                             + self._transient_reserve_tokens)
        if self.references is not None:
            projection = self.references.prepare_request(
                context_messages,
                can_page=self._can_page,
                defer_delivery=True,
                budget_chars=skill_reference_budget_chars(
                    full_request, prepared_tools.definitions,
                    token_limit - self._transient_reserve_tokens, output_tokens,
                ),
            )
            context_messages, references = projection.messages, projection.references
            blocked_reason = projection.blocked_reason
            budget_blocked = projection.budget_blocked
            on_committed = projection.on_committed
            on_response = projection.on_response
            self._request_reference_tokens = projection.input_tokens
        if (not blocked_reason and base_input_tokens + self._request_reference_tokens > token_limit):
            blocked_reason = "Model input exceeds the safe context budget. Compact context before retrying."
            budget_blocked = True
            on_committed = None
            on_response = None
        provider_messages = ([*context_messages, transient_message]
                             if transient_message is not None else context_messages)
        return PreparedContext(provider_messages, context_messages, references,
                               self._request_reference_tokens,
                               self._request_reference_tokens + self._transient_tokens, blocked_reason,
                               on_committed, budget_blocked, on_response)

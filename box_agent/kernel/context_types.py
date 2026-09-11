"""Inputs and outcomes for replaceable compaction, separate from request Context."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..schema import Message
from ..tools.base import Tool


@dataclass(frozen=True, slots=True)
class CompactionInput:
    """Resolved run inputs; an engine commits no SessionLog state itself.

    token_limit is the safe input threshold. tools restores runtime state;
    estimate_tools is the exact offered schema set used for cost estimation.
    Call before_summary before any model summary or deterministic fallback.
    """
    history: tuple[Message, ...]
    token_limit: int
    llm: Any
    summary_llm: Any | None = None
    tools: dict[str, Tool] | None = None
    api_total_tokens: int = 0
    api_prompt_tokens: int | None = None
    skip_check: bool = False
    allow_llm_summary: bool = True
    session_id: str = ""
    turn_id: str = ""
    title: str = ""
    before_summary: Callable[[int], None] | None = None
    force: bool = False
    estimate_tools: dict[str, Any] | None = None
    summary_input_token_limit: int | None = None


@dataclass(frozen=True)
class CompactionOutcome:
    """Observable result of one context-compaction decision.

    Iteration preserves the historical ``(messages, skip_next, estimate)``
    return contract for callers that have not migrated yet.  ``skip_next`` is
    intentionally always false: every subsequent request must be rechecked.
    """

    messages: list[Message] | None
    estimated_before: int
    estimated_after: int
    mode: str = "none"
    summary_calls: int = 0
    error: str | None = None
    error_type: str | None = None
    trigger_source: str = "none"
    protected_messages: int = 0
    still_over_limit: bool | None = None

    def __post_init__(self) -> None:
        blocked = self.mode == "blocked"
        if self.still_over_limit is not None and self.still_over_limit != blocked:
            raise ValueError("still_over_limit must match the blocked compaction mode")
        object.__setattr__(self, "still_over_limit", blocked)

    @property
    def blocked(self) -> bool:
        return self.mode == "blocked"

    def __iter__(self):
        yield self.messages
        yield False
        yield self.estimated_before

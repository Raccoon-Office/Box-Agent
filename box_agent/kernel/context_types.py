"""Inputs and outcomes for replaceable compaction, separate from request Context."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..events import CompactionDegradation, ErrorEvent
from ..schema import Message
from ..tools.base import Tool

if TYPE_CHECKING:
    from ..tools.engine.contracts import ToolDefinitionView
    from .context_engine import ContextTokenCalibration


# Legacy projectors take history alone. The shared projector also accepts
# max_skill_bodies to rebuild locators and visibility metadata together.
CompactedHistoryProjector = Callable[..., list[Message] | Awaitable[list[Message]]]


@dataclass(slots=True)
class ContextCompactionState:
    """Track compaction health across turns of one live session."""

    step: int = 0
    last_compaction_step: int | None = None
    rapid_refills: int = 0

    @property
    def refilled_rapidly(self) -> bool:
        return self.last_compaction_step is not None and 0 < self.step - self.last_compaction_step <= 3

    def record(self) -> None:
        if self.last_compaction_step is None or self.step > self.last_compaction_step:
            self.rapid_refills = self.rapid_refills + 1 if self.refilled_rapidly else 0
            self.last_compaction_step = self.step


@dataclass(frozen=True, slots=True)
class InitialContextTransition:
    """Commit a new context limit only after its first request fits."""

    token_limit: int
    previous_token_limit: int
    summary_llm: Any
    commit: Callable[[], None]


@dataclass(frozen=True, slots=True)
class CompactionInput:
    """Resolved run inputs; an engine commits no SessionLog state itself.

    token_limit is the safe input threshold. tools restores runtime state;
    estimate_tools is the exact offered schema set used for cost estimation.
    summary_tools and summary_request_history carry the main request prefix.
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
    session_id: str = ""
    turn_id: str = ""
    title: str = ""
    before_summary: Callable[[int], None] | None = None
    force: bool = False
    estimate_tools: dict[str, Any] | None = None
    summary_input_token_limit: int | None = None
    summary_projector: Callable[[list[Message], Any], list[Message]] | None = None
    request_headroom_tokens: int | None = None
    compacted_history_projector: CompactedHistoryProjector | None = None
    compaction_source_projector: Callable[[list[Message]], list[Message]] | None = None
    session_log: Any = None
    calibration: ContextTokenCalibration | None = None
    summary_tools: tuple[ToolDefinitionView, ...] | None = None
    summary_request_history: tuple[Message, ...] | None = None


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
    summary_retries: int = 0
    head_truncated: bool = False
    error_code: int | str | None = None
    summary_path: str | None = None
    degradation: CompactionDegradation | None = None
    provider_error: ErrorEvent | None = None

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

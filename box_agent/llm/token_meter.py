"""Per-turn token accounting via a context-local accumulator.

Every LLM call in the system funnels through :class:`LLMClient`
(``generate`` / ``generate_stream``) — the main multi-step loop, the
Layer-2 summarization call, and the background ``MemoryExtractor``.  By
recording usage at that single choke point into a context-local
accumulator, a consumer can scope one accumulator per turn and read the
combined total when the turn ends.

Scoping model
-------------
``start_token_meter()`` installs a fresh :class:`TokenAccumulator` into a
:class:`~contextvars.ContextVar` and returns its reset token.  Anything
that runs within that context — including ``asyncio.create_task`` children,
which copy the context and therefore observe the *same* accumulator object
— adds to it via :func:`record_usage`.  ``reset_token_meter()`` restores the
previous accumulator; it does not destroy the object, so a late background
task that finishes after reset still mutates the (now-detached) accumulator
harmlessly.

Caveat: the background ``MemoryExtractor`` is fire-and-forget.  Extractions
that have not completed by the time the consumer reads the total (notably
the ``loop_end`` extraction) are not yet reflected — the count is
best-effort for memory, exact for the synchronous main loop and
summarization calls.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

__all__ = [
    "TokenAccumulator",
    "start_token_meter",
    "reset_token_meter",
    "get_token_meter",
    "record_usage",
]


@dataclass
class TokenAccumulator:
    """Running token totals for a single scoped context (e.g. one turn)."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0
    cached_tokens: int = 0
    cache_usage_reported_calls: int = 0

    def add(self, usage: Any) -> None:
        """Fold one provider usage record into the running totals.

        ``usage`` is a ``TokenUsage`` (or any object exposing the same
        attributes); ``None`` is ignored so callers need not guard.
        """

        if usage is None:
            return
        self.prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
        self.completion_tokens += getattr(usage, "completion_tokens", 0) or 0
        self.total_tokens += getattr(usage, "total_tokens", 0) or 0
        if getattr(usage, "cache_read_input_tokens_reported", False):
            self.cached_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
            self.cache_usage_reported_calls += 1
        self.calls += 1

    def merge(self, other: TokenAccumulator | None) -> None:
        """Fold a separately scoped accumulator into this one."""
        if other is None:
            return
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens
        self.cached_tokens += other.cached_tokens
        self.cache_usage_reported_calls += other.cache_usage_reported_calls
        self.calls += other.calls


_METER: ContextVar[TokenAccumulator | None] = ContextVar(
    "box_agent_token_meter", default=None
)


def start_token_meter() -> Token:
    """Install a fresh accumulator for the current context; return reset token."""

    return _METER.set(TokenAccumulator())


def reset_token_meter(token: Token) -> None:
    """Restore the accumulator that was active before :func:`start_token_meter`."""

    _METER.reset(token)


def get_token_meter() -> TokenAccumulator | None:
    """Return the accumulator active in the current context, or ``None``."""

    return _METER.get()


def record_usage(usage: Any) -> None:
    """Add a usage record to the active accumulator (no-op if none is active)."""

    meter = _METER.get()
    if meter is not None:
        meter.add(usage)

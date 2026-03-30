"""Structured event types emitted by the agent execution core.

All agent loop consumers (CLI, ACP, JSON-RPC) receive these events
instead of performing their own LLM call / tool execution logic.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Union


class StopReason(str, Enum):
    """Why the agent loop terminated."""

    END_TURN = "end_turn"
    MAX_STEPS = "max_steps"
    CANCELLED = "cancelled"
    ERROR = "error"


# ── Step lifecycle ──────────────────────────────────────────────


@dataclass(frozen=True)
class StepStart:
    """Beginning of an agent step (one LLM call + tool execution cycle)."""

    step: int
    max_steps: int


@dataclass(frozen=True)
class StepEnd:
    """Step completed."""

    step: int
    elapsed_seconds: float
    total_elapsed_seconds: float


# ── LLM output ──────────────────────────────────────────────────


@dataclass(frozen=True)
class ThinkingEvent:
    """Extended thinking content from the LLM."""

    content: str


@dataclass(frozen=True)
class ContentEvent:
    """Text response content from the LLM."""

    content: str


@dataclass(frozen=True)
class TokenUsageEvent:
    """Token usage reported by the API after an LLM call."""

    total_tokens: int


# ── Tool execution ──────────────────────────────────────────────


@dataclass(frozen=True)
class ToolCallStart:
    """LLM requested a tool call."""

    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolCallResult:
    """Tool execution completed."""

    tool_call_id: str
    tool_name: str
    success: bool
    content: str
    error: str | None = None


# ── Safety ──────────────────────────────────────────────────────


@dataclass
class ConfirmationRequired:
    """Safety layer requests user confirmation before proceeding.

    The consumer MUST call ``respond.set_result(True/False)`` so the
    core can continue.  If nobody responds within the timeout the core
    treats it as a rejection.

    TODO: Not yet yielded by ``core.run_agent_loop``.  Currently,
    safety confirmation still happens inside tool implementations via
    ``safety.ask_user_confirmation()`` (blocking ``input()``).  A future
    phase should intercept safety checks in the core and yield this
    event instead, so ACP and other non-terminal consumers can handle
    confirmation through their own protocol.
    """

    tool_call_id: str
    tool_name: str
    message: str
    respond: asyncio.Future = field(repr=False)


# ── Artifacts (structured file/image output) ────────────────────


@dataclass(frozen=True)
class ArtifactEvent:
    """A file or image produced by tool execution (e.g. sandbox plot).

    Consumers use this to present rich previews instead of plain-text
    placeholders like ``[foo.png]``.

    Attributes:
        tool_call_id: The tool call that produced this artifact.
        artifact_type: One of ``"image"``, ``"file"``, ``"plot"``.
        filename: Original filename (e.g. ``"chart.png"``).
        path: Absolute path inside the sandbox workspace.
        mime_type: MIME type if known (e.g. ``"image/png"``).
        size_bytes: File size in bytes, or -1 if unknown.
    """

    tool_call_id: str
    artifact_type: str  # "image" | "file" | "plot"
    filename: str
    path: str
    mime_type: str = "application/octet-stream"
    size_bytes: int = -1


# ── Summarization ───────────────────────────────────────────────


@dataclass(frozen=True)
class SummarizationEvent:
    """Message history is being summarized to stay within token limits."""

    estimated_tokens: int
    api_tokens: int
    token_limit: int


# ── Errors & completion ─────────────────────────────────────────


@dataclass(frozen=True)
class ErrorEvent:
    """An error occurred during the agent loop."""

    message: str
    is_fatal: bool = False
    exception: Exception | None = field(default=None, repr=False)


@dataclass(frozen=True)
class LogFileEvent:
    """Log file path for this run."""

    path: str


@dataclass(frozen=True)
class DoneEvent:
    """Agent loop finished."""

    stop_reason: StopReason
    final_content: str


# ── Memory ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class MemoryEvent:
    """Memory operation event."""

    action: str  # "recall" | "save_summary" | "update_manual"
    session_id: str = ""
    detail: str = ""


# ── Union type ──────────────────────────────────────────────────

AgentEvent = Union[
    StepStart,
    StepEnd,
    ThinkingEvent,
    ContentEvent,
    TokenUsageEvent,
    ToolCallStart,
    ToolCallResult,
    ArtifactEvent,
    ConfirmationRequired,
    SummarizationEvent,
    MemoryEvent,
    ErrorEvent,
    LogFileEvent,
    DoneEvent,
]

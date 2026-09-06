"""Kernel-owned contracts for the runtime capabilities used by the loop."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Iterator
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ..schema import LLMResponse, Message, StreamEvent
from ..tools.base import Tool


@runtime_checkable
class SummaryLLMPort(Protocol):
    """Non-streaming model operation used to summarize context."""

    async def generate(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        *,
        thinking_enabled: bool = False,
        session_id: str = "",
        turn_id: str = "",
        title: str = "",
        call_kind: str = "",
    ) -> LLMResponse: ...


@runtime_checkable
class LLMPort(Protocol):
    """Streaming model operation consumed by the main loop.

    Context summarization is a separate, conditional capability represented by
    ``SummaryLLMPort``.  Keeping the contracts separate preserves the existing
    public behavior for streaming-only LLM implementations until a summary is
    actually required.
    """

    def generate_stream(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        *,
        thinking_enabled: bool = False,
        session_id: str = "",
        turn_id: str = "",
        title: str = "",
        call_kind: str = "",
    ) -> AsyncIterator[StreamEvent]: ...


@runtime_checkable
class PermissionGatewayPort(Protocol):
    """Host permission decision used by permission retry handling."""

    async def negotiate(self, permission_request: dict[str, Any]) -> bool: ...


@runtime_checkable
class MemoryLookupPort(Protocol):
    """Read-only prompt matching supplied by the memory manager."""

    def auto_match_context(
        self,
        query: str,
        *,
        limit: int = 3,
    ) -> list[dict[str, str]]: ...


@runtime_checkable
class MemoryExtractionPort(Protocol):
    """Lifecycle-triggered memory extraction."""

    async def maybe_extract(
        self,
        messages: list[Message],
        trigger: str,
        *,
        turn_id: str | None = None,
    ) -> bool: ...


@runtime_checkable
class MemoryPromotionPort(Protocol):
    """Candidate lookup and plan operations used for memory promotion."""

    def list_promotion_candidates(
        self,
        *,
        hit_threshold: int,
        cooldown_days: int,
    ) -> list[Any]: ...

    def mark_proposed(self, candidate_ids: list[str]) -> None: ...

    def read_all_context_entries(self) -> list[Any]: ...

    async def plan_promotion(
        self,
        candidates: list[Any],
        llm: LLMPort,
    ) -> Any | None: ...


@runtime_checkable
class SessionStorePort(Protocol):
    """Durable session operations already called by the kernel."""

    def append(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        surface_op: str | dict[str, Any] | None = None,
        source_event_seqs: list[int] | None = None,
        ignorable: bool = False,
    ) -> dict[str, Any]: ...

    def append_unlogged_messages(
        self,
        messages: list[Message],
        *,
        turn: int,
        step: int | None,
        tool_result_metadata: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]: ...

    def replace_surface(
        self,
        messages: list[Message],
        *,
        turn: int,
        step: int,
    ) -> list[dict[str, Any]]: ...

    def flush(self) -> None: ...


@runtime_checkable
class HookBusPort(Protocol):
    """Lifecycle notification and tool interception operations."""

    @property
    def hooks(self) -> list[Any]: ...

    async def fire_agent_start(
        self,
        *,
        messages: list[Message],
        tools: dict[str, Tool],
        max_steps: int,
    ) -> None: ...

    async def fire_step_start(self, *, step: int, max_steps: int) -> None: ...

    async def fire_llm_response(self, *, response: LLMResponse) -> None: ...

    async def fire_step_end(
        self,
        *,
        step: int,
        elapsed_seconds: float,
        total_elapsed_seconds: float,
    ) -> None: ...

    async def fire_done(self, *, stop_reason: Any, final_content: str) -> None: ...

    async def fire_error(
        self,
        *,
        message: str,
        is_fatal: bool,
        exception: Exception | None,
    ) -> None: ...

    async def fire_tool_start(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]: ...

    async def fire_tool_result(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        success: bool,
        content: str,
        error: str | None,
    ) -> tuple[str, str | None]: ...


@runtime_checkable
class ToolCatalogPort(Protocol):
    """Stable collection operations used with existing ``Tool`` objects."""

    def __contains__(self, name: object) -> bool: ...

    def __getitem__(self, name: str) -> Tool: ...

    def __setitem__(self, name: str, tool: Tool) -> None: ...

    def __delitem__(self, name: str) -> None: ...

    def __iter__(self) -> Iterator[str]: ...

    def __len__(self) -> int: ...

    def values(self) -> Iterable[Tool]: ...

    def get(self, name: str) -> Tool | None: ...


@runtime_checkable
class ToolExposureResultPort(Protocol):
    """Kernel-owned view of one per-step tool exposure result."""

    tools: list[Tool]
    mcp_generations: dict[str, int]


@runtime_checkable
class ToolExposurePort(Protocol):
    """Per-step dynamic tool exposure and stale-offer validation."""

    def prepare_tools(self, candidates: list[Tool]) -> ToolExposureResultPort: ...

    def validate_call(
        self,
        name: str,
        offered_generation: int | None,
        target_tool: Tool | None = None,
    ) -> str | None: ...


@runtime_checkable
class ToolResultStorePort(Protocol):
    """Oversized-result persistence and fresh-result budgeting."""

    aggregate_budget: int

    def set_context_token_limit(self, token_limit: int) -> None: ...

    def initialize_history(self, messages: list[Message]) -> None: ...

    def process_message(
        self,
        message: Message,
        *,
        tool: Tool | None,
        session_id: str = "",
        persistence_content: str | list[dict[str, Any]] | None = None,
        content_already_processed: bool = False,
    ) -> Message: ...

    def enforce_fresh_budget(
        self,
        messages: list[Message],
        *,
        tools: dict[str, Tool],
        session_id: str = "",
    ) -> "ToolResultBudgetOutcomePort": ...


@runtime_checkable
class ToolResultBudgetOutcomePort(Protocol):
    """Kernel-owned fields read from a fresh-result budget pass."""

    persisted_count: int
    fresh_count: int
    original_chars: int
    remaining_chars: int


@dataclass(frozen=True, slots=True)
class KernelServices:
    """Resolved per-run capabilities consumed directly by the kernel."""

    llm: LLMPort
    summary_llm: SummaryLLMPort | None
    permission_gateway: PermissionGatewayPort | None
    memory_lookup: MemoryLookupPort | None
    memory_extraction: MemoryExtractionPort | None
    memory_promotion: MemoryPromotionPort | None
    session_store: SessionStorePort | None
    hook_bus: HookBusPort
    tool_catalog: ToolCatalogPort
    tool_exposure: ToolExposurePort | None
    tool_result_store: ToolResultStorePort | None


__all__ = [
    "HookBusPort",
    "KernelServices",
    "LLMPort",
    "MemoryExtractionPort",
    "MemoryLookupPort",
    "MemoryPromotionPort",
    "PermissionGatewayPort",
    "SessionStorePort",
    "SummaryLLMPort",
    "ToolCatalogPort",
    "ToolExposureResultPort",
    "ToolExposurePort",
    "ToolResultBudgetOutcomePort",
    "ToolResultStorePort",
]

import json
from dataclasses import dataclass
from enum import Enum
from functools import cached_property
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field, PrivateAttr


class LLMProvider(str, Enum):
    """LLM provider types."""

    ANTHROPIC = "anthropic"
    OPENAI = "openai"


class StreamEvent(BaseModel):
    """A single chunk from the LLM streaming response.

    Attributes:
        type: "thinking", "text", or "finish".
        delta: Incremental text for thinking/text chunks. Empty on finish.
        finish_reason: Only set when type == "finish" (e.g. "end_turn", "tool_use").
        usage: Token usage, only set on the finish event.
        tool_calls: Accumulated tool calls, only set on the finish event.
    """

    type: str  # "thinking" | "text" | "finish"
    delta: str = ""
    finish_reason: str | None = None
    usage: "TokenUsage | None" = None
    tool_calls: "list[ToolCall] | None" = None
    # Response object id from the provider payload (for example
    # ``chatcmpl-...`` for OpenAI-compatible APIs or ``msg_...`` for Anthropic).
    provider_response_id: str | None = None
    # HTTP/gateway request correlation id from response metadata/headers.
    provider_request_id: str | None = None
    # Tool calls dropped because their streamed arguments were truncated
    # mid-flight (relay/provider hit max_tokens). Each entry: {"name", "arguments_len"}.
    # Surfaced so the agent loop can report *what* was being written when cut off.
    truncated_tool_calls: "list[dict[str, Any]] | None" = None
    # Upstream ``finish_reason`` before local truncation-detection overrides it.
    # ``None`` distinguishes "gateway never sent one" from "gateway said stop".
    raw_finish_reason: str | None = None
    # True when tool_call arguments are unparseable AND the upstream stream
    # ended without a ``finish_reason`` — i.e. the connection dropped mid
    # tool-call rather than the model hitting an output cap.
    stream_dropped_mid_tool: bool = False
    # Tool calls stopped locally while their raw JSON arguments were still
    # streaming. They were never parsed or executed.
    oversized_tool_calls: "list[dict[str, Any]] | None" = None
    # Structured, host-invisible liveness/progress metadata.
    activity: "dict[str, Any] | None" = None


class FunctionCall(BaseModel):
    """Function call details."""

    name: str
    arguments: dict[str, Any]  # Function arguments as dict


class ToolCall(BaseModel):
    """Tool call structure."""

    id: str
    type: str  # "function"
    function: FunctionCall


@dataclass(frozen=True, slots=True)
class RequestUsageSnapshot:
    """Measured-request fingerprints, private to runtime and Session Log replay."""

    route: str
    history: tuple[str, ...]
    tools: dict[str, str]
    request_only: tuple[str, ...] = ()


class TokenUsage(BaseModel):
    """Token usage statistics from one real provider response."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    # Internal compaction metadata. Keep the established serialized usage
    # payload (prompt/completion/total) stable for hosts and trace consumers.
    input_tokens: int = Field(default=0, exclude=True)
    output_tokens: int = Field(default=0, exclude=True)
    # Optional provider detail for diagnostics; already included in completion_tokens.
    reasoning_tokens: int | None = Field(default=None, ge=0, exclude=True)
    cache_creation_input_tokens: int = Field(default=0, exclude=True)
    cache_read_input_tokens: int = Field(default=0, exclude=True)
    cache_read_input_tokens_reported: bool = Field(default=False, exclude=True)
    _request_snapshot: RequestUsageSnapshot | None = PrivateAttr(default=None)
    _request_route_unverified: bool = PrivateAttr(default=False)

    def reported_usage(self) -> dict[str, int]:
        """Return provider totals with cache reads when the provider reported them."""

        usage = self.model_dump(
            include={"prompt_tokens", "completion_tokens", "total_tokens"}
        )
        if self.cache_read_input_tokens_reported:
            usage["cached_tokens"] = self.cache_read_input_tokens
        return usage

    @property
    def context_tokens(self) -> int:
        """Return the complete context size represented by this response."""

        explicit = (
            self.input_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
            + self.output_tokens
        )
        if explicit > 0:
            return explicit
        compatible_total = self.prompt_tokens + self.completion_tokens
        return compatible_total if compatible_total > 0 else self.total_tokens


class Message(BaseModel):
    """Chat message."""

    # Request traces and byte estimates consume the rendered content.
    # Session Log writes retain the additional material used to reconstruct it.
    PROJECTION_METADATA: ClassVar[frozenset[str]] = frozenset({
        "host_connector_status", "earlier_user_messages", "skill_compaction_ranges",
        "compaction_node", "summary_body", "summary_visible_from", "summary_latest_full",
        "fallback_user_count", "omitted_ranges",
    })
    role: str  # "system", "user", "assistant", "tool"
    source: Literal["user", "runtime"] = Field(default="user", exclude=True)
    runtime_kind: Literal["skill_catalog", "tool_descriptions", "compaction_notice"] | None = None
    content: str | list[dict[str, Any]]  # Can be string or list of content blocks
    thinking: str | None = None  # Extended thinking content for assistant messages
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None  # For tool role
    usage: TokenUsage | None = None
    # Semantic tool projections and delivery diagnostics have already been
    # bounded. Preserve their first-send budget decision across Session Log replay.
    tool_result_budget_exempt: bool | None = None
    # Reconstructed from the main request/header sequence on replay. A provider
    # may have received this result even when no assistant reply was committed.
    _tool_result_submitted: bool = PrivateAttr(default=False)
    # Estimated request-only input included in this response's provider usage
    # but intentionally absent from the next durable request. Persist the
    # numeric adjustment so context estimation remains correct after resume.
    request_only_input_tokens: int = Field(default=0, ge=0)
    # Current trusted host snapshot, even when unchanged and omitted from text.
    # Persist for change detection/recovery; never infer state from user XML.
    host_connector_status: str | None = None
    # Durable original input and resource associations; only ContextEngine
    # derives the provider view. Older messages have no such record.
    input_facts: dict[str, Any] | None = None
    # Original user texts carried across compaction boundaries by the Session Log.
    # Only the bounded rendering in content is sent to the model.
    earlier_user_messages: list[str] | None = None
    # Durable compaction structure. Only the rendering in content is sent to the model.
    compaction_node: Literal["summary_head", "fallback_head", "appended"] | None = None
    summary_body: str | None = None
    # Archive entries before this index are hidden. A latest user original
    # can be rendered in full even when earlier entries are bounded.
    summary_visible_from: int | None = None
    summary_latest_full: bool | None = None
    # Last N entries in a mixed fallback head are separate user anchors.
    fallback_user_count: int | None = None
    omitted_ranges: list[tuple[int, int]] | None = None
    # Verified source identity and line/text spans in an [Invoked Skills]
    # runtime message. Persist for coverage checks after Session Log replay.
    skill_compaction_ranges: tuple[dict[str, Any], ...] | None = None
    # Request-only multimodal overlays may carry raw image bytes to a provider
    # without persisting them in durable traces. Excluded from serialization so
    # this remains an internal observability directive.
    trace_redact_content: bool = Field(default=False, exclude=True)

    # Store these projection positions in __dict__ so they survive copies
    # without affecting Pydantic message equality or serialized model fields.
    @cached_property
    def _surface_seq(self) -> int | None:
        """Sequence of the log event that currently occupies this position."""
        return None

    @cached_property
    def _compacted_tail_length(self) -> int | None:
        """Number of retained nodes with obsolete usage before a plan is replayed."""
        return None

    def model_content(self) -> str | list[dict[str, Any]]:
        """Label host references without rewriting persisted message content."""
        # The date record already has a durable name in the Session Log.
        runtime_kind = self.runtime_kind or ("current_date" if self.name == "current_date" else None)
        if (self.role == "user" and self.source == "runtime"
                and runtime_kind is not None and isinstance(self.content, str)):
            if runtime_kind == "tool_descriptions":
                try:
                    descriptor = json.loads(self.content)
                except json.JSONDecodeError:
                    descriptor = None
                if isinstance(descriptor, dict):
                    descriptor.update(source=self.source, runtime_kind=runtime_kind)
                    return json.dumps(descriptor, ensure_ascii=False)
            return f"[Host runtime reference source=runtime kind={runtime_kind}]\n{self.content}"
        return self.content


class LLMResponse(BaseModel):
    """LLM response."""

    content: str
    thinking: str | None = None  # Extended thinking blocks
    tool_calls: list[ToolCall] | None = None
    finish_reason: str
    usage: TokenUsage | None = None  # Token usage from API response
    provider_response_id: str | None = None
    # See StreamEvent.truncated_tool_calls — propagated for diagnostics on
    # finish_reason in ("length", "max_tokens").
    truncated_tool_calls: list[dict[str, Any]] | None = None
    # See StreamEvent.raw_finish_reason / stream_dropped_mid_tool — propagated
    # so the agent loop can pick a repair strategy per truncation cause.
    raw_finish_reason: str | None = None
    stream_dropped_mid_tool: bool = False
    oversized_tool_calls: list[dict[str, Any]] | None = None

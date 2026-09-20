from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


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


class TokenUsage(BaseModel):
    """Token usage statistics from one real provider response."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    # Internal compaction metadata. Keep the established serialized usage
    # payload (prompt/completion/total) stable for hosts and trace consumers.
    input_tokens: int = Field(default=0, exclude=True)
    output_tokens: int = Field(default=0, exclude=True)
    cache_creation_input_tokens: int = Field(default=0, exclude=True)
    cache_read_input_tokens: int = Field(default=0, exclude=True)

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

    role: str  # "system", "user", "assistant", "tool"
    source: Literal["user", "runtime"] = Field(default="user", exclude=True)
    content: str | list[dict[str, Any]]  # Can be string or list of content blocks
    thinking: str | None = None  # Extended thinking content for assistant messages
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None  # For tool role
    usage: TokenUsage | None = None
    # Estimated request-only input included in this response's provider usage
    # but intentionally absent from the next durable request. Persist the
    # numeric adjustment so context estimation remains correct after resume.
    request_only_input_tokens: int = Field(default=0, ge=0)
    # Request-only multimodal overlays may carry raw image bytes to a provider
    # without persisting them in durable traces. Excluded from serialization so
    # this remains an internal observability directive.
    trace_redact_content: bool = Field(default=False, exclude=True)


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

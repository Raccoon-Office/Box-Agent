from enum import Enum
from typing import Any

from pydantic import BaseModel


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


class Message(BaseModel):
    """Chat message."""

    role: str  # "system", "user", "assistant", "tool"
    content: str | list[dict[str, Any]]  # Can be string or list of content blocks
    thinking: str | None = None  # Extended thinking content for assistant messages
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None  # For tool role
    # Internal canonical state carried across history transforms. LLM adapters
    # intentionally do not serialize this field to provider message payloads.
    state_checkpoint: str | None = None


class TokenUsage(BaseModel):
    """Token usage statistics from LLM API response."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


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

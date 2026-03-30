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

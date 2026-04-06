"""Box Agent - Minimal single agent with basic tools and MCP support."""

from .agent import Agent
from .events import AgentEvent, StopReason
from .llm import LLMClient
from .schema import FunctionCall, LLMProvider, LLMResponse, Message, ToolCall

__version__ = "0.7.8"

__all__ = [
    "Agent",
    "AgentEvent",
    "StopReason",
    "LLMClient",
    "LLMProvider",
    "Message",
    "LLMResponse",
    "ToolCall",
    "FunctionCall",
]

"""Box Agent - Minimal single agent with basic tools and MCP support."""

from .agent import Agent
from .events import AgentEvent, StopReason
from .hooks import BaseHook, HookManager, load_hooks
from .llm import LLMClient
from .schema import FunctionCall, LLMProvider, LLMResponse, Message, ToolCall

__version__ = "0.8.23"

__all__ = [
    "Agent",
    "AgentEvent",
    "BaseHook",
    "FunctionCall",
    "HookManager",
    "LLMClient",
    "LLMProvider",
    "LLMResponse",
    "Message",
    "StopReason",
    "ToolCall",
    "load_hooks",
]

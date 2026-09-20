"""Box Agent - Minimal single agent with basic tools and MCP support."""

import sys
from pathlib import Path

from .agent import Agent, AgentRunOptions
from .agent_service import AgentService
from .sdk import AgentClient
from .run_control import PermissionBroker, RunControl
from .api import (
    ControlCommand,
    ControlCommandKind,
    EventEnvelope,
    RunRequest,
    RunResult,
    RunStatus,
)
from .events import AgentEvent, StopReason
from .hooks import BaseHook, HookManager, load_hooks
from .llm import LLMClient
from .schema import FunctionCall, LLMProvider, LLMResponse, Message, ToolCall

__version__ = "0.9.8"


def _frozen_runtime_version(default: str) -> str:
    """Use the outer runtime bundle version when running a frozen ACP binary."""
    if not getattr(sys, "frozen", False):
        return default
    version_path = Path(sys.executable).resolve().parent.parent / "VERSION"
    try:
        bundled_version = version_path.read_text(encoding="utf-8").strip()
    except OSError:
        return default
    return bundled_version or default


__version__ = _frozen_runtime_version(__version__)

__all__ = [
    "Agent",
    "AgentClient",
    "AgentEvent",
    "AgentRunOptions",
    "AgentService",
    "ControlCommand",
    "ControlCommandKind",
    "EventEnvelope",
    "BaseHook",
    "FunctionCall",
    "HookManager",
    "LLMClient",
    "LLMProvider",
    "LLMResponse",
    "Message",
    "RunRequest",
    "RunResult",
    "RunControl",
    "PermissionBroker",
    "RunStatus",
    "StopReason",
    "ToolCall",
    "load_hooks",
]

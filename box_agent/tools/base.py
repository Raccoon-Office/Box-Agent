"""Base tool classes."""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel


class ToolResult(BaseModel):
    """Tool execution result."""

    success: bool
    content: str = ""
    error: str | None = None
    permission_request: dict | None = None  # capability request payload
    raw_output: dict | None = None  # optional structured payload for host UIs
    model_context: str | None = None  # optional compact content for future LLM turns
    state_checkpoint: str | None = None  # optional canonical state for compaction


class Tool:
    """Base class for all tools."""

    parallel_safe: bool = False

    @property
    def name(self) -> str:
        """Tool name."""
        raise NotImplementedError

    @property
    def description(self) -> str:
        """Tool description."""
        raise NotImplementedError

    @property
    def parameters(self) -> dict[str, Any]:
        """Tool parameters schema (JSON Schema format)."""
        raise NotImplementedError

    async def execute(self, *args, **kwargs) -> ToolResult:  # type: ignore
        """Execute the tool with arbitrary arguments."""
        raise NotImplementedError

    def to_schema(self) -> dict[str, Any]:
        """Convert tool to Anthropic tool schema."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }

    def to_openai_schema(self) -> dict[str, Any]:
        """Convert tool to OpenAI tool schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class EventEmittingTool(Tool):
    """Tool that can emit progress events during execution.

    Subclasses call ``_emit(payload)`` to push structured events to a
    shared ``asyncio.Queue``.  The core loop wires the queue before
    execution and drains it in the foreground generator so events are
    yielded to consumers in real-time.
    """

    def __init__(self) -> None:
        # Set by core.py before execution to collect progress events.
        self._event_queue: asyncio.Queue | None = None
        self._parent_tool_call_id: str = ""

    async def execute_with_event_context(
        self,
        *,
        event_queue: asyncio.Queue,
        parent_tool_call_id: str,
        **kwargs: Any,
    ) -> ToolResult:
        """Execute with progress events routed to a parent tool call.

        Subclasses that are truly parallel-safe should override this if they
        need per-call context without shared mutable state.
        """
        previous_queue = self._event_queue
        previous_parent_tool_call_id = self._parent_tool_call_id
        self._event_queue = event_queue
        self._parent_tool_call_id = parent_tool_call_id
        try:
            return await self.execute(**kwargs)
        finally:
            self._event_queue = previous_queue
            self._parent_tool_call_id = previous_parent_tool_call_id

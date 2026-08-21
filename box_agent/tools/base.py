"""Base tool classes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from .schema_validation import (
    ToolArgumentIssue,
    ToolSchemaValidationError,
    validate_tool_arguments,
)


class ToolResult(BaseModel):
    """Tool execution result."""

    success: bool
    content: str = ""
    error: str | None = None
    permission_request: dict | None = None  # capability request payload
    raw_output: dict | None = None  # optional structured payload for host UIs
    model_context: str | None = None  # optional compact content for future LLM turns
    # Complete persistable text when ``content`` is intentionally bounded by
    # the tool. The shared result-storage seam consumes it before the
    # object leaves the execution loop; excluding it avoids duplicating a large
    # payload in serialized host events.
    persistence_content: str | None = Field(default=None, exclude=True)
    # Optional multimodal content a tool asks the loop to deliver to the MAIN
    # model as a follow-up ``user`` message, appended once after the whole tool
    # batch completes (before the next model call). Each entry is a provider-
    # shaped content block (e.g. a text block plus ``image``/``image_url``
    # blocks). This is how a tool hands real images to a vision-capable main
    # model instead of proxying them to a side call. ``None`` (the default)
    # means the loop behaves exactly as before. Excluded from serialization
    # because image payloads must not bloat host events.
    followup_user_content: list[dict] | None = Field(default=None, exclude=True)


@dataclass(frozen=True, slots=True)
class ToolInvocationContext:
    """Optional runtime context hidden behind the tool invocation interface."""

    event_queue: asyncio.Queue | None = None
    parent_tool_call_id: str = ""


class Tool:
    """Base class for all tools."""

    parallel_safe: bool = False
    # Model-facing results above this size are persisted by the shared agent
    # loop. Tools that already self-bound output may opt out with ``math.inf``;
    # they can still hand complete recoverable text to the persistence seam
    # through ``ToolResult.persistence_content``.
    max_result_size_chars: float = 50_000

    def compaction_state(self) -> tuple[str, str] | None:
        """Return trusted read-only runtime state for history compaction."""

        return None

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

    async def invoke(
        self,
        arguments: dict[str, Any],
        *,
        context: ToolInvocationContext | None = None,
    ) -> ToolResult:
        """Validate an invocation and execute the tool implementation."""

        try:
            issues = validate_tool_arguments(self.parameters, arguments)
        except ToolSchemaValidationError:
            return self._invalid_schema_result()
        if issues:
            return self._invalid_arguments_result(issues)
        return await self._invoke_validated(arguments, context=context)

    def _invalid_schema_result(self) -> ToolResult:
        message = "tool parameter schema is invalid"
        return ToolResult(
            success=False,
            error=f"INVALID_TOOL_SCHEMA: {self.name}\n- /: {message}",
            raw_output={
                "code": "INVALID_TOOL_SCHEMA",
                "tool": self.name,
                "issues": [
                    {
                        "path": "/",
                        "keyword": "schema",
                        "message": message,
                    }
                ],
            },
        )

    def _invalid_arguments_result(
        self,
        issues: tuple[ToolArgumentIssue, ...],
    ) -> ToolResult:
        details = "\n".join(
            f"- {issue.path}: {issue.message}" for issue in issues
        )
        return ToolResult(
            success=False,
            error=f"INVALID_TOOL_ARGUMENTS: {self.name}\n{details}",
            raw_output={
                "code": "INVALID_TOOL_ARGUMENTS",
                "tool": self.name,
                "issues": [issue.to_dict() for issue in issues],
            },
        )

    async def _invoke_validated(
        self,
        arguments: dict[str, Any],
        *,
        context: ToolInvocationContext | None,
    ) -> ToolResult:
        return await self.execute(**arguments)

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

    async def _invoke_validated(
        self,
        arguments: dict[str, Any],
        *,
        context: ToolInvocationContext | None,
    ) -> ToolResult:
        if context is not None and context.event_queue is not None:
            return await self.execute_with_event_context(
                event_queue=context.event_queue,
                parent_tool_call_id=context.parent_tool_call_id,
                **arguments,
            )
        return await self.execute(**arguments)

"""PPT tools for structured event emission.

Three tools that let the LLM output structured PPT progress events
(plan updates, outline deltas, HTML deltas) in real-time.  Each tool
validates the payload and emits it via the ``EventEmittingTool`` queue
pattern so consumers (ACP/officev3) receive events as they're produced.
"""

from __future__ import annotations

from typing import Any

from .base import EventEmittingTool, ToolResult


class PPTPlanChatTool(EventEmittingTool):
    """Emit structured PPT plan events for the ``ppt_plan_chat`` session mode."""

    def __init__(self) -> None:
        super().__init__()

    @property
    def name(self) -> str:
        return "ppt_emit_plan"

    @property
    def description(self) -> str:
        return (
            "Emit a structured PPT plan event to the client. Use this tool to "
            "output plan JSON, ask the user a clarifying question, or signal "
            "execution progress (action start/end). The client dispatches on "
            "the 'type' field."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["ppt_plan_json", "ppt_ask_user", "ppt_execution_event"],
                    "description": (
                        "Event type discriminator. "
                        "'ppt_plan_json' — full or partial plan structure (goals/actions). "
                        "'ppt_ask_user' — ask the user a clarifying question (ends current turn). "
                        "'ppt_execution_event' — signal action start/end during execution."
                    ),
                },
                "data": {
                    "type": "object",
                    "description": "Event payload. Structure depends on 'type'.",
                },
            },
            "required": ["type", "data"],
        }

    async def execute(self, type: str, data: dict) -> ToolResult:  # type: ignore[override]
        allowed = {"ppt_plan_json", "ppt_ask_user", "ppt_execution_event"}
        if type not in allowed:
            return ToolResult(
                success=False,
                content="",
                error=f"Invalid type '{type}'. Must be one of: {', '.join(sorted(allowed))}",
            )
        self._emit({"type": type, **data})
        return ToolResult(success=True, content=f"[{type}] event emitted")


class PPTOutlineTool(EventEmittingTool):
    """Emit structured PPT outline events for the ``ppt_outline`` session mode."""

    def __init__(self) -> None:
        super().__init__()

    @property
    def name(self) -> str:
        return "ppt_emit_outline"

    @property
    def description(self) -> str:
        return (
            "Emit a structured PPT outline event to the client. Use this tool to "
            "signal stage transitions, stream outline text deltas, output structured "
            "outline data, or deliver the final outline result."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": [
                        "ppt_outline_stage",
                        "ppt_outline_delta",
                        "ppt_outline_structured",
                        "ppt_outline_result",
                    ],
                    "description": (
                        "Event type discriminator. "
                        "'ppt_outline_stage' — stage transition (analyze, generate, generate_image, page_style). "
                        "'ppt_outline_delta' — incremental text delta for streaming display. "
                        "'ppt_outline_structured' — structured outline data (confirmed_pages, page_style). "
                        "'ppt_outline_result' — final complete outline JSON."
                    ),
                },
                "data": {
                    "type": "object",
                    "description": "Event payload. Structure depends on 'type'.",
                },
            },
            "required": ["type", "data"],
        }

    async def execute(self, type: str, data: dict) -> ToolResult:  # type: ignore[override]
        allowed = {
            "ppt_outline_stage",
            "ppt_outline_delta",
            "ppt_outline_structured",
            "ppt_outline_result",
        }
        if type not in allowed:
            return ToolResult(
                success=False,
                content="",
                error=f"Invalid type '{type}'. Must be one of: {', '.join(sorted(allowed))}",
            )
        self._emit({"type": type, **data})
        return ToolResult(success=True, content=f"[{type}] event emitted")


class PPTEditorHTMLTool(EventEmittingTool):
    """Emit structured PPT HTML events for per-page HTML generation."""

    parallel_safe = True  # officev3 calls per-page in parallel

    def __init__(self) -> None:
        super().__init__()

    @property
    def name(self) -> str:
        return "ppt_emit_html"

    @property
    def description(self) -> str:
        return (
            "Emit a structured PPT HTML event to the client. Use this tool to "
            "stream incremental HTML deltas or deliver the final complete HTML "
            "for a single PPT page."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": [
                        "ppt_editor_standard_html_delta",
                        "ppt_editor_standard_html_result",
                    ],
                    "description": (
                        "Event type discriminator. "
                        "'ppt_editor_standard_html_delta' — incremental HTML chunk for streaming display. "
                        "'ppt_editor_standard_html_result' — final complete HTML for the page."
                    ),
                },
                "data": {
                    "type": "object",
                    "description": "Event payload. Structure depends on 'type'.",
                },
            },
            "required": ["type", "data"],
        }

    async def execute(self, type: str, data: dict) -> ToolResult:  # type: ignore[override]
        allowed = {
            "ppt_editor_standard_html_delta",
            "ppt_editor_standard_html_result",
        }
        if type not in allowed:
            return ToolResult(
                success=False,
                content="",
                error=f"Invalid type '{type}'. Must be one of: {', '.join(sorted(allowed))}",
            )
        self._emit({"type": type, **data})
        return ToolResult(success=True, content=f"[{type}] event emitted")

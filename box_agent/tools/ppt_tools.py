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
            "Emit a structured PPT plan event to the client.\n\n"
            "Three event types:\n"
            "- 'ppt_plan_json': Output the plan. data MUST contain {done, data: {title, description, goals[]}}. "
            "goals[] use {id, description, actions[]}. actions[] use {id, description, details, status, result, dependencies[]}.\n"
            "- 'ppt_ask_user': Ask a clarifying question. data MUST contain {question, goal_id, action_id}. "
            "goal_id and action_id MUST be non-empty strings — create a preliminary goal/action if needed. "
            "Do NOT include options/choices/buttons. After emitting, end your turn immediately.\n"
            "- 'ppt_execution_event': Signal action progress. data contains {event, goal_id, action_id}."
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
                        "'ppt_plan_json' — structured plan with GoalActionList format. "
                        "'ppt_ask_user' — ask clarifying question (ends current turn, no options/choices). "
                        "'ppt_execution_event' — signal action start/end during execution."
                    ),
                },
                "data": {
                    "type": "object",
                    "description": (
                        "Event payload. For ppt_plan_json: {done: bool, data: {title, description, goals: [{id, description, actions: [{id, description, details, status, result, dependencies}]}]}}. "
                        "For ppt_ask_user: {question: str, goal_id: str, action_id: str}. "
                        "For ppt_execution_event: {event: str, goal_id: str, action_id: str}."
                    ),
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

        # Validate ppt_plan_json structure
        if type == "ppt_plan_json":
            inner = data.get("data")
            if not isinstance(inner, dict):
                return ToolResult(
                    success=False,
                    content="",
                    error="ppt_plan_json data must contain 'data' object with {title, description, goals[]}",
                )
            goals = inner.get("goals")
            if not isinstance(goals, list) or not goals:
                return ToolResult(
                    success=False,
                    content="",
                    error="ppt_plan_json data.data.goals must be a non-empty array",
                )
            for g in goals:
                if not isinstance(g, dict) or "id" not in g or "description" not in g:
                    return ToolResult(
                        success=False,
                        content="",
                        error="Each goal must have 'id' and 'description' fields (not goal_id/title)",
                    )
                actions = g.get("actions")
                if isinstance(actions, list):
                    for a in actions:
                        if not isinstance(a, dict):
                            continue
                        missing = [f for f in ("id", "description", "details", "status") if f not in a]
                        if missing:
                            return ToolResult(
                                success=False,
                                content="",
                                error=f"Action missing required fields: {', '.join(missing)}. Each action needs id, description, details, status, result, dependencies.",
                            )

        # Validate ppt_ask_user structure
        if type == "ppt_ask_user":
            if "question" not in data:
                return ToolResult(
                    success=False,
                    content="",
                    error="ppt_ask_user data must contain 'question' field",
                )
            # goal_id and action_id must be non-empty strings
            for field in ("goal_id", "action_id"):
                val = data.get(field, "")
                if not val or not isinstance(val, str) or not val.strip():
                    return ToolResult(
                        success=False,
                        content="",
                        error=f"ppt_ask_user '{field}' must be a non-empty string (e.g. 'goal_1'/'action_1'). "
                        f"Create a preliminary goal/action if none exists yet, then reference it here.",
                    )
            # Reject options/choices/buttons
            for forbidden in ("options", "choices", "buttons", "selection_schema"):
                if forbidden in data:
                    return ToolResult(
                        success=False,
                        content="",
                        error=f"ppt_ask_user must NOT contain '{forbidden}'. Only free-text question is supported.",
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

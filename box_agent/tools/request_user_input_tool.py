"""Structured pause point for tasks that genuinely need user input."""

from __future__ import annotations

from typing import Any

from .base import Tool, ToolResult


class RequestUserInputTool(Tool):
    """Record one focused clarification request without discarding task state."""

    aliases = ("clarify",)
    ends_turn_on_success = True

    @property
    def name(self) -> str:
        return "request_user_input"

    @property
    def description(self) -> str:
        return (
            "Pause the current task when missing user-provided information genuinely "
            "blocks a faithful result. Ask one focused question, list only the fields "
            "that are actually required, then end the turn. Existing artifacts and the "
            "active delivery workflow are preserved; when the user replies in the same "
            "session, continue from those artifacts instead of restarting. Do not use "
            "this for optional details that can safely be marked as pending or omitted."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "One concise, user-facing clarification question.",
                },
                "missing_fields": {
                    "type": "array",
                    "description": "The minimal facts or choices required to resume.",
                    "items": {"type": "string"},
                    "maxItems": 6,
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Briefly explain why these fields are necessary and cannot be "
                        "safely inferred."
                    ),
                },
            },
            "required": ["question", "missing_fields"],
            "additionalProperties": False,
        }

    async def execute(
        self,
        question: str,
        missing_fields: list[str],
        reason: str = "",
    ) -> ToolResult:
        if not isinstance(question, str) or not question.strip():
            return ToolResult(
                success=False,
                error="question must be a non-empty focused clarification question",
            )
        if not isinstance(missing_fields, list):
            return ToolResult(
                success=False,
                error="missing_fields must be an array of required input names",
            )

        normalized_question = question.strip()
        normalized_fields = [
            str(field).strip()
            for field in missing_fields
            if str(field).strip()
        ][:6]
        normalized_reason = str(reason).strip()

        if not normalized_fields:
            return ToolResult(
                success=False,
                error="missing_fields must name at least one required input",
            )

        payload = {
            "type": "user_input_request",
            "version": 1,
            "status": "waiting",
            "question": normalized_question,
            "missingFields": normalized_fields,
            "reason": normalized_reason,
            "resumeBehavior": "continue_existing_task",
        }
        return ToolResult(
            success=True,
            content=(
                "Waiting for user input. Ask the exact focused question now, then end "
                "this turn. Preserve existing artifacts; after the user replies, "
                "continue the same task from its current checkpoint."
            ),
            raw_output=payload,
            model_context=(
                "User input is required before continuing. End the turn after asking "
                "the recorded question; do not fabricate the missing fields. Preserve "
                "existing artifacts and resume this same task after the user's reply."
            ),
        )

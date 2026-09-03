"""Structured user-visible decisions with policy-bounded automatic defaults."""

from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

from .base import Tool, ToolResult


_OPTION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DECISION_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MIN_OPTIONS = 2
_MAX_OPTIONS = 6
_MIN_AUTO_SUBMIT_SECONDS = 10
_MAX_AUTO_SUBMIT_SECONDS = 120
_FORBIDDEN_AUTO_SUBMIT_KINDS = frozenset(
    {
        "authentication",
        "authorization",
        "delete",
        "destructive_action",
        "external_message",
        "payment",
        "permission",
        "purchase",
        "publish",
    }
)


class RequestUserDecisionTool(Tool):
    """Pause for one user-visible execution decision without losing task state."""

    ends_turn_on_success = True

    @property
    def name(self) -> str:
        return "request_user_decision"

    @property
    def description(self) -> str:
        return (
            "Pause the current task for one finite, user-visible decision whose options "
            "materially change the deliverable, scope, format, or other user-facing "
            "outcome. Do not use this for internal implementation choices: choose those "
            "yourself. Do not use it for missing facts; use request_user_input instead. "
            "Provide 2-6 concise options with stable IDs. You may request a timeout default "
            "only when it preserves the user's stated intent, is low-risk, and is reversible. "
            "Prefer progress over waiting: when one option safely continues the user's explicit "
            "request, put that recommended option first, set it as the default, and request a "
            "30 second timeout. Every call must supply the default, timeout, risk, reversibility, "
            "and intent-preservation declarations; the runtime will deny automatic submission "
            "for sensitive or unsafe choices. If the model can "
            "choose a safe path without changing the user-visible outcome, do not call this tool. "
            "The runtime decides whether automatic submission is actually allowed. After "
            "calling this tool, end the turn and do not repeat the options in Markdown."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "One concise user-facing decision question.",
                },
                "decision_kind": {
                    "type": "string",
                    "description": (
                        "Stable snake_case category such as delivery_scope, "
                        "delivery_format, or content_direction."
                    ),
                },
                "options": {
                    "type": "array",
                    "minItems": _MIN_OPTIONS,
                    "maxItems": _MAX_OPTIONS,
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": "Stable ASCII option identifier.",
                            },
                            "label": {
                                "type": "string",
                                "description": "Short user-facing option label.",
                            },
                            "description": {
                                "type": "string",
                                "description": "Concise effect or tradeoff of this option.",
                            },
                        },
                        "required": ["id", "label"],
                        "additionalProperties": False,
                    },
                },
                "default_option_id": {
                    "type": "string",
                    "description": (
                        "Option to submit if an allowed timeout expires. Prefer the option that "
                        "safely continues the user's explicit request without reducing scope."
                    ),
                },
                "requested_auto_submit_seconds": {
                    "type": "integer",
                    "minimum": _MIN_AUTO_SUBMIT_SECONDS,
                    "maximum": _MAX_AUTO_SUBMIT_SECONDS,
                    "default": 30,
                    "description": (
                        "Requested timeout. The runtime may remove it. Requires a default "
                        "option plus low risk, reversible, and intent-preserving declarations. "
                        "Request 30 seconds when those conditions hold."
                    ),
                },
                "risk_level": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": "Impact if the default is submitted without a click.",
                },
                "reversible": {
                    "type": "boolean",
                    "description": "Whether the selected branch can be safely undone.",
                },
                "preserves_user_intent": {
                    "type": "boolean",
                    "description": "Whether the default preserves the explicit user request.",
                },
                "allow_freeform": {
                    "type": "boolean",
                    "description": "Allow a custom text response instead of an option.",
                },
                "reason": {
                    "type": "string",
                    "description": "Why this user-visible decision is required.",
                },
            },
            "required": [
                "question",
                "decision_kind",
                "options",
                "default_option_id",
                "requested_auto_submit_seconds",
                "risk_level",
                "reversible",
                "preserves_user_intent",
            ],
            "additionalProperties": False,
        }

    async def execute(
        self,
        question: str,
        decision_kind: str,
        options: list[dict[str, Any]],
        default_option_id: str = "",
        requested_auto_submit_seconds: int | None = None,
        risk_level: str | None = None,
        reversible: bool | None = None,
        preserves_user_intent: bool | None = None,
        allow_freeform: bool = False,
        reason: str = "",
    ) -> ToolResult:
        normalized_question = str(question).strip()
        normalized_kind = str(decision_kind).strip().lower()
        normalized_reason = str(reason).strip()
        if not normalized_question or len(normalized_question) > 1_000:
            return ToolResult(
                success=False,
                error="question must contain 1-1000 characters",
            )
        if not _DECISION_KIND_RE.fullmatch(normalized_kind):
            return ToolResult(
                success=False,
                error="decision_kind must be a stable snake_case identifier",
            )
        if not isinstance(options, list) or not (
            _MIN_OPTIONS <= len(options) <= _MAX_OPTIONS
        ):
            return ToolResult(
                success=False,
                error=f"options must contain {_MIN_OPTIONS}-{_MAX_OPTIONS} choices",
            )

        normalized_options: list[dict[str, str]] = []
        option_ids: set[str] = set()
        for option in options:
            if not isinstance(option, dict):
                return ToolResult(success=False, error="each option must be an object")
            option_id = str(option.get("id") or "").strip()
            label = str(option.get("label") or "").strip()
            description = str(option.get("description") or "").strip()
            if not _OPTION_ID_RE.fullmatch(option_id):
                return ToolResult(
                    success=False,
                    error="option ids must be unique stable ASCII identifiers",
                )
            if option_id in option_ids:
                return ToolResult(success=False, error="option ids must be unique")
            if not label or len(label) > 120:
                return ToolResult(
                    success=False,
                    error="option labels must contain 1-120 characters",
                )
            if len(description) > 500:
                return ToolResult(
                    success=False,
                    error="option descriptions must contain at most 500 characters",
                )
            option_ids.add(option_id)
            normalized_option = {"id": option_id, "label": label}
            if description:
                normalized_option["description"] = description
            normalized_options.append(normalized_option)

        normalized_default = str(default_option_id).strip()
        if not normalized_default:
            return ToolResult(
                success=False,
                error="default_option_id is required for every user decision",
            )
        if normalized_default and normalized_default not in option_ids:
            return ToolResult(
                success=False,
                error="default_option_id must match one of the supplied option ids",
            )
        if (
            isinstance(requested_auto_submit_seconds, bool)
            or not isinstance(requested_auto_submit_seconds, int)
            or not (
                _MIN_AUTO_SUBMIT_SECONDS
                <= requested_auto_submit_seconds
                <= _MAX_AUTO_SUBMIT_SECONDS
            )
        ):
            return ToolResult(
                success=False,
                error="requested_auto_submit_seconds must contain an integer from 10 to 120",
            )
        normalized_risk = str(risk_level or "").strip().lower()
        if normalized_risk not in {"low", "medium", "high"}:
            return ToolResult(
                success=False,
                error="risk_level is required and must be low, medium, or high",
            )
        if not isinstance(reversible, bool):
            return ToolResult(success=False, error="reversible must be explicitly declared")
        if not isinstance(preserves_user_intent, bool):
            return ToolResult(
                success=False,
                error="preserves_user_intent must be explicitly declared",
            )
        if len(normalized_reason) > 500:
            return ToolResult(success=False, error="reason must contain at most 500 characters")

        auto_submit = self._auto_submit_policy(
            decision_kind=normalized_kind,
            default_option_id=normalized_default,
            requested_seconds=requested_auto_submit_seconds,
            risk_level=normalized_risk,
            reversible=reversible,
            preserves_user_intent=preserves_user_intent,
        )
        request_id = f"decision_{uuid4().hex}"
        payload: dict[str, Any] = {
            "type": "user_decision_request",
            "schemaVersion": 1,
            "requestId": request_id,
            "status": "waiting",
            "question": normalized_question,
            "decisionKind": normalized_kind,
            "options": normalized_options,
            "allowFreeform": allow_freeform is True,
            "reason": normalized_reason,
            "autoSubmit": auto_submit,
            "resumeBehavior": "continue_existing_task",
        }
        if normalized_default:
            payload["defaultOptionId"] = normalized_default

        fallback_lines = [f"Decision required: {normalized_question}"]
        fallback_lines.extend(
            f"- {option['id']}: {option['label']}"
            + (f" — {option['description']}" if option.get("description") else "")
            for option in normalized_options
        )
        if normalized_default:
            fallback_lines.append(f"Default option: {normalized_default}")

        return ToolResult(
            success=True,
            content="\n".join(fallback_lines),
            raw_output=payload,
            model_context=(
                "The host received a structured user decision request. End the turn now "
                "without repeating the question or options in prose. Preserve all existing "
                "artifacts and continue this same task after the host returns the selection."
            ),
        )

    @staticmethod
    def _auto_submit_policy(
        *,
        decision_kind: str,
        default_option_id: str,
        requested_seconds: int | None,
        risk_level: str,
        reversible: bool,
        preserves_user_intent: bool,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "allowed": False,
            "requestedSeconds": requested_seconds,
        }
        if requested_seconds is None:
            result["denialReason"] = "not_requested"
            return result
        if isinstance(requested_seconds, bool) or not isinstance(requested_seconds, int):
            result["denialReason"] = "invalid_timeout"
            return result
        if not (_MIN_AUTO_SUBMIT_SECONDS <= requested_seconds <= _MAX_AUTO_SUBMIT_SECONDS):
            result["denialReason"] = "invalid_timeout"
            return result
        if not default_option_id:
            result["denialReason"] = "missing_default"
            return result
        if decision_kind in _FORBIDDEN_AUTO_SUBMIT_KINDS:
            result["denialReason"] = "sensitive_decision"
            return result
        if str(risk_level).strip().lower() != "low":
            result["denialReason"] = "risk_not_low"
            return result
        if reversible is not True:
            result["denialReason"] = "not_reversible"
            return result
        if preserves_user_intent is not True:
            result["denialReason"] = "does_not_preserve_user_intent"
            return result

        return {
            "allowed": True,
            "requestedSeconds": requested_seconds,
            "effectiveSeconds": requested_seconds,
            "behavior": "submit_default",
        }

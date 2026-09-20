"""Host-neutral machine-readable execution result reporting."""

from __future__ import annotations

from typing import Any

from .base import Tool, ToolResult

_OUTCOMES = ("completed", "blocked", "needs_input")
_CHANGE_KINDS = ("code", "document", "design", "configuration", "other")
_CHECK_STATUSES = ("passed", "failed", "skipped")
_CRITERION_STATUSES = ("passed", "needs_human", "failed")


def _text_list(values: list[Any] | None) -> list[str]:
    return [text for value in values or [] if (text := str(value).strip())]


def _valid_text(value: Any, max_length: int) -> bool:
    return isinstance(value, str) and 1 <= len(value.strip()) <= max_length


class ReportExecutionResultTool(Tool):
    """Publish the execution facts that a host can bind to its own workflow."""

    @property
    def name(self) -> str:
        return "report_execution_result"

    @property
    def description(self) -> str:
        return (
            "Report the final, machine-readable result of the current execution to the "
            "host after the work and verification are finished. Report only observed "
            "changes, checks, limitations, and questions. A completed result requires "
            "at least one change, passed checks, and criterion evidence; needs_input "
            "requires at least one question. This result "
            "does not submit, accept, or authorize work in any external team system: the "
            "host owns workflow identity, task versions, context, and submission."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "outcome": {
                    "type": "string",
                    "enum": list(_OUTCOMES),
                    "description": "Execution outcome; never use completed to mean accepted.",
                },
                "summary": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 4000,
                    "description": "Concise factual summary of the execution result.",
                },
                "changes": {
                    "type": "array",
                    "maxItems": 200,
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {
                                "type": "string",
                                "enum": list(_CHANGE_KINDS),
                            },
                            "summary": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 1000,
                            },
                            "reference": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 2000,
                                "description": (
                                    "Host-resolvable file, artifact, URL, or other reference."
                                ),
                            },
                        },
                        "required": ["kind", "summary", "reference"],
                        "additionalProperties": False,
                    },
                },
                "checks": {
                    "type": "array",
                    "maxItems": 200,
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 240,
                            },
                            "status": {
                                "type": "string",
                                "enum": list(_CHECK_STATUSES),
                            },
                            "summary": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 1000,
                            },
                            "reference": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 2000,
                            },
                            "advisory": {
                                "type": "boolean",
                                "description": "True when this check is informative and does not block delivery.",
                            },
                        },
                        "required": ["name", "status", "summary"],
                        "additionalProperties": False,
                    },
                },
                "criteria_evaluations": {
                    "type": "array",
                    "maxItems": 50,
                    "description": (
                        "Zero-based evaluations for the host-provided acceptance "
                        "criteria. Use needs_human when the evidence exists but the "
                        "criterion requires human judgment."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "criterion_index": {
                                "type": "integer",
                                "minimum": 0,
                                "maximum": 49,
                            },
                            "status": {
                                "type": "string",
                                "enum": list(_CRITERION_STATUSES),
                            },
                            "summary": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 1000,
                            },
                            "evidence": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 100,
                                "items": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 2000,
                                },
                            },
                        },
                        "required": [
                            "criterion_index",
                            "status",
                            "summary",
                            "evidence",
                        ],
                        "additionalProperties": False,
                    },
                },
                "known_limitations": {
                    "type": "array",
                    "maxItems": 100,
                    "items": {"type": "string", "minLength": 1, "maxLength": 1000},
                },
                "questions": {
                    "type": "array",
                    "maxItems": 100,
                    "items": {"type": "string", "minLength": 1, "maxLength": 1000},
                },
            },
            "required": [
                "outcome",
                "summary",
                "changes",
                "checks",
                "criteria_evaluations",
                "known_limitations",
                "questions",
            ],
            "additionalProperties": False,
        }

    async def execute(
        self,
        outcome: str,
        summary: str,
        changes: list[dict[str, Any]] | None = None,
        checks: list[dict[str, Any]] | None = None,
        criteria_evaluations: list[dict[str, Any]] | None = None,
        known_limitations: list[Any] | None = None,
        questions: list[Any] | None = None,
    ) -> ToolResult:
        normalized_outcome = outcome.strip().lower()
        normalized_summary = summary.strip()
        normalized_changes = [dict(item) for item in changes or []]
        normalized_checks = [dict(item) for item in checks or []]
        normalized_criteria = [dict(item) for item in criteria_evaluations or []]
        normalized_limitations = _text_list(known_limitations)
        normalized_questions = _text_list(questions)

        if normalized_outcome not in _OUTCOMES:
            return ToolResult(
                success=False,
                error=f"Unknown execution outcome: {outcome}",
            )
        if not normalized_summary:
            return ToolResult(
                success=False,
                error="Execution result summary is required.",
            )
        if any(
            change.get("kind") not in _CHANGE_KINDS
            or not _valid_text(change.get("summary"), 1000)
            or not _valid_text(change.get("reference"), 2000)
            for change in normalized_changes
        ):
            return ToolResult(
                success=False,
                error="Execution result contains an invalid change.",
            )
        if any(
            not _valid_text(check.get("name"), 240)
            or check.get("status") not in _CHECK_STATUSES
            or not _valid_text(check.get("summary"), 1000)
            or (
                check.get("reference") is not None
                and not _valid_text(check.get("reference"), 2000)
            )
            for check in normalized_checks
        ):
            return ToolResult(
                success=False,
                error="Execution result contains an invalid verification check.",
            )
        seen_criterion_indices: set[int] = set()
        for criterion in normalized_criteria:
            criterion_index = criterion.get("criterion_index")
            evidence = criterion.get("evidence")
            if (
                not isinstance(criterion_index, int)
                or isinstance(criterion_index, bool)
                or not 0 <= criterion_index <= 49
                or criterion.get("status") not in _CRITERION_STATUSES
                or not _valid_text(criterion.get("summary"), 1000)
                or not isinstance(evidence, list)
                or not 1 <= len(evidence) <= 100
                or any(not _valid_text(item, 2000) for item in evidence)
            ):
                return ToolResult(
                    success=False,
                    error=(
                        "Execution result contains an invalid criterion evaluation."
                    ),
                )
            if criterion_index in seen_criterion_indices:
                return ToolResult(
                    success=False,
                    error="Criterion indices must be unique.",
                )
            seen_criterion_indices.add(criterion_index)
        if normalized_outcome == "completed" and not normalized_changes:
            return ToolResult(
                success=False,
                error="Completed execution results require at least one change.",
            )
        if normalized_outcome == "completed" and not normalized_checks:
            return ToolResult(
                success=False,
                error="Completed execution results require all checks to pass.",
            )
        if normalized_outcome == "completed" and any(
            check.get("status") != "passed" and check.get("advisory") is not True
            for check in normalized_checks
        ):
            return ToolResult(
                success=False,
                error="Completed execution results require all checks to pass.",
            )
        if normalized_outcome == "completed" and not normalized_criteria:
            return ToolResult(
                success=False,
                error=(
                    "Completed execution results require acceptance criterion evidence."
                ),
            )
        if normalized_outcome == "completed" and any(
            criterion.get("status") == "failed"
            for criterion in normalized_criteria
        ):
            return ToolResult(
                success=False,
                error="Completed execution results cannot contain failed criteria.",
            )
        if normalized_outcome == "completed" and any(
            not _text_list(criterion.get("evidence"))
            for criterion in normalized_criteria
        ):
            return ToolResult(
                success=False,
                error=(
                    "Completed execution results require evidence for every criterion."
                ),
            )
        if normalized_outcome == "needs_input" and not normalized_questions:
            return ToolResult(
                success=False,
                error="needs_input execution results require at least one question.",
            )

        raw_output = {
            "type": "execution_result",
            "version": 1,
            "outcome": normalized_outcome,
            "summary": normalized_summary,
            "changes": normalized_changes,
            "checks": normalized_checks,
            "criteriaEvaluations": [
                {
                    "criterionIndex": criterion.get("criterion_index"),
                    "status": criterion.get("status"),
                    "summary": str(criterion.get("summary", "")).strip(),
                    "evidence": _text_list(criterion.get("evidence")),
                }
                for criterion in normalized_criteria
            ],
            "knownLimitations": normalized_limitations,
            "questions": normalized_questions,
        }
        return ToolResult(
            success=True,
            content=f"Reported execution result: {normalized_outcome}.",
            raw_output=raw_output,
            model_context=(
                "The host received the machine-readable execution result. "
                "Do not claim that an external team workflow accepted it."
            ),
        )

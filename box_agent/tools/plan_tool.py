"""Plan Tool - user-visible planning snapshots for host UIs.

Unlike ``todo_write``, this tool records the proposed approach, scope, risks,
and verification strategy. Todo items remain the execution progress tracker.
"""

from __future__ import annotations

import json
from datetime import datetime
from itertools import count
from typing import Any

from .base import Tool, ToolResult


_VALID_PLAN_STATUSES = ("draft", "active", "revised", "complete")


def _now() -> str:
    return datetime.now().isoformat()


def _normalize_text_list(values: list[Any] | str | None) -> list[str]:
    if not values:
        return []
    if isinstance(values, str):
        values = [values]
    normalized: list[str] = []
    for value in values:
        text = str(value).strip()
        if text:
            normalized.append(text)
    return normalized


def _normalize_steps(values: list[Any] | dict[str, Any] | str | None) -> list[dict[str, str]]:
    if not values:
        return []
    if isinstance(values, str):
        raw_values = values.strip()
        try:
            parsed_values = json.loads(raw_values)
        except json.JSONDecodeError:
            parsed_values = None
        values = parsed_values if isinstance(parsed_values, list) else [raw_values]
    elif isinstance(values, dict):
        values = [values]

    steps: list[dict[str, str]] = []
    for index, value in enumerate(values, start=1):
        if isinstance(value, dict):
            title = str(
                value.get("title")
                or value.get("step")
                or value.get("text")
                or value.get("task")
                or ""
            ).strip()
            details = str(
                value.get("details")
                or value.get("detail")
                or value.get("description")
                or ""
            ).strip()
        else:
            title = str(value).strip()
            details = ""

        if title or details:
            steps.append({"id": str(index), "title": title or details, "details": details})

    return steps


def _plan_snapshot(plan: dict[str, Any] | None, *, action: str | None = None) -> dict[str, Any]:
    """Return a host-friendly plan snapshot payload."""
    summary = {
        "steps": len(plan.get("steps", [])) if plan else 0,
        "verification": len(plan.get("verification", [])) if plan else 0,
        "risks": len(plan.get("risks", [])) if plan else 0,
        "assumptions": len(plan.get("assumptions", [])) if plan else 0,
    }
    payload: dict[str, Any] = {
        "type": "plan_snapshot",
        "version": 1,
        "plan": dict(plan) if plan else None,
        "summary": summary,
    }
    if action is not None:
        payload["action"] = action
    return payload


class PlanStore:
    """Single-plan store for the current session."""

    def __init__(self):
        self._plan: dict[str, Any] | None = None
        self._counter = count(1)

    def set(
        self,
        *,
        title: str,
        objective: str = "",
        scope: str = "",
        steps: list[Any] | dict[str, Any] | str | None = None,
        verification: list[Any] | str | None = None,
        risks: list[Any] | str | None = None,
        assumptions: list[Any] | str | None = None,
        status: str = "active",
    ) -> dict[str, Any]:
        now = _now()
        created_at = self._plan.get("created_at") if self._plan else now
        plan_id = self._plan.get("id") if self._plan else str(next(self._counter))
        self._plan = {
            "id": plan_id,
            "title": title.strip() or "Plan",
            "objective": objective.strip(),
            "scope": scope.strip(),
            "status": status if status in _VALID_PLAN_STATUSES else "active",
            "steps": _normalize_steps(steps),
            "verification": _normalize_text_list(verification),
            "risks": _normalize_text_list(risks),
            "assumptions": _normalize_text_list(assumptions),
            "created_at": created_at,
            "updated_at": now,
        }
        return self._plan

    def clear(self) -> None:
        self._plan = None

    def get(self) -> dict[str, Any] | None:
        return self._plan


class PlanWriteTool(Tool):
    """Create, replace, or clear the current user-visible plan."""

    def __init__(self, store: PlanStore):
        self._store = store

    @property
    def name(self) -> str:
        return "plan_write"

    @property
    def description(self) -> str:
        return (
            "Publish a structured, user-visible plan for the current task. Use this "
            "when the user asks for a plan/proposal, when the approach needs to be "
            "shown before substantial work, or when the host UI should render a plan "
            "card. This is not an execution progress tracker; use todo_write "
            "separately to track completed/in-progress/pending work. Do not use this "
            "for greetings, acknowledgements, approval replies, or short messages like "
            "ok, continue, confirmed, 好的, 收到, or 继续 unless they also contain a new "
            "concrete task. Every call must include action. For action='set', always "
            "include a non-empty title and pass steps, verification, risks, and assumptions "
            "as JSON arrays, never as JSON-encoded strings."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["set", "clear"],
                    "description": "Required on every call. Set/replace the plan, or clear it.",
                },
                "title": {
                    "type": "string",
                    "description": "Short plan title. Required for action='set'.",
                },
                "objective": {
                    "type": "string",
                    "description": "What the plan is intended to achieve.",
                },
                "scope": {
                    "type": "string",
                    "description": "Boundaries and non-goals for the work.",
                },
                "status": {
                    "type": "string",
                    "enum": list(_VALID_PLAN_STATUSES),
                    "description": "Plan lifecycle status. Defaults to active.",
                },
                "steps": {
                    "type": "array",
                    "description": (
                        "Ordered plan steps as a JSON array, never a JSON-encoded string. "
                        "These describe approach, not progress."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "details": {"type": "string"},
                        },
                        "required": ["title"],
                    },
                },
                "verification": {
                    "type": "array",
                    "description": "Checks or commands that should prove the work.",
                    "items": {"type": "string"},
                },
                "risks": {
                    "type": "array",
                    "description": "Known risks or uncertainties.",
                    "items": {"type": "string"},
                },
                "assumptions": {
                    "type": "array",
                    "description": "Assumptions behind the plan.",
                    "items": {"type": "string"},
                },
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str | None = None,
        title: str | None = None,
        objective: str = "",
        scope: str = "",
        status: str = "active",
        steps: list[Any] | dict[str, Any] | str | None = None,
        verification: list[Any] | str | None = None,
        risks: list[Any] | str | None = None,
        assumptions: list[Any] | str | None = None,
    ) -> ToolResult:
        normalized_action = str(action or "").strip().lower()
        if not normalized_action and any(
            value for value in (title, objective, scope, steps, verification, risks, assumptions)
        ):
            normalized_action = "set"

        if normalized_action == "clear":
            self._store.clear()
            return ToolResult(
                success=True,
                content="Cleared the current plan.",
                raw_output=_plan_snapshot(None, action="clear"),
            )

        if normalized_action != "set":
            return ToolResult(success=False, error=f"Unknown action: {action}")

        normalized_title = str(title or "").strip()
        if not normalized_title:
            normalized_title = str(objective or "").strip()
        if not normalized_title:
            normalized_steps = _normalize_steps(steps)
            normalized_title = normalized_steps[0]["title"] if normalized_steps else ""
        if not normalized_title:
            return ToolResult(success=False, error="'title' is required for set.")

        plan = self._store.set(
            title=normalized_title,
            objective=objective,
            scope=scope,
            status=status,
            steps=steps,
            verification=verification,
            risks=risks,
            assumptions=assumptions,
        )
        return ToolResult(
            success=True,
            content=f"Set plan #{plan['id']}: {plan['title']}",
            raw_output=_plan_snapshot(plan, action="set"),
        )


class PlanReadTool(Tool):
    """Read the current user-visible plan."""

    def __init__(self, store: PlanStore):
        self._store = store

    @property
    def name(self) -> str:
        return "plan_read"

    @property
    def description(self) -> str:
        return "Read the current structured plan for the task, if one has been published."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self) -> ToolResult:
        plan = self._store.get()
        if plan is None:
            return ToolResult(
                success=True,
                content="No current plan.",
                raw_output=_plan_snapshot(None),
            )
        return ToolResult(
            success=True,
            content=self._format_plan(plan),
            raw_output=_plan_snapshot(plan),
        )

    @staticmethod
    def _format_plan(plan: dict[str, Any]) -> str:
        lines = [f"Plan #{plan['id']}: {plan['title']} [{plan['status']}]"]
        if plan.get("objective"):
            lines.append(f"Objective: {plan['objective']}")
        if plan.get("scope"):
            lines.append(f"Scope: {plan['scope']}")
        for step in plan.get("steps", []):
            detail = f" - {step['details']}" if step.get("details") else ""
            lines.append(f"  {step['id']}. {step['title']}{detail}")
        return "\n".join(lines)

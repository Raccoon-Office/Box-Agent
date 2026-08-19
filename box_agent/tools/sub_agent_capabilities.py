"""Normalize a general-purpose sub-agent request and resolve inherited tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..config import ToolLimitsConfig
from .base import Tool

_DEFAULT_SUB_AGENT_LIMITS = ToolLimitsConfig().sub_agent
GENERAL_LOOP_MAX_STEPS = _DEFAULT_SUB_AGENT_LIMITS.general_max_steps
GENERAL_LOOP_MAX_TOOL_CALLS = _DEFAULT_SUB_AGENT_LIMITS.general_max_tool_calls

MINIMAL_DELEGATION_EXAMPLE: dict[str, Any] = {
    "task": "Complete one self-contained delegated task.",
}


@dataclass(frozen=True)
class DelegationBudget:
    max_steps: int
    max_tool_calls: int

    def to_dict(self) -> dict[str, int]:
        return {
            "max_steps": self.max_steps,
            "max_tool_calls": self.max_tool_calls,
        }


@dataclass(frozen=True)
class DelegationSpec:
    title: str
    task: str
    skill_names: tuple[str, ...]
    required_tool_names: tuple[str, ...]
    budget: DelegationBudget
    defaults_applied: tuple[str, ...]


@dataclass(frozen=True)
class CapabilityFailure:
    """Stable structured failure returned before a child LLM starts."""

    code: str
    message: str
    retryable: bool
    invalid_fields: tuple[str, ...] = ()
    defaults_applied: tuple[str, ...] = ()
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": "sub_agent_delegation_error",
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "invalid_fields": list(self.invalid_fields),
            "defaults_applied": list(self.defaults_applied),
        }
        if self.details:
            payload.update(self.details)
        if self.code == "INVALID_DELEGATION_SPEC":
            payload["minimal_valid_example"] = MINIMAL_DELEGATION_EXAMPLE
            payload["retry_limit"] = 1
            if any(
                field == "budget" or field.startswith("budget.")
                for field in self.invalid_fields
            ):
                payload["field_corrections"] = {
                    "budget": {
                        "message": "Pass budget as a JSON object, never as a JSON string.",
                        "example": {"max_steps": 12, "max_tool_calls": 25},
                    }
                }
        return payload


@dataclass(frozen=True)
class ResolvedCapabilityBundle:
    spec: DelegationSpec
    tools: dict[str, Tool]
    skills: tuple[Any, ...]

    @property
    def resolved_tool_names(self) -> tuple[str, ...]:
        return tuple(self.tools)

    @property
    def resolved_skill_names(self) -> tuple[str, ...]:
        return tuple(skill.name for skill in self.skills)

    def diagnostic_payload(self) -> dict[str, Any]:
        return {
            "capability_source": "parent",
            "requested_tools": list(self.spec.required_tool_names),
            "resolved_tools": list(self.resolved_tool_names),
            "requested_skills": list(self.spec.skill_names),
            "resolved_skills": list(self.resolved_skill_names),
            "budget": self.spec.budget.to_dict(),
            "defaults_applied": list(self.spec.defaults_applied),
        }


def _normalized_string_list(
    value: Any,
    *,
    field_name: str,
    invalid_fields: list[str],
) -> tuple[str, ...]:
    if not isinstance(value, list):
        invalid_fields.append(field_name)
        return ()
    normalized: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            invalid_fields.append(f"{field_name}[{index}]")
            continue
        normalized.add(item.strip())
    return tuple(sorted(normalized))


def _unknown_fields(
    value: Mapping[str, Any],
    allowed: set[str],
    prefix: str,
) -> list[str]:
    return [f"{prefix}.{name}" for name in sorted(set(value) - allowed)]


def parse_delegation_spec(
    *,
    task: Any,
    title: Any = "",
    skills: Any = None,
    required_tools: Any = None,
    budget: Any = None,
    default_required_tools: tuple[str, ...] = (),
    general_max_steps: int = GENERAL_LOOP_MAX_STEPS,
    general_max_tool_calls: int = GENERAL_LOOP_MAX_TOOL_CALLS,
) -> DelegationSpec | CapabilityFailure:
    """Validate a single-loop delegation request and apply runtime defaults."""

    invalid_fields: list[str] = []
    defaults_applied: list[str] = []

    if not isinstance(task, str) or not task.strip():
        invalid_fields.append("task")
        normalized_task = ""
    else:
        normalized_task = task.strip()

    if title is None:
        title = ""
    if not isinstance(title, str):
        invalid_fields.append("title")
        normalized_title = ""
    else:
        normalized_title = title.strip()
        if not normalized_title:
            defaults_applied.append("title")

    if skills is None:
        skills = []
        defaults_applied.append("skills")
    skill_names = _normalized_string_list(
        skills,
        field_name="skills",
        invalid_fields=invalid_fields,
    )

    if required_tools is None:
        required_tool_names = tuple(sorted(set(default_required_tools)))
        defaults_applied.append("required_tools")
    else:
        required_tool_names = _normalized_string_list(
            required_tools,
            field_name="required_tools",
            invalid_fields=invalid_fields,
        )
    if "sub_agent" in required_tool_names:
        invalid_fields.append("required_tools")

    if budget is None:
        budget = {}
    if not isinstance(budget, dict):
        invalid_fields.append("budget")
        budget = {}
    else:
        invalid_fields.extend(
            _unknown_fields(budget, {"max_steps", "max_tool_calls"}, "budget")
        )

    def budget_value(name: str, maximum: int) -> int:
        if name not in budget:
            defaults_applied.append(f"budget.{name}")
        value = budget.get(name, maximum)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            invalid_fields.append(f"budget.{name}")
            return maximum
        return min(value, maximum)

    max_steps = budget_value("max_steps", general_max_steps)
    max_tool_calls = budget_value("max_tool_calls", general_max_tool_calls)

    if invalid_fields:
        return CapabilityFailure(
            code="INVALID_DELEGATION_SPEC",
            message=(
                "The sub-agent delegation is invalid; fix the listed fields "
                "and retry at most once."
            ),
            retryable=True,
            invalid_fields=tuple(sorted(set(invalid_fields))),
            defaults_applied=tuple(sorted(set(defaults_applied))),
        )

    return DelegationSpec(
        title=normalized_title,
        task=normalized_task,
        skill_names=skill_names,
        required_tool_names=required_tool_names,
        budget=DelegationBudget(
            max_steps=max_steps,
            max_tool_calls=max_tool_calls,
        ),
        defaults_applied=tuple(sorted(set(defaults_applied))),
    )


class CapabilityResolver:
    """Resolve selected Skills and a strict subset of live parent tools."""

    def resolve(
        self,
        spec: DelegationSpec,
        *,
        parent_tools: Mapping[str, Tool],
        skill_loader: Any | None = None,
    ) -> ResolvedCapabilityBundle | CapabilityFailure:
        skills_or_failure = self._resolve_skills(spec, skill_loader)
        if isinstance(skills_or_failure, CapabilityFailure):
            return skills_or_failure

        resolved_tools: dict[str, Tool] = {}
        for name in spec.required_tool_names:
            tool = parent_tools.get(name)
            if tool is None or name == "sub_agent":
                return CapabilityFailure(
                    code="REQUIRED_TOOL_NOT_FOUND",
                    message=f"Required tool '{name}' is not available in the parent session.",
                    retryable=False,
                    details={"tool": name},
                )
            resolved_tools[name] = tool

        return ResolvedCapabilityBundle(
            spec=spec,
            tools=resolved_tools,
            skills=tuple(sorted(skills_or_failure, key=lambda skill: skill.name)),
        )

    def _resolve_skills(
        self,
        spec: DelegationSpec,
        skill_loader: Any | None,
    ) -> tuple[Any, ...] | CapabilityFailure:
        if not spec.skill_names:
            return ()
        if skill_loader is None:
            return CapabilityFailure(
                code="SKILL_PROVIDER_UNAVAILABLE",
                message="This sub-agent selected Skills, but no live SkillLoader is available.",
                retryable=False,
            )

        try:
            skill_loader.maybe_reload()
        except Exception as exc:
            return CapabilityFailure(
                code="SKILL_PROVIDER_UNAVAILABLE",
                message=f"The live SkillLoader could not be refreshed: {exc}",
                retryable=True,
            )

        resolved: dict[str, Any] = {}
        visiting: list[str] = []

        def visit(name: str) -> CapabilityFailure | None:
            if name in resolved:
                return None
            if name in visiting:
                cycle = visiting[visiting.index(name) :] + [name]
                return CapabilityFailure(
                    code="SKILL_DEPENDENCY_CYCLE",
                    message="Selected Skill dependencies contain a cycle.",
                    retryable=False,
                    details={"cycle": cycle},
                )

            skill = skill_loader.get_skill(name)
            if skill is None:
                disabled = skill_loader.get_skill(name, include_disabled=True)
                if disabled is not None:
                    return CapabilityFailure(
                        code="SKILL_DISABLED",
                        message=f"Selected Skill '{name}' is disabled.",
                        retryable=False,
                        details={"skill": name},
                    )
                return CapabilityFailure(
                    code="SKILL_NOT_FOUND",
                    message=f"Selected Skill '{name}' was not found.",
                    retryable=False,
                    details={"skill": name},
                )
            if getattr(skill, "broken", False):
                return CapabilityFailure(
                    code="SKILL_BROKEN",
                    message=f"Selected Skill '{name}' is malformed and cannot be loaded.",
                    retryable=False,
                    details={
                        "skill": name,
                        "reason": getattr(skill, "broken_reason", None),
                    },
                )

            visiting.append(name)
            for dependency in sorted(set(skill.required_skills or [])):
                failure = visit(dependency)
                if failure is not None:
                    return failure
            visiting.pop()
            resolved[name] = skill
            return None

        for name in spec.skill_names:
            failure = visit(name)
            if failure is not None:
                return failure
        return tuple(resolved.values())

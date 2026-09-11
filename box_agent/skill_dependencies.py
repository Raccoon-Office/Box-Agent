"""One required-Skill resolution policy for parent and delegated tasks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SkillDependencyError(ValueError):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message


def resolve_required_skills(loader: Any, names: tuple[str, ...] | list[str]) -> tuple[Any, ...]:
    """Validate a deterministic dependency closure without loading it into context."""
    resolved: dict[str, Any] = {}
    visiting: list[str] = []

    def visit(name: str) -> None:
        if name in resolved:
            return
        if name in visiting:
            raise SkillDependencyError("SKILL_DEPENDENCY_CYCLE",
                                       "Selected Skill dependencies contain a cycle.",
                                       {"cycle": visiting[visiting.index(name):] + [name]})
        skill = loader.get_skill(name)
        if skill is None:
            disabled = loader.get_skill(name, include_disabled=True)
            code = "SKILL_DISABLED" if disabled is not None else "SKILL_NOT_FOUND"
            reason = "disabled" if disabled is not None else "not found"
            raise SkillDependencyError(code, f"Required Skill '{name}' is {reason}.", {"skill": name})
        if getattr(skill, "broken", False):
            raise SkillDependencyError("SKILL_BROKEN", f"Required Skill '{name}' is malformed and cannot be loaded.",
                                       {"skill": name, "reason": skill.broken_reason})
        visiting.append(name)
        for dependency in sorted(set(skill.required_skills or [])):
            visit(dependency)
        visiting.pop()
        resolved[name] = skill

    for name in names:
        visit(name)
    return tuple(resolved.values())

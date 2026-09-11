"""Paginated, metadata-only discovery of locally installed Skills."""

from collections.abc import Callable, Collection, MutableSet
from hashlib import sha256
import json
from typing import Any
from types import SimpleNamespace

from .base import Tool, ToolResult
from .schema_validation import validate_tool_arguments
from .skill_loader import Skill, SkillLoader


class ListSkillsTool(Tool):
    """Expose local catalog pages without selecting or loading Skill bodies."""

    def __init__(
        self,
        skill_loader: SkillLoader,
        *,
        include_disabled: bool = False,
        allowed_skill_names: Collection[str] | None = None,
        blocked_skill_names: Collection[str] | None = None,
        explicitly_allowed_skill_names: MutableSet[str] | None = None,
        skill_access_filter: Callable[[Any], bool] | None = None,
        skill_filter: Callable[[Any], bool] | None = None,
    ) -> None:
        self.skill_loader = skill_loader
        self.include_disabled = include_disabled
        self.allowed_skill_names = (
            frozenset(allowed_skill_names) if allowed_skill_names is not None else None
        )
        self.blocked_skill_names = frozenset(blocked_skill_names or ())
        self.explicitly_allowed_skill_names = explicitly_allowed_skill_names
        self.skill_access_filter = skill_access_filter
        self.skill_filter = skill_filter

    @property
    def name(self) -> str:
        return "list_skills"

    @property
    def description(self) -> str:
        return (
            "List or search locally installed Skill names, descriptions and availability. "
            "Use an empty query to browse all available Skills, or an exact name to "
            "diagnose an unavailable Skill. This does not load instructions or access "
            "SkillHub. Follow next_offset for more results; if revision changes, restart "
            "from offset 0. Use get_skill to read a chosen Skill."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "default": ""},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
            },
            "additionalProperties": False,
        }

    def _in_scope(self, name: str) -> bool:
        return self.allowed_skill_names is None or name in self.allowed_skill_names

    def _metadata(self, skill: Skill) -> dict[str, object]:
        reason = None
        # include_disabled controls diagnostic visibility, never availability.
        if self.skill_loader.get_skill(skill.name) is None:
            reason = "Skill is disabled by the current configuration."
        elif skill.broken:
            reason = f"Skill is malformed: {skill.broken_reason or 'invalid SKILL.md'}"
        elif self.skill_access_filter is not None and not self.skill_access_filter(skill):
            reason = "Skill is not enabled for this conversation."
        elif skill.name in self.blocked_skill_names and (
            self.explicitly_allowed_skill_names is None
            or skill.name not in self.explicitly_allowed_skill_names
        ):
            reason = "Skill is blocked by the execution profile unless explicitly selected."
        return {
            **skill.to_metadata_dict(),
            "available": reason is None,
            "unavailable_reason": reason,
        }

    async def execute(
        self, query: str = "", offset: int = 0, limit: int = 20
    ) -> ToolResult:
        issues = validate_tool_arguments(
            self.parameters, {"query": query, "offset": offset, "limit": limit}
        )
        if issues:
            return self._invalid_arguments_result(issues)
        self.skill_loader.maybe_reload()
        query = query.strip()
        skills = [
            skill for skill in self.skill_loader.search_skills(include_disabled=True)
            if self.skill_filter is None or self.skill_filter(skill)
        ]
        catalog = {
            skill.name: self._metadata(skill)
            for skill in skills if self._in_scope(skill.name)
        }
        for diagnostic in self.skill_loader.unavailable_skills_metadata():
            name = str(diagnostic["name"])
            if self._in_scope(name) and (
                self.skill_filter is None
                or self.skill_filter(SimpleNamespace(**diagnostic))
            ):
                catalog.setdefault(name, diagnostic)

        # Scope/policy, source metadata and search terms all affect a page.
        # Skill bodies do not participate in a metadata-only catalog revision.
        revision_payload = {
            "include_disabled": self.include_disabled,
            "catalog": [catalog[name] for name in sorted(catalog)],
            "keywords": {
                skill.name: skill.keywords or []
                for skill in skills if self._in_scope(skill.name)
            },
        }
        revision = sha256(json.dumps(
            revision_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        exact = catalog.get(query)
        if exact is not None and not exact["available"]:
            matches = [exact]
        else:
            matches = [
                catalog[skill.name]
                for skill in self.skill_loader.search_skills(query, include_disabled=True)
                if skill.name in catalog and (
                    catalog[skill.name]["available"]
                    or (self.include_disabled and self.skill_loader.get_skill(skill.name) is None)
                )
            ]
        page = matches[offset:offset + limit]
        next_offset = offset + len(page) if offset + len(page) < len(matches) else None
        payload = {
            "query": query,
            "revision": revision,
            "offset": offset,
            "limit": limit,
            "total": len(matches),
            "next_offset": next_offset,
            "skills": page,
        }
        return ToolResult(
            success=True,
            content=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            raw_output=payload,
        )

"""Paginated, metadata-only discovery of locally installed Skills."""

from collections.abc import Callable, Collection, MutableSet
from hashlib import sha256
import json
from typing import Any
from types import SimpleNamespace

from ..execution_profile import is_skill_blocked
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
        self.skill_loader = skill_loader.conversation_catalog
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
            "Use an empty query to browse public Skills, or an exact name to "
            "inspect an internal method or diagnose an unavailable Skill. "
            "This does not load instructions or access "
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

    def _metadata(self, skill: Skill, loader: SkillLoader) -> dict[str, object]:
        declared = skill.declared_skill or skill
        reason = None
        # include_disabled controls diagnostic visibility, never availability.
        if loader.get_skill(skill.name) is None:
            reason = "Skill is disabled by the current configuration."
        elif skill.broken:
            reason = f"Skill is malformed: {skill.broken_reason or 'invalid SKILL.md'}"
        elif self.skill_access_filter is not None and not self.skill_access_filter(declared):
            reason = "Skill is not enabled for this conversation."
        elif is_skill_blocked(
            skill.name, self.blocked_skill_names, self.explicitly_allowed_skill_names, declared_name=declared.name
        ):
            reason = "Skill is blocked by the execution profile unless explicitly selected."
        return {
            **declared.to_metadata_dict(),
            "skill_name": skill.name,
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
        loader = self.skill_loader.tool_catalog
        await loader.areload()
        query = query.strip()
        skills = [
            skill for skill in loader.search_skills(include_disabled=True)
            if self._in_scope(skill.name)
            and (self.skill_filter is None or self.skill_filter(skill.declared_skill or skill))
        ]
        # Each connector has both its legacy short name (when it wins) and its
        # discovered path. Show one row per file, using the path for ambiguous names.
        paths_by_name = {}
        for skill in skills:
            paths_by_name.setdefault((skill.declared_skill or skill).name, set()).add(skill.skill_path)
        visible = {skill.name: skill for skill in skills}
        for skill in skills:
            declared = skill.declared_skill
            if declared is not None and visible.get(declared.name) is declared:
                redundant = declared.name if len(paths_by_name[declared.name]) > 1 else skill.name
                visible.pop(redundant)
        catalog = {name: self._metadata(skill, loader) for name, skill in visible.items()}
        for diagnostic in loader.unavailable_skills_metadata():
            name = str(diagnostic["name"])
            if self._in_scope(name) and (
                self.skill_filter is None
                or self.skill_filter(SimpleNamespace(**diagnostic))
            ):
                catalog.setdefault(name, {**diagnostic, "skill_name": name})

        # Scope/policy, source metadata and search terms all affect a page.
        # Skill bodies do not participate in a metadata-only catalog revision.
        revision_payload = {
            "include_disabled": self.include_disabled,
            "catalog": [catalog[name] for name in sorted(catalog)],
            "keywords": {
                name: skill.keywords or [] for name, skill in visible.items()
            },
        }
        revision = sha256(json.dumps(
            revision_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        exact = [row for key, row in catalog.items() if key == query or row["name"] == query]
        if exact and (len(exact) > 1 or any(
            not row["available"] or row.get("user_visible") is False or row["name"] != query
            for row in exact
        )):
            matches = sorted(exact, key=lambda row: (row["name"] != row["skill_name"], row["skill_name"]))
        else:
            matches = [
                catalog[skill.name]
                for skill in loader.search_skills(query, include_disabled=True)
                if skill.user_visible and skill.name in catalog and (
                    catalog[skill.name]["available"]
                    or (self.include_disabled and loader.get_skill(skill.name) is None)
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
        if any(row["skill_name"] != row["name"] for row in page):
            payload["read_hint"] = "Pass the chosen row's skill_name to get_skill to read that specific file."
        return ToolResult(
            success=True,
            content=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            raw_output=payload,
        )

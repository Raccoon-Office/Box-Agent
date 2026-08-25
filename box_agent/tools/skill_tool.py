"""
Skill Tool - Tool for Agent to load Skills on-demand

Implements Progressive Disclosure (Level 2): Load full skill content when needed
"""

from pathlib import Path
from hashlib import sha256
from typing import Any, Dict, List, Literal, Mapping, MutableSet, Optional, Tuple

from .base import Tool, ToolResult
from .skill_loader import SkillLoader

SkillSource = Literal["builtin", "user"]


class GetSkillTool(Tool):
    """Tool to get detailed information about a specific skill"""

    aliases = ("skill_view",)
    loads_active_skill_instructions = True

    def __init__(
        self,
        skill_loader: SkillLoader,
        *,
        include_disabled: bool = False,
        preloaded_skill_hashes: Mapping[str, str] | None = None,
        blocked_skill_names: set[str] | frozenset[str] | None = None,
        explicitly_allowed_skill_names: MutableSet[str] | None = None,
        workflow_locked_skill_names: MutableSet[str] | None = None,
    ):
        self.skill_loader = skill_loader
        self.include_disabled = include_disabled
        self.preloaded_skill_hashes = preloaded_skill_hashes
        self.blocked_skill_names = blocked_skill_names or frozenset()
        self.explicitly_allowed_skill_names = explicitly_allowed_skill_names
        self.workflow_locked_skill_names = workflow_locked_skill_names

    @property
    def name(self) -> str:
        return "get_skill"

    @property
    def description(self) -> str:
        return "Get complete content and guidance for a specified skill, used for executing specific types of tasks"

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "skill_name": {
                    "type": "string",
                    "description": "Name of the skill to retrieve (use list_skills to view available skills)",
                }
            },
            "required": ["skill_name"],
        }

    async def execute(self, skill_name: str) -> ToolResult:
        """Get detailed information about specified skill"""
        normalized_name = skill_name.strip()
        if (
            self.workflow_locked_skill_names
            and not self._workflow_allows(normalized_name)
            and (
                self.explicitly_allowed_skill_names is None
                or normalized_name not in self.explicitly_allowed_skill_names
            )
        ):
            active = ", ".join(sorted(self.workflow_locked_skill_names))
            return ToolResult(
                success=False,
                content="",
                error=(
                    f"Skill '{normalized_name}' cannot replace the active workflow "
                    f"Skills ({active}). Follow the preloaded workflow and its "
                    "declared dependencies; do not retry loading a competing Skill."
                ),
            )
        if (
            normalized_name in self.blocked_skill_names
            and not (
                self.preloaded_skill_hashes
                and normalized_name in self.preloaded_skill_hashes
            )
            and (
                self.explicitly_allowed_skill_names is None
                or normalized_name not in self.explicitly_allowed_skill_names
            )
        ):
            return ToolResult(
                success=False,
                content="",
                error=(
                    f"Skill '{normalized_name}' is disabled by the active execution "
                    "profile unless the user explicitly requests it. Continue with "
                    "bounded direct work and do not retry loading this Skill."
                ),
            )

        # Auto-reload if the user skills directory has been touched since last scan
        self.skill_loader.maybe_reload()

        skill = self.skill_loader.get_skill(
            skill_name,
            include_disabled=self.include_disabled,
        )

        if not skill:
            available = ", ".join(
                self.skill_loader.list_skills(include_disabled=self.include_disabled)
            )
            return ToolResult(
                success=False,
                content="",
                error=f"Skill '{skill_name}' does not exist. Available skills: {available}",
            )

        # A broken skill (SKILL.md present but unparseable) returns a
        # diagnostic prompt so the model doesn't invent guidance from a
        # directory name it can't verify. Success is True — this is a real
        # answer to "give me the skill", not a tool failure that should be
        # retried. The rendered content clearly tells the model to ask the
        # user to fix SKILL.md instead of proceeding.
        result = skill.to_prompt()
        if self.preloaded_skill_hashes and self.preloaded_skill_hashes.get(
            skill.name
        ) == sha256(result.encode("utf-8")).hexdigest():
            message = (
                f"Skill '{skill.name}' is already preloaded in this session. "
                "Follow its system instructions directly."
            )
            return ToolResult(
                success=True,
                content=message,
                model_context=message,
            )
        raw_output = None
        if skill.broken:
            raw_output = {
                "broken": True,
                "broken_reason": skill.broken_reason,
                "skill_path": str(skill.skill_path) if skill.skill_path else None,
            }
        return ToolResult(success=True, content=result, raw_output=raw_output)

    def _workflow_allows(self, skill_name: str) -> bool:
        """Allow the active workflow and dependencies it explicitly declares."""
        if not self.workflow_locked_skill_names:
            return True
        if skill_name in self.workflow_locked_skill_names:
            return True
        for active_name in self.workflow_locked_skill_names:
            active_skill = self.skill_loader.get_skill(
                active_name,
                include_disabled=self.include_disabled,
            )
            if active_skill is None:
                continue
            declared = set(active_skill.required_skills or [])
            declared.update(active_skill.related_skills or [])
            if skill_name in declared:
                return True
        return False


def create_skill_tools(
    skills_dir: Optional[str] = None,
    sources: Optional[List[Tuple[str | Path, SkillSource]]] = None,
    defer_discovery: bool = False,
) -> tuple[List[Tool], Optional[SkillLoader]]:
    """Create skill tool for Progressive Disclosure.

    Args:
        skills_dir: Legacy single-directory entry (treated as builtin).
        sources: Ordered list of (directory, source_label) tuples. Earlier entries
            win on name conflicts (e.g. user → builtin).
        defer_discovery: If True, skip the inline ``discover_skills()`` call
            and let the caller schedule discovery on a background task. The
            returned ``GetSkillTool`` still binds to the loader — once the
            background task fills ``loaded_skills``, the tool sees the
            catalog. Used by the ACP path to keep stdio setup off the skill
            file-parse critical path.

    Returns:
        Tuple of (list of tools, skill loader).
    """
    if sources is not None:
        loader = SkillLoader(sources=sources)
    else:
        loader = SkillLoader(skills_dir=skills_dir or "./skills")

    if not defer_discovery:
        skills = loader.discover_skills()
        import sys as _sys

        _sys.stderr.write(f"✅ Discovered {len(skills)} Claude Skills\n")

    tools: List[Tool] = [GetSkillTool(loader)]
    return tools, loader

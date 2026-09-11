"""
Skill Tool - Tool for Agent to load Skills on-demand

Implements Progressive Disclosure (Level 2): Load full skill content when needed
"""

from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Mapping, MutableSet, Optional, Tuple

from .base import Tool, ToolResult, ToolInvocationContext
from .skill_loader import SKILL_USAGE_GUIDANCE, SkillLoader

SkillSource = Literal["builtin", "connector", "user"]


class GetSkillTool(Tool):
    """Tool to get detailed information about a specific skill"""

    aliases = ("skill_view",)
    uses_invocation_context = True

    def __init__(
        self,
        skill_loader: SkillLoader,
        *,
        include_disabled: bool = False,
        allowed_skill_names: frozenset[str] | None = None,
        preloaded_skill_hashes: Mapping[str, str] | None = None,
        blocked_skill_names: set[str] | frozenset[str] | None = None,
        explicitly_allowed_skill_names: MutableSet[str] | None = None,
        skill_access_filter: Callable[[Any], bool] | None = None,
    ):
        self.skill_loader = skill_loader
        self.include_disabled = include_disabled
        self.allowed_skill_names = allowed_skill_names
        self.preloaded_skill_hashes = preloaded_skill_hashes
        self.blocked_skill_names = blocked_skill_names or frozenset()
        self.explicitly_allowed_skill_names = explicitly_allowed_skill_names
        self.skill_access_filter = skill_access_filter

    @property
    def name(self) -> str:
        return "get_skill"

    @property
    def description(self) -> str:
        return (
            "Read a Skill's method and resource paths. Follow next_offset with the returned revision "
            "when paged. Read required_skills before their steps; related_skills are optional. "
            "Skill guidance does not grant tools or permission. Use list_skills for names and availability. "
            + SKILL_USAGE_GUIDANCE
        )

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "skill_name": {
                    "type": "string",
                    "description": "Name of the skill to retrieve (use list_skills to view available skills)",
                },
                "offset": {"type": "integer", "minimum": 0, "description": "Zero-based line offset; omit to read the whole Skill when it fits."},
                "limit": {"type": "integer", "minimum": 1, "description": "Maximum lines for a bounded page."},
                "revision": {"type": "string", "description": "Version returned by a previous page; restart if it changed."},
            },
            "required": ["skill_name"],
            "additionalProperties": False,
        }

    def check_access(self, skill_name: str) -> ToolResult | None:
        """Check this reader's current scope without reading or recording a body."""
        from ..skill_dependencies import resolve_required_skills, SkillDependencyError

        name = skill_name.strip()
        if self.allowed_skill_names is not None and name not in self.allowed_skill_names:
            return ToolResult(success=False, error="Skill is outside this task's assigned scope.")
        if name in self.blocked_skill_names and name not in (self.explicitly_allowed_skill_names or ()):
            return ToolResult(success=False, error=(
                f"Skill '{name}' is disabled by the active execution profile unless the user explicitly requests it. "
                "Continue with bounded direct work and do not retry loading this Skill."))
        self.skill_loader.maybe_reload()
        if self.allowed_skill_names is not None or self.skill_access_filter is not None:
            try:
                dependencies = resolve_required_skills(self.skill_loader, [name])
            except SkillDependencyError as exc:
                return ToolResult(success=False, error=str(exc), raw_output={"code": exc.code})
            if self.allowed_skill_names is not None and any(skill.name not in self.allowed_skill_names for skill in dependencies):
                return ToolResult(success=False, error="Required Skill is outside this task's assigned scope.")
            for skill in dependencies:
                if self.skill_access_filter is not None and not self.skill_access_filter(skill):
                    return ToolResult(success=False, error=(
                        f"Skill '{skill.name}' is not enabled for this conversation. "
                        "Connector Skills can only be enabled through the conversation connector picker."
                    ))
        return None

    def _read(self, skill_name: str, *, reader=None, **kwargs: Any) -> ToolResult:
        from ..skill_runtime import SkillRuntime

        denied = self.check_access(skill_name)
        if denied is not None:
            return denied
        name = skill_name.strip()
        # Legacy preload hashes are not evidence that text survives in this
        # request. Only the session reader can issue a verified reuse receipt.
        read = reader or SkillRuntime(self.skill_loader).read
        return read(name, **kwargs)

    async def execute(self, skill_name: str, offset: int = 0, limit: int | None = None,
                      revision: str | None = None) -> ToolResult:
        return self._read(skill_name, offset=offset, limit=limit, revision=revision)

    async def _invoke_validated(self, arguments: dict[str, Any], *,
                                context: ToolInvocationContext | None) -> ToolResult:
        return self._read(**arguments, reader=context.skill_reader if context is not None else None)


def create_skill_tools(
    skills_dir: Optional[str] = None,
    sources: Optional[List[Tuple[str | Path, SkillSource]]] = None,
    defer_discovery: bool = False,
) -> tuple[List[Tool], Optional[SkillLoader]]:
    """Create skill tool for Progressive Disclosure.

    Args:
        skills_dir: Legacy single-directory entry (treated as builtin).
        sources: Ordered list of (directory, source_label) tuples. Earlier entries
            win on name conflicts (e.g. user → connector → builtin).
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

    from .skill_catalog_tool import ListSkillsTool

    tools: List[Tool] = [GetSkillTool(loader), ListSkillsTool(loader)]
    return tools, loader

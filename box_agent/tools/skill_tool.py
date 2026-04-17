"""
Skill Tool - Tool for Agent to load Skills on-demand

Implements Progressive Disclosure (Level 2): Load full skill content when needed
"""

from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

from .base import Tool, ToolResult
from .skill_loader import SkillLoader

SkillSource = Literal["builtin", "user"]


class GetSkillTool(Tool):
    """Tool to get detailed information about a specific skill"""

    def __init__(self, skill_loader: SkillLoader):
        self.skill_loader = skill_loader

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
        # Auto-reload if the user skills directory has been touched since last scan
        self.skill_loader.maybe_reload()

        skill = self.skill_loader.get_skill(skill_name)

        if not skill:
            available = ", ".join(self.skill_loader.list_skills())
            return ToolResult(
                success=False,
                content="",
                error=f"Skill '{skill_name}' does not exist. Available skills: {available}",
            )

        result = skill.to_prompt()
        return ToolResult(success=True, content=result)


def create_skill_tools(
    skills_dir: Optional[str] = None,
    sources: Optional[List[Tuple[str | Path, SkillSource]]] = None,
) -> tuple[List[Tool], Optional[SkillLoader]]:
    """Create skill tool for Progressive Disclosure.

    Args:
        skills_dir: Legacy single-directory entry (treated as builtin).
        sources: Ordered list of (directory, source_label) tuples. Earlier entries
            win on name conflicts (e.g. user → builtin).

    Returns:
        Tuple of (list of tools, skill loader).
    """
    if sources is not None:
        loader = SkillLoader(sources=sources)
    else:
        loader = SkillLoader(skills_dir=skills_dir or "./skills")

    skills = loader.discover_skills()
    import sys as _sys

    _sys.stderr.write(f"✅ Discovered {len(skills)} Claude Skills\n")

    tools: List[Tool] = [GetSkillTool(loader)]
    return tools, loader

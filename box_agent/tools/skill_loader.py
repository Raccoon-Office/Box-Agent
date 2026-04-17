"""
Skill Loader - Load Claude Skills from multiple sources.

Supports:
- Builtin skills shipped with the package (read-only)
- User skills at ~/.box-agent/skills/ (writable from officev3)
- User skills override builtin ones on name conflict
- mtime-based auto reload (no explicit trigger needed)
"""

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Literal, Optional, Tuple

import yaml

SkillSource = Literal["builtin", "user"]


def _warn(msg: str) -> None:
    """Write diagnostic message to stderr (never stdout)."""
    sys.stderr.write(msg + "\n")


@dataclass
class Skill:
    """Skill data structure"""

    name: str
    description: str
    content: str
    source: SkillSource = "builtin"
    license: Optional[str] = None
    allowed_tools: Optional[List[str]] = None
    metadata: Optional[Dict[str, str]] = None
    skill_path: Optional[Path] = None

    def to_prompt(self) -> str:
        """Convert skill to prompt format"""
        skill_root = str(self.skill_path.parent) if self.skill_path else "unknown"

        return f"""
# Skill: {self.name}

{self.description}

**Skill Root Directory:** `{skill_root}`

All files and references in this skill are relative to this directory.

---

{self.content}
"""

    def to_metadata_dict(self) -> Dict[str, object]:
        """Structured metadata for officev3 / ACP _meta payloads."""
        return {
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "path": str(self.skill_path) if self.skill_path else None,
        }


@dataclass
class _SourceEntry:
    """Internal: a single skills source directory with a label."""

    directory: Path
    source: SkillSource
    last_mtime: float = 0.0


class SkillLoader:
    """Skill loader supporting multiple prioritized sources."""

    def __init__(
        self,
        sources: Optional[List[Tuple[str | Path, SkillSource]]] = None,
        skills_dir: Optional[str] = None,
    ):
        """
        Initialize Skill Loader.

        Args:
            sources: Ordered list of (directory, source_label) tuples. Earlier
                entries take priority on name conflicts. Use this for the
                multi-source workflow (user + builtin).
            skills_dir: Legacy single-directory argument. Treated as a single
                "builtin" source when sources is not provided.
        """
        if sources is None:
            legacy = skills_dir or "./skills"
            sources = [(legacy, "builtin")]

        self._sources: List[_SourceEntry] = [
            _SourceEntry(directory=Path(d).expanduser(), source=s) for d, s in sources
        ]
        self.loaded_skills: Dict[str, Skill] = {}

    # Backward compatibility — expose the first source directory
    @property
    def skills_dir(self) -> Path:
        return self._sources[0].directory if self._sources else Path("./skills")

    def load_skill(self, skill_path: Path, source: SkillSource = "builtin") -> Optional[Skill]:
        """Load a single skill from a SKILL.md file."""
        try:
            content = skill_path.read_text(encoding="utf-8")

            frontmatter_match = re.match(r"^---\n(.*?)\n---\n(.*)$", content, re.DOTALL)
            if not frontmatter_match:
                _warn(f"⚠️  {skill_path} missing YAML frontmatter")
                return None

            frontmatter_text = frontmatter_match.group(1)
            skill_content = frontmatter_match.group(2).strip()

            try:
                frontmatter = yaml.safe_load(frontmatter_text)
            except yaml.YAMLError as e:
                _warn(f"❌ Failed to parse YAML frontmatter: {e}")
                return None

            if "name" not in frontmatter or "description" not in frontmatter:
                _warn(f"⚠️  {skill_path} missing required fields (name or description)")
                return None

            skill_dir = skill_path.parent
            processed_content = self._process_skill_paths(skill_content, skill_dir)

            return Skill(
                name=frontmatter["name"],
                description=frontmatter["description"],
                content=processed_content,
                source=source,
                license=frontmatter.get("license"),
                allowed_tools=frontmatter.get("allowed-tools"),
                metadata=frontmatter.get("metadata"),
                skill_path=skill_path,
            )

        except Exception as e:
            _warn(f"❌ Failed to load skill ({skill_path}): {e}")
            return None

    def _process_skill_paths(self, content: str, skill_dir: Path) -> str:
        """Replace relative paths in skill content with absolute paths."""
        import re

        def replace_dir_path(match):
            prefix = match.group(1)
            rel_path = match.group(2)
            abs_path = skill_dir / rel_path
            if abs_path.exists():
                return f"{prefix}{abs_path}"
            return match.group(0)

        pattern_dirs = r"(python\s+|`)((?:scripts|references|assets)/[^\s`\)]+)"
        content = re.sub(pattern_dirs, replace_dir_path, content)

        def replace_doc_path(match):
            prefix = match.group(1)
            filename = match.group(2)
            suffix = match.group(3)
            abs_path = skill_dir / filename
            if abs_path.exists():
                return f"{prefix}`{abs_path}` (use read_file to access){suffix}"
            return match.group(0)

        pattern_docs = r"(see|read|refer to|check)\s+([a-zA-Z0-9_-]+\.(?:md|txt|json|yaml))([.,;\s])"
        content = re.sub(pattern_docs, replace_doc_path, content, flags=re.IGNORECASE)

        def replace_markdown_link(match):
            prefix = match.group(1) if match.group(1) else ""
            link_text = match.group(2)
            filepath = match.group(3)
            clean_path = filepath[2:] if filepath.startswith("./") else filepath
            abs_path = skill_dir / clean_path
            if abs_path.exists():
                return f"{prefix}[{link_text}](`{abs_path}`) (use read_file to access)"
            return match.group(0)

        pattern_markdown = (
            r"(?:(Read|See|Check|Refer to|Load|View)\s+)?\[(`?[^`\]]+`?)\]"
            r"\(((?:\./)?[^)]+\.(?:md|txt|json|yaml|js|py|html))\)"
        )
        content = re.sub(pattern_markdown, replace_markdown_link, content, flags=re.IGNORECASE)

        return content

    def discover_skills(self) -> List[Skill]:
        """Discover skills from all sources; user overrides builtin on name conflict."""
        self.loaded_skills = {}
        discovered: List[Skill] = []

        # Reverse order: load lower-priority sources first, then higher-priority
        # ones overwrite by dict assignment.
        for entry in reversed(self._sources):
            if not entry.directory.exists():
                continue
            for skill_file in entry.directory.rglob("SKILL.md"):
                skill = self.load_skill(skill_file, source=entry.source)
                if skill:
                    self.loaded_skills[skill.name] = skill

            # Cache mtime for reload detection
            entry.last_mtime = self._dir_mtime(entry.directory)

        discovered = list(self.loaded_skills.values())
        return discovered

    @staticmethod
    def _dir_mtime(directory: Path) -> float:
        """Return the max mtime across the directory tree (cheap recursive stat).

        Used to detect added/removed/modified skill files.
        """
        if not directory.exists():
            return 0.0
        try:
            latest = directory.stat().st_mtime
            for path in directory.rglob("*"):
                try:
                    mt = path.stat().st_mtime
                    if mt > latest:
                        latest = mt
                except OSError:
                    continue
            return latest
        except OSError:
            return 0.0

    def maybe_reload(self) -> bool:
        """Reload skills if any source directory's mtime has changed.

        Returns:
            True if a reload was performed, False otherwise.
        """
        changed = False
        for entry in self._sources:
            current = self._dir_mtime(entry.directory)
            if current != entry.last_mtime:
                changed = True
                break

        if changed:
            self.discover_skills()
        return changed

    def get_skill(self, name: str) -> Optional[Skill]:
        """Get a loaded skill by name."""
        return self.loaded_skills.get(name)

    def list_skills(self) -> List[str]:
        """List all loaded skill names."""
        return list(self.loaded_skills.keys())

    def list_skills_metadata(self) -> List[Dict[str, object]]:
        """Return structured metadata for every loaded skill.

        Intended for officev3 / ACP `_meta.skills` payloads.
        """
        return [skill.to_metadata_dict() for skill in self.loaded_skills.values()]

    def get_skills_metadata_prompt(self) -> str:
        """Generate a metadata-only prompt for Progressive Disclosure Level 1."""
        if not self.loaded_skills:
            return ""

        prompt_parts = ["## Available Skills\n"]
        prompt_parts.append(
            "You have access to specialized skills. Each skill provides expert guidance for specific tasks.\n"
        )
        prompt_parts.append(
            "Load a skill's full content using the appropriate skill tool when needed.\n"
        )

        for skill in self.loaded_skills.values():
            prompt_parts.append(f"- `{skill.name}`: {skill.description}")

        return "\n".join(prompt_parts)

"""
Skill Loader - Load Claude Skills from multiple sources.

Supports:
- Builtin skills shipped with the package (read-only)
- User skills at ~/.box-agent/skills/ (writable from officev3)
- User skills override builtin ones on name conflict, except reserved runtime skills
- mtime-based auto reload (no explicit trigger needed)
- Manifest-based whitelist for builtin sources: any SKILL.md left on disk
  (e.g. by a downstream host that updated box-agent without deleting old
  files) but absent from ``_manifest.json`` is ignored as an orphan.
"""

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Set, Tuple

import yaml

SkillSource = Literal["builtin", "user"]

MANIFEST_FILENAME = "_manifest.json"
RESERVED_BUILTIN_SKILL_NAMES = frozenset({"roadmap"})


def _warn(msg: str) -> None:
    """Write diagnostic message to stderr (never stdout)."""
    sys.stderr.write(msg + "\n")


_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+")


def _tokenize(text: str) -> Set[str]:
    """Tokenize mixed zh/en text into a set of matchable tokens.

    English: lowercased word chunks, length >= 2.
    Chinese: the full run plus every 2-char sliding window
    (so "邮件" matches "发邮件" and "邮件草稿").
    """
    if not text:
        return set()
    tokens: Set[str] = set()
    for chunk in _TOKEN_RE.findall(text.lower()):
        if "\u4e00" <= chunk[0] <= "\u9fff":
            tokens.add(chunk)
            for i in range(len(chunk) - 1):
                tokens.add(chunk[i : i + 2])
        elif len(chunk) >= 2:
            tokens.add(chunk)
    return tokens


SKILL_SLOT_SENTINEL = "__BOX_AGENT_SKILLS_SLOT__"
SKILL_SETTINGS_PATH = Path.home() / ".box-agent" / "config" / "skill-settings.json"
_SKILL_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


def move_skill_slot_to_end(system_prompt_text: str) -> str:
    """Move the progressive skill metadata slot to the final prompt position."""
    if SKILL_SLOT_SENTINEL not in system_prompt_text:
        return system_prompt_text
    without_slot = system_prompt_text.replace(SKILL_SLOT_SENTINEL, "").rstrip()
    if not without_slot:
        return SKILL_SLOT_SENTINEL
    return f"{without_slot}\n\n{SKILL_SLOT_SENTINEL}"


def _read_disabled_skill_names(settings_path: Optional[Path]) -> Set[str]:
    """Read officev3 skill enable/disable state.

    The file is optional and owned by the desktop app. Missing or malformed
    settings should never break agent startup; they simply mean all skills are
    enabled.
    """
    if settings_path is None:
        return set()

    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()

    raw_names = data.get("disabledSkillNames") if isinstance(data, dict) else None
    if not isinstance(raw_names, list):
        return set()

    return {
        name
        for name in raw_names
        if isinstance(name, str) and _SKILL_NAME_RE.match(name)
    }


@dataclass
class Skill:
    """Skill data structure.

    A skill can be ``broken`` — meaning the SKILL.md file was present but
    couldn't be parsed (bad YAML, missing name/description, unreadable file,
    frontmatter that isn't a mapping). In that case Hermes-style directory
    name is used as ``name``, ``description`` explains the failure, and
    ``content`` is empty. Broken skills stay in the catalog on purpose:
    users who authored the skill deserve to see that it exists but is
    misconfigured — the alternative (silently dropping it) sends them
    hunting for a skill they can't find. Loading the full content via
    ``get_skill`` returns a diagnostic instead of pushing empty content
    into the model.
    """

    name: str
    description: str
    content: str
    source: SkillSource = "builtin"
    license: Optional[str] = None
    allowed_tools: Optional[List[str]] = None
    metadata: Optional[Dict[str, Any]] = None
    skill_path: Optional[Path] = None
    keywords: Optional[List[str]] = None
    required_skills: Optional[List[str]] = None
    related_skills: Optional[List[str]] = None
    capabilities: Optional[List[str]] = None
    workflow: Optional[str] = None
    broken: bool = False
    broken_reason: Optional[str] = None

    def to_prompt(self) -> str:
        """Convert skill to prompt format.

        For a broken skill, return an unmistakable diagnostic instead of an
        empty content block so the model doesn't waste a turn trying to
        "follow the skill" that isn't there.
        """
        skill_root = str(self.skill_path.parent) if self.skill_path else "unknown"

        if self.broken:
            reason = self.broken_reason or "unknown parse failure"
            return f"""
# Skill: {self.name}  ⚠️  UNAVAILABLE

This skill's SKILL.md exists but could not be loaded: **{reason}**

**Skill Root Directory:** `{skill_root}`

Ask the user to fix the SKILL.md frontmatter (`name`, `description` and
valid YAML) before using this skill. Do NOT invent guidance based on the
directory name — you have no reliable content for this skill.
"""

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
            "allowed_tools": self.allowed_tools or [],
            "required_skills": self.required_skills or [],
            "related_skills": self.related_skills or [],
            "capabilities": self.capabilities or [],
            "workflow": self.workflow,
            "broken": self.broken,
            "broken_reason": self.broken_reason,
        }


@dataclass
class _SourceEntry:
    """Internal: a single skills source directory with a label."""

    directory: Path
    source: SkillSource
    last_mtime: float = 0.0
    signature: Tuple[Tuple[str, int, int], ...] = field(default_factory=tuple)
    # Optional whitelist of skill names. None means "no manifest, accept all".
    # Empty set means "manifest present but lists zero skills" → load nothing.
    manifest_names: Optional[Set[str]] = None
    # Optional manifest-listed SKILL.md paths. None means "scan with rglob".
    manifest_paths: Optional[Tuple[Path, ...]] = None
    manifest_loaded: bool = False


class SkillLoader:
    """Skill loader supporting multiple prioritized sources.

    Parse errors from individual SKILL.md files are accumulated in
    ``self.parse_errors`` and summarized once at the end of
    :meth:`discover_skills`. This matters on ACP startup: a downstream host
    that drops in dozens of malformed skills used to spam stderr per file
    (each ``sys.stderr.write`` is a real syscall on Windows) and blow the
    host's ``initialize`` timeout. Aggregating keeps the boot path fast and
    still surfaces the count for diagnostics.
    """

    def __init__(
        self,
        sources: Optional[List[Tuple[str | Path, SkillSource]] | str | Path] = None,
        skills_dir: Optional[str] = None,
        skill_settings_path: Optional[str | Path] = None,
    ):
        """
        Initialize Skill Loader.

        Args:
            sources: Ordered list of (directory, source_label) tuples. Earlier
                entries take priority on name conflicts. Also accepts a single
                str/Path for legacy single-directory usage (treated as
                "builtin" source).
            skills_dir: Legacy single-directory keyword. Treated as a single
                "builtin" source when sources is not provided.
        """
        if isinstance(sources, (str, Path)):
            sources = [(sources, "builtin")]
        elif sources is None:
            legacy = skills_dir or "./skills"
            sources = [(legacy, "builtin")]

        self._sources: List[_SourceEntry] = [
            _SourceEntry(directory=Path(d).expanduser(), source=s) for d, s in sources
        ]
        self._skill_settings_path: Optional[Path] = (
            Path(skill_settings_path).expanduser()
            if skill_settings_path
            else self._default_skill_settings_path()
        )
        self._skill_settings_signature: tuple[str, int, int] | None = None
        self.loaded_skills: Dict[str, Skill] = {}
        self._all_skills: Dict[str, Skill] = {}
        # Accumulated (path, reason) pairs from the most recent discover_skills
        # run. Reset at the start of each discovery so callers can react to a
        # single pass without seeing stale data from earlier reloads.
        self.parse_errors: List[Tuple[Path, str]] = []

    @staticmethod
    def _parse_skill_name_list(raw_value: object) -> Optional[List[str]]:
        """Normalize frontmatter skill-name lists.

        Supports either YAML lists or comma/whitespace-separated strings so
        skill authors can keep routing metadata lightweight.
        """
        if isinstance(raw_value, str):
            raw_names = re.split(r"[,，\s]+", raw_value)
        elif isinstance(raw_value, list):
            raw_names = [str(name) for name in raw_value]
        else:
            return None

        names: List[str] = []
        for raw_name in raw_names:
            name = raw_name.strip()
            if name and _SKILL_NAME_RE.match(name) and name not in names:
                names.append(name)
        return sorted(names) or None

    @staticmethod
    def _parse_skill_name(raw_value: object) -> Optional[str]:
        if not isinstance(raw_value, str):
            return None
        name = raw_value.strip()
        return name if name and _SKILL_NAME_RE.match(name) else None

    # Backward compatibility — expose the first source directory
    @property
    def skills_dir(self) -> Path:
        return self._sources[0].directory if self._sources else Path("./skills")

    def with_expert_skill_sources(self, skill_names: List[str]) -> "SkillLoader":
        """Clone this loader with uninstalled bundled skills requested by an expert.

        Recommended skills intentionally stay out of the builtin manifest until
        a user installs them. The clone adds only the exact requested skill
        directories, keeping this capability scoped to the expert session.
        """
        requested_names = [
            name.strip()
            for name in skill_names
            if isinstance(name, str) and _SKILL_NAME_RE.match(name.strip())
        ]
        if not requested_names:
            return self

        extra_sources: List[Tuple[Path, SkillSource]] = []
        seen_names: Set[str] = set()
        for name in requested_names:
            if name in seen_names or self.get_skill(name, include_disabled=True):
                continue
            seen_names.add(name)
            for entry in self._sources:
                if entry.source != "builtin":
                    continue
                candidate = entry.directory / name
                skill_path = candidate / "SKILL.md"
                if not skill_path.is_file():
                    continue
                skill = self.load_skill(skill_path, source="builtin")
                if skill is not None and skill.name == name:
                    extra_sources.append((candidate, "builtin"))
                    break

        if not extra_sources:
            return self

        loader = SkillLoader(
            sources=[
                *((entry.directory, entry.source) for entry in self._sources),
                *extra_sources,
            ],
            skill_settings_path=self._skill_settings_path,
        )
        loader.discover_skills()
        return loader

    def _default_skill_settings_path(self) -> Optional[Path]:
        """Use officev3 skill settings only for the officev3 user-skill source.

        Tests and standalone loaders often point at temporary skill roots; they
        must not be affected by the developer machine's real desktop settings.
        """
        user_skills_dir = Path.home() / ".box-agent" / "skills"
        for entry in self._sources:
            try:
                if entry.directory.expanduser().resolve() == user_skills_dir.resolve():
                    return SKILL_SETTINGS_PATH
            except OSError:
                if entry.directory.expanduser() == user_skills_dir:
                    return SKILL_SETTINGS_PATH
        return None

    def _broken_placeholder(
        self,
        skill_path: Path,
        source: SkillSource,
        reason: str,
    ) -> Skill:
        """Build a directory-name placeholder for a SKILL.md that failed to load.

        Mirrors Hermes' fallback behavior — a broken skill stays visible so
        the author knows it exists but is misconfigured, instead of silently
        vanishing from ``## Available Skills`` and confusing them.
        The record is also appended to ``self.parse_errors`` so operators
        still get the aggregate stderr summary.
        """
        self.parse_errors.append((skill_path, reason))
        return Skill(
            name=skill_path.parent.name,
            description=f"(SKILL.md malformed — {reason})",
            content="",
            source=source,
            skill_path=skill_path,
            broken=True,
            broken_reason=reason,
        )

    def load_skill(self, skill_path: Path, source: SkillSource = "builtin") -> Optional[Skill]:
        """Load a single skill from a SKILL.md file.

        On a parse failure the return value is a *broken placeholder*: a
        Skill built from the directory name with an empty content block
        and ``broken=True`` set. Callers can filter with ``skill.broken``
        when they want to hide malformed entries. The parse reason is also
        recorded in ``self.parse_errors`` so ``discover_skills`` can emit
        one aggregate summary line. Returns ``None`` only when we can't
        even determine a placeholder name (e.g. path outside a directory).
        """
        try:
            content = skill_path.read_text(encoding="utf-8")
        except OSError as e:
            return self._broken_placeholder(skill_path, source, f"unreadable file: {e}")
        except Exception as e:  # pragma: no cover — defensive
            return self._broken_placeholder(skill_path, source, f"unexpected read error: {e}")

        try:
            frontmatter_match = re.match(r"^---\n(.*?)\n---\n(.*)$", content, re.DOTALL)
            if not frontmatter_match:
                return self._broken_placeholder(
                    skill_path, source, "missing YAML frontmatter"
                )

            frontmatter_text = frontmatter_match.group(1)
            skill_content = frontmatter_match.group(2).strip()

            try:
                frontmatter = yaml.safe_load(frontmatter_text)
            except yaml.YAMLError as e:
                return self._broken_placeholder(
                    skill_path, source, f"YAML parse error: {e}"
                )

            if not isinstance(frontmatter, dict):
                return self._broken_placeholder(
                    skill_path, source, "frontmatter is not a YAML mapping"
                )

            if "name" not in frontmatter or "description" not in frontmatter:
                return self._broken_placeholder(
                    skill_path,
                    source,
                    "missing required fields (name or description)",
                )

            skill_dir = skill_path.parent
            processed_content = self._process_skill_paths(skill_content, skill_dir)

            raw_keywords = frontmatter.get("keywords")
            if isinstance(raw_keywords, str):
                keywords_list = [k.strip() for k in re.split(r"[,，\s]+", raw_keywords) if k.strip()]
            elif isinstance(raw_keywords, list):
                keywords_list = [str(k).strip() for k in raw_keywords if str(k).strip()]
            else:
                keywords_list = None

            required_skills = self._parse_skill_name_list(
                frontmatter.get("required_skills", frontmatter.get("required-skills"))
            )
            related_skills = self._parse_skill_name_list(
                frontmatter.get("related_skills", frontmatter.get("related-skills"))
            )
            allowed_tools = self._parse_skill_name_list(
                frontmatter.get("allowed_tools", frontmatter.get("allowed-tools"))
            )

            raw_metadata = frontmatter.get("metadata")
            metadata = raw_metadata if isinstance(raw_metadata, dict) else None
            capabilities = self._parse_skill_name_list(
                frontmatter.get(
                    "capabilities",
                    frontmatter.get(
                        "capability",
                        metadata.get("capabilities", metadata.get("capability"))
                        if metadata
                        else None,
                    ),
                )
            )
            workflow = self._parse_skill_name(
                frontmatter.get(
                    "workflow",
                    frontmatter.get(
                        "workflow_kind",
                        metadata.get("workflow", metadata.get("workflow_kind"))
                        if metadata
                        else None,
                    ),
                )
            )

            return Skill(
                name=frontmatter["name"],
                description=frontmatter["description"],
                content=processed_content,
                source=source,
                license=frontmatter.get("license"),
                allowed_tools=allowed_tools,
                metadata=metadata,
                skill_path=skill_path,
                keywords=keywords_list,
                required_skills=required_skills,
                related_skills=related_skills,
                capabilities=capabilities,
                workflow=workflow,
            )

        except Exception as e:
            return self._broken_placeholder(skill_path, source, f"unexpected error: {e}")

    def _process_skill_paths(self, content: str, skill_dir: Path) -> str:
        """Replace relative paths in skill content with absolute paths."""
        import re

        # Skills may need to embed their installed root in shell snippets.
        # Resolve the explicit placeholder before the narrower relative-path
        # rewrites below so the model never has to guess a package/user path.
        content = content.replace("{skill_dir}", str(skill_dir.resolve()))

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
        """Discover skills, preserving canonical implementations of reserved runtimes."""
        self.loaded_skills = {}
        self._all_skills = {}
        # Reset per-run parse errors so callers always see the current pass only.
        self.parse_errors = []
        orphan_count = 0
        reserved_override_count = 0
        discovered: List[Skill] = []
        disabled_skill_names = _read_disabled_skill_names(self._skill_settings_path)

        # Reverse order: load lower-priority sources first, then higher-priority
        # ones overwrite by dict assignment.
        for entry in reversed(self._sources):
            if not entry.directory.exists():
                continue

            # Manifest only applies to builtin sources. For user skills we
            # never want to hide SKILL.md files the user (or officev3) dropped
            # in at runtime.
            if entry.source == "builtin":
                self._load_manifest(entry)

            for skill_file in self._iter_skill_files(entry):
                skill = self.load_skill(skill_file, source=entry.source)
                if skill is None:
                    continue

                if (
                    entry.source == "builtin"
                    and entry.manifest_names is not None
                    and skill.name not in entry.manifest_names
                ):
                    # Orphan builtin skill (installer left old files behind).
                    # Silent by default; the aggregate count is logged below.
                    orphan_count += 1
                    continue

                if (
                    entry.source == "user"
                    and skill.name in RESERVED_BUILTIN_SKILL_NAMES
                ):
                    # These skills own host-negotiated runtime contracts.  A
                    # user prompt skill may extend the workflow under another
                    # name, but must not replace the packaged implementation.
                    reserved_override_count += 1
                    continue

                self._all_skills[skill.name] = skill

                if skill.name in disabled_skill_names:
                    self.loaded_skills.pop(skill.name, None)
                    continue

                self.loaded_skills[skill.name] = skill

            # Cache a cheap signature for reload detection. Keep last_mtime for
            # backward compatibility with older tests/debug code that may read it.
            entry.signature = self._source_signature(entry)
            entry.last_mtime = max((mtime for _, mtime, _ in entry.signature), default=0) / 1_000_000_000

        self._skill_settings_signature = self._file_signature(self._skill_settings_path)
        discovered = list(self.loaded_skills.values())

        # Aggregate diagnostics: one line total, not one per broken file. The
        # first few offending paths are attached to help operators locate them
        # without spamming the log on directories with dozens of broken skills.
        if self.parse_errors:
            sample = "; ".join(
                f"{path.name}: {reason}" for path, reason in self.parse_errors[:3]
            )
            more = (
                f" (+{len(self.parse_errors) - 3} more)"
                if len(self.parse_errors) > 3
                else ""
            )
            _warn(
                f"⚠️  Skipped {len(self.parse_errors)} malformed SKILL.md file(s): "
                f"{sample}{more}"
            )
        if orphan_count:
            _warn(
                f"⚠️  Ignored {orphan_count} orphan builtin skill(s) not listed in "
                f"{MANIFEST_FILENAME} (leftovers from a previous installer)."
            )
        if reserved_override_count:
            _warn(
                f"⚠️  Ignored {reserved_override_count} user skill override(s) for "
                "reserved builtin runtime names. Rename the user skill to extend it."
            )

        return discovered

    def _skill_pool(self, include_disabled: bool = False) -> Dict[str, Skill]:
        if include_disabled:
            return getattr(self, "_all_skills", self.loaded_skills) or self.loaded_skills
        return self.loaded_skills

    def _iter_skill_files(self, entry: _SourceEntry) -> List[Path]:
        """Return candidate SKILL.md files for one source.

        Builtin package skills usually ship a manifest with explicit paths; use
        it to avoid walking large resource trees such as OOXML schemas or JS
        bundles on every discovery. User skills keep recursive discovery so
        officev3-authored skills are picked up without regenerating a manifest.
        """
        if (
            entry.source == "builtin"
            and entry.manifest_names is not None
            and entry.manifest_paths is not None
        ):
            return [path for path in entry.manifest_paths if path.is_file()]

        return list(entry.directory.rglob("SKILL.md"))

    def _load_manifest(self, entry: _SourceEntry) -> None:
        """Populate ``entry.manifest_names`` from ``_manifest.json`` if present.

        Missing manifest → ``manifest_names`` stays ``None`` (no filtering),
        preserving backward compatibility with builtin skills directories that
        pre-date the manifest (dev trees, third-party bundles, etc.).
        """

        manifest_path = entry.directory / MANIFEST_FILENAME
        if not manifest_path.is_file():
            entry.manifest_names = None
            entry.manifest_paths = None
            entry.manifest_loaded = True
            return

        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _warn(
                f"⚠️  Failed to read builtin skills manifest at {manifest_path}: {exc}. "
                f"Falling back to unfiltered discovery."
            )
            entry.manifest_names = None
            entry.manifest_paths = None
            entry.manifest_loaded = True
            return

        raw_skills = data.get("skills") if isinstance(data, dict) else None
        if not isinstance(raw_skills, list):
            _warn(
                f"⚠️  Builtin skills manifest {manifest_path} is malformed "
                f"(missing 'skills' list); falling back to unfiltered discovery."
            )
            entry.manifest_names = None
            entry.manifest_paths = None
            entry.manifest_loaded = True
            return

        names: Set[str] = set()
        paths: list[Path] = []
        all_paths_known = True
        for item in raw_skills:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                if not self._manifest_item_is_available(item):
                    continue
                names.add(item["name"])
                raw_path = item.get("path")
                if isinstance(raw_path, str) and raw_path.strip():
                    paths.append(entry.directory / raw_path)
                else:
                    all_paths_known = False
            elif isinstance(item, str):
                names.add(item)
                all_paths_known = False
        entry.manifest_names = names
        entry.manifest_paths = tuple(paths) if all_paths_known else None
        entry.manifest_loaded = True

    @staticmethod
    def _manifest_item_is_available(item: dict[str, object]) -> bool:
        """Return whether an optional builtin host/platform contract is met."""

        raw = item.get("availability")
        if raw is None:
            return True
        if not isinstance(raw, dict):
            return False

        platforms = raw.get("platforms")
        if platforms is not None:
            if not isinstance(platforms, list) or not all(
                isinstance(platform, str) and platform for platform in platforms
            ):
                return False
            if sys.platform not in platforms:
                return False

        required_env_paths = raw.get("required_env_paths")
        if required_env_paths is not None:
            if not isinstance(required_env_paths, list) or not all(
                isinstance(name, str) and name for name in required_env_paths
            ):
                return False
            for name in required_env_paths:
                value = os.environ.get(name, "").strip()
                if not value or not Path(value).expanduser().exists():
                    return False

        return platforms is not None or required_env_paths is not None

    @staticmethod
    def _stat_signature(path: Path, root: Path) -> tuple[str, int, int] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            rel = str(path)
        return (rel, stat.st_mtime_ns, stat.st_size)

    @staticmethod
    def _file_signature(path: Optional[Path]) -> tuple[str, int, int] | None:
        if path is None:
            return None
        try:
            stat = path.stat()
        except OSError:
            return None
        return (str(path), stat.st_mtime_ns, stat.st_size)

    def _source_signature(self, entry: _SourceEntry) -> Tuple[Tuple[str, int, int], ...]:
        """Return a lightweight signature for files that affect skill loading."""
        if not entry.directory.exists():
            return ()

        candidates: list[Path] = []
        manifest = entry.directory / MANIFEST_FILENAME
        if manifest.is_file():
            candidates.append(manifest)

        if (
            entry.source == "builtin"
            and entry.manifest_names is not None
            and entry.manifest_paths is not None
        ):
            candidates.extend(entry.manifest_paths)
        else:
            candidates.extend(entry.directory.rglob("SKILL.md"))

        signatures = [
            signature
            for path in candidates
            if (signature := self._stat_signature(path, entry.directory)) is not None
        ]
        return tuple(sorted(signatures))

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
        if hasattr(self, "_skill_settings_path"):
            if (
                self._file_signature(self._skill_settings_path)
                != self._skill_settings_signature
            ):
                changed = True
        for entry in self._sources:
            current = self._source_signature(entry)
            if current != entry.signature:
                changed = True
                break

        if changed:
            self.discover_skills()
        return changed

    def get_skill(self, name: str, *, include_disabled: bool = False) -> Optional[Skill]:
        """Get a loaded skill by name."""
        return self._skill_pool(include_disabled=include_disabled).get(name)

    def list_skills(self, *, include_disabled: bool = False) -> List[str]:
        """List all loaded skill names."""
        return list(self._skill_pool(include_disabled=include_disabled).keys())

    def list_skills_metadata(self, *, include_disabled: bool = False) -> List[Dict[str, object]]:
        """Return structured metadata for every loaded skill.

        Intended for officev3 / ACP `_meta.skills` payloads.
        """
        return [
            skill.to_metadata_dict()
            for skill in self._skill_pool(include_disabled=include_disabled).values()
        ]

    def filter_by_query(
        self,
        query: Optional[str],
        *,
        always_on: frozenset[str] = frozenset({"memory-guide"}),
        max_skills: int = 16,
        include_disabled: bool = False,
    ) -> List[Skill]:
        """Return skills relevant to ``query`` plus the always_on set.

        Matching strategy: tokenize query and each skill's (name, keywords,
        description) via :func:`_tokenize`. Score = name_overlap*5 +
        keywords_overlap*3 + description_overlap*1. Top ``max_skills`` by
        score (score > 0) are returned, then each matched skill's
        required_skills and related_skills are added one hop when available,
        followed by always_on skills.

        Empty / whitespace-only / no-overlap query → only always_on skills.
        This is intentional: greetings like "hi" / "你好" should NOT trigger
        the full skill catalog injection.
        """
        skill_pool = self._skill_pool(include_disabled=include_disabled)
        always_skills = [s for s in skill_pool.values() if s.name in always_on]

        if not query or not query.strip():
            return always_skills

        query_tokens = _tokenize(query)
        if not query_tokens:
            return always_skills

        scored: List[Tuple[int, Skill]] = []
        for skill in skill_pool.values():
            if skill.name in always_on:
                continue
            try:
                name_overlap = len(query_tokens & _tokenize(skill.name))
                if skill.broken:
                    # A broken skill's description is a diagnostic string
                    # ("(SKILL.md malformed — YAML parse error: ...)") which
                    # contains generic english tokens (error, parse, scanning)
                    # that would incorrectly match unrelated user queries.
                    # Only surface it when the query hits its directory name,
                    # so the author who wrote the broken skill can still see
                    # it in ## Available Skills by asking about it by name.
                    score = name_overlap * 5
                else:
                    kw_overlap = len(query_tokens & _tokenize(" ".join(skill.keywords or [])))
                    desc_overlap = len(query_tokens & _tokenize(skill.description))
                    score = name_overlap * 5 + kw_overlap * 3 + desc_overlap
            except Exception as exc:
                _warn(
                    "Skipped skill during query filtering: "
                    f"name={skill.name!r}, path={skill.skill_path}, error={exc}"
                )
                continue
            if score > 0:
                scored.append((score, skill))

        scored.sort(key=lambda x: (-x[0], x[1].name))
        primary_matches = [s for _, s in scored[:max_skills]]
        matched: List[Skill] = []
        seen: Set[str] = set()

        def append_skill(skill_name: str) -> None:
            if skill_name in seen:
                return
            skill = skill_pool.get(skill_name)
            if skill is None:
                return
            matched.append(skill)
            seen.add(skill.name)

        for skill in primary_matches:
            append_skill(skill.name)
        for skill in primary_matches:
            for skill_name in (skill.required_skills or []) + (skill.related_skills or []):
                append_skill(skill_name)

        for skill in always_skills:
            append_skill(skill.name)
        return matched

    def get_skills_metadata_prompt(
        self,
        query: Optional[str] = None,
        *,
        include_disabled: bool = False,
    ) -> str:
        """Generate a metadata-only prompt for Progressive Disclosure Level 1.

        When ``query`` is provided, only skills matched by
        :meth:`filter_by_query` (plus always_on) are listed. When ``query`` is
        ``None``, all loaded skills are listed (legacy behavior — kept so
        callers that have not adopted filtering still work).
        """
        skill_pool = self._skill_pool(include_disabled=include_disabled)
        if not skill_pool:
            return ""

        if query is None:
            skills_to_render = list(skill_pool.values())
        else:
            skills_to_render = self.filter_by_query(
                query,
                include_disabled=include_disabled,
            )

        prompt_parts = ["## Available Skills\n"]
        prompt_parts.append(
            "You have access to specialized skills. Each skill provides expert guidance for specific tasks.\n"
        )
        prompt_parts.append(
            "Load a skill's full content using the appropriate skill tool when needed.\n"
        )

        if self._sources:
            prompt_parts.append("**Skill source directories (the ONLY places skills are loaded from):**")
            for entry in self._sources:
                prompt_parts.append(f"- `{entry.source}`: `{entry.directory}`")
            prompt_parts.append(
                "Do NOT search any other directory for skills. "
                "If the user asks where skills are stored, answer with the paths above. "
                "Custom skills should be added under the `user` source directory."
            )
            prompt_parts.append("")

        if not skills_to_render:
            prompt_parts.append(
                "**Skill catalog:** (no skills matched the current request; "
                "call `list_skills` if you need to discover available skills.)"
            )
        else:
            prompt_parts.append("**Skill catalog:**")
            for skill in skills_to_render:
                routing_hints = []
                if skill.allowed_tools:
                    routing_hints.append(
                        f"allowed tools: {', '.join(skill.allowed_tools)}"
                    )
                if skill.required_skills:
                    routing_hints.append(
                        f"required: {', '.join(skill.required_skills)}"
                    )
                if skill.related_skills:
                    routing_hints.append(
                        f"related: {', '.join(skill.related_skills)}"
                    )
                if skill.capabilities:
                    routing_hints.append(
                        f"capabilities: {', '.join(skill.capabilities)}"
                    )
                if skill.workflow:
                    routing_hints.append(f"workflow: {skill.workflow}")
                routing_suffix = (
                    f" [{'; '.join(routing_hints)}]"
                    if routing_hints
                    else ""
                )
                # Broken skill (SKILL.md present but malformed) is rendered
                # with an unmistakable prefix so the model knows not to try
                # to use it. `get_skill` returns a diagnostic when called on
                # one of these.
                broken_prefix = "⚠️ " if skill.broken else ""
                prompt_parts.append(
                    f"- {broken_prefix}`{skill.name}` ({skill.source}): {skill.description}{routing_suffix}"
                )

        return "\n".join(prompt_parts)


class SkillSelector:
    """Stateful helper that filters skill metadata in the system prompt
    based on the cumulative user query.

    Use:
        selector = SkillSelector(skill_loader)
        # After Agent() has finalized its system message:
        selector.bind(agent.messages[0].content)
        # Before each turn:
        new_prompt = selector.update(user_input)
        if new_prompt is not None:
            agent.messages[0].content = new_prompt

    Cumulative semantics: each call to ``update`` appends the new user
    input to the running query string. Filtered skill set grows
    monotonically across turns — once a skill is matched, it stays.
    Returns ``None`` when nothing changed so the caller can preserve
    cache-friendly prompt stability.
    """

    SLOT = SKILL_SLOT_SENTINEL

    def __init__(self, skill_loader: "SkillLoader", *, include_disabled: bool = False) -> None:
        self._loader = skill_loader
        self._include_disabled = include_disabled
        self._prefix: Optional[str] = None
        self._suffix: Optional[str] = None
        self._cumulative: List[str] = []
        self._last_sig: Tuple[str, ...] = ()
        self._last_matched_names: Tuple[str, ...] = ()

    @property
    def bound(self) -> bool:
        return self._prefix is not None

    @property
    def cumulative_query(self) -> str:
        """Accumulated user-input query joined with spaces.

        Other selectors (e.g. lazy MCP gating) can reuse this so they share a
        single source of truth for what the session has been about.
        """
        return " ".join(self._cumulative)

    @property
    def matched_skill_names(self) -> Tuple[str, ...]:
        """Skill names matched by the most recent update, in rendered order."""
        return self._last_matched_names

    def bind(self, system_prompt_text: str) -> None:
        """Capture the prefix and suffix around the skill slot sentinel.

        Always resets ``_last_sig`` so the next ``update()`` call is
        guaranteed to materialize a real catalog (replacing the sentinel)
        even if the skill set has not changed since the previous turn.
        """
        if self.SLOT not in system_prompt_text:
            self._prefix = None
            self._suffix = None
            return
        head, _, tail = system_prompt_text.partition(self.SLOT)
        self._prefix = head
        self._suffix = tail
        self._last_sig = ()

    def update(self, user_input: str) -> Optional[str]:
        """Update cumulative query and return new system prompt text.

        Returns ``None`` when the helper is not bound or the resulting
        skill set is identical to the previous turn.
        """
        if self._prefix is None or self._suffix is None:
            return None
        self._loader.maybe_reload()
        if user_input and user_input.strip():
            self._cumulative.append(user_input.strip())
        query = " ".join(self._cumulative)
        if not query:
            skills_md = ""
            sig: Tuple[str, ...] = ()
            matched_names: Tuple[str, ...] = ()
        else:
            skills = self._loader.filter_by_query(
                query,
                include_disabled=self._include_disabled,
            )
            matched_names = tuple(s.name for s in skills)
            sig = tuple(sorted(matched_names))
            if skills:
                skills_md = self._loader.get_skills_metadata_prompt(
                    query=query,
                    include_disabled=self._include_disabled,
                )
            else:
                skills_md = ""
        self._last_matched_names = matched_names
        if sig == self._last_sig:
            return None
        self._last_sig = sig
        return self._prefix + skills_md + self._suffix

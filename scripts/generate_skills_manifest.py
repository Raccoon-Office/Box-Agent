"""Generate _manifest.json for box_agent/skills/ (builtin skills whitelist).

The manifest is the single source of truth for which builtin skills should be
loaded at runtime. Any SKILL.md found inside the builtin skills directory but
not listed here is treated as an orphan (e.g. left over by a non-deleting
package update on a downstream host like officev3) and ignored by SkillLoader.

Run before each release:

    python scripts/generate_skills_manifest.py

The script writes ``box_agent/skills/_manifest.json`` and then it must be
committed to git so that the file ships inside the wheel (covered by
``recursive-include box_agent/skills *`` in MANIFEST.in).
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Tuple

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "box_agent" / "skills"
MANIFEST_PATH = SKILLS_DIR / "_manifest.json"

# Only host contracts and core Office workflows belong in the builtin catalog.
# Every other SKILL.md may remain in the wheel/runtime during the marketplace
# migration, but is intentionally omitted from _manifest.json and therefore
# invisible to ordinary sessions. Marketplace installation copies those skills
# into ~/.box-agent/skills/, where the user source is discovered independently
# of this builtin whitelist.
BUILTIN_SKILL_NAMES: frozenset[str] = frozenset(
    {
        "browser-use",
        "data-dashboard",
        "docx",
        "html-templates",
        "mcp-config",
        "memory-guide",
        "pdf",
        "pptx",
        "research-synthesis",
        "roadmap",
        "scheduled-task",
        "xlsx",
    }
)

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def _parse_skill_name(skill_md: Path) -> str | None:
    """Extract the ``name`` field from a SKILL.md frontmatter block."""

    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"warn: cannot read {skill_md}: {exc}", file=sys.stderr)
        return None

    match = _FRONTMATTER_RE.match(text)
    if not match:
        print(f"warn: {skill_md} missing YAML frontmatter, skipping", file=sys.stderr)
        return None

    try:
        frontmatter = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        print(f"warn: {skill_md} invalid YAML: {exc}", file=sys.stderr)
        return None

    name = frontmatter.get("name")
    if not name or not isinstance(name, str):
        print(f"warn: {skill_md} missing 'name' field, skipping", file=sys.stderr)
        return None
    return name


def _parse_builtin_availability(skill_md: Path) -> dict[str, list[str]] | None:
    """Read optional host/platform conditions copied into the builtin manifest."""

    text = skill_md.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None
    frontmatter: dict[str, Any] = yaml.safe_load(match.group(1)) or {}
    raw = frontmatter.get("builtin_availability")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SystemExit(f"error: {skill_md} has invalid builtin_availability")

    availability: dict[str, list[str]] = {}
    for key in ("platforms", "required_env_paths"):
        value = raw.get(key)
        if value is None:
            continue
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item.strip() for item in value
        ):
            raise SystemExit(f"error: {skill_md} has invalid builtin_availability.{key}")
        availability[key] = [item.strip() for item in value]
    if not availability:
        raise SystemExit(f"error: {skill_md} has empty builtin_availability")
    return availability


def _collect_skills() -> List[Tuple[str, str]]:
    """Return ``(name, relative_path)`` tuples for every builtin skill."""

    if not SKILLS_DIR.exists():
        raise SystemExit(f"error: skills directory not found: {SKILLS_DIR}")

    discovered: List[Tuple[str, str]] = []
    for skill_md in sorted(SKILLS_DIR.rglob("SKILL.md")):
        rel = skill_md.relative_to(SKILLS_DIR).as_posix()
        name = _parse_skill_name(skill_md)
        if not name:
            continue
        discovered.append((name, rel))

    seen: dict[str, str] = {}
    for name, rel in discovered:
        if name in seen:
            raise SystemExit(
                f"error: duplicate skill name '{name}' in builtin skills "
                f"({seen[name]} vs {rel})"
            )
        seen[name] = rel

    missing = BUILTIN_SKILL_NAMES - seen.keys()
    if missing:
        raise SystemExit(
            "error: builtin skill whitelist references missing skills: "
            + ", ".join(sorted(missing))
        )

    entries: List[Tuple[str, str]] = []
    for name, rel in discovered:
        if name not in BUILTIN_SKILL_NAMES:
            print(
                f"info: excluding '{rel}' from builtin manifest "
                f"(marketplace skill source)",
                file=sys.stderr,
            )
            continue
        entries.append((name, rel))

    return entries


def _read_box_agent_version() -> str:
    init_py = REPO_ROOT / "box_agent" / "__init__.py"
    text = init_py.read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else "unknown"


def main() -> int:
    entries = _collect_skills()
    payload = {
        "schema_version": 1,
        "box_agent_version": _read_box_agent_version(),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "skills": [],
    }
    for name, rel in entries:
        item: dict[str, object] = {"name": name, "path": rel}
        availability = _parse_builtin_availability(SKILLS_DIR / rel)
        if availability is not None:
            item["availability"] = availability
        payload["skills"].append(item)

    MANIFEST_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {MANIFEST_PATH} ({len(entries)} skills)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

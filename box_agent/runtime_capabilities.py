"""Versioned capabilities exposed by the Box-Agent runtime.

Keep this manifest conservative: a capability is advertised only after its
runtime implementation is present. Host applications can use the versioned
values to negotiate data contracts without inferring support from source files.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError


_RUNTIME_CAPABILITIES_PATH = (
    Path(__file__).resolve().parent
    / "skills"
    / "roadmap"
    / "references"
    / "runtime-capabilities-v1.json"
)
_ROADMAP_SKILL_ROOT = _RUNTIME_CAPABILITIES_PATH.parents[1]
_BUILTIN_SKILLS_MANIFEST_PATH = _ROADMAP_SKILL_ROOT.parent / "_manifest.json"
_RUNTIME_CAPABILITIES_SCHEMA_PATH = (
    _ROADMAP_SKILL_ROOT / "references" / "runtime-capabilities-v1.schema.json"
)
_REQUIRED_ROADMAP_RESOURCES = (
    "SKILL.md",
    "scripts/build_roadmap_artifact.js",
    "scripts/extract_roadmap_spec.js",
    "scripts/migrate_roadmap_spec.js",
    "scripts/roadmap_contract_core.js",
    "scripts/roadmap_geometry_core.js",
    "scripts/roadmap_html_core.js",
    "scripts/roadmap_io.js",
    "runtime/registry.json",
    "runtime/roadmap.css",
    "runtime/roadmap-editor.js",
    "references/roadmap-draft.schema.json",
    "references/roadmap-spec.schema.json",
)


def _disabled_capabilities() -> dict[str, Any]:
    return {
        "contract": "box-agent.runtime-capabilities",
        "contractVersion": 1,
        "deckProtocolVersion": 1,
        "roadmap": {
            "schemaVersion": 1,
            "geometryVersion": 1,
            "rendererVersion": None,
            "capabilities": [],
        },
    }


def _node_runtime_available() -> bool:
    try:
        from box_agent.tools.runtime import build_skill_runtime_context

        return build_skill_runtime_context(sandbox_mode=False).get("node").available
    except Exception:
        return False


def _roadmap_resources_available(skill_root: Path) -> bool:
    return all((skill_root / relative).is_file() for relative in _REQUIRED_ROADMAP_RESOURCES)


def _roadmap_builtin_manifest_available(
    skill_root: Path,
    manifest_path: Path,
) -> bool:
    """Return whether the authoritative builtin manifest enables Roadmap."""

    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    entries = payload.get("skills") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return False
    expected_path = f"{skill_root.name}/SKILL.md"
    return any(
        isinstance(entry, dict)
        and entry.get("name") == "roadmap"
        and entry.get("path") == expected_path
        for entry in entries
    )


@lru_cache(maxsize=1)
def _capability_validator() -> Draft202012Validator:
    schema = json.loads(_RUNTIME_CAPABILITIES_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def runtime_capabilities(
    *,
    skills_enabled: bool = True,
    node_available: bool | None = None,
    roadmap_skill_available: bool | None = None,
    capabilities_path: Path | None = None,
    skill_root: Path | None = None,
    builtin_manifest_path: Path | None = None,
) -> dict[str, Any]:
    """Return capabilities that are usable in this runtime, failing closed."""

    manifest_path = capabilities_path or _RUNTIME_CAPABILITIES_PATH
    root = skill_root or _ROADMAP_SKILL_ROOT
    skills_manifest_path = builtin_manifest_path or (
        _BUILTIN_SKILLS_MANIFEST_PATH
        if skill_root is None
        else root.parent / "_manifest.json"
    )
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        _capability_validator().validate(payload)
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        SchemaError,
        ValidationError,
    ):
        return _disabled_capabilities()

    runtime_has_roadmap_skill = (
        _roadmap_builtin_manifest_available(root, skills_manifest_path)
        if roadmap_skill_available is None
        else roadmap_skill_available
    )
    if (
        not skills_enabled
        or not runtime_has_roadmap_skill
        or not _roadmap_resources_available(root)
    ):
        return _disabled_capabilities()
    runtime_has_node = _node_runtime_available() if node_available is None else node_available
    if not runtime_has_node:
        return _disabled_capabilities()
    return payload


__all__ = ["runtime_capabilities"]

"""Roadmap-specific validation for controlled HTML artifact metadata."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from .resource_paths import bundled_resource_dir

_HTML_ARTIFACT_METADATA_LIMIT = 16 * 1024 * 1024
_ROADMAP_LAYOUT_ID = "roadmap-swimlane-v1"
_ROADMAP_GENERATOR_RE = re.compile(r"^Box Agent Roadmap Artifact v(\d+)$")
_ROADMAP_SUPPORTED_VERSIONS = {
    "generator": "1",
    "artifact": "1",
    "schema": "1",
    "geometry": "1",
    "renderer": "1",
}
_ROADMAP_RUNTIME_SCRIPT_IDS = frozenset(
    {
        "contract-core",
        "geometry-core",
        "editor",
    }
)
_ROADMAP_JSON_SCRIPT_IDS = frozenset(
    {
        "deck-document",
        "roadmap-geometry",
        "roadmap-diagnostics",
        "roadmap-pending-questions",
        "roadmap-palette",
        "roadmap-editor-metadata",
    }
)


@lru_cache(maxsize=1)
def _roadmap_spec_validator() -> Draft202012Validator:
    references = (
        bundled_resource_dir("skills", package_dir=Path(__file__).resolve().parent)
        / "roadmap" / "references"
    )
    draft_schema = json.loads(
        (references / "roadmap-draft.schema.json").read_text(encoding="utf-8")
    )
    spec_schema = json.loads(
        (references / "roadmap-spec.schema.json").read_text(encoding="utf-8")
    )
    registry = Registry().with_resource(
        draft_schema["$id"], Resource.from_contents(draft_schema)
    )
    return Draft202012Validator(spec_schema, registry=registry)


def _attribute_value(attributes: str, name: str) -> str | None:
    match = re.search(
        rf"(?:^|\s){re.escape(name)}\s*=\s*([\"'])(.*?)\1",
        attributes,
        re.IGNORECASE | re.DOTALL,
    )
    return match.group(2).strip() if match else None


def _unique_meta_content(content: str, name: str) -> str | None:
    values = []
    for match in re.finditer(r"<meta\b([^>]*)>", content, re.IGNORECASE):
        attributes = match.group(1)
        meta_name = _attribute_value(attributes, "name")
        if meta_name and meta_name.lower() == name.lower():
            values.append(_attribute_value(attributes, "content"))
    return values[0] if len(values) == 1 and values[0] is not None else None


def _inspect_roadmap_protocol(content: str) -> tuple[bool, bool]:
    """Return ``(recognizable, supported)`` from structural protocol markers."""
    generator = _unique_meta_content(content, "generator")
    generator_match = _ROADMAP_GENERATOR_RE.fullmatch(generator or "")
    if not generator_match:
        return False, False
    if _unique_meta_content(content, "box-agent-artifact-layout-id") != _ROADMAP_LAYOUT_ID:
        return False, False

    body_matches = re.findall(r"<body\b([^>]*)>", content, re.IGNORECASE)
    if len(body_matches) != 1:
        return False, False
    body_attributes = body_matches[0]
    if (
        _attribute_value(body_attributes, "data-artifact-kind") != "roadmap"
        or _attribute_value(body_attributes, "data-layout-id") != _ROADMAP_LAYOUT_ID
    ):
        return False, False

    versions = {
        "generator": generator_match.group(1),
        "artifact": _unique_meta_content(content, "box-agent-artifact-version"),
        "schema": _attribute_value(body_attributes, "data-schema-version"),
        "geometry": _attribute_value(body_attributes, "data-geometry-version"),
        "renderer": _attribute_value(body_attributes, "data-renderer-version"),
    }
    if any(version is None or not version.isdigit() for version in versions.values()):
        return False, False
    return True, versions == _ROADMAP_SUPPORTED_VERSIONS


def _has_safe_roadmap_runtime_structure(content: str) -> bool:
    """Validate the embedded runtime shape without comparing source bytes."""
    markup = re.sub(
        r"<(?:script|style)\b[^>]*>[\s\S]*?</(?:script|style)\s*>",
        "",
        content,
        flags=re.IGNORECASE,
    )
    if re.search(r"\son[a-z0-9_-]+\s*=", markup, re.IGNORECASE):
        return False
    if re.search(
        r"<(?:a|form|iframe|object|embed|base|link|img|video|audio|svg|math)\b",
        markup,
        re.IGNORECASE,
    ):
        return False
    if re.search(
        r"\s(?:href|src|srcset|action|formaction)\s*=", markup, re.IGNORECASE
    ):
        return False
    if re.search(r"<meta\b[^>]*\bhttp-equiv\s*=", markup, re.IGNORECASE):
        return False

    runtime_script_ids: set[str] = set()
    json_script_ids: set[str] = set()
    for match in re.finditer(
        r"<script\b([^>]*)>[\s\S]*?</script\s*>", content, re.IGNORECASE
    ):
        attributes = match.group(1)
        if _attribute_value(attributes, "src") is not None:
            return False
        script_type = (_attribute_value(attributes, "type") or "").lower()
        if script_type == "application/json":
            script_id = _attribute_value(attributes, "id")
            if script_id not in _ROADMAP_JSON_SCRIPT_IDS or script_id in json_script_ids:
                return False
            json_script_ids.add(script_id)
            continue
        runtime_id = _attribute_value(attributes, "data-roadmap-runtime")
        if runtime_id not in _ROADMAP_RUNTIME_SCRIPT_IDS or runtime_id in runtime_script_ids:
            return False
        runtime_script_ids.add(runtime_id)

    if (
        json_script_ids != _ROADMAP_JSON_SCRIPT_IDS
        or runtime_script_ids != _ROADMAP_RUNTIME_SCRIPT_IDS
    ):
        return False
    styles = re.findall(r"<style\b[^>]*>([\s\S]*?)</style\s*>", content, re.IGNORECASE)
    return len(styles) == 1


def roadmap_metadata_for_html_artifact(abs_file: Path, size: int) -> tuple[str, str]:
    """Return ``(layout_id, edit_mode)`` for a recognizable Roadmap artifact."""
    if abs_file.suffix.lower() not in {".html", ".htm"}:
        return "", ""
    if size < 0 or size > _HTML_ARTIFACT_METADATA_LIMIT:
        return "", ""
    try:
        content = abs_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return "", ""

    layout_id = _unique_meta_content(content, "box-agent-artifact-layout-id")
    if layout_id != _ROADMAP_LAYOUT_ID or len(layout_id) > 128:
        return "", ""
    try:
        recognizable, supported = _inspect_roadmap_protocol(content)
        if not recognizable or not _has_safe_roadmap_runtime_structure(content):
            return "", ""
    except (OSError, UnicodeError):
        return "", ""
    sources = re.findall(
        r"<script\b(?=[^>]*\bid=[\"']deck-document[\"'])"
        r"(?=[^>]*\btype=[\"']application/json[\"'])[^>]*>"
        r"([\s\S]*?)</script\s*>",
        content,
        re.IGNORECASE,
    )
    if len(sources) != 1:
        return "", ""
    try:
        document = json.loads(sources[0])
        if supported:
            _roadmap_spec_validator().validate(document)
    except Exception:
        return "", ""
    return layout_id, "editable" if supported else "read_only"


def roadmap_layout_id_for_html_artifact(abs_file: Path, size: int) -> str:
    """Return the recognizable Roadmap layout id, or an empty string when invalid."""
    return roadmap_metadata_for_html_artifact(abs_file, size)[0]

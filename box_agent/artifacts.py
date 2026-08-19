"""Artifact naming, output-directory, and metadata helpers.

These helpers are shared by the runtime and host/tool integrations.  Keeping
them outside the agent loop lets integrations use the artifact contract
without importing ``box_agent.core``.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Final

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from .events import ArtifactEvent

__all__ = [
    "OUTPUT_SUBDIR",
    "artifact_scan_root",
    "avoid_collision",
    "ensure_output_dir",
    "make_artifact",
    "safe_output_name",
]

OUTPUT_SUBDIR: Final[str] = "output"

_MIME_KIND_PREFIX = (
    ("image/", "image"),
    ("video/", "video"),
    ("audio/", "audio"),
    ("text/csv", "data"),
    ("text/tab-separated-values", "data"),
    ("application/json", "data"),
    ("application/x-ndjson", "data"),
    ("application/xml", "data"),
    ("text/x-python", "code"),
    ("text/x-", "code"),
    ("application/javascript", "code"),
    ("application/typescript", "code"),
    ("text/markdown", "document"),
    ("text/html", "document"),
    ("application/pdf", "document"),
    ("application/msword", "document"),
    ("application/vnd.openxmlformats-officedocument.wordprocessingml", "document"),
    ("application/vnd.ms-excel", "spreadsheet"),
    ("application/vnd.openxmlformats-officedocument.spreadsheetml", "spreadsheet"),
    ("application/vnd.ms-powerpoint", "presentation"),
    ("application/vnd.openxmlformats-officedocument.presentationml", "presentation"),
    ("application/zip", "archive"),
    ("application/x-tar", "archive"),
    ("application/gzip", "archive"),
    ("application/x-7z-compressed", "archive"),
    ("text/", "document"),
)

_EXT_KIND = {
    ".csv": "data",
    ".tsv": "data",
    ".json": "data",
    ".jsonl": "data",
    ".ndjson": "data",
    ".parquet": "data",
    ".xml": "data",
    ".yaml": "data",
    ".yml": "data",
    ".py": "code",
    ".js": "code",
    ".ts": "code",
    ".jsx": "code",
    ".tsx": "code",
    ".rs": "code",
    ".go": "code",
    ".java": "code",
    ".c": "code",
    ".cpp": "code",
    ".rb": "code",
    ".sh": "code",
    ".md": "document",
    ".rst": "document",
    ".html": "document",
    ".htm": "document",
    ".pdf": "document",
    ".doc": "document",
    ".docx": "document",
    ".txt": "document",
    ".xlsx": "spreadsheet",
    ".xls": "spreadsheet",
    ".ods": "spreadsheet",
    ".pptx": "presentation",
    ".ppt": "presentation",
    ".key": "presentation",
    ".zip": "archive",
    ".tar": "archive",
    ".gz": "archive",
    ".7z": "archive",
    ".rar": "archive",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".gif": "image",
    ".svg": "image",
    ".webp": "image",
    ".bmp": "image",
    ".tiff": "image",
    ".mp4": "video",
    ".webm": "video",
    ".mov": "video",
    ".mp3": "audio",
    ".wav": "audio",
    ".ogg": "audio",
    ".flac": "audio",
}

_EXT_MIME_OVERRIDES = {
    ".md": "text/markdown",
    ".rst": "text/x-rst",
    ".jsonl": "application/x-ndjson",
    ".ndjson": "application/x-ndjson",
    ".parquet": "application/vnd.apache.parquet",
    ".tsv": "text/tab-separated-values",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".webp": "image/webp",
    ".key": "application/vnd.apple.keynote",
}
_SAFE_NAME_RE = re.compile(r"[^a-z0-9._-]+")
_HTML_ARTIFACT_METADATA_LIMIT = 16 * 1024 * 1024
_ROADMAP_LAYOUT_ID = "roadmap-swimlane-v1"
_ROADMAP_GENERATOR = "Box Agent Roadmap Artifact v1"
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
    references = Path(__file__).resolve().parent / "skills" / "roadmap" / "references"
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


def _safe_inline_script(value: str) -> str:
    return re.sub(r"</script", r"<\\/script", value, flags=re.IGNORECASE)


def _runtime_module(source: str, name: str, require_body: str) -> str:
    return "\n".join(
        (
            f"(function(){{const module={{exports:{{}}}};const exports=module.exports;{require_body}",
            _safe_inline_script(source),
            f";window.{name}=module.exports;}})();",
        )
    )


@lru_cache(maxsize=1)
def _trusted_roadmap_runtime_surface() -> tuple[dict[str, str], str]:
    skill_root = Path(__file__).resolve().parent / "skills" / "roadmap"
    scripts = skill_root / "scripts"
    contract_source = (scripts / "roadmap_contract_core.js").read_text(encoding="utf-8")
    geometry_source = (scripts / "roadmap_geometry_core.js").read_text(encoding="utf-8")
    editor_source = (skill_root / "runtime" / "roadmap-editor.js").read_text(
        encoding="utf-8"
    )
    contract_require = (
        'const require=(request)=>{if(request==="crypto")return '
        '{createHash:()=>{throw new Error("crypto hashing is unavailable in the Roadmap '
        'editor runtime")}};throw new Error(`Unsupported runtime module: ${request}`);};'
    )
    geometry_require = (
        'const require=(request)=>{if(request==="./roadmap_contract_core.js")return '
        'window.__roadmapContractCore;throw new Error(`Unsupported runtime module: '
        '${request}`);};'
    )
    return (
        {
            "contract-core": _runtime_module(
                contract_source, "__roadmapContractCore", contract_require
            ),
            "geometry-core": _runtime_module(
                geometry_source, "__roadmapGeometryCore", geometry_require
            ),
            "editor": _safe_inline_script(editor_source).strip(),
        },
        (skill_root / "runtime" / "roadmap.css").read_text(encoding="utf-8"),
    )


def _has_trusted_roadmap_runtime_surface(content: str) -> bool:
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

    expected_scripts, expected_css = _trusted_roadmap_runtime_surface()
    actual_scripts: dict[str, str] = {}
    json_script_ids: set[str] = set()
    for match in re.finditer(
        r"<script\b([^>]*)>([\s\S]*?)</script\s*>", content, re.IGNORECASE
    ):
        attrs, source = match.groups()

        def attr(name: str) -> str:
            value = re.search(
                rf"\b{re.escape(name)}\s*=\s*([\"'])(.*?)\1",
                attrs,
                re.IGNORECASE,
            )
            return value.group(2) if value else ""

        if attr("src"):
            return False
        if attr("type").lower() == "application/json":
            script_id = attr("id")
            if script_id not in _ROADMAP_JSON_SCRIPT_IDS or script_id in json_script_ids:
                return False
            json_script_ids.add(script_id)
            continue
        runtime_id = attr("data-roadmap-runtime")
        if runtime_id not in expected_scripts or runtime_id in actual_scripts:
            return False
        actual_scripts[runtime_id] = source.strip()

    if json_script_ids != _ROADMAP_JSON_SCRIPT_IDS or actual_scripts != expected_scripts:
        return False
    styles = re.findall(r"<style\b[^>]*>([\s\S]*?)</style\s*>", content, re.IGNORECASE)
    return len(styles) == 1 and styles[0].strip() == expected_css.strip()


def _html_artifact_layout_id(abs_file: Path, size: int) -> str:
    if abs_file.suffix.lower() not in {".html", ".htm"}:
        return ""
    if size < 0 or size > _HTML_ARTIFACT_METADATA_LIMIT:
        return ""
    try:
        content = abs_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""

    def meta(name: str) -> str:
        pattern = re.compile(
            rf"<meta\b(?=[^>]*\bname=[\"']{re.escape(name)}[\"'])(?=[^>]*\bcontent=[\"']([^\"']*)[\"'])[^>]*>",
            re.IGNORECASE,
        )
        match = pattern.search(content)
        return match.group(1).strip() if match else ""

    layout_id = meta("box-agent-artifact-layout-id")
    if layout_id != _ROADMAP_LAYOUT_ID or len(layout_id) > 128:
        return ""
    if meta("generator") != _ROADMAP_GENERATOR:
        return ""
    try:
        if not _has_trusted_roadmap_runtime_surface(content):
            return ""
    except (OSError, UnicodeError):
        return ""
    sources = re.findall(
        r"<script\b(?=[^>]*\bid=[\"']deck-document[\"'])"
        r"(?=[^>]*\btype=[\"']application/json[\"'])[^>]*>"
        r"([\s\S]*?)</script\s*>",
        content,
        re.IGNORECASE,
    )
    if len(sources) != 1:
        return ""
    try:
        _roadmap_spec_validator().validate(json.loads(sources[0]))
    except Exception:
        return ""
    return layout_id


def _classify_kind(filename: str, mime: str | None) -> str:
    """Map a filename and MIME type to a coarse host-facing kind."""
    normalized_mime = (mime or "").lower()
    for prefix, kind in _MIME_KIND_PREFIX:
        if normalized_mime.startswith(prefix) or normalized_mime == prefix:
            return kind
    return _EXT_KIND.get(Path(filename).suffix.lower(), "file")


def ensure_output_dir(workspace_dir: str | Path) -> Path:
    """Return ``{workspace}/output/``, creating it if needed."""
    output_dir = Path(workspace_dir).expanduser().resolve() / OUTPUT_SUBDIR
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def artifact_scan_root(
    workspace_dir: str | Path | None,
    artifact_root_dir: str | Path | None = None,
) -> Path | None:
    """Resolve the root used for artifact discovery without creating it."""
    if artifact_root_dir:
        return Path(artifact_root_dir).expanduser().resolve()
    if not workspace_dir:
        return None
    return Path(workspace_dir).expanduser().resolve() / OUTPUT_SUBDIR


def safe_output_name(name: str, *, default_ext: str = "") -> str:
    """Normalize a proposed artifact name: lowercase, ASCII, kebab-safe."""
    stem = name.strip() or "artifact"
    suffix = Path(stem).suffix.lower()
    base = _SAFE_NAME_RE.sub("-", Path(stem).stem.lower()).strip("-._") or "artifact"
    if not suffix and default_ext:
        suffix = default_ext if default_ext.startswith(".") else f".{default_ext}"
    return f"{base}{suffix}"


def avoid_collision(directory: Path, filename: str) -> Path:
    """Return a non-existing path inside ``directory`` by appending ``-N``."""
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    index = 2
    while True:
        candidate = directory / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def make_artifact(
    tool_call_id: str,
    abs_file: Path,
    workspace_root: Path,
) -> ArtifactEvent:
    """Build an :class:`ArtifactEvent` from a real on-disk file."""
    abs_resolved = abs_file.resolve()
    try:
        rel_path = abs_resolved.relative_to(workspace_root.resolve()).as_posix()
    except ValueError:
        rel_path = abs_resolved.name

    mime, _ = mimetypes.guess_type(str(abs_resolved))
    if not mime:
        mime = _EXT_MIME_OVERRIDES.get(abs_resolved.suffix.lower())
    mime = mime or "application/octet-stream"
    try:
        size = abs_resolved.stat().st_size
    except OSError:
        size = -1

    digest = ""
    try:
        if 0 <= size <= 64 * 1024 * 1024:
            hasher = hashlib.sha256()
            with abs_resolved.open("rb") as file_obj:
                for chunk in iter(lambda: file_obj.read(1 << 16), b""):
                    hasher.update(chunk)
            digest = hasher.hexdigest()[:16]
    except OSError:
        pass

    layout_id = _html_artifact_layout_id(abs_resolved, size)

    return ArtifactEvent(
        tool_call_id=tool_call_id,
        kind=_classify_kind(abs_resolved.name, mime),
        filename=abs_resolved.name,
        rel_path=rel_path,
        abs_path=str(abs_resolved),
        uri=abs_resolved.as_uri(),
        mime=mime,
        size=size,
        sha256=digest,
        produced_at=datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        layout_id=layout_id,
    )

"""Artifact naming and metadata helpers.

These helpers are shared by the runtime and host/tool integrations.  Keeping
them outside the agent loop lets integrations use the artifact contract
without importing ``box_agent.core``.
"""

from __future__ import annotations

import hashlib
import mimetypes
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

from .events import ArtifactEvent
from .roadmap_artifacts import roadmap_metadata_for_html_artifact

__all__ = [
    "artifact_scan_root",
    "avoid_collision",
    "make_artifact",
    "safe_output_name",
]

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


def _classify_kind(filename: str, mime: str | None) -> str:
    """Map a filename and MIME type to a coarse host-facing kind."""
    normalized_mime = (mime or "").lower()
    for prefix, kind in _MIME_KIND_PREFIX:
        if normalized_mime.startswith(prefix) or normalized_mime == prefix:
            return kind
    return _EXT_KIND.get(Path(filename).suffix.lower(), "file")


def artifact_scan_root(
    workspace_dir: str | Path | None,
) -> Path | None:
    """Resolve the session cwd used for artifact discovery without creating it."""
    if not workspace_dir:
        return None
    return Path(workspace_dir).expanduser().resolve()


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

    layout_id, edit_mode = roadmap_metadata_for_html_artifact(abs_resolved, size)

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
        edit_mode=edit_mode,
    )

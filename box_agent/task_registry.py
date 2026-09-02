"""Canonical task and artifact lineage registry owned by Box-Agent."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .events import ArtifactEvent
from .task_context import TaskContext


REGISTRY_SCHEMA_VERSION = 2


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _registry_path(workspace_dir: str | Path, task_id: str) -> Path:
    return (
        Path(workspace_dir).expanduser().resolve()
        / ".box-agent"
        / "task-registry"
        / "tasks"
        / f"{task_id}.json"
    )


def _read_record(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_record(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _initial_record(
    context: TaskContext,
    artifact_root_dir: str | Path | None,
) -> dict[str, Any]:
    now = _now()
    return {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "session_id": context.session_id,
        "task_id": context.task_id,
        "current_turn_id": context.turn_id,
        "artifact_root_dir": (
            str(Path(artifact_root_dir).expanduser().resolve())
            if artifact_root_dir is not None
            else None
        ),
        "execution_status": "running",
        "created_at": now,
        "updated_at": now,
        "artifacts": [],
    }


def begin_task(
    workspace_dir: str | Path,
    context: TaskContext,
    *,
    artifact_root_dir: str | Path | None,
) -> Path:
    """Create or resume the canonical record for one logical task."""
    path = _registry_path(workspace_dir, context.task_id)
    payload = _read_record(path) or _initial_record(context, artifact_root_dir)
    payload.update(
        {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "session_id": context.session_id,
            "task_id": context.task_id,
            "current_turn_id": context.turn_id,
            "execution_status": "running",
            "updated_at": _now(),
        }
    )
    payload.pop("delivery_status", None)
    if artifact_root_dir is not None:
        payload["artifact_root_dir"] = str(
            Path(artifact_root_dir).expanduser().resolve()
        )
    payload.setdefault("artifacts", [])
    _write_record(path, payload)
    return path


def finish_task(
    workspace_dir: str | Path,
    context: TaskContext,
    *,
    execution_status: str,
    artifact_root_dir: str | Path | None,
) -> Path:
    """Persist the terminal execution state without inferring delivery policy."""
    path = _registry_path(workspace_dir, context.task_id)
    payload = _read_record(path) or _initial_record(context, artifact_root_dir)
    payload.update(
        {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "session_id": context.session_id,
            "task_id": context.task_id,
            "current_turn_id": context.turn_id,
            "execution_status": execution_status,
            "updated_at": _now(),
        }
    )
    payload.pop("delivery_status", None)
    _write_record(path, payload)
    return path


def _content_sha256(path: Path, fallback: str) -> str:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return fallback


@dataclass(frozen=True, slots=True)
class ArtifactLineage:
    artifact_id: str
    artifact_revision_id: str
    sha256: str
    manifest_path: str


def register_artifact_revision(
    workspace_dir: str | Path,
    context: TaskContext,
    artifact: ArtifactEvent,
    *,
    artifact_root_dir: str | Path | None,
) -> ArtifactLineage:
    """Register one emitted file and return stable artifact/revision ids."""
    registry_path = begin_task(
        workspace_dir,
        context,
        artifact_root_dir=artifact_root_dir,
    )
    payload = _read_record(registry_path) or _initial_record(context, artifact_root_dir)
    rel_path = artifact.rel_path or artifact.filename
    artifact_key = f"{context.task_id}\0{rel_path}"
    artifact_id = f"artifact_{hashlib.sha256(artifact_key.encode()).hexdigest()[:24]}"
    content_sha256 = _content_sha256(Path(artifact.abs_path), artifact.sha256)
    revision_key = f"{artifact_id}\0{content_sha256}\0{artifact.size}"
    revision_id = f"revision_{hashlib.sha256(revision_key.encode()).hexdigest()[:24]}"
    revision = {
        "artifact_revision_id": revision_id,
        "turn_id": context.turn_id,
        "sha256": content_sha256,
        "size": artifact.size,
        "produced_at": artifact.produced_at,
    }
    artifacts = payload.setdefault("artifacts", [])
    record = next(
        (
            item
            for item in artifacts
            if isinstance(item, dict) and item.get("artifact_id") == artifact_id
        ),
        None,
    )
    if record is None:
        record = {
            "artifact_id": artifact_id,
            "kind": artifact.kind,
            "filename": artifact.filename,
            "rel_path": rel_path,
            "mime": artifact.mime,
            "revisions": [],
        }
        artifacts.append(record)
    revisions = record.setdefault("revisions", [])
    if not any(
        isinstance(item, dict) and item.get("artifact_revision_id") == revision_id
        for item in revisions
    ):
        revisions.append(revision)
    record["current_revision_id"] = revision_id
    payload["updated_at"] = _now()
    _write_record(registry_path, payload)
    return ArtifactLineage(
        artifact_id=artifact_id,
        artifact_revision_id=revision_id,
        sha256=content_sha256,
        manifest_path=str(registry_path),
    )


__all__ = [
    "ArtifactLineage",
    "begin_task",
    "finish_task",
    "register_artifact_revision",
]

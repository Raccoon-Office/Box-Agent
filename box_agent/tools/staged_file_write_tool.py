"""Transactional chunked text-file writer for large generated artifacts."""

from __future__ import annotations

import hashlib
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .argument_limits import MAX_GENERATED_BODY_CHARS, RECOMMENDED_GENERATED_BODY_CHARS
from .base import Tool, ToolResult
from .file_tools import _resolve_from_active_root
from .safety import backup_file, validate_path_in_workspace

if TYPE_CHECKING:
    from .permissions import PermissionEngine

MAX_STAGED_FILE_BYTES = 10 * 1024 * 1024
MAX_STAGED_FILE_CHUNKS = 2_048
STAGING_MAX_AGE_SECONDS = 24 * 60 * 60


@dataclass
class _StagedWrite:
    target: Path
    temporary: Path
    expected_chunks: int | None = None
    next_index: int = 0
    size_bytes: int = 0


class StagedFileWriteTool(Tool):
    """Build a large text file in bounded chunks and publish it atomically."""

    def __init__(
        self,
        workspace_dir: str = ".",
        allow_full_access: bool = True,
        permission_engine: PermissionEngine | None = None,
        relative_root_dir: str | None = None,
    ) -> None:
        self.workspace_dir = Path(workspace_dir).absolute()
        self.relative_root_dir = (
            Path(relative_root_dir).absolute() if relative_root_dir else self.workspace_dir
        )
        self.allow_full_access = allow_full_access
        self._perm = permission_engine
        self._writes: dict[str, _StagedWrite] = {}
        self._staging_dir = self.relative_root_dir / ".box-agent-staging"
        self._cleanup_stale_files()

    @property
    def name(self) -> str:
        return "staged_file_write"

    @property
    def description(self) -> str:
        return (
            "Transactionally build a large UTF-8 text file in bounded chunks. "
            "Call begin once, append_text or append_file in ascending chunk_index order, "
            "then commit. Save the write_id returned by begin and include that exact "
            "write_id in every append_text, append_file, commit, or abort call. If begin "
            "declares expected_chunks, commit reuses it unless "
            "commit explicitly overrides it. The final target remains unchanged until "
            "commit succeeds. "
            f"Keep generated text chunks near {RECOMMENDED_GENERATED_BODY_CHARS} characters "
            f"and never exceed {MAX_GENERATED_BODY_CHARS}."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["begin", "append_text", "append_file", "commit", "abort"],
                },
                "path": {
                    "type": "string",
                    "description": "Final target path for begin, or source path for append_file.",
                },
                "write_id": {
                    "type": "string",
                    "description": (
                        "Required for append_text, append_file, commit, and abort. Copy the "
                        "exact write_id returned by begin. Omit only for begin."
                    ),
                },
                "chunk_index": {"type": "integer", "minimum": 0},
                "content": {
                    "type": "string",
                    "maxLength": MAX_GENERATED_BODY_CHARS,
                    "description": "UTF-8 text for append_text.",
                },
                "expected_chunks": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_STAGED_FILE_CHUNKS,
                    "description": (
                        "Expected final chunk count. Declare it at begin so commit can "
                        "reuse it, or provide it explicitly at commit."
                    ),
                },
                "expected_sha256": {
                    "type": "string",
                    "description": "Optional lowercase SHA-256 expected at commit.",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        }

    async def execute(
        self,
        action: str,
        path: str | None = None,
        write_id: str | None = None,
        chunk_index: int | None = None,
        content: str | None = None,
        expected_chunks: int | None = None,
        expected_sha256: str | None = None,
    ) -> ToolResult:
        try:
            if action == "begin":
                return self._begin(path, expected_chunks)
            if action == "append_text":
                return self._append_text(write_id, chunk_index, content)
            if action == "append_file":
                return self._append_file(write_id, chunk_index, path)
            if action == "commit":
                return self._commit(write_id, expected_chunks, expected_sha256)
            if action == "abort":
                return self._abort(write_id)
            return ToolResult(success=False, error=f"Unknown staged file action: {action}")
        except (OSError, UnicodeError) as exc:
            return ToolResult(success=False, error=f"STAGED_FILE_WRITE_FAILED: {exc}")

    def _resolve(self, path: str) -> Path:
        return _resolve_from_active_root(
            path,
            workspace_dir=self.workspace_dir,
            relative_root_dir=self.relative_root_dir,
        )

    def _permission_error(self, path: Path, capability: str) -> ToolResult | None:
        if self._perm:
            decision = self._perm.check(
                capability=capability,
                resource={"path": str(path)},
                tool_name=self.name,
            )
            if not decision.allowed:
                return ToolResult(
                    success=False,
                    error=decision.reason,
                    permission_request=decision.permission_request,
                )
        elif not self.allow_full_access:
            error = validate_path_in_workspace(path, self.workspace_dir)
            if error:
                return ToolResult(success=False, error=error)
        return None

    def _begin(
        self,
        path: str | None,
        expected_chunks: int | None,
    ) -> ToolResult:
        if not path:
            return ToolResult(success=False, error="begin requires path")
        target = self._resolve(path)
        if error := self._permission_error(target, "filesystem.write"):
            return error
        for active_write_id, active in self._writes.items():
            if active.target == target:
                return ToolResult(
                    success=False,
                    error=(
                        "STAGED_FILE_WRITE_TARGET_ACTIVE: this target already has an "
                        f"active transaction; write_id={active_write_id}; "
                        f"next_chunk_index={active.next_index}; "
                        f"expected_chunks={active.expected_chunks}. Continue with that "
                        "write_id using append_text, append_file, commit, or abort; do not "
                        "begin another transaction for the same target."
                    ),
                    raw_output={
                        "type": "staged_file_write",
                        "action": "begin_rejected",
                        "write_id": active_write_id,
                        "next_index": active.next_index,
                        "size_bytes": active.size_bytes,
                        "expected_chunks": active.expected_chunks,
                    },
                )
        self._staging_dir.mkdir(parents=True, exist_ok=True)
        write_id = uuid.uuid4().hex
        temporary = self._staging_dir / f"{write_id}.part"
        temporary.write_bytes(b"")
        self._writes[write_id] = _StagedWrite(
            target=target,
            temporary=temporary,
            expected_chunks=expected_chunks,
        )
        return ToolResult(
            success=True,
            content=(
                f"Started staged write {write_id}; next chunk_index=0, "
                f"recommended chunk size={RECOMMENDED_GENERATED_BODY_CHARS}."
            ),
            raw_output={
                "type": "staged_file_write",
                "action": "begin",
                "write_id": write_id,
                "next_index": 0,
                "chunk_limit": MAX_GENERATED_BODY_CHARS,
                "expected_chunks": expected_chunks,
            },
        )

    def _get_write(
        self,
        write_id: str | None,
    ) -> tuple[str, _StagedWrite] | ToolResult:
        if not write_id:
            if len(self._writes) == 1:
                return next(iter(self._writes.items()))
            return ToolResult(
                success=False,
                error=(
                    "STAGED_FILE_WRITE_ID_REQUIRED: include the write_id returned by begin; "
                    f"active_writes={len(self._writes)}."
                ),
            )
        state = self._writes.get(write_id)
        if state is None:
            if len(self._writes) == 1:
                return next(iter(self._writes.items()))
            return ToolResult(
                success=False,
                error=f"STAGED_FILE_WRITE_ID_UNKNOWN: write_id={write_id!r} is not active.",
            )
        return write_id, state

    def _append_text(
        self,
        write_id: str | None,
        chunk_index: int | None,
        content: str | None,
    ) -> ToolResult:
        resolved = self._get_write(write_id)
        if isinstance(resolved, ToolResult):
            return resolved
        resolved_write_id, state = resolved
        if content is None:
            return ToolResult(success=False, error="append_text requires content")
        if len(content) > MAX_GENERATED_BODY_CHARS:
            return ToolResult(
                success=False,
                error=(
                    "STAGED_FILE_CHUNK_TOO_LARGE: "
                    f"{len(content)} characters; limit is {MAX_GENERATED_BODY_CHARS}."
                ),
            )
        return self._append_bytes(
            resolved_write_id,
            state,
            chunk_index,
            content.encode("utf-8"),
        )

    def _append_file(
        self,
        write_id: str | None,
        chunk_index: int | None,
        path: str | None,
    ) -> ToolResult:
        resolved = self._get_write(write_id)
        if isinstance(resolved, ToolResult):
            return resolved
        resolved_write_id, state = resolved
        if not path:
            return ToolResult(success=False, error="append_file requires source path")
        source = self._resolve(path)
        if error := self._permission_error(source, "filesystem.read"):
            return error
        if not source.is_file():
            return ToolResult(success=False, error=f"Source file not found: {path}")
        data = source.read_bytes()
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            return ToolResult(success=False, error="append_file supports UTF-8 text files only")
        return self._append_bytes(resolved_write_id, state, chunk_index, data)

    def _append_bytes(
        self,
        write_id: str,
        state: _StagedWrite,
        chunk_index: int | None,
        data: bytes,
    ) -> ToolResult:
        if chunk_index != state.next_index:
            return ToolResult(
                success=False,
                error=(
                    f"STAGED_FILE_CHUNK_OUT_OF_ORDER: expected {state.next_index}, "
                    f"got {chunk_index}."
                ),
            )
        if state.next_index >= MAX_STAGED_FILE_CHUNKS:
            return ToolResult(success=False, error="STAGED_FILE_TOO_MANY_CHUNKS")
        if state.size_bytes + len(data) > MAX_STAGED_FILE_BYTES:
            return ToolResult(success=False, error="STAGED_FILE_TOTAL_SIZE_EXCEEDED")
        with state.temporary.open("ab") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        state.next_index += 1
        state.size_bytes += len(data)
        return ToolResult(
            success=True,
            content=(
                f"Appended chunk {chunk_index}; next chunk_index={state.next_index}; "
                f"total_bytes={state.size_bytes}. Continue with write_id={write_id}."
            ),
            raw_output={
                "type": "staged_file_write",
                "action": "append",
                "write_id": write_id,
                "next_index": state.next_index,
                "size_bytes": state.size_bytes,
            },
        )

    def _commit(
        self,
        write_id: str | None,
        expected_chunks: int | None,
        expected_sha256: str | None,
    ) -> ToolResult:
        resolved = self._get_write(write_id)
        if isinstance(resolved, ToolResult):
            return resolved
        resolved_write_id, state = resolved
        effective_expected_chunks = (
            expected_chunks
            if expected_chunks is not None
            else state.expected_chunks
        )
        if (
            effective_expected_chunks is None
            or effective_expected_chunks != state.next_index
        ):
            return ToolResult(
                success=False,
                error=(
                    "STAGED_FILE_CHUNK_COUNT_MISMATCH: "
                    f"expected_chunks={effective_expected_chunks}, "
                    f"actual={state.next_index}."
                ),
            )
        digest = hashlib.sha256(state.temporary.read_bytes()).hexdigest()
        if expected_sha256 and expected_sha256.lower() != digest:
            return ToolResult(
                success=False,
                error=f"STAGED_FILE_HASH_MISMATCH: actual_sha256={digest}",
            )
        state.target.parent.mkdir(parents=True, exist_ok=True)
        if state.target.exists():
            backup_file(state.target)
        os.replace(state.temporary, state.target)
        self._writes.pop(resolved_write_id, None)
        return ToolResult(
            success=True,
            content=(
                f"Committed {state.target} ({state.size_bytes} bytes, sha256={digest})."
            ),
            raw_output={
                "type": "artifact",
                "path": str(state.target),
                "size_bytes": state.size_bytes,
                "sha256": digest,
            },
        )

    def _abort(self, write_id: str | None) -> ToolResult:
        resolved = self._get_write(write_id)
        if isinstance(resolved, ToolResult):
            return resolved
        resolved_write_id, state = resolved
        state.temporary.unlink(missing_ok=True)
        self._writes.pop(resolved_write_id, None)
        return ToolResult(success=True, content="Aborted staged write.")

    def cleanup_pending_writes(self) -> list[str]:
        """Discard transactions that did not commit before the turn ended."""

        cleaned: list[str] = []
        for write_id, state in list(self._writes.items()):
            state.temporary.unlink(missing_ok=True)
            self._writes.pop(write_id, None)
            cleaned.append(write_id)
        return cleaned

    def _cleanup_stale_files(self) -> None:
        if not self._staging_dir.is_dir():
            return
        cutoff = time.time() - STAGING_MAX_AGE_SECONDS
        for candidate in self._staging_dir.glob("*.part"):
            try:
                if candidate.stat().st_mtime < cutoff:
                    candidate.unlink()
            except OSError:
                continue

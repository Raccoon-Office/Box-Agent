"""Explicit publication using the shared file-owned artifact contract."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import os
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from box_agent.tools.permissions import PermissionEngine

from box_agent.artifact_publication import STAGING_DIRECTORY, metadata_path, read_metadata, write_metadata
from box_agent.tools.base import Tool, ToolResult, ToolInvocationRejected, check_tool_cancellation
from box_agent.tools.file_change_receipts import (
    FileWriteInvocationContext, before_write, destination_lock, file_change_result,
)
from box_agent.tools.safety import builtin_skill_write_error


class PublishArtifactTool(Tool):
    """Record intentional delivery without inferring it from a tool result."""

    def __init__(self, workspace_dir: str | Path, permission_engine: PermissionEngine | None = None):
        self.workspace_dir = Path(workspace_dir).resolve()
        self._perm = permission_engine
        self._pending: dict[str, tuple[Path, Path]] = {}

    @property
    def name(self) -> str:
        return "publish_artifact"

    @property
    def description(self) -> str:
        return (
            "After creating and checking a main user-facing file, declare it as "
            "a primary user-facing artifact. Call once for each requested deliverable. "
            "Already published builder outputs and images "
            "do not need this call. Use it to promote selected intermediate files. "
            "For a self-contained single-file script deliverable with verifiable ownership, first call "
            "action=begin with its final path; generate and check the file at the returned "
            "staging_path, then action=commit with the returned write_id. Only commit "
            "replaces the final path and records generation. Plain publication never "
            "proves who generated an existing file. HTML with relative assets and directory "
            "builders must keep their existing generation/publication flow. Use action=abort to discard staging."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Final file path inside the session workspace."},
                "action": {"type": "string", "enum": ["publish", "begin", "commit", "abort"],
                           "default": "publish"},
                "write_id": {"type": "string", "description": "Exact transaction ID from begin."},
            },
            "additionalProperties": False,
        }

    def _file(self, path: str) -> Path | None:
        if not path.strip():
            return None
        try:
            candidate = Path(path).expanduser()
            if not candidate.is_absolute():
                candidate = self.workspace_dir / candidate
            candidate = candidate.resolve()
            candidate.relative_to(self.workspace_dir)
            return candidate if candidate.is_file() else None
        except (OSError, RuntimeError, ValueError):
            return None

    def _target(self, path: str | None) -> Path:
        if not path or not path.strip():
            raise ValueError("A non-empty final path is required")
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace_dir / candidate
        candidate = candidate.resolve()
        candidate.relative_to(self.workspace_dir)
        return candidate

    def _write_error(self, target: Path, context) -> ToolResult | None:
        if isinstance(context, FileWriteInvocationContext) and context.validate_write_target:
            if error := context.validate_write_target(target):
                return error
        if error := builtin_skill_write_error(target):
            return ToolResult(success=False, error=error)
        if self._perm:
            decision = self._perm.check(capability="filesystem.write",
                                        resource={"path": str(target)}, tool_name=self.name)
            if not decision.allowed:
                return ToolResult(success=False, error=decision.reason,
                                  permission_request=decision.permission_request)
        return None

    async def _invoke_validated(self, arguments, *, context):
        return await self._execute(context=context, **arguments)

    async def execute(self, path: str | None = None, action: str = "publish",
                      write_id: str | None = None) -> ToolResult:
        return await self._execute(path, action, write_id)

    async def _execute(self, path: str | None = None, action: str = "publish",
                       write_id: str | None = None, *, context=None) -> ToolResult:
        try:
            if action == "begin":
                target = self._target(path)
                if error := self._write_error(target, context):
                    return error
                check_tool_cancellation()
                # A random private input directory keeps file extensions intact for builders.
                staging_root = self.workspace_dir / STAGING_DIRECTORY
                staging_root.mkdir(exist_ok=True)
                if staging_root.resolve().parent != self.workspace_dir or staging_root.is_symlink():
                    raise ValueError("Artifact staging directory must stay inside the workspace")
                directory = Path(tempfile.mkdtemp(prefix="artifact-", dir=staging_root))
                staging = directory / target.name
                transaction = uuid.uuid4().hex
                self._pending[transaction] = (target, staging)
                return ToolResult(success=True, content=f"Generate and check the file at {staging}, then commit write_id={transaction}.",
                                  raw_output={"write_id": transaction, "staging_path": str(staging)})
            if action in {"commit", "abort"}:
                if not write_id or write_id not in self._pending:
                    raise ValueError("Unknown artifact write_id for this tool session")
                target, staging = self._pending[write_id]
                if action == "abort":
                    self._pending.pop(write_id)
                    self._cleanup_input(staging)
                    return ToolResult(success=True, content="Aborted artifact generation.")
                async with destination_lock(target):
                    resolved = target.resolve()
                    resolved.relative_to(self.workspace_dir)
                    if resolved != target:
                        raise ValueError("Artifact destination changed during generation")
                    if error := self._write_error(target, context):
                        return error
                    if staging.is_symlink() or staging.resolve() != staging or not staging.is_file():
                        raise ValueError("Generated artifact must be a regular staging file")
                    check_tool_cancellation()
                    target.parent.mkdir(parents=True, exist_ok=True)
                    # Seal a private copy, not the input inode a script may still hold open.
                    digest = hashlib.sha256()
                    fd, name = tempfile.mkstemp(prefix=f".{target.name}.commit-", dir=target.parent)
                    sealed = Path(name)
                    try:
                        with os.fdopen(fd, "wb") as output, staging.open("rb") as source:
                            while chunk := await asyncio.to_thread(source.read, 1024 * 1024):
                                check_tool_cancellation()
                                output.write(chunk)
                                digest.update(chunk)
                            output.flush()
                            os.fsync(output.fileno())
                        check_tool_cancellation()
                        # Copying yields to scripts; recheck authority at the commit seam.
                        if self.workspace_dir.resolve() != self.workspace_dir or target.resolve() != target:
                            raise ValueError("Artifact destination changed during generation")
                        if error := self._write_error(target, context):
                            return error
                        before = before_write(target)
                        write_metadata(target, {"type": "artifact", "sha256": digest.hexdigest(),
                                                "size_bytes": sealed.stat().st_size})
                        os.replace(sealed, target)
                    finally:
                        sealed.unlink(missing_ok=True)
                    self._pending.pop(write_id)
                    result = ToolResult(success=True, content=f"Committed and published {target.relative_to(self.workspace_dir)}.",
                                        raw_output={"type": "artifact", "abs_path": str(target),
                                                    "placement": "primary", "sha256": digest.hexdigest()})
                    result = file_change_result(result, self.workspace_dir, target, before, digest.hexdigest())
                    self._cleanup_input(staging)
                    return result
            if action != "publish":
                raise ValueError("Unknown artifact action")
            file = self._file(path or "")
            if file is None:
                return ToolResult(success=False, error="File is missing or outside the workspace")
            write_metadata(file, {**read_metadata(metadata_path(file)), "type": "artifact"})
            return ToolResult(success=True, content=f"Published {file.relative_to(self.workspace_dir).as_posix()}.",
                              raw_output={"type": "artifact", "abs_path": str(file), "placement": "primary"})
        except ToolInvocationRejected:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            return ToolResult(success=False, error=f"Could not publish artifact: {exc}")

    @staticmethod
    def _cleanup_input(staging: Path) -> None:
        # Builders can leave QA side files, and Windows may retain an open input.
        # Cleanup cannot turn a committed file into an unreceipted failed result.
        try:
            shutil.rmtree(staging.parent)
        except OSError:
            pass

    def cleanup_pending_writes(self) -> list[str]:
        discarded = []
        for transaction, (_, staging) in list(self._pending.items()):
            self._pending.pop(transaction)
            self._cleanup_input(staging)
            discarded.append(str(staging))
        return discarded

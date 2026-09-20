"""Explicit publication using the shared file-owned artifact contract."""

from __future__ import annotations

from pathlib import Path

from box_agent.artifact_publication import metadata_path, read_metadata, write_metadata
from box_agent.tools.base import Tool, ToolResult


class PublishArtifactTool(Tool):
    """Record intentional delivery without inferring it from a tool result."""

    def __init__(self, workspace_dir: str | Path):
        self.workspace_dir = Path(workspace_dir).resolve()

    @property
    def name(self) -> str:
        return "publish_artifact"

    @property
    def description(self) -> str:
        return (
            "After creating and checking a main user-facing file, declare it as "
            "a primary user-facing artifact. Call once for each requested deliverable. "
            "Already published builder outputs and images "
            "do not need this call. Use it to promote selected intermediate files."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path inside the session workspace."},
            },
            "required": ["path"],
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

    async def execute(self, path: str) -> ToolResult:
        file = self._file(path)
        if file is None:
            return ToolResult(success=False, error="File is missing or outside the workspace")
        try:
            write_metadata(file, {**read_metadata(metadata_path(file)), "type": "artifact"})
        except OSError as exc:
            return ToolResult(success=False, error=f"Could not publish artifact: {exc}")
        return ToolResult(
            success=True,
            content=f"Published {file.relative_to(self.workspace_dir).as_posix()}.",
            raw_output={"type": "artifact", "abs_path": str(file), "placement": "primary"},
        )

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
            "Register an existing, validated task output as deliverable or process. "
            "Deliverable directly satisfies this task; process is a useful intermediate "
            "such as cleaned data or analysis code. Never register inputs merely read, "
            "caches, thumbnails or internal files. Do not create files for text-only answers. "
            "Already registered builder outputs and standalone images need no extra call."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path inside the session workspace."},
                "role": {
                    "type": "string",
                    "enum": ["deliverable", "process"],
                    "description": "Task output role; defaults to deliverable.",
                },
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

    async def execute(self, path: str, role: str = "deliverable") -> ToolResult:
        if role not in {"deliverable", "process"}:
            return ToolResult(success=False, error="Invalid artifact role")
        file = self._file(path)
        if file is None:
            return ToolResult(success=False, error="File is missing or outside the workspace")
        try:
            with file.open("rb"):
                pass
            write_metadata(file, {
                **read_metadata(metadata_path(file)),
                "type": "artifact", "artifact_role": role,
            })
        except OSError as exc:
            return ToolResult(success=False, error=f"Could not publish artifact: {exc}")
        return ToolResult(
            success=True,
            content=f"Published {file.relative_to(self.workspace_dir).as_posix()}.",
            raw_output={"type": "artifact", "abs_path": str(file), "artifact_role": role},
        )

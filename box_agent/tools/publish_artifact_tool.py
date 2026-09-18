"""Model-declared user-facing files for one agent turn."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from box_agent.artifacts import make_artifact
from box_agent.events import ArtifactEvent
from box_agent.tools.base import Tool, ToolResult


@dataclass(frozen=True)
class PublishedArtifact:
    artifact: ArtifactEvent
    placement: str


class PublishArtifactTool(Tool):
    """Record intentional delivery without inferring it from a tool result."""

    def __init__(self, workspace_dir: str | Path):
        self.workspace_dir = Path(workspace_dir).resolve()
        self._declared: list[Path] = []

    @property
    def name(self) -> str:
        return "publish_artifact"

    @property
    def description(self) -> str:
        return (
            "After creating and checking a main user-facing file, declare it as "
            "a primary artifact. Call once for each main file the user intends "
            "to receive; multiple primary artifacts are allowed. Other files "
            "remain process files. Call before the final answer."
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
        if file not in self._declared:
            self._declared.append(file)
        return ToolResult(
            success=True,
            content=f"Declared {file.relative_to(self.workspace_dir).as_posix()} as primary.",
            raw_output={"type": "artifact_publication", "placement": "primary"},
        )

    def finalize(self, tool_call_id: str = "publish_artifact") -> list[PublishedArtifact]:
        return [
            PublishedArtifact(
                artifact=make_artifact(
                    tool_call_id, file, self.workspace_dir,
                ),
                placement="primary",
            )
            for declared in self._declared
            if (file := self._file(str(declared))) is not None
        ]

    def clear(self) -> None:
        self._declared.clear()

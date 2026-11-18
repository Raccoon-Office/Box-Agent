"""Child-owned progress used when a delegated call reaches its deadline."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from ..events import ArtifactEvent, ToolCallResult
from .base import Tool, ToolResult


CHECKPOINT_TOOL = "sub_agent_checkpoint"
WORK_ITEMS_SCHEMA = {
    "type": "array",
    "minItems": 1,
    "maxItems": 12,
    "description": (
        "Independently verifiable items for long multi-item work. Give each item a stable, distinct ID "
        "and a self-contained brief so the child can checkpoint it and only pending items can be resumed."
    ),
    "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "minLength": 1, "maxLength": 64},
            "task": {"type": "string", "minLength": 1, "maxLength": 800},
        },
        "required": ["id", "task"],
        "additionalProperties": False,
    },
}


def normalize_work_items(value: Any) -> tuple[dict[str, str], ...] | None:
    if value is None:
        return ()
    if not isinstance(value, list) or not 1 <= len(value) <= 12:
        return None
    items: dict[str, dict[str, str]] = {}
    for item in value:
        if not isinstance(item, dict) or set(item) != {"id", "task"}:
            return None
        item_id, task = item["id"], item["task"]
        if not isinstance(item_id, str) or not isinstance(task, str):
            return None
        item_id, task = item_id.strip(), task.strip()
        if not 1 <= len(item_id) <= 64 or not 1 <= len(task) <= 800 or item_id in items:
            return None
        items[item_id] = {"id": item_id, "task": task}
    return tuple(items.values())


@dataclass(slots=True)
class SubAgentHandoff:
    work_items: tuple[dict[str, str], ...]
    completed: dict[str, str] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)

    def remaining_items(self) -> list[dict[str, str]]:
        return [item for item in self.work_items if item["id"] not in self.completed]

    def _note_file(self, path: Any, sha256: Any) -> None:
        if not isinstance(path, str) or not path or len(path) > 320 or "\\" in path:
            return
        relative = PurePosixPath(path)
        if relative.is_absolute() or ".." in relative.parts or ":" in path:
            return
        if path in self.artifacts or len(self.artifacts) < 16:
            digest = sha256 if isinstance(sha256, str) and len(sha256) <= 64 else ""
            if digest or path not in self.artifacts:
                self.artifacts[path] = digest

    def observe(self, event: ArtifactEvent | ToolCallResult) -> None:
        if isinstance(event, ArtifactEvent):
            self._note_file(event.rel_path, event.sha256)
            return
        if not event.success or not isinstance(event.raw_output, dict):
            return
        if event.tool_name == CHECKPOINT_TOOL:
            item_id, summary = event.raw_output.get("item_id"), event.raw_output.get("summary")
            if isinstance(summary, str) and any(item["id"] == item_id for item in self.work_items):
                self.completed.setdefault(item_id, summary)
            return
        if event.tool_name in {"write_file", "append_file", "edit_file"} and event.raw_output.get("file_changes_version") == 2:
            changes = event.raw_output.get("file_changes")
            if not isinstance(changes, list):
                return
            for change in changes:
                if isinstance(change, dict) and change.get("after_sha256"):
                    self._note_file(change.get("path"), change["after_sha256"])

    def result(
        self, code: str, message: str, *, child_id: str, resume_from: str | None,
    ) -> ToolResult:
        pending = self.remaining_items()
        payload: dict[str, Any] = {
            "type": "sub_agent_handoff",
            "code": code,
            "message": message,
            "sub_agent_id": child_id,
            "remaining_item_ids": [item["id"] for item in pending],
        }
        if resume_from is not None:
            payload["resume_from"] = resume_from
            next_step = (
                "Verify the checkpoint and reuse the committed outputs. Continue only the pending items "
                "with sub_agent(resume_from=<value>, task='Continue pending items')."
            )
        elif self.work_items and not pending:
            next_step = "All assigned items were checkpointed. Verify and synthesize the existing results."
        else:
            next_step = (
                "Verify saved files and reported completion. Continue only work you can identify as unfinished. "
                "The original delegation may already have produced usable work."
            )
        payload["next_step"] = next_step
        payload["completed_items"] = [
            {"id": item_id, "summary": summary} for item_id, summary in self.completed.items()
        ]
        payload["artifacts"] = [{"path": path, "sha256": sha} for path, sha in self.artifacts.items()]
        return ToolResult(success=False, error=json.dumps(payload, ensure_ascii=False), raw_output=payload)


class SubAgentCheckpointTool(Tool):
    """Record one assigned item after its result is available to the parent."""

    name = CHECKPOINT_TOOL
    allowed_during_wrapup = True
    description = (
        "Record an assigned work item as complete immediately after verifying its result. "
        "Include the concise result or the committed workspace-relative output path. "
        "A checkpoint is retained if this delegation times out. Keep working on pending items."
    )
    parameters = {
        "type": "object",
        "properties": {
            "item_id": {"type": "string", "minLength": 1, "maxLength": 64},
            "summary": {"type": "string", "minLength": 1, "maxLength": 400},
        },
        "required": ["item_id", "summary"],
        "additionalProperties": False,
    }

    def __init__(self, handoff: SubAgentHandoff) -> None:
        self._handoff = handoff

    async def execute(self, item_id: str, summary: str) -> ToolResult:
        if not any(item["id"] == item_id for item in self._handoff.work_items) or not summary.strip():
            return ToolResult(success=False, error="Checkpoint requires an assigned item ID and a non-empty result.")
        return ToolResult(
            success=True,
            content=f"Checkpoint recorded for {item_id}.",
            raw_output={"item_id": item_id, "summary": summary.strip()},
        )

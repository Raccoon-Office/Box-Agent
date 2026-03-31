"""Todo Tool - Task tracking for multi-step agent workflows.

Lets the agent decompose complex tasks into trackable items,
update progress, and stay oriented across long execution chains.

Design:
- Two tool classes share a single ``TodoStore`` (in-memory list).
- Store is injected at construction time — the wiring lives in setup.py.
- Optional ``persist_path`` makes the store survive restarts.
"""

from __future__ import annotations

import json
from datetime import datetime
from itertools import count
from pathlib import Path
from typing import Any

from .base import Tool, ToolResult


# ── Shared store ────────────────────────────────────────────


class TodoStore:
    """Lightweight in-memory todo list with optional JSON persistence."""

    def __init__(self, persist_path: Path | None = None):
        self._items: dict[str, dict] = {}
        self._counter = count(1)
        self._persist_path = persist_path
        if persist_path and persist_path.exists():
            self._load()

    # -- internal helpers --------------------------------------------------

    def _next_id(self) -> str:
        return str(next(self._counter))

    def _save(self) -> None:
        if self._persist_path is None:
            return
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        self._persist_path.write_text(
            json.dumps(list(self._items.values()), indent=2, ensure_ascii=False)
        )

    def _load(self) -> None:
        try:
            items = json.loads(self._persist_path.read_text())  # type: ignore[union-attr]
            for item in items:
                self._items[item["id"]] = item
            # Resume counter after highest existing id
            if self._items:
                max_id = max(int(i) for i in self._items)
                self._counter = count(max_id + 1)
        except Exception:
            pass

    # -- public API --------------------------------------------------------

    def create(self, task: str, priority: str = "medium") -> dict:
        todo_id = self._next_id()
        item = {
            "id": todo_id,
            "task": task,
            "status": "pending",
            "priority": priority,
            "created_at": datetime.now().isoformat(),
        }
        self._items[todo_id] = item
        self._save()
        return item

    def update(self, todo_id: str, *, status: str | None = None, task: str | None = None) -> dict | None:
        item = self._items.get(todo_id)
        if item is None:
            return None
        if status is not None:
            item["status"] = status
        if task is not None:
            item["task"] = task
        self._save()
        return item

    def delete(self, todo_id: str) -> bool:
        removed = self._items.pop(todo_id, None) is not None
        if removed:
            self._save()
        return removed

    def get(self, todo_id: str) -> dict | None:
        return self._items.get(todo_id)

    def list(self, status: str | None = None) -> list[dict]:
        items = list(self._items.values())
        if status:
            items = [i for i in items if i["status"] == status]
        return items


# ── Tools ───────────────────────────────────────────────────

_VALID_STATUSES = ("pending", "in_progress", "completed")
_VALID_PRIORITIES = ("high", "medium", "low")


class TodoWriteTool(Tool):
    """Create, update, or delete todo items."""

    def __init__(self, store: TodoStore):
        self._store = store

    @property
    def name(self) -> str:
        return "todo_write"

    @property
    def description(self) -> str:
        return (
            "Manage a todo list for tracking multi-step tasks. "
            "Actions: 'create' a new item, 'update' an existing item's status or text, "
            "or 'delete' an item. Use this to decompose complex work into trackable steps "
            "and mark progress as you go."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "update", "delete"],
                    "description": "Operation to perform.",
                },
                "task": {
                    "type": "string",
                    "description": "Task description (required for 'create', optional for 'update').",
                },
                "todo_id": {
                    "type": "string",
                    "description": "ID of the todo item (required for 'update' and 'delete').",
                },
                "status": {
                    "type": "string",
                    "enum": list(_VALID_STATUSES),
                    "description": "Status to set (for 'update'). One of: pending, in_progress, completed.",
                },
                "priority": {
                    "type": "string",
                    "enum": list(_VALID_PRIORITIES),
                    "description": "Priority level (for 'create'). Default: medium.",
                },
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str,
        task: str | None = None,
        todo_id: str | None = None,
        status: str | None = None,
        priority: str = "medium",
    ) -> ToolResult:
        if action == "create":
            if not task:
                return ToolResult(success=False, error="'task' is required for create.")
            item = self._store.create(task, priority)
            return ToolResult(success=True, content=f"Created todo #{item['id']}: {task}")

        if action == "update":
            if not todo_id:
                return ToolResult(success=False, error="'todo_id' is required for update.")
            item = self._store.update(todo_id, status=status, task=task)
            if item is None:
                return ToolResult(success=False, error=f"Todo #{todo_id} not found.")
            return ToolResult(success=True, content=f"Updated todo #{todo_id}: [{item['status']}] {item['task']}")

        if action == "delete":
            if not todo_id:
                return ToolResult(success=False, error="'todo_id' is required for delete.")
            if not self._store.delete(todo_id):
                return ToolResult(success=False, error=f"Todo #{todo_id} not found.")
            return ToolResult(success=True, content=f"Deleted todo #{todo_id}.")

        return ToolResult(success=False, error=f"Unknown action: {action}")


class TodoReadTool(Tool):
    """Read the current todo list."""

    def __init__(self, store: TodoStore):
        self._store = store

    @property
    def name(self) -> str:
        return "todo_read"

    @property
    def description(self) -> str:
        return (
            "Read the current todo list. Returns all items or filtered by status. "
            "Use this to review progress and decide what to work on next."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "todo_id": {
                    "type": "string",
                    "description": "Optional: get a single item by ID.",
                },
                "status": {
                    "type": "string",
                    "enum": list(_VALID_STATUSES),
                    "description": "Optional: filter by status.",
                },
            },
        }

    async def execute(self, todo_id: str | None = None, status: str | None = None) -> ToolResult:
        # Single item lookup
        if todo_id:
            item = self._store.get(todo_id)
            if item is None:
                return ToolResult(success=False, error=f"Todo #{todo_id} not found.")
            return ToolResult(success=True, content=self._format_items([item]))

        # List (optionally filtered)
        items = self._store.list(status)
        if not items:
            label = f" ({status})" if status else ""
            return ToolResult(success=True, content=f"No todo items{label}.")
        return ToolResult(success=True, content=self._format_items(items))

    @staticmethod
    def _format_items(items: list[dict]) -> str:
        status_icon = {"pending": "○", "in_progress": "◑", "completed": "●"}
        lines = []
        for item in items:
            icon = status_icon.get(item["status"], "?")
            pri = f" [{item['priority']}]" if item.get("priority", "medium") != "medium" else ""
            lines.append(f"  {icon} #{item['id']} {item['task']}{pri}")

        # Summary line
        total = len(items)
        done = sum(1 for i in items if i["status"] == "completed")
        active = sum(1 for i in items if i["status"] == "in_progress")
        pending = total - done - active
        summary = f"Total: {total} | ● {done} done · ◑ {active} active · ○ {pending} pending"

        return "\n".join(lines) + "\n" + summary

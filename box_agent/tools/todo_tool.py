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


_VALID_STATUSES = ("pending", "in_progress", "completed")
_VALID_PRIORITIES = ("high", "medium", "low")
_MAX_TODO_MODEL_CONTEXT_CHARS = 12_000


def _todo_snapshot(items: list[dict]) -> dict[str, Any]:
    """Return a host-friendly todo snapshot payload."""
    normalized = [dict(item) for item in items]
    total = len(normalized)
    completed = sum(1 for item in normalized if item.get("status") == "completed")
    in_progress = sum(1 for item in normalized if item.get("status") == "in_progress")
    pending = sum(1 for item in normalized if item.get("status") == "pending")
    return {
        "type": "todo_snapshot",
        "items": normalized,
        "summary": {
            "total": total,
            "completed": completed,
            "in_progress": in_progress,
            "pending": pending,
        },
    }


# ── Shared store ────────────────────────────────────────────


def _todo_state_error(items: list[dict]) -> str | None:
    """Return an error when unfinished work has no unique active item."""
    unfinished = [item for item in items if item.get("status") != "completed"]
    if not unfinished:
        return None
    active = [item for item in unfinished if item.get("status") == "in_progress"]
    if len(active) == 1:
        return None
    return (
        "Todo list must contain exactly one in_progress item while unfinished "
        f"work remains; found {len(active)}."
    )


def _validate_todo_records(items: Any) -> None:
    """Validate todo record shapes without enforcing workflow state."""
    if not isinstance(items, list):
        raise ValueError("Todo data must be a list.")

    seen_ids: set[str] = set()
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Todo #{index} must be an object.")

        todo_id = item.get("id")
        if (
            not isinstance(todo_id, str)
            or not todo_id.isdigit()
            or int(todo_id) < 1
            or str(int(todo_id)) != todo_id
        ):
            raise ValueError(f"Todo #{index} has an invalid id: {todo_id!r}.")
        if todo_id in seen_ids:
            raise ValueError(f"Duplicate todo id: {todo_id}.")
        seen_ids.add(todo_id)

        task = item.get("task")
        if not isinstance(task, str) or not task.strip():
            raise ValueError(f"Todo #{todo_id} has no task.")
        status = item.get("status")
        if status not in _VALID_STATUSES:
            raise ValueError(f"Invalid status for todo #{todo_id}: {status}.")
        priority = item.get("priority", "medium")
        if priority not in _VALID_PRIORITIES:
            raise ValueError(f"Invalid priority for todo #{todo_id}: {priority}.")
        created_at = item.get("created_at")
        if not isinstance(created_at, str) or not created_at.strip():
            raise ValueError(f"Todo #{todo_id} has no created_at timestamp.")


def _validate_todo_items(items: Any) -> None:
    """Validate persisted and in-memory todo records without mutating state."""
    _validate_todo_records(items)
    state_error = _todo_state_error(items)
    if state_error:
        raise ValueError(state_error)


def _migrate_legacy_todo_state(items: list[dict]) -> bool:
    """Repair active-item counts accepted by the legacy persisted format."""
    unfinished = [item for item in items if item["status"] != "completed"]
    if not unfinished:
        return False

    active = [item for item in unfinished if item["status"] == "in_progress"]
    if len(active) == 1:
        return False
    if not active:
        unfinished[0]["status"] = "in_progress"
        return True

    for item in active[1:]:
        item["status"] = "pending"
    return True


def _context_item(item: dict) -> dict[str, Any]:
    """Return a bounded todo representation for model-only context."""
    task = str(item.get("task") or "")
    if len(task) > 500:
        task = task[:497] + "..."
    return {
        "id": item.get("id"),
        "task": task,
        "status": item.get("status"),
        "priority": item.get("priority", "medium"),
    }


def _todo_next_instruction(items: list[dict]) -> str:
    unfinished = [item for item in items if item.get("status") != "completed"]
    if not unfinished:
        return "All todo items are completed. Do not assume unfinished work remains."
    active = [item for item in unfinished if item.get("status") == "in_progress"]
    if len(active) != 1:
        return (
            "The persisted todo state has no unique in_progress item. Repair it with "
            "action='set' before continuing execution."
        )
    current = active[0]
    has_pending = any(item.get("status") == "pending" for item in unfinished)
    if not has_pending:
        return (
            f"Continue only with todo #{current.get('id')}. After completing and verifying "
            "it, use action='transition' without next_todo_id to complete the final "
            "unfinished item."
        )
    return (
        f"Continue only with todo #{current.get('id')}. After completing and verifying "
        "it, use action='transition' to complete it and activate the next pending item "
        "atomically. Use action='set' only to initialize or materially rebuild the list."
    )


def _todo_model_context(
    items: list[dict],
    *,
    action: str,
) -> str:
    """Return a bounded checkpoint of the complete current todo state."""
    if not items:
        return (
            f"Todo action '{action}' succeeded. The current todo list is empty. "
            "Do not assume unfinished work remains from an older list."
        )

    summary = _todo_snapshot(items)["summary"]
    instruction = _todo_next_instruction(items)
    current = [_context_item(item) for item in items]
    full_context = (
        f"Todo action '{action}' succeeded. This is the complete current todo list "
        "and execution state:\n"
        f"{json.dumps(current, indent=2, ensure_ascii=False)}\n"
        f"Summary: {json.dumps(summary, ensure_ascii=False)}\n"
        f"{instruction}"
    )
    if len(full_context) <= _MAX_TODO_MODEL_CONTEXT_CHARS:
        return full_context

    active = [item for item in items if item.get("status") == "in_progress"]
    preview_source = active + [
        item for item in items if item.get("status") == "pending"
    ][:5]
    preview = [_context_item(item) for item in preview_source]
    return (
        f"Todo action '{action}' succeeded. The complete list was omitted from model "
        "context because it exceeds the context limit; use todo_read when the full "
        "list is needed.\n"
        f"Summary: {json.dumps(summary, ensure_ascii=False)}\n"
        f"Active and next pending preview: "
        f"{json.dumps(preview, ensure_ascii=False)}\n"
        f"{instruction}"
    )


class TodoStore:
    """Lightweight todo list with validated, legacy-compatible persistence."""

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
        """Load a complete snapshot and migrate legacy active-item state."""
        persist_path = self._persist_path
        if persist_path is None:
            return
        try:
            loaded = json.loads(persist_path.read_text())
            _validate_todo_records(loaded)
            candidate_items = [dict(item) for item in loaded]
            migrated = _migrate_legacy_todo_state(candidate_items)
            _validate_todo_items(candidate_items)
            max_id = max((int(item["id"]) for item in candidate_items), default=0)
        except (OSError, UnicodeError, TypeError, ValueError) as exc:
            raise ValueError(
                f"Invalid persisted todo state in {persist_path}: {exc}"
            ) from exc

        self._items = {item["id"]: item for item in candidate_items}
        self._counter = count(max_id + 1)
        if migrated:
            self._save()

    def _commit(self, items: list[dict]) -> None:
        _validate_todo_items(items)
        self._items = {str(item["id"]): dict(item) for item in items}
        self._save()

    # -- public API --------------------------------------------------------

    def create(
        self,
        task: str,
        priority: str = "medium",
        status: str | None = None,
    ) -> dict:
        items = [dict(item) for item in self._items.values()]
        if status is None:
            has_unfinished = any(item["status"] != "completed" for item in items)
            status = "pending" if has_unfinished else "in_progress"
        new_item = {
            "task": task.strip(),
            "status": status,
            "priority": priority,
            "created_at": datetime.now().isoformat(),
        }
        state_error = _todo_state_error([*items, new_item])
        if state_error:
            raise ValueError(state_error)
        todo_id = self._next_id()
        item = {"id": todo_id, **new_item}
        items.append(item)
        self._commit(items)
        return item

    def replace(self, todos: list[dict[str, Any]]) -> list[dict]:
        current = self._items
        current_items = list(current.values())
        supplied_ids = [
            str(todo["id"]).strip()
            for todo in todos
            if todo.get("id") is not None
        ]
        seen_ids: set[str] = set()
        for todo_id in supplied_ids:
            if todo_id in seen_ids:
                raise ValueError(f"Duplicate todo id: {todo_id}.")
            seen_ids.add(todo_id)
        unknown_ids = [todo_id for todo_id in supplied_ids if todo_id not in current]
        if unknown_ids:
            raise ValueError(
                f"Todo #{unknown_ids[0]} does not exist; omit id for a new todo."
            )

        requested_shape = (
            [
                (
                    str(todo["task"]).strip(),
                    str(
                        todo.get("priority")
                        or current_items[index].get("priority")
                        or "medium"
                    ),
                )
                for index, todo in enumerate(todos)
            ]
            if len(todos) == len(current_items)
            else []
        )
        current_shape = [
            (str(item["task"]).strip(), str(item.get("priority") or "medium"))
            for item in current_items
        ]
        if current_items and requested_shape == current_shape:
            requested_statuses = [
                str(todo.get("status") or current_items[index]["status"])
                for index, todo in enumerate(todos)
            ]
            current_statuses = [str(item["status"]) for item in current_items]
            if requested_statuses != current_statuses:
                raise ValueError(
                    "Existing todo statuses must be advanced with action='transition'; "
                    "call todo_read if canonical todo IDs are unavailable."
                )

        existing_ids_by_task: dict[str, list[str]] = {}
        for item in current_items:
            existing_ids_by_task.setdefault(str(item["task"]).strip(), []).append(
                str(item["id"])
            )
        for todo in todos:
            if todo.get("id") is not None:
                continue
            task = str(todo["task"]).strip()
            matching_ids = existing_ids_by_task.get(task)
            if not matching_ids:
                continue
            missing_ids = [todo_id for todo_id in matching_ids if todo_id not in seen_ids]
            if missing_ids:
                expected = ", ".join(f"#{todo_id}" for todo_id in missing_ids)
                raise ValueError(
                    f"Existing todo '{task}' must preserve its id ({expected}); "
                    "call todo_read before rebuilding the list."
                )

        candidate_state = []
        for todo in todos:
            supplied_id = todo.get("id")
            existing = current.get(str(supplied_id).strip()) if supplied_id is not None else None
            candidate_state.append({
                "status": str(
                    todo.get("status")
                    or (existing or {}).get("status")
                    or "pending"
                )
            })
        state_error = _todo_state_error(candidate_state)
        if state_error:
            raise ValueError(state_error)

        items: list[dict] = []
        for todo in todos:
            supplied_id = todo.get("id")
            if supplied_id is None:
                todo_id = self._next_id()
                created_at = datetime.now().isoformat()
                existing = None
            else:
                todo_id = str(supplied_id).strip()
                existing = current[todo_id]
                created_at = existing.get("created_at") or datetime.now().isoformat()
            items.append({
                "id": todo_id,
                "task": str(todo["task"]).strip(),
                "status": str(
                    todo.get("status")
                    or (existing or {}).get("status")
                    or "pending"
                ),
                "priority": str(
                    todo.get("priority")
                    or (existing or {}).get("priority")
                    or "medium"
                ),
                "created_at": created_at,
            })
        self._commit(items)
        return self.list()

    def update(self, todo_id: str, *, status: str | None = None, task: str | None = None) -> dict | None:
        if todo_id not in self._items:
            return None
        items = [dict(item) for item in self._items.values()]
        item = next(item for item in items if item["id"] == todo_id)
        if status is not None:
            item["status"] = status
        if task is not None:
            item["task"] = task.strip()
        self._commit(items)
        return item

    def delete(self, todo_id: str) -> dict | None:
        removed = self._items.get(todo_id)
        if removed is None:
            return None
        items = [
            dict(item) for item in self._items.values() if item["id"] != todo_id
        ]
        self._commit(items)
        return removed

    def transition(
        self,
        todo_id: str,
        next_todo_id: str | None = None,
    ) -> tuple[dict, dict | None] | None:
        if todo_id not in self._items:
            return None
        if next_todo_id == todo_id:
            raise ValueError("'next_todo_id' must differ from 'todo_id'.")

        items = [dict(item) for item in self._items.values()]
        current = next(item for item in items if item["id"] == todo_id)
        if current["status"] != "in_progress":
            raise ValueError(f"Todo #{todo_id} is not in_progress.")
        current["status"] = "completed"

        next_item = None
        if next_todo_id is not None:
            next_item = next(
                (item for item in items if item["id"] == next_todo_id),
                None,
            )
            if next_item is None:
                raise ValueError(f"Todo #{next_todo_id} not found.")
            if next_item["status"] != "pending":
                raise ValueError(f"Todo #{next_todo_id} is not pending.")
            next_item["status"] = "in_progress"

        self._commit(items)
        return current, next_item

    def get(self, todo_id: str) -> dict | None:
        item = self._items.get(todo_id)
        return dict(item) if item is not None else None

    def list(self, status: str | None = None) -> list[dict]:
        items = [dict(item) for item in self._items.values()]
        if status:
            items = [i for i in items if i["status"] == status]
        return items


# ── Tools ───────────────────────────────────────────────────

class TodoWriteTool(Tool):
    """Create, replace, transition, update, or delete todo items."""

    def __init__(self, store: TodoStore):
        self._store = store

    def _result(
        self,
        *,
        content: str,
        action: str,
        item: dict | None = None,
        transition: dict[str, Any] | None = None,
    ) -> ToolResult:
        items = self._store.list()
        snapshot = _todo_snapshot(items)
        model_context = _todo_model_context(items, action=action)
        raw_output = {**snapshot, "action": action}
        if item is not None:
            raw_output["item"] = dict(item)
        if transition is not None:
            raw_output["transition"] = dict(transition)
        return ToolResult(
            success=True,
            content=content,
            raw_output=raw_output,
            model_context=model_context,
            state_checkpoint=model_context,
        )

    @property
    def name(self) -> str:
        return "todo_write"

    @property
    def description(self) -> str:
        return (
            "Manage a todo list for tracking multi-step tasks. "
            "Actions: 'set' the full current list in one call, 'create' a new item, "
            "'transition' atomically completes the active item and starts the next, "
            "'update' changes an existing item, and 'delete' removes an item. "
            "Use 'set' only for new or substantially revised multi-step work. Preserve "
            "the id of unchanged existing items in the complete ordered todos array. "
            "Status-only changes to the same ordered list are rejected; use "
            "'transition' for progress. Existing tasks submitted without their canonical "
            "ids are also rejected; call todo_read first if the ids are unavailable. "
            "When initializing an empty todo list, omit ids; any supplied ids are "
            "ignored and canonical todo ids are assigned automatically. "
            "Use 'transition' for normal progress instead of rebuilding the whole list. "
            "If a current plan exists, call plan_read before setting todos. Derive the "
            "todos from plan steps in order and keep the plan's objective, scope, and "
            "verification requirements aligned. A plan step may be split into executable "
            "subtasks, but do not omit plan steps, change their meaning, or add unrelated "
            "work. Revise the plan with plan_write before materially changing execution. "
            "Use this to decompose complex work into trackable steps "
            "and mark progress as you go: keep exactly the current item in_progress, mark "
            "finished items completed, and move the next item to in_progress before working "
            "on it. This tool is only a progress tracker: it is not "
            "factual evidence, a search strategy, or a source for final conclusions. Do not "
            "narrow the user's request or lower verification standards because a todo exists."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["set", "create", "transition", "update", "delete"],
                    "description": "Operation to perform.",
                },
                "todos": {
                    "type": "array",
                    "description": (
                        "Complete ordered todo list for action='set'. This replaces the "
                        "current list. Each item requires task and status, with optional "
                        "priority. When unfinished work remains, the complete list must "
                        "contain exactly one in_progress item; an empty or fully completed "
                        "list must contain none."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": (
                                    "Existing todo ID to preserve during action='set'. "
                                    "Omit for a new todo and when initializing an empty "
                                    "todo list; supplied ids are ignored during initialization."
                                ),
                            },
                            "task": {"type": "string", "description": "Task description."},
                            "status": {
                                "type": "string",
                                "enum": list(_VALID_STATUSES),
                                "description": (
                                    "Required task status. Across the complete set payload, "
                                    "unfinished work requires exactly one in_progress item."
                                ),
                            },
                            "priority": {
                                "type": "string",
                                "enum": list(_VALID_PRIORITIES),
                                "description": "Priority level. Default: medium.",
                            },
                        },
                        "required": ["task", "status"],
                    },
                },
                "task": {
                    "type": "string",
                    "description": "Task description (required for 'create', optional for 'update').",
                },
                "todo_id": {
                    "type": "string",
                    "description": (
                        "ID of the todo item (required for 'transition', 'update', "
                        "and 'delete')."
                    ),
                },
                "next_todo_id": {
                    "type": "string",
                    "description": (
                        "Pending todo to activate for action='transition'. Omit only "
                        "when completing the final unfinished todo."
                    ),
                },
                "status": {
                    "type": "string",
                    "enum": list(_VALID_STATUSES),
                    "description": (
                        "Status to set for 'create' or 'update'. One of: pending, "
                        "in_progress, completed."
                    ),
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
        todos: list[dict[str, Any]] | None = None,
        next_todo_id: str | None = None,
    ) -> ToolResult:
        try:
            if action == "set":
                normalized_todos = todos
                if isinstance(todos, list) and not self._store.list():
                    normalized_todos = [
                        {key: value for key, value in todo.items() if key != "id"}
                        if isinstance(todo, dict)
                        else todo
                        for todo in todos
                    ]
                validation_error = self._validate_todos(normalized_todos)
                if validation_error:
                    return ToolResult(success=False, error=validation_error)
                items = self._store.replace(normalized_todos or [])
                return self._result(
                    content=(
                        f"Set todo list with {len(items)} "
                        f"item{'s' if len(items) != 1 else ''}."
                    ),
                    action="set",
                )

            if action == "create":
                if not task or not task.strip():
                    return ToolResult(success=False, error="'task' is required for create.")
                if status is not None and status not in _VALID_STATUSES:
                    return ToolResult(success=False, error=f"Invalid status: {status}.")
                if priority not in _VALID_PRIORITIES:
                    return ToolResult(success=False, error=f"Invalid priority: {priority}.")
                item = self._store.create(task, priority, status)
                return self._result(
                    content=f"Created todo #{item['id']}: {item['task']}",
                    action="create",
                    item=item,
                )

            if action == "transition":
                if not todo_id:
                    return ToolResult(
                        success=False,
                        error="'todo_id' is required for transition.",
                    )
                changed = self._store.transition(todo_id, next_todo_id)
                if changed is None:
                    return ToolResult(success=False, error=f"Todo #{todo_id} not found.")
                completed, next_item = changed
                transition = {
                    "completed_id": completed["id"],
                    "in_progress_id": next_item["id"] if next_item else None,
                }
                content = f"Completed todo #{completed['id']}."
                if next_item is not None:
                    content += f" Started todo #{next_item['id']}: {next_item['task']}"
                return self._result(
                    content=content,
                    action="transition",
                    transition=transition,
                )

            if action == "update":
                if not todo_id:
                    return ToolResult(success=False, error="'todo_id' is required for update.")
                if status is not None and status not in _VALID_STATUSES:
                    return ToolResult(success=False, error=f"Invalid status: {status}.")
                if task is not None and not task.strip():
                    return ToolResult(success=False, error="'task' must not be empty.")
                item = self._store.update(todo_id, status=status, task=task)
                if item is None:
                    return ToolResult(success=False, error=f"Todo #{todo_id} not found.")
                return self._result(
                    content=f"Updated todo #{todo_id}: [{item['status']}] {item['task']}",
                    action="update",
                    item=item,
                )

            if action == "delete":
                if not todo_id:
                    return ToolResult(success=False, error="'todo_id' is required for delete.")
                removed = self._store.delete(todo_id)
                if removed is None:
                    return ToolResult(success=False, error=f"Todo #{todo_id} not found.")
                return self._result(
                    content=f"Deleted todo #{todo_id}.",
                    action="delete",
                )

            return ToolResult(success=False, error=f"Unknown action: {action}")
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))

    @staticmethod
    def _validate_todos(todos: list[dict[str, Any]] | None) -> str | None:
        if todos is None:
            return "'todos' is required for set."
        if not isinstance(todos, list):
            return "'todos' must be a list for set."
        for index, todo in enumerate(todos, start=1):
            if not isinstance(todo, dict):
                return f"Todo #{index} must be an object."
            task = str(todo.get("task") or "").strip()
            if not task:
                return f"'task' is required for todo #{index}."
            if "id" in todo and not str(todo.get("id") or "").strip():
                return f"Invalid id for todo #{index}."
            if not str(todo.get("status") or "").strip():
                return f"'status' is required for todo #{index}."
            status = str(todo["status"])
            if status not in _VALID_STATUSES:
                return f"Invalid status for todo #{index}: {status}."
            priority = str(todo.get("priority") or "medium")
            if priority not in _VALID_PRIORITIES:
                return f"Invalid priority for todo #{index}: {priority}."
        return None


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
        all_items = self._store.list()
        snapshot = _todo_snapshot(all_items)
        model_context = _todo_model_context(all_items, action="read")

        # Single item lookup
        if todo_id:
            item = self._store.get(todo_id)
            if item is None:
                return ToolResult(success=False, error=f"Todo #{todo_id} not found.")
            return ToolResult(
                success=True,
                content=self._format_items([item]),
                raw_output=snapshot,
                model_context=model_context,
                state_checkpoint=model_context,
            )

        # List (optionally filtered)
        items = [item for item in all_items if status is None or item["status"] == status]
        if not items:
            label = f" ({status})" if status else ""
            return ToolResult(
                success=True,
                content=f"No todo items{label}.",
                raw_output=snapshot,
                model_context=model_context,
                state_checkpoint=model_context,
            )
        return ToolResult(
            success=True,
            content=self._format_items(items),
            raw_output=snapshot,
            model_context=model_context,
            state_checkpoint=model_context,
        )

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

"""Test cases for Todo Tool."""

import json
import tempfile
from pathlib import Path

import pytest

from box_agent.tools.todo_tool import TodoReadTool, TodoStore, TodoWriteTool


# ── Fixtures ────────────────────────────────────────────────


@pytest.fixture
def store():
    return TodoStore()


@pytest.fixture
def writer(store):
    return TodoWriteTool(store)


@pytest.fixture
def reader(store):
    return TodoReadTool(store)


# ── TodoWriteTool tests ─────────────────────────────────────


@pytest.mark.asyncio
async def test_create(writer, reader):
    result = await writer.execute(action="create", task="Implement feature A")
    assert result.success
    assert "#1" in result.content
    assert result.raw_output["type"] == "todo_snapshot"
    assert result.raw_output["action"] == "create"
    assert result.raw_output["items"][0]["task"] == "Implement feature A"
    assert result.raw_output["summary"] == {
        "total": 1,
        "completed": 0,
        "in_progress": 1,
        "pending": 0,
    }

    result = await reader.execute()
    assert result.success
    assert "Implement feature A" in result.content
    assert result.raw_output["type"] == "todo_snapshot"
    assert result.raw_output["summary"]["in_progress"] == 1


@pytest.mark.asyncio
async def test_create_with_priority(writer, reader):
    result = await writer.execute(action="create", task="Fix critical bug", priority="high")
    assert result.success

    result = await reader.execute()
    assert "[high]" in result.content


@pytest.mark.asyncio
async def test_create_with_initial_status(writer, reader):
    result = await writer.execute(action="create", task="Draft outline", status="in_progress")
    assert result.success
    assert result.raw_output["items"][0]["status"] == "in_progress"
    assert result.raw_output["summary"] == {
        "total": 1,
        "completed": 0,
        "in_progress": 1,
        "pending": 0,
    }

    result = await reader.execute()
    assert result.raw_output["summary"]["in_progress"] == 1


@pytest.mark.asyncio
async def test_create_rejects_explicit_pending_without_active_item(writer):
    result = await writer.execute(action="create", task="Draft outline", status="pending")

    assert not result.success
    assert "exactly one in_progress" in result.error


@pytest.mark.asyncio
async def test_set_replaces_full_todo_list(writer, reader):
    await writer.execute(action="create", task="Old task")

    result = await writer.execute(
        action="set",
        todos=[
            {"task": "Inspect logs", "status": "completed", "priority": "high"},
            {"task": "Compare opencode todo tool", "status": "in_progress", "priority": "high"},
            {"task": "Adapt Box-Agent todo_write", "status": "pending", "priority": "medium"},
        ],
    )

    assert result.success
    assert result.raw_output["action"] == "set"
    assert [
        {
            "id": item["id"],
            "task": item["task"],
            "status": item["status"],
            "priority": item["priority"],
        }
        for item in result.raw_output["items"]
    ] == [
        {
            "id": "2",
            "task": "Inspect logs",
            "status": "completed",
            "priority": "high",
        },
        {
            "id": "3",
            "task": "Compare opencode todo tool",
            "status": "in_progress",
            "priority": "high",
        },
        {
            "id": "4",
            "task": "Adapt Box-Agent todo_write",
            "status": "pending",
            "priority": "medium",
        },
    ]
    assert result.raw_output["summary"] == {
        "total": 3,
        "completed": 1,
        "in_progress": 1,
        "pending": 1,
    }

    result = await reader.execute()
    assert "Old task" not in result.content
    assert "Inspect logs" in result.content
    assert "Compare opencode todo tool" in result.content
    assert "Adapt Box-Agent todo_write" in result.content


@pytest.mark.asyncio
async def test_set_initialization_ignores_supplied_ids(writer):
    result = await writer.execute(
        action="set",
        todos=[
            {"id": "99", "task": "Inspect implementation", "status": "in_progress"},
            {"id": "", "task": "Run verification", "status": "pending"},
        ],
    )

    assert result.success
    assert [item["id"] for item in result.raw_output["items"]] == ["1", "2"]
    assert [item["status"] for item in result.raw_output["items"]] == [
        "in_progress",
        "pending",
    ]
    assert result.model_context is not None
    assert '"id": "1"' in result.model_context
    assert '"id": "2"' in result.model_context


def test_store_replace_keeps_foreign_ids_strict_when_empty(store):
    with pytest.raises(ValueError, match="Todo #99 does not exist"):
        store.replace(
            [
                {"id": "99", "task": "Inspect implementation", "status": "in_progress"},
            ]
        )


@pytest.mark.asyncio
async def test_set_preserves_existing_identity_and_never_reuses_ids(writer):
    initial = await writer.execute(
        action="set",
        todos=[
            {"task": "Old A", "status": "in_progress"},
            {"task": "Old B", "status": "pending", "priority": "high"},
        ],
    )
    old_b = initial.raw_output["items"][1]

    result = await writer.execute(
        action="set",
        todos=[
            {
                "id": old_b["id"],
                "task": "Old B",
                "status": "pending",
            },
            {"task": "Inserted", "status": "in_progress"},
        ],
    )

    assert result.success
    assert [item["id"] for item in result.raw_output["items"]] == ["2", "3"]
    assert result.raw_output["items"][0]["status"] == "pending"
    assert result.raw_output["items"][0]["priority"] == "high"
    assert result.raw_output["items"][0]["created_at"] == old_b["created_at"]

    follow_up = await writer.execute(action="create", task="Follow-up")
    assert follow_up.success
    assert follow_up.raw_output["item"]["id"] == "4"


@pytest.mark.asyncio
async def test_set_requires_valid_todos(writer):
    result = await writer.execute(action="set")
    assert not result.success
    assert "'todos' is required" in result.error

    result = await writer.execute(action="set", todos=[{"status": "pending"}])
    assert not result.success
    assert "'task' is required" in result.error

    result = await writer.execute(action="set", todos=[{"task": "Missing status"}])
    assert not result.success
    assert "'status' is required" in result.error

    result = await writer.execute(action="set", todos=[{"task": "Bad", "status": "cancelled"}])
    assert not result.success
    assert "Invalid status" in result.error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "todos, active_count",
    [
        ([{"task": "Pending only", "status": "pending"}], 0),
        (
            [
                {"task": "Active A", "status": "in_progress"},
                {"task": "Active B", "status": "in_progress"},
            ],
            2,
        ),
    ],
)
async def test_set_rejects_invalid_active_item_count(writer, todos, active_count):
    result = await writer.execute(action="set", todos=todos)

    assert not result.success
    assert f"found {active_count}" in result.error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "todos",
    [
        [],
        [
            {"task": "Done A", "status": "completed"},
            {"task": "Done B", "status": "completed"},
        ],
    ],
)
async def test_set_allows_no_active_item_when_nothing_is_unfinished(writer, todos):
    result = await writer.execute(action="set", todos=todos)

    assert result.success
    assert result.raw_output["summary"]["in_progress"] == 0


@pytest.mark.asyncio
async def test_failed_set_does_not_consume_ids_or_change_existing_state(writer):
    initial = await writer.execute(action="create", task="Current")

    failed = await writer.execute(
        action="set",
        todos=[
            {"task": "Invalid pending", "status": "pending"},
        ],
    )

    assert not failed.success
    assert writer._store.list() == initial.raw_output["items"]

    created = await writer.execute(action="create", task="Next")
    assert created.success
    assert created.raw_output["item"]["id"] == "2"


@pytest.mark.asyncio
async def test_set_rejects_duplicate_and_unknown_existing_ids(writer):
    initial = await writer.execute(action="create", task="Current")
    current = initial.raw_output["items"][0]

    duplicate = await writer.execute(
        action="set",
        todos=[
            {"id": current["id"], "task": "Current", "status": "in_progress"},
            {"id": current["id"], "task": "Duplicate", "status": "pending"},
        ],
    )
    assert not duplicate.success
    assert "Duplicate todo id" in duplicate.error

    unknown = await writer.execute(
        action="set",
        todos=[
            {"id": "999", "task": "Unknown", "status": "in_progress"},
        ],
    )
    assert not unknown.success
    assert "does not exist" in unknown.error
    assert writer._store.list() == initial.raw_output["items"]


@pytest.mark.asyncio
async def test_create_requires_task(writer):
    result = await writer.execute(action="create")
    assert not result.success
    assert "required" in result.error.lower()


@pytest.mark.asyncio
async def test_update_status(writer, reader):
    await writer.execute(action="create", task="Do something")

    result = await writer.execute(action="update", todo_id="1", status="in_progress")
    assert result.success
    assert "in_progress" in result.content

    result = await writer.execute(action="update", todo_id="1", status="completed")
    assert result.success
    assert "completed" in result.content


@pytest.mark.asyncio
async def test_transition_atomically_advances_to_next_todo(writer):
    await writer.execute(action="create", task="Inspect implementation", status="in_progress")
    await writer.execute(action="create", task="Run verification")

    result = await writer.execute(
        action="transition",
        todo_id="1",
        next_todo_id="2",
    )

    assert result.success
    assert result.raw_output["transition"] == {
        "completed_id": "1",
        "in_progress_id": "2",
    }
    assert [item["status"] for item in result.raw_output["items"]] == [
        "completed",
        "in_progress",
    ]
    assert result.model_context is not None
    assert "Changed items" in result.model_context
    assert "Inspect implementation" in result.model_context
    assert "Run verification" in result.model_context
    assert "action='transition'" in result.model_context
    assert "without next_todo_id" in result.model_context
    assert "complete current todo list" not in result.model_context


@pytest.mark.asyncio
async def test_transition_completes_final_todo_without_next_id(writer):
    await writer.execute(action="create", task="Final task")

    result = await writer.execute(action="transition", todo_id="1")

    assert result.success
    assert result.raw_output["summary"] == {
        "total": 1,
        "completed": 1,
        "in_progress": 0,
        "pending": 0,
    }
    assert result.raw_output["transition"]["in_progress_id"] is None
    assert "All todo items are completed" in result.model_context


@pytest.mark.asyncio
async def test_transition_requires_next_id_when_pending_work_remains(writer):
    await writer.execute(action="create", task="Current")
    await writer.execute(action="create", task="Next")

    result = await writer.execute(action="transition", todo_id="1")

    assert not result.success
    assert "found 0" in result.error
    items = writer._store.list()
    assert [item["status"] for item in items] == ["in_progress", "pending"]


@pytest.mark.asyncio
async def test_update_cannot_leave_pending_work_without_active_item(writer):
    await writer.execute(action="create", task="Current")
    await writer.execute(action="create", task="Next")

    result = await writer.execute(action="update", todo_id="1", status="completed")

    assert not result.success
    assert "found 0" in result.error


@pytest.mark.asyncio
async def test_delete_cannot_remove_active_item_while_pending_work_remains(writer):
    await writer.execute(action="create", task="Current")
    await writer.execute(action="create", task="Next")

    result = await writer.execute(action="delete", todo_id="1")

    assert not result.success
    assert "found 0" in result.error


@pytest.mark.asyncio
async def test_set_model_context_is_bounded_for_large_lists(writer):
    todos = [
        {
            "task": f"Task {index}: " + "x" * 500,
            "status": "in_progress" if index == 0 else "pending",
        }
        for index in range(100)
    ]

    result = await writer.execute(action="set", todos=todos)

    assert result.success
    assert len(result.model_context) <= 12_000
    assert "complete list was omitted" in result.model_context
    assert "todo_read" in result.model_context


@pytest.mark.asyncio
async def test_update_task_text(writer, reader):
    await writer.execute(action="create", task="Old description")

    result = await writer.execute(action="update", todo_id="1", task="New description")
    assert result.success

    result = await reader.execute(todo_id="1")
    assert "New description" in result.content


@pytest.mark.asyncio
async def test_update_not_found(writer):
    result = await writer.execute(action="update", todo_id="999", status="completed")
    assert not result.success
    assert "not found" in result.error.lower()


@pytest.mark.asyncio
async def test_update_requires_id(writer):
    result = await writer.execute(action="update", status="completed")
    assert not result.success
    assert "required" in result.error.lower()


@pytest.mark.asyncio
async def test_delete(writer, reader):
    await writer.execute(action="create", task="Temporary task")

    result = await writer.execute(action="delete", todo_id="1")
    assert result.success

    result = await reader.execute()
    assert "No todo items" in result.content


@pytest.mark.asyncio
async def test_delete_not_found(writer):
    result = await writer.execute(action="delete", todo_id="999")
    assert not result.success


@pytest.mark.asyncio
async def test_unknown_action(writer):
    result = await writer.execute(action="explode")
    assert not result.success
    assert "Unknown action" in result.error


# ── TodoReadTool tests ──────────────────────────────────────


@pytest.mark.asyncio
async def test_read_empty(reader):
    result = await reader.execute()
    assert result.success
    assert "No todo items" in result.content


@pytest.mark.asyncio
async def test_read_single_by_id(writer, reader):
    await writer.execute(action="create", task="Task A")
    await writer.execute(action="create", task="Task B")

    result = await reader.execute(todo_id="2")
    assert result.success
    assert "Task B" in result.content
    assert "Task A" not in result.content
    assert [item["task"] for item in result.raw_output["items"]] == ["Task A", "Task B"]
    assert result.raw_output["summary"] == {
        "total": 2,
        "completed": 0,
        "in_progress": 1,
        "pending": 1,
    }


@pytest.mark.asyncio
async def test_read_filter_by_status(writer, reader):
    await writer.execute(action="create", task="Pending task")
    await writer.execute(action="create", task="Done task")
    await writer.execute(action="update", todo_id="2", status="completed")

    result = await reader.execute(status="completed")
    assert result.success
    assert "Done task" in result.content
    assert "Pending task" not in result.content
    assert [item["task"] for item in result.raw_output["items"]] == [
        "Pending task",
        "Done task",
    ]
    assert result.raw_output["summary"]["total"] == 2


@pytest.mark.asyncio
async def test_read_summary_line(writer, reader):
    await writer.execute(action="create", task="A")
    await writer.execute(action="create", task="B")
    await writer.execute(action="create", task="C")
    await writer.execute(action="update", todo_id="1", status="in_progress")
    await writer.execute(action="update", todo_id="2", status="completed")

    result = await reader.execute()
    assert "1 done" in result.content
    assert "1 active" in result.content
    assert "1 pending" in result.content


# ── TodoStore persistence ───────────────────────────────────


def _persisted_item(
    todo_id: str,
    *,
    status: str,
    priority: str = "medium",
) -> dict:
    return {
        "id": todo_id,
        "task": f"Task {todo_id}",
        "status": status,
        "priority": priority,
        "created_at": "2026-08-10T00:00:00",
    }


@pytest.mark.asyncio
async def test_persistence():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "todos.json"

        # First session: create items
        store1 = TodoStore(persist_path=path)
        store1.create("Persistent task", "high")
        store1.create("Another task")

        # Second session: reload from disk
        store2 = TodoStore(persist_path=path)
        items = store2.list()
        assert len(items) == 2
        assert items[0]["task"] == "Persistent task"
        assert items[0]["priority"] == "high"

        # Counter should resume (next id = 3)
        item = store2.create("Third task")
        assert item["id"] == "3"


def test_persistence_load_does_not_rewrite_valid_file(tmp_path):
    path = tmp_path / "todos.json"
    items = [
        _persisted_item("1", status="in_progress"),
        _persisted_item("2", status="pending"),
    ]
    original = json.dumps(items, ensure_ascii=False, separators=(",", ":"))
    path.write_text(original, encoding="utf-8")

    store = TodoStore(persist_path=path)

    assert store.list() == items
    assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    "statuses, expected_statuses",
    [
        (["pending", "pending"], ["in_progress", "pending"]),
        (["in_progress", "in_progress"], ["in_progress", "pending"]),
    ],
)
def test_persistence_migrates_legacy_active_item_count(
    tmp_path,
    statuses,
    expected_statuses,
):
    path = tmp_path / "todos.json"
    items = [
        _persisted_item(str(index), status=status)
        for index, status in enumerate(statuses, start=1)
    ]
    path.write_text(json.dumps(items), encoding="utf-8")

    store = TodoStore(persist_path=path)

    assert [item["status"] for item in store.list()] == expected_statuses
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert [item["status"] for item in persisted] == expected_statuses


@pytest.mark.parametrize(
    "items, error",
    [
        ({"id": "1"}, "must be a list"),
        ([42], "must be an object"),
        (
            [
                _persisted_item("1", status="in_progress"),
                _persisted_item("1", status="pending"),
            ],
            "Duplicate todo id",
        ),
        ([_persisted_item("abc", status="in_progress")], "invalid id"),
        ([_persisted_item("01", status="in_progress")], "invalid id"),
        ([_persisted_item("1", status="cancelled")], "Invalid status"),
        (
            [_persisted_item("1", status="in_progress", priority="urgent")],
            "Invalid priority",
        ),
        (
            [
                {
                    key: value
                    for key, value in _persisted_item(
                        "1",
                        status="in_progress",
                    ).items()
                    if key != "created_at"
                }
            ],
            "created_at",
        ),
    ],
)
def test_persistence_rejects_invalid_records(tmp_path, items, error):
    path = tmp_path / "todos.json"
    path.write_text(json.dumps(items), encoding="utf-8")

    with pytest.raises(ValueError, match=error):
        TodoStore(persist_path=path)


def test_persistence_rejects_malformed_json(tmp_path):
    path = tmp_path / "todos.json"
    path.write_text('[{"id": "1"}', encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid persisted todo state"):
        TodoStore(persist_path=path)


def test_failed_reload_keeps_previous_state_unchanged(tmp_path):
    path = tmp_path / "todos.json"
    store = TodoStore()
    store.create("Existing task")
    previous = store.list()
    path.write_text(
        json.dumps(
            [
                _persisted_item("2", status="in_progress"),
                _persisted_item("3", status="cancelled"),
            ]
        ),
        encoding="utf-8",
    )
    store._persist_path = path

    with pytest.raises(ValueError, match="Invalid status"):
        store._load()

    assert store.list() == previous


@pytest.mark.parametrize(
    "items, expected_next_id",
    [
        ([], "1"),
        (
            [
                _persisted_item("2", status="completed"),
                _persisted_item("5", status="completed"),
            ],
            "6",
        ),
    ],
)
def test_persistence_allows_state_without_unfinished_work(
    tmp_path,
    items,
    expected_next_id,
):
    path = tmp_path / "todos.json"
    path.write_text(json.dumps(items), encoding="utf-8")

    store = TodoStore(persist_path=path)
    created = store.create("New work")

    assert created["id"] == expected_next_id
    assert created["status"] == "in_progress"


# ── Schema tests ────────────────────────────────────────────


def test_anthropic_schema(writer, reader):
    schema = writer.to_schema()
    assert schema["name"] == "todo_write"
    assert "input_schema" in schema
    assert "set" in schema["input_schema"]["properties"]["action"]["enum"]
    assert "transition" in schema["input_schema"]["properties"]["action"]["enum"]
    assert "todos" in schema["input_schema"]["properties"]
    assert "id" in schema["input_schema"]["properties"]["todos"]["items"]["properties"]
    todo_items_schema = schema["input_schema"]["properties"]["todos"]["items"]
    assert todo_items_schema["required"] == ["task", "status"]
    assert "exactly one in_progress" in schema["input_schema"]["properties"]["todos"]["description"]
    assert "next_todo_id" in schema["input_schema"]["properties"]

    schema = reader.to_schema()
    assert schema["name"] == "todo_read"


def test_openai_schema(writer, reader):
    schema = writer.to_openai_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "todo_write"

    schema = reader.to_openai_schema()
    assert schema["function"]["name"] == "todo_read"


def test_todo_write_description_keeps_todo_as_progress_tracker(writer):
    description = writer.description

    assert "only a progress tracker" in description
    assert "not factual evidence" in description
    assert "not narrow the user's request" in description
    assert "current item in_progress" in description
    assert "finished items completed" in description
    assert "call plan_read before setting todos" in description
    assert "Derive the todos from plan steps in order" in description
    assert "Revise the plan with plan_write" in description
    assert "Use 'set' only" in description
    assert "Use 'transition' for normal progress" in description
    assert "Preserve the id" in description

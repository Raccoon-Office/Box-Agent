"""Test cases for Todo Tool."""

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

    result = await reader.execute()
    assert result.success
    assert "Implement feature A" in result.content


@pytest.mark.asyncio
async def test_create_with_priority(writer, reader):
    result = await writer.execute(action="create", task="Fix critical bug", priority="high")
    assert result.success

    result = await reader.execute()
    assert "[high]" in result.content


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


@pytest.mark.asyncio
async def test_read_filter_by_status(writer, reader):
    await writer.execute(action="create", task="Pending task")
    await writer.execute(action="create", task="Done task")
    await writer.execute(action="update", todo_id="2", status="completed")

    result = await reader.execute(status="completed")
    assert result.success
    assert "Done task" in result.content
    assert "Pending task" not in result.content


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


# ── Schema tests ────────────────────────────────────────────


def test_anthropic_schema(writer, reader):
    schema = writer.to_schema()
    assert schema["name"] == "todo_write"
    assert "input_schema" in schema

    schema = reader.to_schema()
    assert schema["name"] == "todo_read"


def test_openai_schema(writer, reader):
    schema = writer.to_openai_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "todo_write"

    schema = reader.to_openai_schema()
    assert schema["function"]["name"] == "todo_read"

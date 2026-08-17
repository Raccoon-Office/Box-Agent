from pathlib import Path

import pytest

from box_agent.tools.argument_limits import MAX_GENERATED_BODY_CHARS
from box_agent.tools.staged_file_write_tool import StagedFileWriteTool


@pytest.mark.asyncio
async def test_staged_write_commits_chunks_atomically(tmp_path: Path):
    target = tmp_path / "large.html"
    target.write_text("old", encoding="utf-8")
    tool = StagedFileWriteTool(workspace_dir=str(tmp_path))

    begin = await tool.execute(
        action="begin", path="large.html", expected_chunks=2
    )
    write_id = begin.raw_output["write_id"]
    first = await tool.execute(
        action="append_text", write_id=write_id, chunk_index=0, content="<html>"
    )
    second = await tool.execute(
        action="append_text", write_id=write_id, chunk_index=1, content="</html>"
    )

    assert begin.success and first.success and second.success
    assert target.read_text(encoding="utf-8") == "old"

    committed = await tool.execute(action="commit", write_id=write_id)

    assert committed.success
    assert target.read_text(encoding="utf-8") == "<html></html>"
    assert committed.raw_output["type"] == "artifact"


@pytest.mark.asyncio
async def test_staged_write_commit_can_override_begin_chunk_count(tmp_path: Path):
    tool = StagedFileWriteTool(workspace_dir=str(tmp_path))
    begin = await tool.execute(
        action="begin", path="replanned.txt", expected_chunks=2
    )
    write_id = begin.raw_output["write_id"]

    appended = await tool.execute(
        action="append_text", write_id=write_id, chunk_index=0, content="done"
    )
    committed = await tool.execute(
        action="commit", write_id=write_id, expected_chunks=1
    )

    assert begin.raw_output["expected_chunks"] == 2
    assert appended.success and committed.success
    assert (tmp_path / "replanned.txt").read_text(encoding="utf-8") == "done"


@pytest.mark.asyncio
async def test_staged_write_rejects_second_begin_for_same_target(tmp_path: Path):
    tool = StagedFileWriteTool(workspace_dir=str(tmp_path))
    first = await tool.execute(
        action="begin", path="deck.patch.json", expected_chunks=4
    )

    duplicate = await tool.execute(
        action="begin", path="./deck.patch.json", expected_chunks=5
    )

    assert first.success
    assert not duplicate.success
    assert duplicate.error.startswith("STAGED_FILE_WRITE_TARGET_ACTIVE")
    assert first.raw_output["write_id"] in duplicate.error
    assert "next_chunk_index=0" in duplicate.error
    assert duplicate.raw_output["write_id"] == first.raw_output["write_id"]
    assert len(tool._writes) == 1
    assert len(list((tmp_path / ".box-agent-staging").glob("*.part"))) == 1


@pytest.mark.asyncio
async def test_staged_write_recovers_missing_write_id_with_one_active_write(tmp_path: Path):
    tool = StagedFileWriteTool(workspace_dir=str(tmp_path))
    begin = await tool.execute(action="begin", path="recovered.txt", expected_chunks=1)

    appended = await tool.execute(action="append_text", chunk_index=0, content="done")
    committed = await tool.execute(action="commit")

    assert begin.success and appended.success and committed.success
    assert appended.raw_output["write_id"] == begin.raw_output["write_id"]
    assert (tmp_path / "recovered.txt").read_text(encoding="utf-8") == "done"


@pytest.mark.asyncio
async def test_staged_write_missing_write_id_is_ambiguous_with_multiple_active_writes(
    tmp_path: Path,
):
    tool = StagedFileWriteTool(workspace_dir=str(tmp_path))
    await tool.execute(action="begin", path="first.txt")
    await tool.execute(action="begin", path="second.txt")

    result = await tool.execute(action="append_text", chunk_index=0, content="no")

    assert not result.success
    assert result.error.startswith("STAGED_FILE_WRITE_ID_REQUIRED")
    assert "active_writes=2" in result.error


@pytest.mark.asyncio
async def test_staged_write_recovers_unknown_write_id_with_one_active_write(tmp_path: Path):
    tool = StagedFileWriteTool(workspace_dir=str(tmp_path))
    begin = await tool.execute(action="begin", path="active.txt", expected_chunks=1)

    appended = await tool.execute(
        action="append_text",
        write_id="not-active",
        chunk_index=0,
        content="recovered",
    )
    committed = await tool.execute(action="commit", write_id="still-not-active")

    assert begin.success and appended.success and committed.success
    assert appended.raw_output["write_id"] == begin.raw_output["write_id"]
    assert (tmp_path / "active.txt").read_text(encoding="utf-8") == "recovered"


@pytest.mark.asyncio
async def test_staged_write_rejects_unknown_write_id_with_multiple_active_writes(
    tmp_path: Path,
):
    tool = StagedFileWriteTool(workspace_dir=str(tmp_path))
    await tool.execute(action="begin", path="first.txt")
    await tool.execute(action="begin", path="second.txt")

    result = await tool.execute(
        action="append_text",
        write_id="not-active",
        chunk_index=0,
        content="no",
    )

    assert not result.success
    assert result.error.startswith("STAGED_FILE_WRITE_ID_UNKNOWN")


@pytest.mark.asyncio
async def test_staged_write_rejects_large_and_out_of_order_chunks(tmp_path: Path):
    tool = StagedFileWriteTool(workspace_dir=str(tmp_path))
    begin = await tool.execute(action="begin", path="large.html")
    write_id = begin.raw_output["write_id"]

    out_of_order = await tool.execute(
        action="append_text", write_id=write_id, chunk_index=1, content="x"
    )
    oversized = await tool.execute(
        action="append_text",
        write_id=write_id,
        chunk_index=0,
        content="x" * (MAX_GENERATED_BODY_CHARS + 1),
    )

    assert not out_of_order.success
    assert out_of_order.error.startswith("STAGED_FILE_CHUNK_OUT_OF_ORDER")
    assert not oversized.success
    assert oversized.error.startswith("STAGED_FILE_CHUNK_TOO_LARGE")
    assert not (tmp_path / "large.html").exists()


@pytest.mark.asyncio
async def test_staged_write_accepts_chunk_at_generated_body_limit(tmp_path: Path):
    tool = StagedFileWriteTool(workspace_dir=str(tmp_path))
    begin = await tool.execute(action="begin", path="at-limit.html", expected_chunks=1)

    appended = await tool.execute(
        action="append_text",
        write_id=begin.raw_output["write_id"],
        chunk_index=0,
        content="x" * MAX_GENERATED_BODY_CHARS,
    )
    committed = await tool.execute(action="commit", write_id=begin.raw_output["write_id"])

    assert appended.success and committed.success
    assert (tmp_path / "at-limit.html").stat().st_size == MAX_GENERATED_BODY_CHARS


@pytest.mark.asyncio
async def test_staged_write_can_append_existing_utf8_file(tmp_path: Path):
    source = tmp_path / "library.js"
    source.write_text("const qr = true;", encoding="utf-8")
    tool = StagedFileWriteTool(workspace_dir=str(tmp_path))
    begin = await tool.execute(action="begin", path="bundle.html")
    write_id = begin.raw_output["write_id"]

    result = await tool.execute(
        action="append_file", write_id=write_id, chunk_index=0, path="library.js"
    )
    committed = await tool.execute(
        action="commit", write_id=write_id, expected_chunks=1
    )

    assert result.success and committed.success
    assert (tmp_path / "bundle.html").read_text(encoding="utf-8") == "const qr = true;"


@pytest.mark.asyncio
async def test_staged_write_cleanup_discards_uncommitted_transactions(tmp_path: Path):
    tool = StagedFileWriteTool(workspace_dir=str(tmp_path))
    first = await tool.execute(action="begin", path="first.html")
    second = await tool.execute(action="begin", path="second.html")

    cleaned = tool.cleanup_pending_writes()

    assert set(cleaned) == {
        first.raw_output["write_id"],
        second.raw_output["write_id"],
    }
    assert list((tmp_path / ".box-agent-staging").glob("*.part")) == []

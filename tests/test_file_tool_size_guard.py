import json
import os
from pathlib import Path

import pytest

from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.tools.argument_limits import streamed_argument_limit
from box_agent.tools.file_tools import (
    AppendTool,
    EditTool,
    MAX_FILE_TOOL_CONTENT_CHARS,
    MAX_WRITE_FILE_BYTES,
    MAX_WRITE_FILE_CHUNKS,
    WriteTool,
)
from box_agent.tools.setup import SANDBOX_INFO_PROMPT, add_workspace_tools


def test_write_file_schema_has_no_content_size_limit_and_supports_chunks():
    content_schema = WriteTool().parameters["properties"]["content"]

    assert "maxLength" not in content_schema
    assert WriteTool().parameters["properties"]["chunk_index"]["default"] == 0
    assert WriteTool().parameters["properties"]["final"]["default"] is True
    assert streamed_argument_limit("write_file") is None


def test_append_file_schema_exposes_content_size_limit():
    content_schema = AppendTool().parameters["properties"]["content"]

    assert content_schema["maxLength"] == MAX_FILE_TOOL_CONTENT_CHARS
    assert f"{MAX_FILE_TOOL_CONTENT_CHARS:,} characters" in content_schema["description"]
    assert "multiple append calls" in content_schema["description"]


@pytest.mark.asyncio
async def test_write_file_writes_content_larger_than_the_legacy_limit(tmp_path):
    tool = WriteTool(workspace_dir=str(tmp_path))
    target = tmp_path / "output" / "large.html"
    content = "<!doctype html>\n" + ("x" * MAX_FILE_TOOL_CONTENT_CHARS)

    result = await tool.execute(path="output/large.html", content=content)

    assert result.success is True
    assert target.read_text(encoding="utf-8") == content


@pytest.mark.asyncio
async def test_write_file_chunks_leave_target_unchanged_until_final_commit(tmp_path):
    target = tmp_path / "large.html"
    target.write_text("old", encoding="utf-8")
    tool = WriteTool(workspace_dir=str(tmp_path))

    first = await tool.execute(
        path="large.html", content="<html>", chunk_index=0, final=False
    )
    assert first.success is True
    assert target.read_text(encoding="utf-8") == "old"

    second = await tool.execute(
        path="large.html", content="</html>", chunk_index=1, final=True
    )
    assert second.success is True
    assert target.read_text(encoding="utf-8") == "<html></html>"
    assert second.raw_output["chunks"] == 2


@pytest.mark.asyncio
async def test_write_file_chunks_enforce_order_and_idempotent_retries(tmp_path):
    tool = WriteTool(workspace_dir=str(tmp_path))

    first = await tool.execute(path="a.txt", content="a", chunk_index=0, final=False)
    duplicate = await tool.execute(path="a.txt", content="a", chunk_index=0, final=False)
    skipped = await tool.execute(path="a.txt", content="c", chunk_index=2, final=True)

    assert first.success is True
    assert duplicate.success is True
    assert duplicate.raw_output["duplicate"] is True
    assert duplicate.raw_output["action"] == "continue"
    assert duplicate.raw_output["next_chunk_index"] == 1
    assert json.loads(duplicate.content) == {
        "duplicate": True,
        "next_chunk_index": 1,
        "action": "continue",
    }
    assert duplicate.raw_output["transaction_state"] == "active"
    assert skipped.success is False
    assert skipped.error.startswith("WRITE_FILE_CHUNK_OUT_OF_ORDER")
    assert skipped.raw_output["transaction_state"] == "active"
    assert skipped.raw_output["next_chunk_index"] == 1
    assert not (tmp_path / "a.txt").exists()


@pytest.mark.asyncio
async def test_write_file_rejects_empty_first_nonfinal_chunk_without_transaction(tmp_path):
    tool = WriteTool(workspace_dir=str(tmp_path))

    rejected = await tool.execute(
        path="a.txt", content="", chunk_index=0, final=False
    )

    assert rejected.success is False
    assert rejected.raw_output["transaction_state"] == "none"
    assert rejected.raw_output["next_chunk_index"] == 0
    assert rejected.raw_output["restart_required"] is True
    assert not list(tmp_path.glob(".a.txt.box-agent-*.part"))
    accepted = await tool.execute(
        path="a.txt", content="first", chunk_index=0, final=False
    )
    assert accepted.raw_output["next_chunk_index"] == 1


@pytest.mark.asyncio
async def test_write_file_rejects_empty_middle_chunk_without_advancing(tmp_path):
    tool = WriteTool(workspace_dir=str(tmp_path))
    await tool.execute(path="a.txt", content="first", chunk_index=0, final=False)

    rejected = await tool.execute(
        path="a.txt", content="", chunk_index=1, final=False
    )

    assert rejected.success is False
    assert rejected.error.startswith("WRITE_FILE_EMPTY_CHUNK")
    assert rejected.raw_output["transaction_state"] == "active"
    assert rejected.raw_output["next_chunk_index"] == 1
    assert rejected.raw_output["size_bytes"] == 5
    assert next(tmp_path.glob(".a.txt.box-agent-*.part")).read_text() == "first"
    committed = await tool.execute(
        path="a.txt", content="last", chunk_index=1, final=True
    )
    assert committed.success is True
    assert (tmp_path / "a.txt").read_text() == "firstlast"


@pytest.mark.asyncio
async def test_write_file_accepts_empty_final_chunk_with_or_without_transaction(tmp_path):
    tool = WriteTool(workspace_dir=str(tmp_path))

    empty = await tool.execute(
        path="a.txt", content="", chunk_index=0, final=True
    )
    assert empty.success is True
    assert empty.raw_output["size_bytes"] == 0
    assert (tmp_path / "a.txt").read_text() == ""

    await tool.execute(path="a.txt", content="body", chunk_index=0, final=False)
    committed = await tool.execute(
        path="a.txt", content="", chunk_index=1, final=True
    )
    assert committed.success is True
    assert committed.raw_output["chunks"] == 2
    assert (tmp_path / "a.txt").read_text() == "body"

    cleared = await tool.execute(path="a.txt", content="", chunk_index=0, final=True)
    assert cleared.success is True
    assert (tmp_path / "a.txt").read_text() == ""


@pytest.mark.asyncio
async def test_write_file_conflict_discards_pending_write_and_can_restart(tmp_path):
    tool = WriteTool(workspace_dir=str(tmp_path))
    (tmp_path / "a.txt").write_text("original")
    await tool.execute(path="a.txt", content="first", chunk_index=0, final=False)

    conflict = await tool.execute(
        path="a.txt", content="changed", chunk_index=0, final=False
    )

    assert conflict.success is False
    assert conflict.raw_output["transaction_state"] == "discarded"
    assert conflict.raw_output["next_chunk_index"] == 0
    assert conflict.raw_output["restart_required"] is True
    assert conflict.raw_output["action"] == "restart"
    assert "restart from chunk_index=0" in conflict.error
    assert (tmp_path / "a.txt").read_text() == "original"
    assert not list(tmp_path.glob(".a.txt.box-agent-*.part"))

    restarted = await tool.execute(path="a.txt", content="changed")
    assert restarted.success is True
    assert (tmp_path / "a.txt").read_text() == "changed"


@pytest.mark.asyncio
async def test_write_file_final_chunk_retry_returns_committed_receipt(tmp_path):
    tool = WriteTool(workspace_dir=str(tmp_path))

    first = await tool.execute(
        path="a.txt", content="first-", chunk_index=0, final=False
    )
    committed = await tool.execute(
        path="a.txt", content="last", chunk_index=1, final=True
    )
    retried = await tool.execute(
        path="a.txt", content="last", chunk_index=1, final=True
    )

    assert first.success is True
    assert committed.success is True
    assert retried == committed
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "first-last"


@pytest.mark.asyncio
async def test_write_file_rejects_conflicting_final_chunk_retry(tmp_path):
    tool = WriteTool(workspace_dir=str(tmp_path))
    await tool.execute(path="a.txt", content="first-", chunk_index=0, final=False)
    committed = await tool.execute(
        path="a.txt", content="last", chunk_index=1, final=True
    )

    conflict = await tool.execute(
        path="a.txt", content="different", chunk_index=1, final=True
    )

    assert committed.success is True
    assert conflict.success is False
    assert conflict.error.startswith("WRITE_FILE_FINAL_CHUNK_CONFLICT")
    assert conflict.raw_output["transaction_state"] == "none"
    assert conflict.raw_output["next_chunk_index"] == 0
    assert conflict.raw_output["restart_required"] is True
    assert "restart from chunk_index=0" in conflict.error
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "first-last"


@pytest.mark.asyncio
async def test_write_file_missing_transaction_gives_restart_index(tmp_path):
    tool = WriteTool(workspace_dir=str(tmp_path))

    missing = await tool.execute(
        path="a.txt", content="continuation", chunk_index=2, final=True
    )

    assert missing.success is False
    assert missing.raw_output["transaction_state"] == "none"
    assert missing.raw_output["next_chunk_index"] == 0
    assert missing.raw_output["restart_required"] is True
    assert missing.raw_output["action"] == "restart"
    assert "restart_required=true" in missing.error


@pytest.mark.asyncio
async def test_write_file_final_retry_rejects_changed_committed_target(tmp_path):
    target = tmp_path / "a.txt"
    tool = WriteTool(workspace_dir=str(tmp_path))
    await tool.execute(path="a.txt", content="first-", chunk_index=0, final=False)
    await tool.execute(path="a.txt", content="last", chunk_index=1, final=True)
    target.write_text("changed elsewhere", encoding="utf-8")

    retry = await tool.execute(
        path="a.txt", content="last", chunk_index=1, final=True
    )

    assert retry.success is False
    assert retry.error.startswith("WRITE_FILE_COMMITTED_STATE_CHANGED")
    assert retry.raw_output["transaction_state"] == "none"
    assert target.read_text(encoding="utf-8") == "changed elsewhere"


@pytest.mark.asyncio
async def test_write_file_new_chunk_zero_replaces_prior_committed_receipt(tmp_path):
    target = tmp_path / "a.txt"
    tool = WriteTool(workspace_dir=str(tmp_path))
    first = await tool.execute(path="a.txt", content="first")

    second = await tool.execute(path="a.txt", content="second")

    assert first.success is True
    assert second.success is True
    assert target.read_text(encoding="utf-8") == "second"


@pytest.mark.asyncio
async def test_write_file_enforces_transaction_size_and_chunk_limits(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "box_agent.tools.file_tools.MAX_WRITE_FILE_BYTES", 3
    )
    monkeypatch.setattr(
        "box_agent.tools.file_tools.MAX_WRITE_FILE_CHUNKS", 2
    )
    tool = WriteTool(workspace_dir=str(tmp_path))

    accepted = await tool.execute(
        path="size.txt", content="abc", chunk_index=0, final=False
    )
    oversized = await tool.execute(
        path="size.txt", content="d", chunk_index=1, final=True
    )
    await tool.execute(path="chunks.txt", content="a", chunk_index=0, final=False)
    await tool.execute(path="chunks.txt", content="b", chunk_index=1, final=False)
    too_many = await tool.execute(
        path="chunks.txt", content="c", chunk_index=2, final=True
    )

    assert accepted.success is True
    assert oversized.error.startswith("WRITE_FILE_TOTAL_SIZE_EXCEEDED")
    assert oversized.raw_output["transaction_state"] == "active"
    assert oversized.raw_output["next_chunk_index"] == 1
    assert too_many.error.startswith("WRITE_FILE_TOO_MANY_CHUNKS")
    assert too_many.raw_output["transaction_state"] == "discarded"
    assert too_many.raw_output["reason"] == "chunk_limit_reached"
    assert MAX_WRITE_FILE_BYTES == 10 * 1024 * 1024
    assert MAX_WRITE_FILE_CHUNKS == 2_048
    assert not (tmp_path / "size.txt").exists()
    assert not (tmp_path / "chunks.txt").exists()

    restarted = await tool.execute(path="chunks.txt", content="new")

    assert restarted.success is True
    assert restarted.raw_output["transaction_state"] == "committed"
    assert (tmp_path / "chunks.txt").read_text(encoding="utf-8") == "new"


@pytest.mark.asyncio
async def test_write_file_commit_failure_reports_authoritative_active_index(
    tmp_path, monkeypatch
):
    tool = WriteTool(workspace_dir=str(tmp_path))
    real_replace = os.replace

    first = await tool.execute(
        path="a.txt", content="first-", chunk_index=0, final=False
    )

    def fail_replace(source, target):
        raise OSError("simulated replace failure")

    # Exercise the replacement fallback on a filesystem without hard links.
    monkeypatch.setattr("box_agent.tools.file_tools.os.link", fail_replace)
    monkeypatch.setattr("box_agent.tools.file_tools.os.replace", fail_replace)
    failed = await tool.execute(
        path="a.txt", content="last", chunk_index=1, final=True
    )

    assert first.success is True
    assert failed.success is False
    assert failed.error == "WRITE_FILE_FAILED: simulated replace failure"
    assert failed.raw_output["transaction_state"] == "active"
    assert failed.raw_output["next_chunk_index"] == 2

    monkeypatch.setattr("box_agent.tools.file_tools.os.replace", real_replace)
    committed = await tool.execute(
        path="a.txt", content="", chunk_index=2, final=True
    )

    assert committed.success is True
    assert committed.raw_output["transaction_state"] == "committed"
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "first-last"


@pytest.mark.asyncio
async def test_write_file_partial_append_failure_rolls_back_before_retry(
    tmp_path, monkeypatch
):
    tool = WriteTool(workspace_dir=str(tmp_path))
    first = await tool.execute(
        path="a.txt", content="abc", chunk_index=0, final=False
    )
    real_write = tool._write_bytes

    def partial_then_fail(path, data, *, append):
        with path.open("ab" if append else "wb") as stream:
            stream.write(data[:1])
            stream.flush()
        raise OSError("simulated partial append")

    monkeypatch.setattr(tool, "_write_bytes", partial_then_fail)
    failed = await tool.execute(
        path="a.txt", content="XYZ", chunk_index=1, final=False
    )

    assert first.success is True
    assert failed.success is False
    assert failed.error == "WRITE_FILE_FAILED: simulated partial append"
    assert failed.raw_output["transaction_state"] == "active"
    assert failed.raw_output["next_chunk_index"] == 1
    temporary = next(tmp_path.glob(".a.txt.box-agent-*.part"))
    assert temporary.read_bytes() == b"abc"

    monkeypatch.setattr(tool, "_write_bytes", real_write)
    retried = await tool.execute(
        path="a.txt", content="XYZ", chunk_index=1, final=True
    )

    assert retried.success is True
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "abcXYZ"


@pytest.mark.asyncio
async def test_write_file_initial_partial_append_failure_discards_transaction(
    tmp_path, monkeypatch
):
    tool = WriteTool(workspace_dir=str(tmp_path))
    real_write = tool._write_bytes

    def initial_partial_then_fail(path, data, *, append):
        if not data:
            return real_write(path, data, append=append)
        with path.open("ab" if append else "wb") as stream:
            stream.write(data[:1])
            stream.flush()
        raise OSError("simulated initial partial append")

    monkeypatch.setattr(tool, "_write_bytes", initial_partial_then_fail)
    failed = await tool.execute(path="a.txt", content="ABCDE")

    assert failed.success is False
    assert failed.raw_output["transaction_state"] == "discarded"
    assert failed.raw_output["reason"] == "initial_chunk_rejected"
    assert not list(tmp_path.glob(".a.txt.box-agent-*.part"))

    monkeypatch.setattr(tool, "_write_bytes", real_write)
    restarted = await tool.execute(path="a.txt", content="ABCDE")

    assert restarted.success is True
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "ABCDE"


@pytest.mark.asyncio
async def test_write_file_failed_partial_append_discards_when_rollback_fails(
    tmp_path, monkeypatch
):
    tool = WriteTool(workspace_dir=str(tmp_path))
    await tool.execute(path="a.txt", content="abc", chunk_index=0, final=False)
    real_write = tool._write_bytes

    def partial_then_fail(path, data, *, append):
        with path.open("ab" if append else "wb") as stream:
            stream.write(data[:1])
            stream.flush()
        raise OSError("simulated partial append")

    def fail_rollback(path, size):
        raise OSError("simulated rollback failure")

    monkeypatch.setattr(tool, "_write_bytes", partial_then_fail)
    monkeypatch.setattr(tool, "_restore_temporary_size", fail_rollback)
    failed = await tool.execute(
        path="a.txt", content="XYZ", chunk_index=1, final=True
    )

    assert failed.success is False
    assert failed.raw_output["transaction_state"] == "discarded"
    assert failed.raw_output["reason"] == "chunk_write_rollback_failed"
    assert not list(tmp_path.glob(".a.txt.box-agent-*.part"))

    monkeypatch.setattr(tool, "_write_bytes", real_write)
    restarted = await tool.execute(path="a.txt", content="fresh")

    assert restarted.success is True
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "fresh"


@pytest.mark.asyncio
async def test_write_file_discards_changed_transaction_temporary_file(tmp_path):
    tool = WriteTool(workspace_dir=str(tmp_path))
    await tool.execute(path="a.txt", content="abc", chunk_index=0, final=False)
    temporary = next(tmp_path.glob(".a.txt.box-agent-*.part"))
    temporary.write_bytes(b"tampered")

    failed = await tool.execute(
        path="a.txt", content="XYZ", chunk_index=1, final=True
    )

    assert failed.success is False
    assert failed.raw_output["transaction_state"] == "discarded"
    assert failed.raw_output["reason"] == "transaction_size_mismatch"
    assert not (tmp_path / "a.txt").exists()
    assert not temporary.exists()

    restarted = await tool.execute(path="a.txt", content="fresh")

    assert restarted.success is True
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "fresh"


@pytest.mark.asyncio
async def test_write_file_discards_transaction_with_invalid_temporary_encoding(
    tmp_path,
):
    tool = WriteTool(workspace_dir=str(tmp_path))
    await tool.execute(path="a.txt", content="ab", chunk_index=0, final=False)
    temporary = next(tmp_path.glob(".a.txt.box-agent-*.part"))
    temporary.write_bytes(b"\xff\xfe")

    failed = await tool.execute(
        path="a.txt", content="", chunk_index=1, final=True
    )

    assert failed.success is False
    assert failed.raw_output["transaction_state"] == "discarded"
    assert failed.raw_output["reason"] == "transaction_content_invalid_utf8"
    assert not (tmp_path / "a.txt").exists()
    assert not temporary.exists()

    restarted = await tool.execute(path="a.txt", content="fresh")

    assert restarted.success is True
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "fresh"


@pytest.mark.asyncio
async def test_write_file_discard_ends_transaction_when_temp_cleanup_fails(
    tmp_path, monkeypatch
):
    tool = WriteTool(workspace_dir=str(tmp_path))
    await tool.execute(path="a.txt", content="abc", chunk_index=0, final=False)
    temporary = next(tmp_path.glob(".a.txt.box-agent-*.part"))
    real_unlink = Path.unlink

    def fail_transaction_unlink(path, *args, **kwargs):
        if path == temporary:
            raise OSError("simulated cleanup failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_transaction_unlink)
    records = tool.discard_pending_writes(reason="test_cleanup")

    assert records[0]["transaction_state"] == "discarded"
    assert records[0]["cleanup_error"] == "simulated cleanup failure"

    monkeypatch.setattr(Path, "unlink", real_unlink)
    restarted = await tool.execute(path="a.txt", content="fresh")

    assert restarted.success is True
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "fresh"
    temporary.unlink()


@pytest.mark.asyncio
async def test_append_file_appends_chunks_and_rejects_oversized_content(tmp_path):
    tool = AppendTool(workspace_dir=str(tmp_path))
    target = tmp_path / "output" / "large.html"

    first = await tool.execute(path="output/large.html", content="<html>")
    second = await tool.execute(path="output/large.html", content="<body>ok</body></html>")
    oversized = await tool.execute(
        path="output/large.html",
        content="x" * (MAX_FILE_TOOL_CONTENT_CHARS + 1),
    )

    assert first.success is True
    assert second.success is True
    assert target.read_text(encoding="utf-8") == "<html><body>ok</body></html>"
    assert oversized.success is False
    assert oversized.error is not None
    assert oversized.error.startswith("FILE_TOOL_ARGUMENT_TOO_LARGE")
    assert target.read_text(encoding="utf-8") == "<html><body>ok</body></html>"


def test_workspace_file_tools_expose_only_write_file_for_transactional_writes(tmp_path):
    tools = []
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(),
        tools=ToolsConfig(enable_bash=False, enable_todo=False, enable_plan=False),
    )

    add_workspace_tools(tools, config, tmp_path)

    names = {tool.name for tool in tools}
    assert "append_file" in names
    assert "write_file" in names
    assert "staged_file_write" not in names


def test_sandbox_prompt_describes_write_file_chunk_protocol():
    assert "`write_file(path, content)`" in SANDBOX_INFO_PROMPT
    assert "chunk_index=0, final=false" in SANDBOX_INFO_PROMPT
    assert "`staged_file_write`" not in SANDBOX_INFO_PROMPT
    assert "禁止把文件正文、heredoc 或 base64 载荷塞进 `bash`" in SANDBOX_INFO_PROMPT


def test_write_file_schema_owns_chunk_protocol_without_system_prompt_duplication():
    prompt = Path("box_agent/config/system_prompt.md").read_text(encoding="utf-8")

    assert "chunk_index=0" in WriteTool().description
    assert "final=false" in WriteTool().description
    assert "chunk_index=0" not in prompt
    assert "final=false" not in prompt


@pytest.mark.asyncio
async def test_edit_file_rejects_oversized_replacement(tmp_path):
    target = tmp_path / "sample.txt"
    target.write_text("old", encoding="utf-8")
    tool = EditTool(workspace_dir=str(tmp_path))

    result = await tool.execute(
        path="sample.txt",
        old_str="old",
        new_str="x" * (MAX_FILE_TOOL_CONTENT_CHARS + 1),
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.startswith("FILE_TOOL_ARGUMENT_TOO_LARGE")
    assert target.read_text(encoding="utf-8") == "old"

"""Line pagination remains exact; oversized pages can also resume mid-line."""

import hashlib
import io
import json
import re
import tracemalloc

import pytest

from box_agent.context_resources import ContextResourceLedger
from box_agent.runtime import run_agent_loop
from box_agent.schema import FunctionCall, Message, StreamEvent, ToolCall
from box_agent.tool_result_storage import ToolResultStorage
from box_agent.tools.base import ToolResult
from box_agent.tools.file import read_tool
from box_agent.tools.file.read_tool import ReadTool


def fragment_text(content):
    return content.split("\n", 1)[1].rsplit("\n[Hint: ", 1)[0]


@pytest.mark.parametrize("block_size", [4, 7, 17, 65536])
@pytest.mark.parametrize("offset,limit", [(1, 20), (3, 2), (100, 1)])
def test_bounded_scanning_preserves_existing_line_and_hash_semantics(monkeypatch, block_size, offset, limit):
    monkeypatch.setattr(read_tool, "READ_BLOCK_BYTES", block_size)
    data = b"first\r\n\n" + "中文😀\rinside\n".encode() + b"bad\xe4\xb8!\nlast\r"
    expected = []
    for raw in io.BytesIO(data):
        text = raw.decode("utf-8", errors="replace")
        if text.endswith("\r\n"):
            text = text[:-2] + "\n"
        elif text.endswith("\r"):
            text = text[:-1] + "\n"
        expected.append(text)
    page = read_tool._scan_read_page(io.BytesIO(data), offset, limit)
    assert page.lines == expected[offset - 1:offset - 1 + limit]
    assert page.selected_chars == sum(map(len, page.lines))
    assert page.source_chars == sum(map(len, expected))
    assert page.total_lines == len(expected)
    assert page.content_version == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize("content", [
    "a" * 170_000,
    "中文😀e\u0301" * 30_000,
    "leading\r\n" + "中文" * 60_000 + "\r\nend\r",
    "short line\n" * 12_000,
], ids=["ascii-line", "unicode-line", "mixed-newlines", "ordinary-lines"])
async def test_fragments_recover_the_file_once_and_keep_unicode(tmp_path, content):
    path = tmp_path / "data.json"
    data = content.encode("utf-8")
    path.write_bytes(data)
    tool = ReadTool(str(tmp_path))
    arguments = {"path": str(path), "limit": 2000}
    parts = []
    byte_end = 0
    while True:
        result = await tool.invoke(arguments)
        assert result.success, result.error
        raw = result.raw_output
        if not raw.get("partial"):
            # The short-lines case preserves the existing 2000-line page.
            assert content.startswith("short line")
            assert raw["next_offset"] == 2001
            assert raw["selected_line_count"] == 2000
            assert raw["context_resource"]["content_version"] == hashlib.sha256(data).hexdigest()
            return
        assert raw["start_byte"] == byte_end
        assert raw["end_byte"] > byte_end
        assert "context_resource" not in raw
        assert "\ufffd" not in result.content
        parts.append(fragment_text(result.content))
        byte_end = raw["end_byte"]
        assert len(result.content) < 20_000
        if raw["next_byte_offset"] is None:
            break
        arguments = {"path": str(path), "byte_offset": raw["next_byte_offset"]}
    assert "".join(parts) == content
    assert byte_end == len(data)
    assert raw["has_more"] is False


async def test_oversized_line_page_starts_at_requested_line_and_continues_to_eof(tmp_path, monkeypatch):
    monkeypatch.setattr(read_tool, "MAX_READ_CHARS", 20)
    monkeypatch.setattr(read_tool, "READ_FRAGMENT_BYTES", 13)
    path = tmp_path / "data.txt"
    selected = "中文😀" * 20 + "\n" + "second selected line\n"
    path.write_text("skip\n" + selected + "after\n", encoding="utf-8", newline="")
    tool = ReadTool(str(tmp_path))
    result = await tool.execute(str(path), offset=2, limit=2)
    parts = []
    while True:
        assert result.success, result.error
        parts.append(fragment_text(result.content))
        position = result.raw_output["next_byte_offset"]
        if position is None:
            break
        # Byte mode wins over stale/default line parameters from the caller.
        result = await tool.execute(str(path), byte_offset=position, offset=1, limit=1)
    assert "".join(parts) == selected + "after\n"
    following = await tool.execute(str(path), offset=4, limit=1)
    assert following.content == "     4|after"


@pytest.mark.parametrize("ending", ["\n", "\r\n", ""])
async def test_split_utf8_and_crlf_are_never_lost_or_duplicated(tmp_path, monkeypatch, ending):
    monkeypatch.setattr(read_tool, "MAX_READ_CHARS", 20)
    monkeypatch.setattr(read_tool, "READ_BLOCK_BYTES", 7)
    monkeypatch.setattr(read_tool, "READ_FRAGMENT_BYTES", 7)
    path = tmp_path / "unicode.txt"
    original = (("abcd中文😀" + ending) * 10).encode()
    path.write_bytes(original)
    tool = ReadTool(str(tmp_path))
    result = await tool.execute(str(path))
    parts = []
    while True:
        assert result.success, result.error
        parts.append(fragment_text(result.content))
        position = result.raw_output["next_byte_offset"]
        if position is None:
            break
        result = await tool.execute(str(path), byte_offset=position)
    assert "".join(parts) == original.decode()


async def test_invalid_utf8_is_reported_and_byte_offsets_still_advance(tmp_path):
    path = tmp_path / "damaged.txt"
    original = (b"x" * (read_tool.READ_FRAGMENT_BYTES - 1) + b"\xe4\xb8!\xff") * 7
    path.write_bytes(original)
    tool = ReadTool(str(tmp_path))
    result = await tool.execute(str(path))
    parts = []
    while True:
        assert result.success, result.error
        parts.append(fragment_text(result.content))
        position = result.raw_output["next_byte_offset"]
        if position is None:
            break
        result = await tool.execute(str(path), byte_offset=position)
    assert "".join(parts) == original.decode("utf-8", errors="replace")
    assert result.raw_output["end_byte"] == len(original)


async def test_continuation_does_not_rescan_the_file_and_still_checks_permissions(tmp_path, monkeypatch):
    path = tmp_path / "data.txt"
    path.write_text("x" * 120_000, encoding="utf-8")
    tool = ReadTool(str(tmp_path))

    def no_scan(*args):
        raise AssertionError("continuation must seek to the saved position")

    monkeypatch.setattr(read_tool, "_scan_read_page", no_scan)
    first = await tool.execute(str(path), byte_offset=0)
    assert first.success, first.error
    result = await tool.execute(str(path), byte_offset=first.raw_output["next_byte_offset"])
    assert result.success, result.error
    monkeypatch.setattr(tool, "_validate_readable_file", lambda *args: ToolResult(success=False, error="permission denied"))
    denied = await tool.execute(str(path), byte_offset=result.raw_output["next_byte_offset"])
    assert denied.error == "permission denied"


@pytest.mark.parametrize("byte_offset", [-1, True, "0"])
async def test_invalid_byte_offset_returns_a_tool_error(tmp_path, byte_offset):
    path = tmp_path / "data.txt"
    path.write_text("text", encoding="utf-8")
    result = await ReadTool(str(tmp_path)).execute(str(path), byte_offset=byte_offset)
    assert not result.success
    assert "non-negative integer" in result.error


@pytest.mark.parametrize("data,byte_offset", [(b"", 0), (b"abc", 3), (b"abc", 10)])
async def test_byte_reads_at_or_beyond_eof_finish_without_continuation(tmp_path, data, byte_offset):
    path = tmp_path / "data.txt"
    path.write_bytes(data)
    result = await ReadTool(str(tmp_path)).execute(str(path), byte_offset=byte_offset)
    assert result.success
    assert fragment_text(result.content) == ""
    assert result.raw_output["next_byte_offset"] is None
    assert result.raw_output["has_more"] is False


async def test_incomplete_utf8_at_eof_finishes_with_a_replacement_notice(tmp_path):
    path = tmp_path / "data.txt"
    path.write_bytes(b"abc\xe4\xb8")
    result = await ReadTool(str(tmp_path)).execute(str(path), byte_offset=0)
    assert result.success
    assert fragment_text(result.content) == "abc\ufffd"
    assert result.raw_output["end_byte"] == 5
    assert result.raw_output["next_byte_offset"] is None
    assert "Non-UTF-8 bytes" in result.content


async def test_large_selected_and_skipped_lines_do_not_expand_read_buffers(tmp_path):
    path = tmp_path / "large.txt"
    with path.open("wb") as stream:
        for _ in range(2048):
            stream.write(b"x" * 8192)
        stream.write(b"\nsmall\n")
    tool = ReadTool(str(tmp_path))
    tracemalloc.start()
    try:
        first = await tool.execute(str(path), limit=1)
        second = await tool.execute(str(path), offset=2, limit=1)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert first.success and first.raw_output["next_byte_offset"]
    assert second.content == "     2|small"
    assert peak < 3 * 1024 * 1024


async def test_complete_externalized_json_can_be_recovered_through_read_file(tmp_path):
    from box_agent.tools.engine.results import ToolResultPipelineInput, process_tool_result

    raw = {"rows": [{"description": "中文" * 1000}] * 25, "status": "ok"}
    result = ToolResult(success=True, content="Query complete", raw_output=raw)
    outcome = process_tool_result(ToolResultPipelineInput(
        messages=[], tool_call_id="query-1", tool_name="mcp_query", arguments={},
        result=result, visible_content=result.content, visible_error=None,
        result_storage=ToolResultStorage(tmp_path), session_id="test",
    ))
    path = outcome.events[0].raw_output["_persisted_output"]["path"]
    tool = ReadTool(str(tmp_path))
    arguments = {"path": path, "limit": 1}
    parts = []
    while True:
        read = await tool.invoke(arguments)
        assert read.success, read.error
        parts.append(fragment_text(read.content))
        position = read.raw_output["next_byte_offset"]
        if position is None:
            break
        arguments = {"path": path, "byte_offset": position}
    assert json.loads("".join(parts)) == raw


async def test_agent_reads_every_fragment_without_suppressing_or_externalizing_it(tmp_path):
    text = "x" * 110_000 + "中文😀" * 2_000
    path = tmp_path / "result.json"
    path.write_text(text, encoding="utf-8")
    tool = ReadTool(str(tmp_path))
    ledger = ContextResourceLedger()

    class ReaderLLM:
        def __init__(self):
            self.parts = []
            self.calls = 0

        async def generate(self, **kwargs):
            raise AssertionError("No summarization is needed for this bounded fixture")

        async def generate_stream(self, messages, **kwargs):
            self.calls += 1
            arguments = {"path": str(path), "limit": 1}
            if self.calls > 1:
                message = next(m for m in reversed(messages) if m.role == "tool")
                assert message.content.startswith("[File fragment:")
                assert len(message.content) < 20_000
                self.parts.append(fragment_text(message.content))
                position = re.search(r'byte_offset=(\d+)', message.content)
                if position is None:
                    yield StreamEvent(type="text", delta="Finished reading")
                    yield StreamEvent(type="finish", finish_reason="stop")
                    return
                arguments = {"path": str(path), "byte_offset": int(position[1])}
            yield StreamEvent(type="finish", finish_reason="tool", tool_calls=[
                ToolCall(id=f"read-{self.calls}", type="function", function=FunctionCall(name="read_file", arguments=arguments)),
            ])

    llm = ReaderLLM()
    events = [event async for event in run_agent_loop(
        llm=llm, messages=[Message(role="user", content="Read this file completely")],
        tools={tool.name: tool}, max_steps=30, workspace_dir=str(tmp_path),
        tool_result_storage=ToolResultStorage(tmp_path / "outputs"), context_resource_ledger=ledger,
    )]
    assert "".join(llm.parts) == text
    assert all(ledger.source(f"read-{n}") is None for n in range(1, llm.calls))
    assert not (tmp_path / "outputs").exists()

"""Test cases for tools."""

import asyncio
import hashlib
import json
import math
import tempfile
from pathlib import Path

import pytest

from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.tools import (
    AppendTool,
    BashTool,
    EditTool,
    JsonlQueryTool,
    ReadTool,
    SearchFilesTool,
    WriteTool,
    add_workspace_tools,
)
from box_agent.tools.file_tools import MAX_SEARCH_OFFSET, MAX_SEARCH_OUTPUT_CHARS
from box_agent.tools.bash_tool import (
    _detect_dingtalk_workspace_violation,
    _detect_lark_user_mode_violation,
)
from box_agent.tools.permissions import CapabilityPolicy, PermissionEngine


def test_file_tool_package_preserves_legacy_imports():
    from box_agent.tools.file import JsonlQueryTool as PackagedJsonlQueryTool
    from box_agent.tools.file import ReadTool as PackagedReadTool
    from box_agent.tools.file_tools import JsonlQueryTool as LegacyJsonlQueryTool
    from box_agent.tools.file_tools import ReadTool as LegacyReadTool

    assert PackagedReadTool is LegacyReadTool
    assert PackagedJsonlQueryTool is LegacyJsonlQueryTool


def test_bounded_read_tools_opt_out_of_shared_result_compression(tmp_path):
    tools = (
        ReadTool(workspace_dir=str(tmp_path)),
        JsonlQueryTool(workspace_dir=str(tmp_path)),
        SearchFilesTool(workspace_dir=str(tmp_path)),
    )

    assert all(math.isinf(tool.max_result_size_chars) for tool in tools)


@pytest.mark.asyncio
async def test_read_tool():
    """Test read file tool."""
    print("\n=== Testing ReadTool ===")

    # Create a temp file
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
        f.write("Hello, World!")
        temp_path = f.name

    try:
        tool = ReadTool()
        result = await tool.execute(path=temp_path)

        assert result.success, f"Read failed: {result.error}"
        assert result.model_context is None
        # ReadTool now returns content with line numbers in format: "LINE_NUMBER|LINE_CONTENT"
        assert "Hello, World!" in result.content, f"Content mismatch: {result.content}"
        assert "|Hello, World!" in result.content, f"Expected line number format: {result.content}"
        descriptor = result.raw_output["context_resource"]
        assert descriptor == {
            "resource_id": str(Path(temp_path).resolve()),
            "content_version": hashlib.sha256(b"Hello, World!").hexdigest(),
            "start_line": 1,
            "end_line": 1,
            "total_lines": 1,
            "resource_class": "reconstructable",
        }
        assert {k: v for k, v in result.raw_output.items() if k != "context_resource"} == {
            "source_char_count": len("Hello, World!"),
            "selected_char_count": len("Hello, World!"),
            "selected_line_count": 1,
            "total_lines": 1,
            "truncated": False,
            "has_more": False,
            "next_offset": None,
        }
        print("✅ ReadTool test passed")
    finally:
        Path(temp_path).unlink()


@pytest.mark.asyncio
async def test_read_tool_keeps_large_generated_page_as_model_content(tmp_path):
    page = tmp_path / "generated.html"
    marker = "EXACT_GENERATED_PAGE_CONTENT"
    page.write_text("<html>\n" + ("<p>detail</p>\n" * 700) + marker, encoding="utf-8")

    result = await ReadTool(workspace_dir=str(tmp_path)).execute(
        path=page.name,
        limit=1_000,
    )

    assert result.success
    assert marker in result.content
    assert result.model_context is None


@pytest.mark.asyncio
async def test_read_tool_reports_selected_range_completeness(tmp_path):
    path = tmp_path / "range.txt"
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")

    result = await ReadTool(workspace_dir=str(tmp_path)).execute(
        path="range.txt",
        offset=2,
        limit=1,
    )

    assert result.success is True
    descriptor = result.raw_output["context_resource"]
    assert descriptor == {
        "resource_id": str(path.resolve()),
        "content_version": hashlib.sha256(b"one\ntwo\nthree\n").hexdigest(),
        "start_line": 2,
        "end_line": 2,
        "total_lines": 3,
        "resource_class": "reconstructable",
    }
    assert {k: v for k, v in result.raw_output.items() if k != "context_resource"} == {
        "source_char_count": len("one\ntwo\nthree\n"),
        "selected_char_count": len("two\n"),
        "selected_line_count": 1,
        "total_lines": 3,
        "truncated": False,
        "has_more": True,
        "next_offset": 3,
    }


@pytest.mark.asyncio
async def test_read_tool_defaults_to_bounded_page_with_continuation_hint(tmp_path):
    path = tmp_path / "large.txt"
    path.write_text("".join(f"line-{index}\n" for index in range(1, 601)), encoding="utf-8")

    result = await ReadTool(workspace_dir=str(tmp_path)).execute(path="large.txt")

    assert result.success is True
    assert "line-500" in result.content
    assert "line-501" not in result.content
    assert "Use offset=501, limit=500 to continue" in result.content
    assert result.raw_output["selected_line_count"] == 500
    assert result.raw_output["total_lines"] == 600
    assert result.raw_output["next_offset"] == 501


@pytest.mark.asyncio
async def test_read_tool_rejects_oversized_page_instead_of_truncating_middle(tmp_path):
    path = tmp_path / "long-line.txt"
    path.write_text("x" * 100_001, encoding="utf-8")

    result = await ReadTool(workspace_dir=str(tmp_path)).execute(path="long-line.txt")

    assert result.success is False
    assert "100,000-character safety limit" in result.error
    assert "smaller limit" in result.error
    assert "Content truncated" not in result.error


@pytest.mark.asyncio
async def test_read_tool_routes_oversized_jsonl_record_to_query_tool(tmp_path):
    path = tmp_path / "trace.jsonl"
    path.write_text(
        json.dumps({"event": "llm.request", "data": "x" * 100_001}) + "\n",
        encoding="utf-8",
    )

    result = await ReadTool(workspace_dir=str(tmp_path)).execute(path="trace.jsonl", limit=1)

    assert result.success is False
    assert "JSONL record at line 1" in result.error
    assert "Use query_jsonl with fields/where" in result.error
    assert "Retry with a smaller limit" not in result.error


@pytest.mark.asyncio
async def test_query_jsonl_summarizes_large_records_without_exposing_raw_body(tmp_path):
    marker = "RAW_BODY_MUST_NOT_BE_RETURNED"
    path = tmp_path / "trace.jsonl"
    path.write_text(
        json.dumps(
            {
                "event": "llm.request",
                "timestamp": "2026-08-15T00:00:00Z",
                "data": {"messages": [{"content": marker + ("x" * 120_000)}]},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = await JsonlQueryTool(workspace_dir=str(tmp_path)).execute(path="trace.jsonl")

    assert result.success is True
    payload = json.loads(result.content)
    assert payload["records"][0]["line"] == 1
    assert payload["records"][0]["data"]["event"] == "llm.request"
    assert payload["records"][0]["data"]["data"] == {
        "$summarized": True,
        "$type": "object",
        "size": 1,
        "keys": ["messages"],
    }
    assert payload["page"]["projection_truncated"] is True
    assert marker not in result.content
    assert result.raw_output["source_size_bytes"] > 120_000


@pytest.mark.asyncio
async def test_query_jsonl_filters_projects_and_resumes_with_cursor(tmp_path):
    path = tmp_path / "events.ndjson"
    rows = [
        {"event": "start", "data": {"value": 0}},
        {"event": "match", "data": {"value": 1}},
        {"event": "match", "data": {"value": 2}},
        {"event": "end", "data": {"value": 3}},
    ]
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    tool = JsonlQueryTool(workspace_dir=str(tmp_path))

    first = await tool.execute(
        path="events.ndjson",
        fields=["/event", "/data/value", "/missing"],
        where={"/event": "match"},
        limit=1,
    )
    first_payload = json.loads(first.content)
    second = await tool.execute(
        path="events.ndjson",
        fields=["/event", "/data/value"],
        where={"/event": "match"},
        cursor=first_payload["page"]["next_cursor"],
        limit=1,
    )
    second_payload = json.loads(second.content)

    assert first.success is True
    assert first_payload["records"] == [
        {
            "line": 2,
            "cursor": first_payload["records"][0]["cursor"],
            "data": {
                "/event": "match",
                "/data/value": 1,
                "/missing": {"$missing": True},
            },
        }
    ]
    assert first_payload["page"]["has_more"] is True
    assert second.success is True
    assert second_payload["records"][0]["line"] == 3
    assert second_payload["records"][0]["data"]["/data/value"] == 2


@pytest.mark.asyncio
async def test_query_jsonl_cursor_survives_append_and_rejects_other_file(tmp_path):
    path = tmp_path / "growing.jsonl"
    path.write_text(
        json.dumps({"event": "first"}) + "\n" + json.dumps({"event": "second"}) + "\n",
        encoding="utf-8",
    )
    tool = JsonlQueryTool(workspace_dir=str(tmp_path))
    first = await tool.execute(path="growing.jsonl", fields=["/event"], limit=1)
    cursor = json.loads(first.content)["page"]["next_cursor"]

    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"event": "third"}) + "\n")
    resumed = await tool.execute(
        path="growing.jsonl",
        fields=["/event"],
        cursor=cursor,
        limit=5,
    )

    other = tmp_path / "other.jsonl"
    other.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    wrong_file = await tool.execute(path="other.jsonl", cursor=cursor)

    assert resumed.success is True
    assert [
        record["data"]["/event"] for record in json.loads(resumed.content)["records"]
    ] == ["second", "third"]
    assert wrong_file.success is False
    assert "different or replaced file" in wrong_file.error


@pytest.mark.asyncio
async def test_query_jsonl_keeps_valid_json_when_projected_field_is_large(tmp_path):
    marker = "PROJECTED_SECRET_BODY"
    path = tmp_path / "large-field.jsonl"
    path.write_text(
        json.dumps({"event": "large", "payload": marker + ("z" * 20_000)}) + "\n",
        encoding="utf-8",
    )

    result = await JsonlQueryTool(workspace_dir=str(tmp_path)).execute(
        path="large-field.jsonl",
        fields=["/event", "/payload"],
    )

    payload = json.loads(result.content)
    projected = payload["records"][0]["data"]["/payload"]
    assert result.success is True
    assert projected["$truncated"] is True
    assert projected["$type"] == "string"
    assert projected["characters"] > 20_000
    assert marker in projected["preview"]
    assert len(result.content) < 10_000
    assert payload["page"]["truncated_fields"] == ["/payload"]


@pytest.mark.asyncio
async def test_query_jsonl_bounds_oversized_physical_record_and_continues(
    tmp_path,
    monkeypatch,
):
    from box_agent.tools.file import jsonl_tool as jsonl_tool_module

    monkeypatch.setattr(jsonl_tool_module, "MAX_JSONL_RECORD_BYTES", 100)
    path = tmp_path / "mixed.jsonl"
    path.write_text(
        json.dumps({"payload": "x" * 200})
        + "\n"
        + json.dumps({"event": "valid"})
        + "\n",
        encoding="utf-8",
    )

    result = await JsonlQueryTool(workspace_dir=str(tmp_path)).execute(path="mixed.jsonl")

    payload = json.loads(result.content)
    assert result.success is True
    assert payload["records"][0]["line"] == 2
    assert payload["records"][0]["data"]["event"] == "valid"
    assert payload["parse_errors"][0]["code"] == "RECORD_TOO_LARGE"
    assert result.raw_output["oversized_record_count"] == 1


@pytest.mark.asyncio
async def test_query_jsonl_reports_invalid_record_and_keeps_scanning(tmp_path):
    path = tmp_path / "invalid.jsonl"
    path.write_text(
        '{"event":"before"}\n{"event": invalid}\n{"event":"after"}\n',
        encoding="utf-8",
    )

    result = await JsonlQueryTool(workspace_dir=str(tmp_path)).execute(
        path="invalid.jsonl",
        fields=["/event"],
    )

    payload = json.loads(result.content)
    assert result.success is True
    assert [record["line"] for record in payload["records"]] == [1, 3]
    assert payload["parse_errors"][0]["line"] == 2
    assert payload["parse_errors"][0]["code"] == "INVALID_JSONL_RECORD"
    assert payload["parse_error_count"] == 1


@pytest.mark.asyncio
async def test_read_tool_rejects_binary_and_directory_paths(tmp_path):
    binary = tmp_path / "payload.bin"
    binary.write_bytes(b"\x00\x01\x02")
    tool = ReadTool(workspace_dir=str(tmp_path))

    binary_result = await tool.execute(path="payload.bin")
    directory_result = await tool.execute(path=".")

    assert binary_result.success is False
    assert "binary file" in binary_result.error
    assert directory_result.success is False
    assert "Use search_files" in directory_result.error


@pytest.mark.asyncio
async def test_search_files_lists_and_searches_without_bash(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("alpha\nneedle here\nomega\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("needle docs\n", encoding="utf-8")
    tool = SearchFilesTool(workspace_dir=str(tmp_path))

    files_result = await tool.execute(pattern="*.py", target="files", path=".")
    content_result = await tool.execute(
        pattern="needle",
        target="content",
        path=".",
        file_glob="*.py",
    )

    assert files_result.success is True
    assert files_result.content == "src/app.py"
    assert content_result.success is True
    assert "src/app.py:2:>needle here" in content_result.content
    assert "README.md" not in content_result.content


@pytest.mark.asyncio
async def test_search_files_not_found_suggests_existing_home_child_path(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    target = home / "Pictures" / "project-assets"
    target.mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    result = await SearchFilesTool(workspace_dir=str(workspace)).execute(
        pattern="*",
        target="files",
        path="pictures/project-assets",
    )

    assert result.success is False
    assert result.permission_request is None
    assert result.raw_output == {
        "code": "PATH_NOT_FOUND",
        "path": "pictures/project-assets",
        "path_candidates": [
            {
                "path": str(target),
                "basis": "home_child_case_insensitive_match",
                "exists": True,
            }
        ],
    }
    assert str(target) in result.error
    assert "retry search_files with this absolute path" in result.error


@pytest.mark.asyncio
async def test_search_files_not_found_uses_active_root_tail_for_home_candidate(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    target = home / "Pictures" / "project-assets"
    target.mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    unresolved = workspace / "pictures" / "project-assets"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    result = await SearchFilesTool(workspace_dir=str(workspace)).execute(
        pattern="*",
        target="files",
        path=str(unresolved),
    )

    assert result.success is False
    assert result.raw_output["path_candidates"] == [
        {
            "path": str(target),
            "basis": "home_child_case_insensitive_match",
            "exists": True,
        }
    ]


@pytest.mark.asyncio
async def test_search_files_not_found_does_not_guess_unrelated_home_child(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    (home / "Pictures" / "project-assets").mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    result = await SearchFilesTool(workspace_dir=str(workspace)).execute(
        pattern="*",
        target="files",
        path="photos/project-assets",
    )

    assert result.success is False
    assert result.raw_output == {
        "code": "PATH_NOT_FOUND",
        "path": "photos/project-assets",
        "path_candidates": [],
    }
    assert "Pictures" not in result.error


@pytest.mark.asyncio
async def test_search_files_path_candidate_does_not_bypass_permission_engine(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    target = home / "Pictures" / "project-assets"
    target.mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    engine = PermissionEngine(
        CapabilityPolicy(
            filesystem_scope="session_workspace",
            session_workspace_root=str(workspace),
        ),
        workspace,
    )
    tool = SearchFilesTool(
        workspace_dir=str(workspace),
        permission_engine=engine,
    )

    unresolved = await tool.execute(
        pattern="*",
        target="files",
        path="pictures/project-assets",
    )
    retried = await tool.execute(
        pattern="*",
        target="files",
        path=str(target),
    )

    assert unresolved.success is False
    assert unresolved.permission_request is None
    assert unresolved.raw_output == {
        "code": "PATH_NOT_FOUND",
        "path": "pictures/project-assets",
        "path_candidates": [],
    }
    assert str(target) not in unresolved.error
    assert retried.success is False
    assert retried.permission_request is not None
    assert retried.permission_request["path"] == str(target)


@pytest.mark.asyncio
async def test_search_files_path_candidate_is_returned_when_home_is_authorized(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    target = home / "Pictures" / "project-assets"
    target.mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    engine = PermissionEngine(
        CapabilityPolicy(
            filesystem_scope="user_home",
            session_workspace_root=str(workspace),
        ),
        workspace,
    )

    result = await SearchFilesTool(
        workspace_dir=str(workspace),
        permission_engine=engine,
    ).execute(
        pattern="*",
        target="files",
        path="pictures/project-assets",
    )

    assert result.success is False
    assert result.permission_request is None
    assert result.raw_output["path_candidates"] == [
        {
            "path": str(target),
            "basis": "home_child_case_insensitive_match",
            "exists": True,
        }
    ]


@pytest.mark.asyncio
async def test_search_files_expands_tilde_path(tmp_path, monkeypatch):
    home = tmp_path / "home"
    target = home / "Downloads" / "design2000"
    target.mkdir(parents=True)
    (target / "brief.txt").write_text("content", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    result = await SearchFilesTool(workspace_dir=str(workspace)).execute(
        pattern="*.txt",
        target="files",
        path="~/Downloads/design2000",
    )

    assert result.success is True
    assert result.content == "brief.txt"


@pytest.mark.asyncio
async def test_search_files_blocks_recursive_search_from_user_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Music").mkdir(parents=True)
    (home / "Music" / "song.txt").write_text("content", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    result = await SearchFilesTool(workspace_dir=str(workspace)).execute(
        pattern="*.txt",
        target="files",
        path="~",
    )

    assert result.success is False
    assert result.error.startswith("BROAD_HOME_SEARCH_BLOCKED:")
    assert "Choose a specific likely directory such as ~/Downloads or ~/Documents" in result.error
    assert result.permission_request is None


@pytest.mark.asyncio
async def test_search_files_paginates_results(tmp_path):
    for index in range(5):
        (tmp_path / f"file-{index}.txt").write_text("value", encoding="utf-8")

    result = await SearchFilesTool(workspace_dir=str(tmp_path)).execute(
        pattern="*.txt",
        target="files",
        limit=2,
    )

    assert result.success is True
    assert result.raw_output["total_matches"] is None
    assert result.raw_output["matched_through"] == 3
    assert result.raw_output["total_is_exact"] is False
    assert result.raw_output["returned_matches"] == 2
    assert result.raw_output["next_offset"] == 2
    assert "more available" in result.content
    assert "Use offset=2, limit=2 to continue" in result.content


@pytest.mark.asyncio
async def test_search_files_stops_after_page_plus_one_match(tmp_path, monkeypatch):
    for index in range(100):
        (tmp_path / f"file-{index:03d}.txt").write_text("value", encoding="utf-8")
    tool = SearchFilesTool(workspace_dir=str(tmp_path))
    checked = 0

    def count_allowed(_path):
        nonlocal checked
        checked += 1
        return True

    monkeypatch.setattr(tool, "_file_allowed", count_allowed)
    result = await tool.execute(pattern="*.txt", target="files", limit=2)

    assert result.success is True
    assert checked == 3
    assert result.raw_output["scanned_files"] == 3
    assert result.raw_output["matched_through"] == 3
    assert result.raw_output["truncated"] is True


@pytest.mark.asyncio
async def test_search_files_bounds_total_output_and_paginates_from_returned_count(tmp_path):
    for index in range(60):
        (tmp_path / f"file-{index:03d}.txt").write_text(
            f"needle {'x' * 1_990}", encoding="utf-8"
        )

    result = await SearchFilesTool(workspace_dir=str(tmp_path)).execute(
        pattern="needle",
        target="content",
    )

    assert result.success is True
    assert len(result.content) <= MAX_SEARCH_OUTPUT_CHARS
    assert result.raw_output["output_limited"] is True
    assert result.raw_output["limit_reason"] == "output_budget"
    assert result.raw_output["returned_matches"] < 50
    assert result.raw_output["next_offset"] == result.raw_output["returned_matches"]
    assert "output budget reached" in result.content
    assert (
        f"Use offset={result.raw_output['next_offset']}, limit=50 to continue"
        in result.content
    )


@pytest.mark.asyncio
async def test_search_files_rejects_offset_above_bounded_maximum(tmp_path):
    result = await SearchFilesTool(workspace_dir=str(tmp_path)).execute(
        pattern="*.txt",
        target="files",
        offset=MAX_SEARCH_OFFSET + 1,
    )

    assert result.success is False
    assert f"at most {MAX_SEARCH_OFFSET:,}" in result.error


@pytest.mark.asyncio
async def test_search_files_discards_matches_before_offset(tmp_path, monkeypatch):
    for index in range(8):
        (tmp_path / f"file-{index}.txt").write_text("value", encoding="utf-8")
    tool = SearchFilesTool(workspace_dir=str(tmp_path))
    retained_page_sizes = []
    original_search = tool._search_sync

    def observe_page(**kwargs):
        scan = original_search(**kwargs)
        retained_page_sizes.append(len(scan["selected"]))
        return scan

    monkeypatch.setattr(tool, "_search_sync", observe_page)
    result = await tool.execute(
        pattern="*.txt",
        target="files",
        offset=5,
        limit=2,
    )

    assert result.success is True
    assert retained_page_sizes == [2]
    assert "file-5.txt" in result.content
    assert "file-0.txt" not in result.content


@pytest.mark.asyncio
async def test_search_files_files_only_paginates_unique_files(tmp_path):
    (tmp_path / "a.txt").write_text("needle\nneedle\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / "c.txt").write_text("needle\n", encoding="utf-8")

    result = await SearchFilesTool(workspace_dir=str(tmp_path)).execute(
        pattern="needle",
        target="content",
        output_mode="files_only",
        limit=2,
    )

    assert result.success is True
    assert result.content.count("a.txt") == 1
    assert "b.txt" in result.content
    assert "c.txt" not in result.content
    assert result.raw_output["matched_through"] == 3
    assert result.raw_output["next_offset"] == 2


@pytest.mark.asyncio
async def test_search_files_count_mode_streams_and_paginates_file_counts(tmp_path):
    (tmp_path / "a.txt").write_text("needle\nneedle\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / "c.txt").write_text("needle\nneedle\nneedle\n", encoding="utf-8")

    result = await SearchFilesTool(workspace_dir=str(tmp_path)).execute(
        pattern="needle",
        target="content",
        output_mode="count",
        offset=1,
        limit=1,
    )

    assert result.success is True
    assert result.content.startswith("b.txt:1")
    assert "a.txt" not in result.content
    assert "c.txt" not in result.content
    assert result.raw_output["matched_through"] == 3
    assert result.raw_output["returned_matches"] == 1
    assert result.raw_output["next_offset"] == 2


@pytest.mark.asyncio
async def test_search_files_timeout_returns_bounded_partial_result(tmp_path, monkeypatch):
    for index in range(10):
        (tmp_path / f"file-{index}.txt").write_text("value", encoding="utf-8")
    tool = SearchFilesTool(
        workspace_dir=str(tmp_path),
        search_timeout_seconds=0.02,
        heartbeat_seconds=0.01,
    )

    def slow_allowed(_path):
        import time

        time.sleep(0.03)
        return True

    monkeypatch.setattr(tool, "_file_allowed", slow_allowed)
    result = await tool.execute(pattern="*.txt", target="files", limit=5)

    assert result.success is True
    assert result.raw_output["timed_out"] is True
    assert result.raw_output["limit_reason"] == "search_timeout"
    assert result.raw_output["truncated"] is True
    assert "timed out" in result.content


@pytest.mark.asyncio
async def test_search_files_hard_timeout_returns_even_if_worker_is_stuck(tmp_path, monkeypatch):
    import time

    tool = SearchFilesTool(
        workspace_dir=str(tmp_path),
        search_timeout_seconds=0.02,
        heartbeat_seconds=0.01,
    )

    def stuck_worker(**_kwargs):
        time.sleep(0.2)
        return {
            "selected": [],
            "matched_results": 0,
            "scanned_files": 0,
            "has_more": False,
            "timed_out": False,
            "cancelled": False,
            "exact_total": True,
        }

    monkeypatch.setattr(tool, "_search_sync", stuck_worker)
    started = time.monotonic()
    result = await tool.execute(pattern="*.txt", target="files")

    assert time.monotonic() - started < 0.15
    assert result.success is True
    assert result.raw_output["timed_out"] is True


@pytest.mark.asyncio
async def test_search_files_emits_heartbeat_and_stops_worker_on_cancel(tmp_path, monkeypatch):
    import threading
    import time

    tool = SearchFilesTool(
        workspace_dir=str(tmp_path),
        search_timeout_seconds=5,
        heartbeat_seconds=0.01,
    )
    worker_stopped = threading.Event()

    def wait_for_cancel(**kwargs):
        stop_event = kwargs["stop_event"]
        while not stop_event.wait(0.005):
            pass
        worker_stopped.set()
        return {
            "selected": [],
            "matched_results": 0,
            "scanned_files": 0,
            "has_more": False,
            "timed_out": False,
            "cancelled": True,
            "exact_total": False,
        }

    monkeypatch.setattr(tool, "_search_sync", wait_for_cancel)
    queue = asyncio.Queue()
    task = asyncio.create_task(
        tool.execute_with_event_context(
            event_queue=queue,
            parent_tool_call_id="search-1",
            pattern="*.txt",
            target="files",
        )
    )

    await asyncio.sleep(0.03)
    assert not queue.empty()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(worker_stopped.wait, 1.0)


@pytest.mark.asyncio
async def test_search_files_returns_permission_request_for_host(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    engine = PermissionEngine(CapabilityPolicy(), workspace)
    outside = Path.home() / "box-agent-search-permission-probe"
    tool = SearchFilesTool(workspace_dir=str(workspace), permission_engine=engine)

    result = await tool.execute(pattern="*", target="files", path=str(outside))

    assert result.success is False
    assert result.permission_request is not None
    assert result.permission_request["scope"] == "filesystem"
    assert result.permission_request["path"] == str(outside)


def test_workspace_tools_register_search_files(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test"),
        agent=AgentConfig(workspace_dir=str(tmp_path)),
        tools=ToolsConfig(
            enable_bash=False,
            enable_todo=False,
            enable_plan=False,
            enable_sub_agent=False,
            enable_skills=False,
            enable_mcp=False,
        ),
    )
    tools = []

    add_workspace_tools(
        tools,
        config,
        tmp_path,
        allow_full_access=False,
        output=lambda *_: None,
        use_output_dir=False,
    )

    tool_names = {tool.name for tool in tools}
    assert "search_files" in tool_names
    assert "query_jsonl" in tool_names
    assert "report_execution_result" in tool_names


def test_add_workspace_tools_applies_configured_bash_timeouts(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test"),
        agent=AgentConfig(workspace_dir=str(tmp_path)),
        tools=ToolsConfig(
            enable_bash=True,
            bash_default_timeout_seconds=450,
            bash_max_timeout_seconds=1800,
            enable_file_tools=False,
            enable_todo=False,
            enable_plan=False,
            enable_sub_agent=False,
            enable_skills=False,
            enable_mcp=False,
        ),
    )
    tools = []

    add_workspace_tools(
        tools,
        config,
        tmp_path,
        allow_full_access=False,
        output=lambda *_: None,
        use_output_dir=False,
    )

    bash_tool = next(tool for tool in tools if tool.name == "bash")
    timeout_schema = bash_tool.parameters["properties"]["timeout"]
    assert timeout_schema["default"] == 450
    assert timeout_schema["maximum"] == 1800


@pytest.mark.asyncio
async def test_initialize_base_tools_can_gate_mcp_until_protocol_ready(
    tmp_path,
    monkeypatch,
):
    from box_agent.tools import setup as setup_module

    started = asyncio.Event()
    loaded_paths: list[str] = []

    async def fake_load_mcp_tools_async(config_path, **kwargs):
        loaded_paths.append(config_path)
        started.set()
        return []

    monkeypatch.setattr(
        setup_module,
        "load_mcp_tools_async",
        fake_load_mcp_tools_async,
    )
    # Create a minimal MCP config file so the code path creates mcp_task.
    mcp_config = tmp_path / "mcp.json"
    mcp_config.write_text('{"mcpServers": {}}')
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(workspace_dir=str(tmp_path)),
        tools=ToolsConfig(
            enable_bash=False,
            enable_skills=False,
            enable_mcp=True,
            mcp_config_path=str(mcp_config),
        ),
    )
    gate = asyncio.Event()

    _, _, mcp_task, _ = await setup_module.initialize_base_tools(
        config,
        output=lambda *_: None,
        mcp_start_gate=gate,
    )
    assert mcp_task is not None
    await asyncio.sleep(0)
    assert started.is_set() is False

    gate.set()
    await mcp_task
    assert started.is_set() is True
    assert loaded_paths == [str(mcp_config)]
    bootstrapped = json.loads(mcp_config.read_text(encoding="utf-8"))
    assert (
        bootstrapped["mcpServers"]["mcp-server-askecho-search-infinity"]["disabled"]
        is False
    )


@pytest.mark.asyncio
async def test_write_tool():
    """Test write file tool."""
    print("\n=== Testing WriteTool ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = Path(tmpdir) / "test.txt"

        tool = WriteTool()
        result = await tool.execute(path=str(file_path), content="Test content")

        assert result.success, f"Write failed: {result.error}"
        assert file_path.exists(), "File was not created"
        assert file_path.read_text() == "Test content", "Content mismatch"
        print("✅ WriteTool test passed")


def test_write_tool_schema_names_active_relative_root(tmp_path):
    artifact_root = tmp_path / "output"
    tool = WriteTool(
        workspace_dir=str(tmp_path),
        relative_root_dir=str(artifact_root),
    )

    description = tool.parameters["properties"]["path"]["description"]

    assert "Prefer a path relative to the active project/artifact root" in description
    assert str(artifact_root) in description
    assert "Absolute paths are used exactly as supplied" in description





@pytest.mark.asyncio
async def test_write_tool_rejects_model_history_placeholder():
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = Path(tmpdir) / "deck.html"
        tool = WriteTool()
        result = await tool.execute(
            path=str(file_path),
            content=(
                "[Full tool-call argument omitted from model history]\n"
                "Tool: write_file\n"
                "Argument: content\n"
                "Path: output/deck.html"
            ),
        )

        assert not result.success
        assert "model-history placeholder" in result.error
        assert result.raw_output["transaction_state"] == "discarded"
        assert result.raw_output["reason"] == "model_history_placeholder"
        assert not file_path.exists()


@pytest.mark.asyncio
async def test_edit_tool():
    """Test edit file tool."""
    print("\n=== Testing EditTool ===")

    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
        f.write("Hello, World!")
        temp_path = f.name

    try:
        tool = EditTool()
        result = await tool.execute(
            path=temp_path, old_str="World", new_str="Agent"
        )

        assert result.success, f"Edit failed: {result.error}"
        content = Path(temp_path).read_text()
        assert content == "Hello, Agent!", f"Content mismatch: {content}"
        print("✅ EditTool test passed")
    finally:
        Path(temp_path).unlink()


@pytest.mark.asyncio
async def test_edit_tool_rejects_model_history_placeholder():
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".html") as f:
        f.write("<html><body>real</body></html>")
        temp_path = f.name

    try:
        tool = EditTool()
        result = await tool.execute(
            path=temp_path,
            old_str="real",
            new_str=(
                "[Full tool-call argument omitted from model history]\n"
                "Tool: edit_file\n"
                "Argument: new_str\n"
                f"Path: {temp_path}"
            ),
        )

        assert not result.success
        assert "model-history placeholder" in result.error
        assert Path(temp_path).read_text() == "<html><body>real</body></html>"
    finally:
        Path(temp_path).unlink()



@pytest.mark.asyncio
async def test_bash_tool():
    """Test bash command tool."""
    print("\n=== Testing BashTool ===")

    tool = BashTool()

    # Test successful command
    result = await tool.execute(command="echo 'Hello from bash'")
    assert result.success, f"Bash failed: {result.error}"
    assert "Hello from bash" in result.content, f"Output mismatch: {result.content}"
    print("✅ BashTool test passed")

    # Test failed command
    result = await tool.execute(command="exit 1")
    assert not result.success, "Command should have failed"
    print("✅ BashTool error handling test passed")


@pytest.mark.asyncio
async def test_bash_tool_blocks_lark_bot_identity_commands():
    tool = BashTool()

    for command in [
        'lark-cli config bind --identity bot-only',
        '$BOX_AGENT_LARK_CLI config bind --identity bot-only',
        'lark-cli config strict-mode bot',
        'lark-cli base +table-list --base-token abc --as bot',
    ]:
        result = await tool.execute(command=command)
        assert not result.success
        assert "Blocked:" in (result.error or "")


@pytest.mark.asyncio
async def test_bash_tool_requires_lark_business_commands_to_use_user_identity():
    tool = BashTool()

    result = await tool.execute(command='lark-cli base +table-list --base-token abc --format json')

    assert not result.success
    assert "must pass `--as user`" in (result.error or "")


@pytest.mark.parametrize(
    "command",
    [
        "lark-cli skills",
        "lark-cli skills list",
        "lark-cli skills read lark-base",
        "$BOX_AGENT_LARK_CLI skills read lark-base",
        "${BOX_AGENT_LARK_CLI} skills list",
        "%BOX_AGENT_LARK_CLI% skills read lark-base",
    ],
)
def test_lark_user_mode_policy_allows_local_embedded_skill_reads(command):
    assert _detect_lark_user_mode_violation(command) is None


def test_lark_user_mode_policy_does_not_exempt_other_skill_commands():
    error = _detect_lark_user_mode_violation("lark-cli skills install lark-base")

    assert error is not None
    assert "must pass `--as user`" in error


@pytest.mark.parametrize(
    "command",
    [
        "lark-cli auth login --recommend",
        r'"D:\Soft\xiaohuanxiong-ai\raccoon-ai\resources\cli-bundle\node_modules\.bin\lark-cli.cmd" auth login',
        r"'C:\Program Files\Raccoon\lark-cli.exe' auth login --no-wait --json",
        "$BOX_AGENT_LARK_CLI auth status",
    ],
)
def test_lark_user_mode_policy_allows_oauth_with_quoted_executable_paths(command):
    assert _detect_lark_user_mode_violation(command) is None


def test_lark_user_mode_policy_still_blocks_business_commands_with_quoted_executable_paths():
    error = _detect_lark_user_mode_violation(
        r'"D:\Soft\Raccoon\lark-cli.cmd" docs +fetch --doc abc'
    )

    assert error is not None
    assert "must pass `--as user`" in error


@pytest.mark.asyncio
async def test_bash_tool_allows_setting_lark_cli_env_without_invoking_cli():
    tool = BashTool()

    result = await tool.execute(command='export BOX_AGENT_LARK_CLI=/tmp/lark-cli')

    assert result.success


@pytest.mark.asyncio
async def test_bash_tool_blocks_dingtalk_dws_control_plane_and_out_of_scope_commands():
    tool = BashTool()

    for command in [
        'dws auth login',
        '$BOX_AGENT_DINGTALK_CLI auth reset',
        'dws profile switch another',
        'dws plugin install arbitrary',
        'dws drive delete --node abc',
        'dws api request /v1.0/anything',
    ]:
        result = await tool.execute(command=command)
        assert not result.success
        assert "Blocked:" in (result.error or "")


def test_dingtalk_dws_policy_allows_v1_read_and_document_write_commands():
    for command in [
        'dws auth status',
        'dws doc search --query "周报"',
        'dws doc read --node doc_1',
        'dws wiki space list',
        'dws wiki node list --space-id space_1',
        'dws drive list-spaces --space-type orgSpace',
        'dws drive download --node file_1 --output /tmp/file_1',
        'dws drive upload --file /tmp/report.md --folder folder_1',
        'dws drive mkdir --name 项目资料 --folder folder_1',
        'dws doc create --name 周报 --content-file /tmp/report.md',
        'dws doc update --node doc_1 --content-file /tmp/report.md',
    ]:
        assert _detect_dingtalk_workspace_violation(command) is None


def test_dingtalk_dws_policy_allows_officev3_bundled_absolute_binary_path():
    assert _detect_dingtalk_workspace_violation(
        '/Applications/办公小浣熊.app/Contents/Resources/cli-bundle/dws doc read --node doc_1'
    ) is None


@pytest.mark.parametrize(
    "command",
    [
        "dws auth login -v",
        "dws drive delete --node abc -v",
        "dws drive upload-info --file-name report.md --file-size 1",
        "dws drive commit --upload-id upload_1 --file-name report.md --file-size 1",
        "dws doc read --node doc_1 & dws auth login",
        "dws doc read --node x;dws auth login",
        "dws doc read --node x&dws auth login",
        "dws doc read --node x&&dws auth login",
        "dws doc read --node x\ndws auth login",
        "export BOX_AGENT_DINGTALK_CLI=/opt/officev3/dws; dws auth login",
        r"d\ws auth login",
        'dws doc read --node "$(dws auth login)"',
        "bash -c 'dws auth status'",
        "bash -c \"bash -c \\\"bash -c 'dws auth status'\\\"\"",
        "(dws auth status)",
        "{ dws auth status; }",
        "dws 'unterminated",
        "dws doc read --node doc_1 --profile another",
        "dws doc read --node doc_1 --client-id another",
        "dws doc read --node doc_1 --client-secret secret",
        "DWS_PROFILE=another dws doc read --node doc_1",
        "PATH=/tmp dws doc read --node doc_1",
        "env -u HOME dws auth login",
        "sudo -u root dws auth login",
    ],
)
def test_dingtalk_dws_policy_blocks_control_plane_bypasses(command: str):
    assert _detect_dingtalk_workspace_violation(command) is not None


@pytest.mark.parametrize(
    "command",
    [
        "rg dws box_agent tests",
        "echo dws",
        "cat /tmp/dws.txt",
        "command -v dws",
        "which dws",
    ],
)
def test_dingtalk_dws_policy_allows_commands_that_only_mention_dws(command: str):
    assert _detect_dingtalk_workspace_violation(command) is None


@pytest.mark.parametrize(
    "command",
    [
        "echo $((dws + 1))",
        "(( dws = 1 ))",
        'value="$(cat <<EOF\ndws auth login\nEOF\n)"',
        'value="$(echo ok # dws auth login\n)"',
    ],
)
def test_dingtalk_dws_policy_allows_arithmetic_variable_names(command: str):
    assert _detect_dingtalk_workspace_violation(command) is None


@pytest.mark.parametrize(
    "command",
    [
        "python3 -c \"print('<<EOF')\"\ndws auth login",
        'python3 -c "x=1\nprint(x << 2)\n"\ndws auth login',
        "echo ok # <<EOF\ndws auth login",
        "echo $(( $(dws auth login) + 1 ))",
        "$(echo dws) auth login",
        "$(printf d)ws auth login",
        "d$(printf ws) auth login",
        "$(printf d)$(printf ws) auth login",
        r"$(printf '\144\167\163') auth login",
    ],
)
def test_dingtalk_dws_policy_checks_commands_near_shell_data(command: str):
    assert _detect_dingtalk_workspace_violation(command) is not None


@pytest.mark.parametrize(
    "command",
    [
        "dws_var=dws; $dws_var auth login",
        "D=d\"w\"s; $D auth login",
        "ref=cmd; cmd=dws; ${!ref} auth login",
        "payload='dws auth login'; bash -c \"$payload\"",
        "bash --norc -c 'dws auth login'",
        "bash --rcfile /dev/null -c 'dws auth login'",
        "eval 'dws auth login'",
        "env -S 'dws auth login'",
        "env --split-string='dws auth login'",
        "printf 'auth\\n' | xargs dws auth login",
        "find . -exec dws auth login {} +",
    ],
)
def test_dingtalk_dws_policy_blocks_parameterized_and_dispatched_calls(
    command: str,
):
    assert _detect_dingtalk_workspace_violation(command) is not None


@pytest.mark.parametrize(
    "command",
    [
        "$(printf git) status",
        "$(printf git) dws.txt",
        "$(printf git) --msg dws",
        "$(printf echo) ok",
        "$(printf ls) -la",
    ],
)
def test_dingtalk_dws_policy_ignores_proven_unrelated_dynamic_commands(
    command: str,
):
    assert _detect_dingtalk_workspace_violation(command) is None


@pytest.mark.parametrize(
    "command",
    [
        """python3 << 'EOF'\n# Let's inspect the values.\nprint('ok')\nEOF""",
        """python3 << 'EOF'\n# Filter values near the Q1'24 x-axis label.\nprint('ok')\nEOF""",
    ],
)
def test_dingtalk_dws_policy_ignores_non_dws_heredoc_bodies(command: str):
    assert _detect_dingtalk_workspace_violation(command) is None


def test_dingtalk_dws_policy_allows_quoted_windows_binary_path(monkeypatch):
    monkeypatch.setattr("box_agent.tools.bash_tool.platform.system", lambda: "Windows")

    assert _detect_dingtalk_workspace_violation(
        r'"C:\Program Files\Office\dws.exe" doc read --node doc_1'
    ) is None


@pytest.mark.asyncio
async def test_bash_tool_blocks_direct_obsidian_write_commands():
    tool = BashTool()

    for command in [
        "obsidian create path=t.md content=hi",
        "/usr/local/bin/obsidian append path=t.md content=hi",
        "$BOX_AGENT_OBSIDIAN_CLI open path=t.md",
        "obsidian daily:append content=hi",
    ]:
        result = await tool.execute(command=command)
        assert not result.success
        assert "obsidian_create_note" in (result.error or "")


@pytest.mark.asyncio
async def test_bash_tool_allows_obsidian_diagnostics():
    tool = BashTool()

    for command in [
        "which obsidian",
        "obsidian help",
        "obsidian version",
    ]:
        result = await tool.execute(command=command, timeout=1)
        # The command may fail when Obsidian CLI is not installed; the point is
        # that BashTool itself must not block diagnostics with the native-tool policy.
        assert "Blocked:" not in (result.error or "")


def test_add_workspace_tools_registers_obsidian_tools(tmp_path: Path):
    tools = []

    add_workspace_tools(
        tools,
        Config(
            llm=LLMConfig(api_key="test-key"),
            agent=AgentConfig(workspace_dir=str(tmp_path)),
            tools=ToolsConfig(enable_mcp=False),
        ),
        tmp_path,
        allow_full_access=False,
        output=lambda *_: None,
        llm=None,
    )

    names = {tool.name for tool in tools}
    assert "obsidian_create_note" in names
    assert "obsidian_update_note" in names
    assert "obsidian_daily_note" in names


async def main():
    """Run all tool tests."""
    print("=" * 80)
    print("Running Tool Tests")
    print("=" * 80)

    await test_read_tool()
    await test_write_tool()
    await test_edit_tool()
    await test_bash_tool()

    print("\n" + "=" * 80)
    print("All tool tests passed! ✅")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())

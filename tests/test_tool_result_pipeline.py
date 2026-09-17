"""Contracts for shared tool-result post-processing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from box_agent.events import ArtifactEvent, DoneEvent, ToolCallResult
from box_agent.kernel.tool_result_pipeline import (
    ToolResultPipelineInput,
    process_tool_result,
)
from box_agent.runtime import run_agent_loop
from box_agent.schema import FunctionCall, LLMResponse, Message, StreamEvent, ToolCall
from box_agent.tool_result_storage import ToolResultStorage
from box_agent.tools.base import Tool, ToolResult
from box_agent.tools.engine import artifact_results


class _OneToolCallLLM:
    def __init__(self, tool_name: str) -> None:
        self._tool_name = tool_name
        self._calls = 0

    async def generate(self, **_kwargs):
        raise AssertionError("this fixture must not need context summarization")

    async def generate_stream(self, messages, tools=None, **_):
        self._calls += 1
        if self._calls == 1:
            yield StreamEvent(
                type="finish",
                finish_reason="tool",
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        type="function",
                        function=FunctionCall(name=self._tool_name, arguments={}),
                    )
                ],
            )
            return
        yield StreamEvent(type="text", delta="done")
        yield StreamEvent(type="finish", finish_reason="stop")


class _EchoTool(Tool):
    def __init__(self, *, parallel_safe: bool) -> None:
        self.parallel_safe = parallel_safe

    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "Return one deterministic result."

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self) -> ToolResult:
        return ToolResult(success=True, content="echoed")


@pytest.mark.asyncio
@pytest.mark.parametrize("parallel_safe", [False, True])
async def test_declared_delivery_scope_guards_raw_results_and_discovery(tmp_path, parallel_safe):
    from box_agent.artifact_publication import write_metadata

    (tmp_path / ".artifact-delivery.json").write_text('{"schema_version":1,"default":"intermediate"}')

    class RenderTool(_EchoTool):
        async def execute(self):
            for name in ("hero.png", "slide.png", "asset-contact.png", "deck.html", "deck.pptx", "overview.png"):
                (tmp_path / name).write_bytes(name.encode())
            for name in ("deck.html", "deck.pptx", "overview.png"):
                write_metadata(tmp_path / name, {"type": "artifact"})
            return ToolResult(success=True, content="[hero.png] [asset-contact.png] [deck.html]",
                              raw_output={"type": "artifact", "path": str(tmp_path / "hero.png"), "kind": "image"})

    tool = RenderTool(parallel_safe=parallel_safe)
    events = [event async for event in run_agent_loop(
        llm=_OneToolCallLLM(tool.name), messages=[Message(role="user", content="Make a deck")],
        tools={tool.name: tool}, max_steps=2, workspace_dir=str(tmp_path))]
    results = [event for event in events if isinstance(event, ToolCallResult)]
    assert results[0].raw_output["type"] == "intermediate_asset"
    assert {event.filename for event in events if isinstance(event, ArtifactEvent)} == {"deck.html", "deck.pptx", "overview.png"}
    assert (tmp_path / "hero.png").is_file()
    # This path bypassed the diff due to its structured result; it still needs
    # durable intermediate identity when moved outside the scoped directory.
    assert (tmp_path / ".hero.png.artifact.json").is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize("parallel_safe", [False, True], ids=["serial", "parallel"])
@pytest.mark.parametrize("success", [False, True], ids=["partial-failure", "success"])
async def test_render_images_stay_on_disk_but_only_overview_is_published(
    tmp_path, parallel_safe, success,
) -> None:
    class RenderTool(_EchoTool):
        async def execute(self) -> ToolResult:
            for name in ("slide-01.png", "contact-sheet-review-01.png", "deck-overview.png"):
                target = tmp_path / name
                if name != "deck-overview.png":
                    target.with_name(f".{name}.artifact.json").write_text(
                        '{"type":"intermediate_asset"}', encoding="utf-8",
                    )
                target.write_bytes(b"whole deck overview" if name == "deck-overview.png" else b"rendered image")
            # Explicit text references and workspace-diff discovery must agree.
            return ToolResult(success=success, content=(
                "[slide-01.png] [contact-sheet-review-01.png] [deck-overview.png]"
            ), error=None if success else "Optional QA failed after rendering")

    tool = RenderTool(parallel_safe=parallel_safe)
    events = [event async for event in run_agent_loop(
        llm=_OneToolCallLLM(tool.name), messages=[Message(role="user", content="Make a deck")],
        tools={tool.name: tool}, max_steps=2, workspace_dir=str(tmp_path),
    )]
    # Preserve the existing serial-failure behavior (no artifact publication).
    expected = {"deck-overview.png"} if success or parallel_safe else set()
    assert {event.filename for event in events if isinstance(event, ArtifactEvent)} == expected
    assert (tmp_path / "slide-01.png").read_bytes() == b"rendered image"


@pytest.mark.parametrize("metadata", [None, "invalid", "[]", '{"type":"artifact"}', "x" * 4097])
def test_standalone_image_names_are_not_hidden_without_intermediate_metadata(tmp_path, metadata):
    image = tmp_path / "slide-01.png"
    image.write_bytes(b"user requested image")
    if metadata is not None:
        image.with_name(f".{image.name}.artifact.json").write_text(metadata)
    events = artifact_results._detect_tool_artifacts(
        "call", "bash", "[slide-01.png]", None, {},
        artifact_results._snapshot_workspace_signatures(str(tmp_path)), str(tmp_path),
    )
    assert [event.filename for event in events] == [image.name]


def test_modified_intermediate_image_is_not_republished_by_later_tools(tmp_path):
    image = tmp_path / "任意名称.png"
    image.write_bytes(b"first")
    image.with_name(f".{image.name}.artifact.json").write_text('{"type":"intermediate_asset"}')
    before = artifact_results._snapshot_workspace_signatures(str(tmp_path))
    image.write_bytes(b"corrected render")
    events = artifact_results._detect_tool_artifacts(
        "later-call", "bash", "[任意名称.png]", None, before,
        artifact_results._snapshot_workspace_signatures(str(tmp_path)), str(tmp_path),
    )
    assert events == []


def test_pipeline_appends_tool_message_before_returning_result_events(tmp_path) -> None:
    messages = [Message(role="user", content="run it")]

    outcome = process_tool_result(
        ToolResultPipelineInput(
            messages=messages,
            tool_call_id="call-1",
            tool_name="echo",
            arguments={},
            result=ToolResult(success=True, content="echoed"),
            visible_content="echoed",
            visible_error=None,
            result_storage=ToolResultStorage(tmp_path),
        )
    )

    assert messages[-1].role == "tool"
    assert messages[-1].tool_call_id == "call-1"
    assert outcome.events[0].tool_call_id == "call-1"


def test_pipeline_returns_web_search_deltas_against_shared_seen_state(tmp_path) -> None:
    messages = [Message(role="user", content="search")]
    seen_result_keys: set[str] = set()
    storage = ToolResultStorage(tmp_path)
    content = json.dumps(
        {"refs": [{"title": "Primary result", "url": "https://example.com/page"}]}
    )

    first = process_tool_result(
        ToolResultPipelineInput(
            messages=messages,
            tool_call_id="search-1",
            tool_name="web_search",
            arguments={"query": "primary result"},
            result=ToolResult(success=True, content=content),
            visible_content=content,
            visible_error=None,
            result_storage=storage,
            web_search_seen_result_keys=seen_result_keys,
        )
    )
    second = process_tool_result(
        ToolResultPipelineInput(
            messages=messages,
            tool_call_id="search-2",
            tool_name="web_search",
            arguments={"query": "primary result"},
            result=ToolResult(success=True, content=content),
            visible_content=content,
            visible_error=None,
            result_storage=storage,
            web_search_seen_result_keys=seen_result_keys,
        )
    )

    assert first.web_search_new_results == 1
    assert second.web_search_new_results == 0
    assert second.web_search_duplicate_results == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("parallel_safe", [False, True], ids=["serial", "parallel"])
async def test_agent_loop_has_matching_tool_message_when_result_is_yielded(
    parallel_safe: bool,
) -> None:
    messages = [Message(role="user", content="run it")]
    tool = _EchoTool(parallel_safe=parallel_safe)
    stream = run_agent_loop(
        llm=_OneToolCallLLM(tool.name),
        messages=messages,
        tools={tool.name: tool},
        max_steps=2,
        artifact_detection_enabled=False,
    )

    try:
        async for event in stream:
            if not isinstance(event, ToolCallResult):
                continue
            assert any(
                message.role == "tool" and message.tool_call_id == event.tool_call_id
                for message in messages
            )
            break
    finally:
        await stream.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("parallel_safe", [False, True], ids=["serial", "parallel"])
@pytest.mark.parametrize("limit", ["file_count", "timeout"])
@pytest.mark.parametrize("explicit_reference", [False, True])
async def test_incomplete_pre_scan_does_not_publish_unchanged_workspace_files(
    tmp_path, monkeypatch, caplog, parallel_safe, limit, explicit_reference,
) -> None:
    for name in ("existing-a.txt", "existing-b.txt", "to-delete.txt"):
        (tmp_path / name).write_text("existing content", encoding="utf-8")
    if limit == "file_count":
        monkeypatch.setenv("BOX_AGENT_ARTIFACT_SCAN_MAX_FILES", "2")
    else:
        monkeypatch.setenv("BOX_AGENT_ARTIFACT_SCAN_TIMEOUT_SECONDS", "2")
        # The first scan expires; all following scans complete within budget.
        ticks = iter((0.0, 3.0))
        monkeypatch.setattr(
            artifact_results, "perf_counter", lambda: next(ticks, 3.0),
        )

    class DeleteTool(_EchoTool):
        async def execute(self) -> ToolResult:
            (tmp_path / "to-delete.txt").unlink()
            return ToolResult(
                success=True,
                content="See [existing-a.txt]" if explicit_reference else "deleted",
            )

    tool = DeleteTool(parallel_safe=parallel_safe)
    events = [
        event async for event in run_agent_loop(
            llm=_OneToolCallLLM(tool.name),
            messages=[Message(role="user", content="Delete the temporary file.")],
            tools={tool.name: tool},
            max_steps=2,
            workspace_dir=str(tmp_path),
        )
    ]

    assert [event.filename for event in events if isinstance(event, ArtifactEvent)] == (
        ["existing-a.txt"] if explicit_reference else []
    )
    assert any(isinstance(event, DoneEvent) for event in events)
    assert "Artifact scan skipped" in caplog.text


@pytest.mark.parametrize("failure", ["walk", "stat"])
def test_scan_access_error_does_not_publish_files_after_access_recovers(
    tmp_path, monkeypatch, failure,
) -> None:
    (tmp_path / "visible.txt").write_text("existing", encoding="utf-8")
    protected = tmp_path / "protected"
    protected.mkdir()
    existing = protected / "existing.txt"
    existing.write_text("existing", encoding="utf-8")
    with monkeypatch.context() as patch:
        if failure == "walk":
            scandir = artifact_results.os.scandir

            def denied_scandir(path):
                if Path(path) == protected:
                    raise PermissionError("directory temporarily inaccessible")
                return scandir(path)

            patch.setattr(artifact_results.os, "scandir", denied_scandir)
        else:
            stat = Path.stat

            def denied_stat(path, *args, **kwargs):
                if path == existing:
                    raise PermissionError("file temporarily inaccessible")
                return stat(path, *args, **kwargs)

            patch.setattr(Path, "stat", denied_stat)
        before = artifact_results._snapshot_workspace_signatures(str(tmp_path))

    after = artifact_results._snapshot_workspace_signatures(str(tmp_path))
    artifacts = artifact_results._detect_tool_artifacts(
        "call-1", "echo", "done", None, before, after, str(tmp_path),
    )

    assert before is None
    assert artifacts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("parallel_safe", [False, True], ids=["serial", "parallel"])
async def test_incomplete_post_scan_still_publishes_explicit_tool_files(
    tmp_path, monkeypatch, parallel_safe,
) -> None:
    monkeypatch.setenv("BOX_AGENT_ARTIFACT_SCAN_MAX_FILES", "1")
    (tmp_path / "existing.txt").write_text("existing content", encoding="utf-8")

    class WriteTool(_EchoTool):
        async def execute(self) -> ToolResult:
            (tmp_path / "report.txt").write_text("report", encoding="utf-8")
            return ToolResult(success=True, content="Saved [report.txt]")

    tool = WriteTool(parallel_safe=parallel_safe)
    events = [
        event async for event in run_agent_loop(
            llm=_OneToolCallLLM(tool.name),
            messages=[Message(role="user", content="Write the report.")],
            tools={tool.name: tool},
            max_steps=2,
            workspace_dir=str(tmp_path),
        )
    ]

    assert [event.filename for event in events if isinstance(event, ArtifactEvent)] == [
        "report.txt",
    ]
    assert any(isinstance(event, DoneEvent) for event in events)

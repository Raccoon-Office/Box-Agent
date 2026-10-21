"""Publication parity across the shared engine, CLI, and ACP."""
import pytest

from box_agent.artifact_publication import metadata_path, read_metadata, write_metadata
from box_agent.tools.publish_artifact_tool import PublishArtifactTool
from box_agent.tools.engine.artifact_results import _detect_tool_artifacts
from box_agent.events import ArtifactEvent
from box_agent.cli_renderer import CliRenderer
from box_agent.acp import _artifact_envelope


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["commit", "abort"])
async def test_transaction_inputs_never_become_artifacts_or_registry_entries(tmp_path, action):
    from box_agent.artifact_publication import apply_delivery_policy
    from box_agent.task_context import TaskContext
    from box_agent.task_registry import register_artifact_revision
    from box_agent.tools.file_tools import WriteTool
    from box_agent.tools.engine.artifact_results import _snapshot_workspace

    publisher = PublishArtifactTool(tmp_path)
    started = await publisher.execute(path="result.md", action="begin")
    assert started.success
    staging = started.raw_output["staging_path"]
    written = await WriteTool(str(tmp_path)).execute(staging, "# Final result\n")
    assert written.success
    raw = apply_delivery_policy(written.raw_output, str(tmp_path))
    assert raw["type"] == "intermediate_asset"
    # Neither exact structured paths, explicit text references, nor workspace
    # discovery can surface transaction inputs, even with a publication sidecar.
    from pathlib import Path
    staged_path = Path(staging)
    write_metadata(staged_path, {"type": "artifact"})
    relative = staged_path.relative_to(tmp_path).as_posix()
    assert staged_path not in _snapshot_workspace(str(tmp_path))
    assert _detect_tool_artifacts("write", "write_file", f"[{relative}]", raw,
                                  {}, {}, str(tmp_path)) == []
    assert _detect_tool_artifacts("shell", "bash", f"[{relative}]", None,
                                  {}, {}, str(tmp_path)) == []
    ended = await publisher.execute(action=action, write_id=started.raw_output["write_id"])
    assert ended.success
    assert not staged_path.exists()
    events = _detect_tool_artifacts("publish", "publish_artifact", ended.content,
                                   ended.raw_output, {}, {}, str(tmp_path))
    assert [e.rel_path for e in events] == (["result.md"] if action == "commit" else [])
    for event in events:
        assert event.placement == "primary"
        register_artifact_revision(tmp_path, TaskContext(
            session_id="s", task_id="task", turn_id="turn",
        ), event)
    registry = tmp_path / ".box-agent/task-registry/tasks/task.json"
    if action == "commit":
        import json
        artifacts = json.loads(registry.read_text())["artifacts"]
        assert [a["rel_path"] for a in artifacts] == ["result.md"]
        assert artifacts[0]["revisions"][0]["turn_id"] == "turn"
    else:
        assert not registry.exists()


@pytest.mark.asyncio
async def test_promoted_intermediate_reaches_cli_and_acp(tmp_path, capsys):
    file = tmp_path / "review.png"
    file.write_bytes(b"image")
    (tmp_path / ".artifact-delivery.json").write_text('{"schema_version":1,"default":"intermediate"}')
    write_metadata(file, {"type": "intermediate_asset", "description": "Requested image"})
    result = await PublishArtifactTool(tmp_path).execute("review.png")
    assert result.success
    events = _detect_tool_artifacts("publish-1", "publish_artifact", result.content,
                                   result.raw_output, {}, {}, str(tmp_path))
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, ArtifactEvent)
    assert event.description == "Requested image"
    CliRenderer().render(event)
    assert "review.png" in capsys.readouterr().out
    assert _artifact_envelope(event)["rel_path"] == "review.png"
    assert file.read_bytes() == b"image"
    assert read_metadata(metadata_path(file))["type"] == "artifact"
    # File-owned publication survives a fresh tool instance and later discovery.
    later = _detect_tool_artifacts("later", "bash", "[review.png]", None,
                                  {}, {}, str(tmp_path))
    assert [e.rel_path for e in later] == ["review.png"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["missing.txt", "../outside.txt", "escape.txt"])
async def test_publication_rejects_missing_and_escaped_paths(tmp_path, path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    (workspace / "escape.txt").symlink_to(outside)
    result = await PublishArtifactTool(workspace).execute(path)
    assert not result.success
    assert not metadata_path(outside).exists()


@pytest.mark.asyncio
async def test_publication_reports_metadata_write_failure(tmp_path, monkeypatch):
    file = tmp_path / "report.txt"
    file.write_text("report")
    def fail(*args):
        raise OSError("read-only filesystem")
    monkeypatch.setattr("box_agent.tools.publish_artifact_tool.write_metadata", fail)
    result = await PublishArtifactTool(tmp_path).execute("report.txt")
    assert not result.success
    assert "read-only filesystem" in result.error
    assert result.raw_output is None


@pytest.mark.asyncio
async def test_three_published_deliverables_keep_roles_across_revisions(tmp_path):
    names = ['plan.xlsx', 'risks.md', 'report.html']
    tool = PublishArtifactTool(tmp_path)
    for name in [*names, 'qa.png', 'calc.json']:
        (tmp_path / name).write_text('initial')
    for name in names:
        result = await tool.execute(name)
        assert result.success
        assert result.raw_output['placement'] == 'primary'
        events = _detect_tool_artifacts('publish', 'publish_artifact', result.content,
                                       result.raw_output, {}, {}, str(tmp_path))
        assert len(events) == 1
        assert _artifact_envelope(events[0])['placement'] == 'primary'

    # Later writes retain producer intent without mutable ACP-only state.
    (tmp_path / 'report.html').write_text('revised')
    events = _detect_tool_artifacts('check', 'bash',
                                   ' '.join(f'[{name}]' for name in [*names, 'qa.png', 'calc.json']),
                                   None, {}, {}, str(tmp_path))
    assert {e.rel_path for e in events if e.placement == 'primary'} == set(names)
    assert {e.rel_path for e in events if e.placement == 'supporting'} == {'qa.png', 'calc.json'}


def test_builder_publication_and_unmarked_structured_output_have_distinct_roles(tmp_path):
    file = tmp_path / 'report.html'
    file.write_text('report')
    raw = {'type': 'artifact', 'path': str(file)}
    events = _detect_tool_artifacts('write', 'write_file', '', raw, {}, {}, str(tmp_path))
    assert events[0].placement == 'supporting'
    write_metadata(file, {'type': 'artifact'})
    events = _detect_tool_artifacts('builder', 'bash', '[report.html]', None, {}, {}, str(tmp_path))
    assert events[0].placement == 'primary'

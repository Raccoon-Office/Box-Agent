"""Publication parity across the shared engine, CLI, and ACP."""
import pytest

from box_agent.artifact_publication import metadata_path, read_metadata, write_metadata
from box_agent.tools.publish_artifact_tool import PublishArtifactTool
from box_agent.tools.engine.artifact_results import _detect_tool_artifacts
from box_agent.events import ArtifactEvent
from box_agent.cli_renderer import CliRenderer
from box_agent.acp import _artifact_envelope


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

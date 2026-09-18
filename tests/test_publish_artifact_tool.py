"""Explicit, turn-scoped artifact delivery declarations."""

from types import SimpleNamespace

import pytest

from box_agent.tools.publish_artifact_tool import PublishArtifactTool
from box_agent.tools.setup import add_workspace_tools
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.acp import _artifact_delivery_envelope


def test_publish_artifact_is_available_in_workspace_tools(tmp_path):
    tools = []
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(workspace_dir=str(tmp_path)),
        tools=ToolsConfig(enable_sub_agent=False),
    )
    add_workspace_tools(tools, config, tmp_path, output=lambda *_: None)
    assert any(isinstance(tool, PublishArtifactTool) for tool in tools)


@pytest.mark.asyncio
async def test_publish_artifact_marks_multiple_primary_files_when_declared(tmp_path):
    first = tmp_path / "first.html"
    second = tmp_path / "second.xlsx"
    first.write_text("first")
    second.write_bytes(b"second")
    tool = PublishArtifactTool(tmp_path)

    assert (await tool.execute("first.html")).success
    assert "placement" not in tool.parameters["properties"]
    assert (await tool.execute("second.xlsx")).success
    assert (await tool.execute("first.html")).success
    assert [(item.artifact.rel_path, item.placement) for item in tool.finalize()] == [
        ("first.html", "primary"),
        ("second.xlsx", "primary"),
    ]


@pytest.mark.asyncio
async def test_publish_artifact_keeps_valid_primaries_and_rejects_outside_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for name in ("first.html", "second.html"):
        (workspace / name).write_text(name)
    outside = tmp_path / "outside.html"
    outside.write_text("outside")
    tool = PublishArtifactTool(workspace)

    assert (await tool.execute("first.html")).success
    assert (await tool.execute("second.html")).success
    assert not (await tool.execute("../outside.html")).success
    assert [(item.artifact.rel_path, item.placement) for item in tool.finalize()] == [
        ("first.html", "primary"),
        ("second.html", "primary"),
    ]


@pytest.mark.asyncio
async def test_publish_artifact_rechecks_file_and_clears_turn_state(tmp_path):
    file = tmp_path / "report.pdf"
    file.write_bytes(b"report")
    tool = PublishArtifactTool(tmp_path)
    assert (await tool.execute("report.pdf")).success
    file.unlink()

    assert tool.finalize() == []
    tool.clear()
    assert tool.finalize() == []


@pytest.mark.asyncio
async def test_missing_primary_does_not_remove_other_declared_files(tmp_path):
    missing = tmp_path / "missing.pdf"
    kept = tmp_path / "kept.xlsx"
    missing.write_bytes(b"draft")
    kept.write_bytes(b"final")
    tool = PublishArtifactTool(tmp_path)
    assert (await tool.execute("missing.pdf")).success
    assert (await tool.execute("kept.xlsx")).success
    missing.unlink()

    assert [item.artifact.rel_path for item in tool.finalize()] == ["kept.xlsx"]


@pytest.mark.asyncio
async def test_explicit_publication_can_deliver_a_marked_intermediate(tmp_path):
    image = tmp_path / "review.png"
    image.write_bytes(b"image")
    image.with_name(f".{image.name}.artifact.json").write_text(
        '{"type":"intermediate_asset"}', encoding="utf-8",
    )
    tool = PublishArtifactTool(tmp_path)

    assert (await tool.execute("review.png")).success
    assert [item.artifact.rel_path for item in tool.finalize()] == ["review.png"]


@pytest.mark.asyncio
async def test_delivery_envelope_contains_explicit_primary_selection(tmp_path):
    file = tmp_path / "report.xlsx"
    file.write_bytes(b"data")
    tool = PublishArtifactTool(tmp_path)
    assert (await tool.execute("report.xlsx")).success

    envelope = _artifact_delivery_envelope(
        tool.finalize(),
        lineages=[SimpleNamespace(
            artifact_id="artifact-1",
            artifact_revision_id="revision-1",
            sha256="hash-1",
            manifest_path="output/.artifacts/manifest.json",
        )],
        session_id="session-1", task_id="task-1", turn_id="turn-1",
    )

    assert envelope["type"] == "artifact_delivery"
    assert envelope["artifacts"][0]["rel_path"] == "report.xlsx"
    assert envelope["artifacts"][0]["placement"] == "primary"
    assert "artifact_role" not in envelope["artifacts"][0]
    assert envelope["artifacts"][0]["artifact_id"] == "artifact-1"
    assert envelope["turn_id"] == "turn-1"

import pytest

from box_agent.artifacts import make_artifact
from box_agent.artifact_publication import write_metadata
from box_agent.acp import _artifact_envelope
from box_agent.tools.publish_artifact_tool import PublishArtifactTool


@pytest.mark.asyncio
async def test_useful_process_can_be_promoted_without_copying(tmp_path):
    file = tmp_path / "cleaned.csv"
    file.write_text("a,b\n1,2")
    tool = PublishArtifactTool(tmp_path)
    assert (await tool.execute("cleaned.csv", role="process")).success
    process = make_artifact("call1", file, tmp_path)
    assert process.artifact_role == "process"
    assert (await tool.execute("cleaned.csv")).success
    delivery = make_artifact("call2", file, tmp_path)
    assert delivery.artifact_role == "deliverable"
    assert process.artifact_role == "process"  # Earlier task/event remains unchanged.
    assert process.sha256 == delivery.sha256
    assert _artifact_envelope(delivery, task_id="task2")["artifact_role"] == "deliverable"
    assert [p.name for p in tmp_path.glob("*.csv")] == ["cleaned.csv"]


def test_unregistered_input_is_only_observed_and_builder_is_deliverable(tmp_path):
    file = tmp_path / "input.csv"
    file.write_text("input")
    assert make_artifact("read", file, tmp_path).artifact_role == "observed"
    write_metadata(file, {"type": "artifact"})
    assert make_artifact("build", file, tmp_path).artifact_role == "deliverable"


@pytest.mark.asyncio
async def test_unreadable_output_cannot_be_published(tmp_path, monkeypatch):
    from pathlib import Path
    file = tmp_path / "report.pdf"
    file.write_bytes(b"report")
    def denied(*args, **kwargs):
        raise PermissionError("not readable")
    monkeypatch.setattr(Path, "open", denied)
    assert not (await PublishArtifactTool(tmp_path).execute("report.pdf")).success

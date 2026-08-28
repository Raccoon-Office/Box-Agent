import json
from pathlib import Path

from box_agent.acp import _artifact_envelope
from box_agent.events import ArtifactEvent
from box_agent.task_context import TaskContext, normalize_task_id
from box_agent.task_registry import begin_task, finish_task, register_artifact_revision


def _artifact(path: Path, *, rel_path: str = "report.md") -> ArtifactEvent:
    return ArtifactEvent(
        tool_call_id="tool-1",
        kind="document",
        filename=path.name,
        rel_path=rel_path,
        abs_path=str(path),
        uri=path.as_uri(),
        size=path.stat().st_size,
        produced_at="2026-08-21T00:00:00+00:00",
    )


def test_task_id_normalization_rejects_path_traversal() -> None:
    assert normalize_task_id(" task-1 ") == "task-1"
    assert normalize_task_id("../task-1") is None


def test_registry_keeps_artifact_id_stable_and_versions_content(tmp_path: Path) -> None:
    output = tmp_path / "output" / "tasks" / "task-1"
    output.mkdir(parents=True)
    file_path = output / "report.md"
    context = TaskContext(session_id="session-1", task_id="task-1", turn_id="turn-1")

    file_path.write_text("first", encoding="utf-8")
    first = register_artifact_revision(
        tmp_path,
        context,
        _artifact(file_path),
        artifact_root_dir=output,
    )
    file_path.write_text("second", encoding="utf-8")
    second = register_artifact_revision(
        tmp_path,
        TaskContext(session_id="session-1", task_id="task-1", turn_id="turn-2"),
        _artifact(file_path),
        artifact_root_dir=output,
    )
    finish_task(
        tmp_path,
        context,
        execution_status="completed",
        artifact_root_dir=output,
    )

    assert first.artifact_id == second.artifact_id
    assert first.artifact_revision_id != second.artifact_revision_id
    record = json.loads(Path(second.manifest_path).read_text(encoding="utf-8"))
    assert record["schema_version"] == 2
    assert record["execution_status"] == "completed"
    assert "delivery_status" not in record
    assert len(record["artifacts"][0]["revisions"]) == 2


def test_registry_reads_legacy_record_without_reinterpreting_delivery_status(
    tmp_path: Path,
) -> None:
    context = TaskContext(session_id="session-1", task_id="task-1", turn_id="turn-2")
    path = tmp_path / ".box-agent" / "task-registry" / "tasks" / "task-1.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": "session-1",
                "task_id": "task-1",
                "current_turn_id": "turn-1",
                "execution_status": "paused",
                "delivery_status": "waiting_for_user",
                "artifacts": [],
            }
        ),
        encoding="utf-8",
    )

    begin_task(tmp_path, context, artifact_root_dir=tmp_path / "output")

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["schema_version"] == 2
    assert record["execution_status"] == "running"
    assert "delivery_status" not in record


def test_artifact_envelope_exposes_canonical_lineage(tmp_path: Path) -> None:
    file_path = tmp_path / "report.md"
    file_path.write_text("content", encoding="utf-8")
    artifact = _artifact(file_path)
    context = TaskContext(session_id="session-1", task_id="task-1", turn_id="turn-1")
    lineage = register_artifact_revision(
        tmp_path,
        context,
        artifact,
        artifact_root_dir=tmp_path,
    )

    payload = _artifact_envelope(
        artifact,
        str(tmp_path),
        session_id=context.session_id,
        task_id=context.task_id,
        turn_id=context.turn_id,
        lineage=lineage,
    )

    assert payload["artifact_id"] == lineage.artifact_id
    assert payload["artifact_revision_id"] == lineage.artifact_revision_id
    assert payload["task_id"] == "task-1"

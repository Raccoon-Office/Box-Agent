import hashlib
import json
from pathlib import Path

from acp_eval import SCHEMA_VERSION
from acp_eval.snapshots import (
    FileRecord,
    build_artifact_inventory,
    snapshot_tree,
    write_snapshot,
)


def test_snapshot_tree_is_deterministic_and_does_not_follow_symlinks(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    (root / "z-目录").mkdir(parents=True)
    (root / "a").mkdir()
    (root / "z-目录" / "你好.txt").write_text("你好\n", encoding="utf-8")
    (root / "a" / "empty.txt").write_bytes(b"")
    binary = b"\x00\xff\x10binary"
    (root / "binary.bin").write_bytes(binary)
    (root / "target.txt").write_text("target", encoding="utf-8")
    (root / "link.txt").symlink_to("target.txt")

    records = snapshot_tree(root)

    assert [record.path for record in records] == [
        "a/empty.txt",
        "binary.bin",
        "link.txt",
        "target.txt",
        "z-目录/你好.txt",
    ]
    by_path = {record.path: record for record in records}
    assert by_path["binary.bin"].kind == "file"
    assert by_path["binary.bin"].size == len(binary)
    assert by_path["binary.bin"].sha256 == hashlib.sha256(binary).hexdigest()
    assert by_path["a/empty.txt"].sha256 == hashlib.sha256(b"").hexdigest()
    assert by_path["link.txt"].kind == "symlink"
    assert by_path["link.txt"].sha256 is None


def test_write_snapshot_uses_atomic_json_storage_and_returns_records(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "note.txt").write_text("snapshot", encoding="utf-8")
    destination = tmp_path / "evidence" / "files-after.json"

    records = write_snapshot(root, destination)

    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload == {
        "schema_version": SCHEMA_VERSION,
        "files": [record.__dict__ for record in records],
    }
    assert list(destination.parent.glob("*.tmp")) == []


def test_artifact_inventory_preserves_artifact_envelopes_and_lists_final_files(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    output = workspace / "output"
    output.mkdir(parents=True)
    report = output / "report.txt"
    report.write_text("final report", encoding="utf-8")
    (output / "nested").mkdir()
    (output / "nested" / "bytes.bin").write_bytes(b"\x00\x01")
    (workspace / "outside.txt").write_text("not an artifact", encoding="utf-8")

    protocol = tmp_path / "protocol.jsonl"
    emitted = {
        "type": "artifact",
        "kind": "document",
        "filename": "report.txt",
        "rel_path": "output/report.txt",
        "abs_path": str(report),
        "uri": report.as_uri(),
        "mime": "text/plain",
        "size": report.stat().st_size,
        "sha256": "event-hash",
        "tool_call_id": "call-1",
    }
    lines = [
        {"method": "session/update", "params": {"update": {"content": "ignored"}}},
        {
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallUpdate": {"rawOutput": emitted},
                }
            },
        },
        {
            "rawOutput": {
                "type": "artifact",
                "kind": "file",
                "filename": "missing.bin",
                "rel_path": "output/missing.bin",
                "mime": "application/octet-stream",
                "size": -1,
                "sha256": "",
            }
        },
    ]
    protocol.write_text(
        "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines),
        encoding="utf-8",
    )

    inventory = build_artifact_inventory(protocol, workspace)

    assert [event["rel_path"] for event in inventory["artifact_events"]] == [
        "output/report.txt",
        "output/missing.bin",
    ]
    assert inventory["artifact_events"][0]["exists"] is True
    assert inventory["artifact_events"][1]["exists"] is False
    assert [item["path"] for item in inventory["final_files"]] == [
        "output/nested/bytes.bin",
        "output/report.txt",
    ]
    report_record = next(
        item for item in inventory["final_files"] if item["path"] == "output/report.txt"
    )
    assert report_record["size"] == report.stat().st_size
    assert report_record["mime_type"] == "text/plain"
    assert report_record["sha256"] == hashlib.sha256(report.read_bytes()).hexdigest()


def test_artifact_inventory_does_not_follow_external_output_symlink(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external_output = tmp_path / "external-output"
    external_output.mkdir()
    (external_output / "outside.txt").write_text("outside", encoding="utf-8")
    (workspace / "output").symlink_to(external_output, target_is_directory=True)

    inventory = build_artifact_inventory(tmp_path / "protocol.jsonl", workspace)

    assert inventory["final_files"] == []


def test_artifact_inventory_lists_cwd_rooted_event_files_without_inputs(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    artifact = workspace / "deck" / "slides.pptx"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"pptx")
    (workspace / "input.md").write_text("source", encoding="utf-8")
    protocol = tmp_path / "protocol.jsonl"
    protocol.write_text(
        json.dumps(
            {
                "rawOutput": {
                    "type": "artifact",
                    "kind": "presentation",
                    "filename": artifact.name,
                    "rel_path": "deck/slides.pptx",
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )

    inventory = build_artifact_inventory(protocol, workspace)

    assert [item["path"] for item in inventory["final_files"]] == [
        "deck/slides.pptx"
    ]

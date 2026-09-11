import json
import mimetypes
import stat
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Iterator, Mapping

from acp_eval import SCHEMA_VERSION
from acp_eval.storage import atomic_write_json, sha256_file


@dataclass(frozen=True)
class FileRecord:
    path: str
    kind: str
    size: int
    mtime_ns: int
    sha256: str | None
    mime_type: str | None


def _file_record(root: Path, entry: Path) -> FileRecord | None:
    metadata = entry.lstat()
    relative_path = entry.relative_to(root).as_posix()
    mime_type = mimetypes.guess_type(entry.name)[0]

    if stat.S_ISLNK(metadata.st_mode):
        return FileRecord(
            path=relative_path,
            kind="symlink",
            size=metadata.st_size,
            mtime_ns=metadata.st_mtime_ns,
            sha256=None,
            mime_type=mime_type,
        )
    if not stat.S_ISREG(metadata.st_mode):
        return None
    return FileRecord(
        path=relative_path,
        kind="file",
        size=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
        sha256=sha256_file(entry),
        mime_type=mime_type,
    )


def snapshot_tree(root: Path) -> list[FileRecord]:
    """Return a stable inventory of regular files and symlinks below root.

    ``lstat`` is used for every directory entry so a symlink is represented as
    a link and is never opened through its target.  Directories themselves are
    not records; their children are returned in relative POSIX path order.
    """

    root = Path(root)
    records: list[FileRecord] = []
    for entry in root.rglob("*"):
        record = _file_record(root, entry)
        if record is not None:
            records.append(record)
    records.sort(key=lambda record: record.path)
    return records


def write_snapshot(root: Path, destination: Path) -> list[FileRecord]:
    records = snapshot_tree(root)
    atomic_write_json(
        destination,
        {
            "schema_version": SCHEMA_VERSION,
            "files": [asdict(record) for record in records],
        },
    )
    return records


def _artifact_envelopes(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, Mapping):
        raw_output = value.get("rawOutput")
        if isinstance(raw_output, Mapping) and raw_output.get("type") == "artifact":
            yield dict(raw_output)
        for child in value.values():
            yield from _artifact_envelopes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _artifact_envelopes(child)


def _read_artifact_envelopes(protocol_path: Path) -> list[dict[str, Any]]:
    envelopes: list[dict[str, Any]] = []
    if not protocol_path.exists():
        return envelopes
    with protocol_path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            envelopes.extend(_artifact_envelopes(message))
    return envelopes


def _event_path_exists(workspace: Path, rel_path: Any) -> bool:
    if not isinstance(rel_path, str) or not rel_path or "\x00" in rel_path:
        return False
    candidate = workspace / Path(rel_path)
    try:
        candidate.resolve().relative_to(workspace.resolve())
        candidate.lstat()
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def build_artifact_inventory(protocol_path: Path, workspace: Path) -> dict[str, Any]:
    """Reconcile legacy output files or cwd-rooted ACP artifact event files."""

    artifact_events = []
    for envelope in _read_artifact_envelopes(protocol_path):
        event = dict(envelope)
        event["exists"] = _event_path_exists(workspace, envelope.get("rel_path"))
        artifact_events.append(event)

    output = workspace / "output"
    final_files: list[dict[str, Any]] = []
    try:
        output_is_directory = stat.S_ISDIR(output.lstat().st_mode)
    except OSError:
        output_is_directory = False
    if output_is_directory:
        for record in snapshot_tree(output):
            if record.kind != "file":
                continue
            final_files.append(asdict(replace(record, path=f"output/{record.path}")))
    else:
        workspace_root = workspace.resolve()
        final_by_path: dict[str, dict[str, Any]] = {}
        for event in artifact_events:
            rel_path = event.get("rel_path")
            if not event["exists"] or not isinstance(rel_path, str):
                continue
            try:
                candidate = (workspace / rel_path).resolve()
                candidate.relative_to(workspace_root)
                record = _file_record(workspace_root, candidate)
            except (OSError, RuntimeError, ValueError):
                continue
            if record is not None and record.kind == "file":
                final_by_path[record.path] = asdict(record)
        final_files.extend(final_by_path[path] for path in sorted(final_by_path))

    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_events": artifact_events,
        "final_files": final_files,
    }

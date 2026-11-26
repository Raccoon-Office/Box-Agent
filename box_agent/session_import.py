"""Publish a desktop-prepared text history as one complete native session."""
from __future__ import annotations

import base64
from collections.abc import Callable, Iterator
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, BinaryIO
from uuid import UUID

from .schema import Message
from .session_log import SESSION_LOG_VERSION, SessionLog, default_session_root
from .user_paths import box_agent_home


_MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
_observer: ContextVar[tuple[Callable[[str, int], None], Callable[[], bool]] | None] = ContextVar(
    "history_import_observer", default=None,
)


class HistoryImportCancelled(Exception):
    """The owner withdrew this import attempt before its next operation."""


def _checkpoint(stage: str, completed: int = 0) -> None:
    observer = _observer.get()
    if observer is not None:
        report, cancelled = observer
        if cancelled():
            raise HistoryImportCancelled()
        report(stage, completed)


def _uuid(value: object) -> str:
    if not isinstance(value, str) or len(value) != 36 or str(UUID(value)) != value:
        raise ValueError("Invalid history import identifier")
    return value


def _read_snapshot(token: str) -> bytes:
    _checkpoint("read")
    directory = box_agent_home().resolve() / "history-import"
    if directory.resolve() != directory:
        raise ValueError("Invalid history staging directory")
    folder = directory / token
    if folder.is_symlink():
        raise ValueError("Invalid history staging folder")
    path = folder / "snapshot.json"
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError("Invalid history staging file")
    with path.open("rb") as source:
        after = os.fstat(source.fileno())
        if not os.path.samestat(before, after) or after.st_size > _MAX_SNAPSHOT_BYTES:
            raise ValueError("Invalid history staging snapshot")
        data = bytearray()
        while len(data) <= after.st_size:
            _checkpoint("read", len(data))
            chunk = source.read(min(65536, after.st_size + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        _checkpoint("read", len(data))
        if len(data) != after.st_size:
            raise ValueError("Incomplete history staging snapshot")
        return bytes(data)


def _history_events(rows: list) -> Iterator[dict[str, Any]]:
    """Describe the same imported prefix for writing and read-only verification."""
    turn = 0
    active_id = None
    seen: set[str] = set()
    for row in rows:
        _checkpoint("parse_messages")
        if not isinstance(row, dict):
            raise ValueError("Invalid imported message")
        role, text = row.get("role"), row.get("text")
        turn_id = _uuid(row.get("turnId"))
        timestamp = row.get("timestamp")
        if type(timestamp) is not int or timestamp < 0 or not isinstance(text, str) or not text.strip():
            raise ValueError("Invalid imported message")
        # These are labels from a foreign conversation. They never become
        # native resource_link inputs, image references, or filesystem grants.
        references = row.get("references", [])
        if not isinstance(references, list):
            raise ValueError("Invalid imported references")
        paths = []
        for reference in references:
            _checkpoint("parse_messages")
            value = reference.get("path") if isinstance(reference, dict) else None
            if not isinstance(value, str) or len(value) > 4096:
                raise ValueError("Invalid imported file reference")
            if value not in text and value not in paths:
                paths.append(value)
        if paths:
            text += "\n\n" + "\n".join(paths)
        if role == "user":
            if turn_id in seen:
                raise ValueError("Duplicate imported turn")
            if turn:
                yield {"type": "turn/end", "data": {"turn": turn, "reason": {"kind": "completed"}}}
            turn += 1
            active_id = turn_id
            seen.add(turn_id)
            yield {"type": "turn/start", "data": {"turn": turn}}
            yield {"type": "turn/binding", "data": {"turn_id": turn_id}}
            facts = {"id": turn_id, "context": {}, "blocks": [{"type": "text", "text": text}]}
            message = Message(role="user", content=text, input_facts=facts)
            payload = message.model_dump(mode="json", exclude_none=True)
            yield {"type": "user/message", "data": payload, "surfaceOp": "append", "time": timestamp}
        elif role == "assistant" and turn_id == active_id:
            message = Message(role="assistant", content=text)
            yield {"type": "assistant/message", "data": {
                "turn": turn, "step": 1, "message": message.model_dump(mode="json", exclude_none=True),
            }, "surfaceOp": "append", "time": timestamp}
        else:
            raise ValueError("Invalid imported role or turn")
    if not turn:
        raise ValueError("No imported user messages")
    yield {"type": "turn/end", "data": {"turn": turn, "reason": {"kind": "completed"}}}


def _write_history(log: SessionLog, rows: list) -> None:
    for seq, event in enumerate(_history_events(rows)):
        log.append(event["type"], event["data"], surface_op=event.get("surfaceOp"),
                   timestamp_ms=event.get("time"))
        _checkpoint("write", seq + 1)
    _checkpoint("file_sync")
    log.flush()
    _checkpoint("file_sync", 1)


def _read_record(source: BinaryIO, limit: int) -> dict[str, Any]:
    line = source.readline(limit)
    if not line.endswith(b"\n"):
        raise ValueError("Incomplete imported history record")
    record = json.loads(line)
    if not isinstance(record, dict):
        raise ValueError("Invalid imported history record")
    return record


def _verify_history(destination: Path, snapshot: dict, origin: str) -> None:
    _checkpoint("verify")
    file = destination / "session.jsonl"
    if destination.is_symlink() or not stat.S_ISREG(file.lstat().st_mode):
        raise ValueError("Invalid import destination")
    with file.open("rb") as existing:
        header = _read_record(existing, 16384)
        expected_header = {
            "type": "session", "version": SESSION_LOG_VERSION,
            "id": snapshot["id"], "origin": origin,
            "cwd": os.path.normcase(os.path.abspath(snapshot["workspace"])),
        }
        if any(header.get(key) != value for key, value in expected_header.items()):
            raise ValueError("Import destination already belongs to another history")
        for seq, expected in enumerate(_history_events(snapshot["messages"])):
            # A user message repeats its text in input_facts. Bound corrupt lines
            # without reading or repairing the live conversation after this prefix.
            actual = _read_record(existing, 2 * _MAX_SNAPSHOT_BYTES + 16384)
            if (type(actual.get("seq")) is not int or actual["seq"] != seq
                    or type(actual.get("time")) is not int
                    or actual.get("surfaceOp") != expected.get("surfaceOp")
                    or any(actual.get(key) != value for key, value in expected.items())):
                raise ValueError("Import destination history does not match the snapshot")
            _checkpoint("verify", seq + 1)


def _sync_history_directories(destination: Path) -> None:
    if os.name == "nt":
        return
    # A visible directory is not a durability acknowledgement. Every reuse
    # confirms the log name, published session and possibly new sessions root.
    for index, directory in enumerate((destination, destination.parent, destination.parent.parent)):
        _checkpoint("directory_sync", index)
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        _checkpoint("directory_sync", index + 1)


def import_staged_history(
    token: str, *, report: Callable[[str, int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict:
    observer = _observer.set((report or (lambda *_: None), cancelled or (lambda: False)))
    try:
        return _import_staged_history(token)
    finally:
        _observer.reset(observer)


def _import_staged_history(token: str) -> dict:
    token = _uuid(token)
    raw = _read_snapshot(token)
    origin = "desktop-history-v1:" + hashlib.sha256(raw).hexdigest()
    snapshot = json.loads(raw)
    _checkpoint("parse", 1)
    if not isinstance(snapshot, dict) or snapshot.get("version") != 1:
        raise ValueError("Unsupported history import")
    session_id = _uuid(snapshot.get("id"))
    if session_id != token or not isinstance(snapshot.get("messages"), list):
        raise ValueError("Invalid history import")
    workspace = Path(snapshot["workspace"])
    if not workspace.is_absolute() or not workspace.is_dir():
        raise ValueError("Invalid import workspace")
    root = default_session_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = root / hashlib.sha256(session_id.encode()).hexdigest()
    if not destination.exists():
        # Publish the closed, durable directory in one rename. Native session directories
        # contain a writer lock and log, so a concurrent winner cannot be replaced.
        with tempfile.TemporaryDirectory(dir=root, prefix=".import-") as temp:
            log = SessionLog.create(temp, session_id=session_id, cwd=workspace, origin=origin)
            try:
                _write_history(log, snapshot["messages"])
            finally:
                log.close()
            try:
                _checkpoint("publish")
                os.rename(log.path.parent, destination)
                _checkpoint("publish", 1)
            except OSError:
                if not destination.exists():
                    raise
    _verify_history(destination, snapshot, origin)
    _sync_history_directories(destination)
    handle = base64.urlsafe_b64encode(session_id.encode()).decode().rstrip("=")
    return {"sessionId": "acp-v1-" + handle}

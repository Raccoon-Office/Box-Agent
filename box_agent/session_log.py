"""Durable append-only JSONL state for one logical Agent session."""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO
from uuid import uuid4

from .schema import Message
from .session_projection import SessionProjection


_log = logging.getLogger(__name__)


SESSION_LOG_VERSION = 1
TOOL_NOT_STARTED = "TOOL_NOT_STARTED"
TOOL_OUTCOME_UNKNOWN = "TOOL_OUTCOME_UNKNOWN"
KNOWN_EVENT_TYPES = frozenset(
    {
        "turn/start",
        "turn/end",
        "step/start",
        "step/end",
        "user/message",
        "assistant/chunk",
        "assistant/message",
        "tool/call",
        "tool/result",
        "request/header",
        "request/context",
        "session/end-seed",
        "surface/reset",
        "goal/change",
        "plan/write",
        "todo/write",
        "skill/change",
        "compaction/start",
        "compaction/summary",
        "compaction/end",
        "subagent/descriptor",
    }
)


class SessionLogCorrupted(ValueError):
    """The stored JSONL is not a valid committed Session Log prefix."""


class SessionLogDurabilityError(OSError):
    """A canonical Session Log write or fsync failed."""


class SessionLogInUseError(RuntimeError):
    """Another writer owns this Session Log."""


class SessionLogWorkspaceMismatch(ValueError):
    """The requested cwd does not own this immutable Session."""


@dataclass(frozen=True, slots=True)
class SessionOpenResult:
    """Result of opening or creating one logical Session."""

    log: "SessionLog"
    resumed: bool


def _encode_record(record: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            record,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _session_dir(root: Path, session_id: str) -> Path:
    key = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return root / key


def default_session_root() -> Path:
    """Return the shared on-disk root for logical Agent Sessions."""
    # Keep session storage inside the configured Box-Agent profile when one is
    # active (BOX_AGENT_HOME), while preserving the historical default path.
    from .user_paths import state_path

    return state_path("sessions")


def _normalize_cwd(cwd: str | Path) -> str:
    """Normalize cwd syntax without resolving symlink identity."""

    return os.path.normcase(os.path.abspath(os.fspath(cwd)))


_LEGACY_WORKFLOW_CHECKPOINT_PREFIX = "[Post-Compaction Workflow Checkpoint]"
_LEGACY_RUNTIME_CONTEXT_PREFIXES = (
    "The host runtime supplied the following internal state update",
    "The user sent the following message while the current task was already running",
)
_LEGACY_WORKFLOW_MARKERS = (
    "CONTROLLED_PRESENTATION_STAGE=",
    "[BOX_AGENT_EXTERNAL_SKILL_CHECKPOINT]",
)


def _is_legacy_workflow_context(message: Message) -> bool:
    """Identify only framework-authored workflow context from older logs."""

    if message.role != "user" or not isinstance(message.content, str):
        return False
    content = message.content.lstrip()
    if content.startswith(_LEGACY_WORKFLOW_CHECKPOINT_PREFIX):
        return True
    return content.startswith(_LEGACY_RUNTIME_CONTEXT_PREFIXES) and any(
        marker in content for marker in _LEGACY_WORKFLOW_MARKERS
    )


def _acquire_writer_lock(path: Path) -> BinaryIO:
    handle = path.open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
            raise SessionLogInUseError(
                "session already has an active writer"
            ) from exc
        raise
    return handle


def _release_writer_lock(handle: BinaryIO) -> None:
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


class SessionLog:
    """Own JSONL encoding, ordered append, durability, and replay."""

    def __init__(
        self,
        path: Path,
        header: dict[str, Any],
        events: list[dict[str, Any]],
        handle: BinaryIO,
        lock_handle: BinaryIO,
    ) -> None:
        self.path = path
        self.header = dict(header)
        self._events = events
        self._handle = handle
        self._lock_handle = lock_handle
        self._closed = False
        self._failed = False
        self.recovery_source: Path | None = None
        recovery_name = header.get("recoverySource")
        if isinstance(recovery_name, str) and Path(recovery_name).name == recovery_name:
            self.recovery_source = path.parent / recovery_name

    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        """Return detached committed and pending events in log order."""

        return tuple(deepcopy(self._events))

    @property
    def failed(self) -> bool:
        return self._failed

    def assert_workspace(self, cwd: str | Path) -> None:
        """Reject a workspace change without resolving symlink aliases."""

        stored_cwd = self.header.get("cwd")
        requested_cwd = _normalize_cwd(cwd)
        if _normalize_cwd(stored_cwd) != requested_cwd:
            raise SessionLogWorkspaceMismatch(
                "session cwd does not match the immutable workspace "
                f"(stored={stored_cwd!r}, requested={requested_cwd!r})"
            )

    @classmethod
    def open_or_create(
        cls,
        root: str | Path,
        *,
        session_id: str,
        cwd: str | Path,
        origin: str | None = None,
        prepare_resume: bool = True,
    ) -> SessionOpenResult:
        """Open an existing Session or create it, with one recovery policy."""

        try:
            log = cls.open(root, session_id=session_id, cwd=cwd)
        except FileNotFoundError:
            directory = _session_dir(Path(root), session_id)
            if directory.exists():
                # A directory without a canonical session file is not a new
                # session; surface the corruption instead of overwriting it.
                raise SessionLogCorrupted(
                    f"session directory exists without {directory / 'session.jsonl'}"
                )
            log = cls.create(
                root,
                session_id=session_id,
                cwd=cwd,
                origin=origin,
            )
            return SessionOpenResult(log=log, resumed=False)
        if prepare_resume:
            try:
                log.prepare_resume()
            except BaseException:
                log.close()
                raise
        return SessionOpenResult(log=log, resumed=True)

    @classmethod
    def create(
        cls,
        root: str | Path,
        *,
        session_id: str,
        cwd: str | Path,
        parent_session: str | None = None,
        origin: str | None = None,
        delegation_depth: int = 0,
    ) -> "SessionLog":
        if not session_id:
            raise ValueError("session_id must not be empty")
        root_path = Path(root)
        directory = _session_dir(root_path, session_id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / "session.jsonl"
        lock_handle = _acquire_writer_lock(directory / ".writer.lock")
        header: dict[str, Any] = {
            "type": "session",
            "version": SESSION_LOG_VERSION,
            "id": session_id,
            "createdAt": int(time.time() * 1000),
            "cwd": _normalize_cwd(cwd),
        }
        if parent_session is not None:
            header["parentSession"] = parent_session
        if origin is not None:
            header["origin"] = origin
        if delegation_depth:
            header["delegationDepth"] = delegation_depth
        try:
            handle = path.open("xb+")
            try:
                handle.write(_encode_record(header))
                handle.flush()
                os.fsync(handle.fileno())
            except BaseException:
                handle.close()
                raise
        except BaseException:
            _release_writer_lock(lock_handle)
            raise
        return cls(path, header, [], handle, lock_handle)

    @classmethod
    def open(
        cls,
        root: str | Path,
        *,
        session_id: str,
        cwd: str | Path,
        recover: bool = False,
    ) -> "SessionLog":
        """Open a session, optionally preserving incompatible logs for host recovery."""
        directory = _session_dir(Path(root), session_id)
        path = directory / "session.jsonl"
        lock_handle = _acquire_writer_lock(directory / ".writer.lock")
        try:
            handle = path.open("r+b")
            workspace_verified = False
            try:
                raw = handle.read()
                original_raw = raw
                header_end = raw.find(b"\n")
                if header_end < 0:
                    raise SessionLogCorrupted(
                        "session log has no complete header"
                    )
                try:
                    header = json.loads(raw[:header_end])
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise SessionLogCorrupted(
                        "session log header is invalid JSON"
                    ) from exc
                if not isinstance(header, dict) or header.get("type") != "session":
                    raise SessionLogCorrupted("session log header is invalid")
                if header.get("id") != session_id:
                    raise ValueError("session log id does not match requested session")
                stored_cwd = header.get("cwd")
                if not isinstance(stored_cwd, str) or not stored_cwd:
                    raise SessionLogCorrupted("session log header has an invalid cwd")
                requested_cwd = _normalize_cwd(cwd)
                if _normalize_cwd(stored_cwd) != requested_cwd:
                    raise SessionLogWorkspaceMismatch(
                        "session cwd does not match the immutable workspace "
                        f"(stored={stored_cwd!r}, requested={requested_cwd!r})"
                    )
                workspace_verified = True
                if header.get("version") != SESSION_LOG_VERSION:
                    raise SessionLogCorrupted("session log version is unsupported")
                committed_end = None
                if raw and not raw.endswith(b"\n"):
                    committed_end = raw.rfind(b"\n") + 1
                    raw = raw[:committed_end]
                raw_lines = raw.splitlines()
                records: list[Any] = [header]
                for index, line in enumerate(raw_lines[1:], start=1):
                    try:
                        records.append(json.loads(line))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise SessionLogCorrupted(
                            f"session log record {index} is invalid JSON"
                        ) from exc
                events = records[1:]
                for seq, event in enumerate(events):
                    if not isinstance(event, dict):
                        raise SessionLogCorrupted(
                            f"session log record {seq + 1} is not an event object"
                        )
                    if event.get("seq") != seq:
                        raise SessionLogCorrupted(
                            f"session log record {seq + 1} has non-contiguous seq"
                        )
                    event_type = event.get("type")
                    if not isinstance(event_type, str) or not event_type:
                        raise SessionLogCorrupted(
                            f"session log record {seq + 1} has an invalid event type"
                        )
                    if (
                        event_type not in KNOWN_EVENT_TYPES
                        and event.get("ignorable") is not True
                    ):
                        raise SessionLogCorrupted(
                            f"session log contains unknown required event {event_type!r}"
                        )
                    if not isinstance(event.get("data"), dict):
                        raise SessionLogCorrupted(
                            f"session log record {seq + 1} has invalid event data"
                        )
                session = cls(path, header, events, handle, lock_handle)
                if recover:
                    session.replay()
                if committed_end is not None:
                    handle.truncate(committed_end)
                    handle.flush()
                    os.fsync(handle.fileno())
                handle.seek(0, os.SEEK_END)
            except BaseException as exc:
                handle.close()
                if recover and workspace_verified and isinstance(exc, SessionLogCorrupted):
                    return cls._recover_log(
                        path, original_raw, session_id, cwd, lock_handle
                    )
                raise
        except BaseException:
            _release_writer_lock(lock_handle)
            raise
        return session

    @classmethod
    def _recover_log(
        cls,
        path: Path,
        raw: bytes,
        session_id: str,
        cwd: str | Path,
        lock_handle: BinaryIO,
    ) -> "SessionLog":
        """Keep the original bytes before replacing an incompatible runtime log.

        The caller holds the writer lock and has verified the session/workspace.
        No historical tools are replayed; the host may supply semantic history.
        """
        suffix = uuid4().hex
        archive = path.with_name(f"session.recovery-{suffix}.jsonl")
        temporary = path.with_name(f".session-{suffix}.tmp")
        header = {
            "type": "session",
            "version": SESSION_LOG_VERSION,
            "id": session_id,
            "createdAt": int(time.time() * 1000),
            "cwd": _normalize_cwd(cwd),
            "recoverySource": archive.name,
        }
        with archive.open("xb") as backup:
            backup.write(raw)
            backup.flush()
            os.fsync(backup.fileno())
        try:
            with temporary.open("xb") as replacement:
                replacement.write(_encode_record(header))
                replacement.flush()
                os.fsync(replacement.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        handle = path.open("r+b")
        try:
            handle.seek(0, os.SEEK_END)
            return cls(path, header, [], handle, lock_handle)
        except BaseException:
            handle.close()
            raise

    def append(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        surface_op: str | dict[str, Any] | None = None,
        source_event_seqs: list[int] | None = None,
        ignorable: bool = False,
    ) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("session log is closed")
        if self._failed:
            raise SessionLogDurabilityError("session log is unusable after an I/O failure")
        event: dict[str, Any] = {
            "type": event_type,
            "seq": len(self._events),
            "time": int(time.time() * 1000),
            "data": data,
        }
        if ignorable:
            event["ignorable"] = True
        if surface_op is not None:
            event["surfaceOp"] = surface_op
        if source_event_seqs is not None:
            event["sourceEventSeqs"] = source_event_seqs
        encoded = _encode_record(event)
        try:
            self._handle.write(encoded)
        except OSError as exc:
            self._failed = True
            raise SessionLogDurabilityError("session log append failed") from exc
        self._events.append(event)
        return dict(event)

    def flush(self) -> None:
        if self._closed:
            raise RuntimeError("session log is closed")
        try:
            self._handle.flush()
            os.fsync(self._handle.fileno())
        except OSError as exc:
            self._failed = True
            raise SessionLogDurabilityError("session log flush failed") from exc

    def _skill_reference_path(self, ref: Mapping[str, Any]) -> Path:
        """Accept only a content-addressed file in this session's namespace."""

        digest = ref.get("sha256") if isinstance(ref, Mapping) else None
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise SessionLogCorrupted("invalid Skill reference hash")
        expected = f"skill-references/{digest}.txt"
        if ref.get("contentRef") != expected:
            raise SessionLogCorrupted("invalid Skill reference path")
        path = self.path.parent / expected
        if path.parent.is_symlink() or path.is_symlink():
            raise SessionLogCorrupted("Skill reference paths must not be symbolic links")
        if path.resolve().parent != (self.path.parent.resolve() / "skill-references"):
            raise SessionLogCorrupted("Skill reference escapes the session directory")
        return path

    def read_skill_reference(self, ref: Mapping[str, Any]) -> str:
        """Read an exact UTF-8 snapshot, validating its path and byte hash."""

        path = self._skill_reference_path(ref)
        try:
            if not path.is_file():
                raise SessionLogCorrupted(
                    "cannot read missing or non-regular Skill reference snapshot"
                )
            content = path.read_bytes()
        except OSError as exc:
            raise SessionLogCorrupted("cannot read Skill reference snapshot") from exc
        if hashlib.sha256(content).hexdigest() != ref["sha256"]:
            raise SessionLogCorrupted("Skill reference content hash mismatch")
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SessionLogCorrupted("Skill reference is not valid UTF-8") from exc

    def store_skill_reference(self, content: str) -> dict[str, str]:
        """Durably publish an immutable snapshot without appending an event.

        After this succeeds, callers append the returned reference plus its
        request/range metadata to ``request/context`` and flush that event
        before calling the provider. Neither a trace nor the original Skill
        file is needed to reconstruct that request's reference text.
        """

        if self._closed:
            raise RuntimeError("session log is closed")
        if self._failed:
            raise SessionLogDurabilityError("session log is unusable after an I/O failure")
        encoded = content.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        ref = {"contentRef": f"skill-references/{digest}.txt", "sha256": digest}
        path = self._skill_reference_path(ref)
        temporary: Path | None = None
        try:
            path.parent.mkdir(mode=0o700, exist_ok=True)
            if path.exists():
                # Never overwrite a corrupt snapshot or change an existing
                # inode. Re-sync a valid file before acknowledging reuse.
                self.read_skill_reference(ref)
                with path.open("rb") as handle:
                    os.fsync(handle.fileno())
            else:
                fd, name = tempfile.mkstemp(prefix=".skill-reference-", dir=path.parent)
                temporary = Path(name)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    # Linking publishes an already-fsynced inode atomically
                    # without replacing any snapshot that appeared meanwhile.
                    os.link(temporary, path)
                except FileExistsError:
                    self.read_skill_reference(ref)
                    with path.open("rb") as handle:
                        os.fsync(handle.fileno())
                temporary.unlink()
                temporary = None
            if os.name != "nt":
                # Persist both the snapshot name and the newly-created
                # namespace. Windows does not support opening directory fds.
                for directory in (path.parent, self.path.parent):
                    directory_fd = os.open(directory, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
        except OSError as exc:
            self._failed = True
            raise SessionLogDurabilityError("Skill reference persistence failed") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        return ref

    def append_unlogged_messages(
        self,
        messages: list[Message],
        *,
        turn: int,
        step: int | None,
        tool_result_metadata: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Append the live Surface suffix, reconciling legitimate live rewrites.

        The Session Log is a recovery aid and must not terminate the active task
        merely because runtime context management rewrote the in-memory Surface.
        Stored-log corruption still fails in ``replay()``; only a valid stored
        Surface that differs from the valid live Surface is replaced here.
        """

        persisted = self.replay().messages
        persisted_payloads = [
            message.model_dump(mode="json", exclude_none=True) for message in persisted
        ]
        live_payloads = [
            message.model_dump(mode="json", exclude_none=True) for message in messages
        ]
        if live_payloads[: len(persisted_payloads)] != persisted_payloads:
            if not messages:
                _log.warning(
                    "session_log/surface_diverged session_id=%s persisted=%d live=0 "
                    "action=retain_persisted",
                    self.header.get("id"),
                    len(persisted_payloads),
                )
                return []
            _log.warning(
                "session_log/surface_diverged session_id=%s persisted=%d live=%d "
                "action=replace_surface",
                self.header.get("id"),
                len(persisted_payloads),
                len(live_payloads),
            )
            return self.replace_surface(
                messages,
                turn=turn,
                step=step if step is not None else 0,
            )

        appended: list[dict[str, Any]] = []
        for message, payload in zip(
            messages[len(persisted_payloads) :],
            live_payloads[len(persisted_payloads) :],
        ):
            if message.role == "user":
                appended.append(
                    self.append(
                        "user/message",
                        payload,
                        surface_op="append",
                    )
                )
            elif message.role == "assistant":
                appended.append(
                    self.append(
                        "assistant/message",
                        {"turn": turn, "step": step, "message": payload},
                        surface_op="append",
                    )
                )
            elif message.role == "tool":
                data: dict[str, Any] = {
                    "turn": turn,
                    "step": step,
                    "message": payload,
                }
                if (
                    message.tool_call_id is not None
                    and tool_result_metadata is not None
                    and message.tool_call_id in tool_result_metadata
                ):
                    data["result"] = tool_result_metadata[message.tool_call_id]
                appended.append(
                    self.append(
                        "tool/result",
                        data,
                        surface_op="append",
                    )
                )
        return appended

    def _surface_nodes(self) -> list[tuple[int, Message]]:
        surface: list[tuple[int, Message]] = []
        for event in self._events:
            if event["type"] == "surface/reset":
                surface.clear()
                continue
            if event["type"] not in {
                "user/message",
                "assistant/message",
                "tool/result",
            }:
                continue
            data = event["data"]
            message_data = data if event["type"] == "user/message" else data["message"]
            message = Message.model_validate(message_data)
            operation = event.get("surfaceOp")
            node = (event["seq"], message)
            if operation == "append":
                surface.append(node)
                continue
            if isinstance(operation, dict) and operation.get("op") == "replace":
                positions = {seq: index for index, (seq, _) in enumerate(surface)}
                start_index = positions[operation["start"]]
                end_index = positions[operation["end"]]
                surface[start_index : end_index + 1] = [node]
        return surface

    def _append_surface_message(
        self,
        message: Message,
        *,
        turn: int,
        step: int,
        surface_op: str | dict[str, Any],
        source_event_seqs: list[int] | None = None,
    ) -> dict[str, Any]:
        payload = message.model_dump(mode="json", exclude_none=True)
        if message.role == "user":
            return self.append(
                "user/message",
                payload,
                surface_op=surface_op,
                source_event_seqs=source_event_seqs,
            )
        if message.role == "assistant":
            return self.append(
                "assistant/message",
                {"turn": turn, "step": step, "message": payload},
                surface_op=surface_op,
                source_event_seqs=source_event_seqs,
            )
        if message.role == "tool":
            return self.append(
                "tool/result",
                {"turn": turn, "step": step, "message": payload},
                surface_op=surface_op,
                source_event_seqs=source_event_seqs,
            )
        raise ValueError(f"role {message.role!r} cannot enter the Session Surface")

    def replace_surface(
        self,
        messages: list[Message],
        *,
        turn: int,
        step: int,
    ) -> list[dict[str, Any]]:
        """Append a replacement whose replay is exactly ``messages``."""

        if not messages:
            raise ValueError("replacement Surface must not be empty")
        old_nodes = self._surface_nodes()
        appended: list[dict[str, Any]] = []
        if old_nodes:
            source_seqs = [seq for seq, _ in old_nodes]
            appended.append(
                self._append_surface_message(
                    messages[0],
                    turn=turn,
                    step=step,
                    surface_op={
                        "op": "replace",
                        "start": source_seqs[0],
                        "end": source_seqs[-1],
                    },
                    source_event_seqs=source_seqs,
                )
            )
            remaining = messages[1:]
        else:
            remaining = messages
        for message in remaining:
            appended.append(
                self._append_surface_message(
                    message,
                    turn=turn,
                    step=step,
                    surface_op="append",
                )
            )
        return appended

    def reset_surface(self, *, reason: str) -> dict[str, Any]:
        """Persist a logical conversation reset without deleting audit history."""

        event = self.append("surface/reset", {"reason": reason})
        self.flush()
        return event

    def replay(self) -> SessionProjection:
        surface: list[tuple[int, Message]] = []
        goal: dict[str, Any] | None = None
        plan: dict[str, Any] | None = None
        todos: list[dict[str, Any]] = []
        skills: list[dict[str, Any]] = []
        for event in self._events:
            event_type = event.get("type")
            data = event["data"]
            if event_type == "surface/reset":
                surface.clear()
                continue
            if event_type == "goal/change":
                value = data.get("goal")
                goal = deepcopy(value) if isinstance(value, dict) else None
                continue
            if event_type == "plan/write":
                value = data.get("plan")
                plan = deepcopy(value) if isinstance(value, dict) else None
                continue
            if event_type == "todo/write":
                value = data.get("todos")
                todos = deepcopy(value) if isinstance(value, list) else []
                continue
            if event_type == "skill/change":
                value = data.get("skills")
                skills = deepcopy(value) if isinstance(value, list) else []
                continue
            if event_type not in {
                "user/message",
                "assistant/message",
                "tool/result",
            }:
                continue
            message_data = (
                data
                if event_type == "user/message"
                else data.get("message")
            )
            try:
                message = Message.model_validate(message_data)
            except Exception as exc:
                raise SessionLogCorrupted(
                    f"session event {event['seq']} has an invalid message"
                ) from exc
            operation = event.get("surfaceOp")
            node = (event["seq"], message)
            if operation == "append":
                surface.append(node)
                continue
            if not isinstance(operation, dict) or operation.get("op") != "replace":
                raise SessionLogCorrupted(
                    f"surface event {event['seq']} has an invalid surfaceOp"
                )
            start = operation.get("start")
            end = operation.get("end")
            positions = {seq: index for index, (seq, _) in enumerate(surface)}
            if start not in positions or end not in positions:
                raise SessionLogCorrupted(
                    f"surface replacement {event['seq']} names a missing node"
                )
            start_index = positions[start]
            end_index = positions[end]
            if start_index > end_index:
                raise SessionLogCorrupted(
                    f"surface replacement {event['seq']} has a reversed range"
                )
            surface[start_index : end_index + 1] = [node]
        return SessionProjection(
            messages=[
                message
                for _, message in surface
                if not _is_legacy_workflow_context(message)
            ],
            goal=goal,
            plan=plan,
            todos=todos,
            skills=skills,
        )

    def repair_interrupted_turn(self) -> list[dict[str, Any]]:
        """Append deterministic normal events that close one crash-open turn."""

        open_turn: int | None = None
        open_step: int | None = None
        pending_calls: dict[str, dict[str, Any]] = {}
        for event in self._events:
            event_type = event.get("type")
            data = event.get("data", {})
            if event_type == "turn/start":
                open_turn = data.get("turn")
                open_step = None
                pending_calls.clear()
            elif event_type == "turn/end":
                open_turn = None
                open_step = None
                pending_calls.clear()
            elif event_type == "step/start":
                open_step = data.get("step")
            elif event_type == "step/end":
                open_step = None
                pending_calls.clear()
            elif event_type == "assistant/message":
                message = Message.model_validate(data.get("message"))
                for call in message.tool_calls or []:
                    pending_calls[call.id] = {
                        "step": data.get("step"),
                        "name": call.function.name,
                        "call_seq": None,
                    }
            elif event_type == "tool/call":
                call = pending_calls.get(str(data.get("callId")))
                if call is not None:
                    call["call_seq"] = event["seq"]
            elif event_type == "tool/result":
                message = Message.model_validate(data.get("message"))
                if message.tool_call_id is not None:
                    pending_calls.pop(message.tool_call_id, None)

        if open_turn is None:
            return []

        closers: list[dict[str, Any]] = []
        for call_id, pending in pending_calls.items():
            started = pending["call_seq"] is not None
            code = TOOL_OUTCOME_UNKNOWN if started else TOOL_NOT_STARTED
            content = (
                "The tool call was durably dispatched, but no result was recorded. "
                "Its outcome is unknown; verify external state before retrying."
                if started
                else "The tool call was not durably dispatched and may be retried if needed."
            )
            event = self.append(
                "tool/result",
                {
                    "turn": open_turn,
                    "step": pending["step"],
                    "message": Message(
                        role="tool",
                        content=content,
                        tool_call_id=call_id,
                        name=pending["name"],
                    ).model_dump(mode="json", exclude_none=True),
                    "error": {
                        "name": (
                            "ToolOutcomeUnknownError"
                            if started
                            else "ToolNotStartedError"
                        ),
                        "code": code,
                    },
                },
                surface_op="append",
                source_event_seqs=(
                    [pending["call_seq"]] if started else None
                ),
            )
            closers.append(event)
        if open_step is not None:
            closers.append(
                self.append(
                    "step/end",
                    {"turn": open_turn, "step": open_step},
                )
            )
        closers.append(
            self.append(
                "turn/end",
                {"turn": open_turn, "reason": {"kind": "interrupted"}},
            )
        )
        return closers

    def prepare_resume(self) -> list[dict[str, Any]]:
        """Close a crash-open lifecycle and durably delimit the loaded prefix."""

        appended = self.repair_interrupted_turn()
        appended.append(self.append("session/end-seed", {}))
        self.flush()
        return appended

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._handle.close()
        finally:
            try:
                _release_writer_lock(self._lock_handle)
            finally:
                self._closed = True

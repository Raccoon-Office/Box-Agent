"""Durable append-only JSONL state for one logical Agent session."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, TypeVar
from uuid import uuid4

from .schema import Message
from .schema.schema import RequestUsageSnapshot
from .session_projection import SessionProjection, mark_submitted_tool_results
from .windows_session_lock import WINDOWS_LOCK_CONTENTION, WindowsSessionLock

if TYPE_CHECKING:
    from .kernel.context_types import CompactionPlan


_log = logging.getLogger(__name__)
_session_io_pool = ThreadPoolExecutor(thread_name_prefix="session-io")
_session_io_worker = threading.local()
_T = TypeVar("_T")


def run_on_session_loop(callback: Callable[..., _T], /, *args: Any) -> _T:
    """Run a synchronous, loop-owned callback at the same point in a session write."""
    loop = getattr(_session_io_worker, "loop", None)
    if loop is None:
        return callback(*args)

    async def invoke() -> _T:
        return callback(*args)

    # The caller is a disk worker. The owner loop awaits it asynchronously and
    # remains free to run the callback, including during cancellation cleanup.
    return asyncio.run_coroutine_threadsafe(invoke(), loop).result()


async def run_session_io(operation: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
    """Settle file work before cancellation can release its session ownership."""
    loop = asyncio.get_running_loop()

    def run() -> _T:
        _session_io_worker.loop = loop
        try:
            return operation(*args, **kwargs)
        finally:
            del _session_io_worker.loop

    # Windows stdout uses the loop's default executor. Slow disk work must not
    # consume the threads needed to deliver a different session's notifications.
    worker = loop.run_in_executor(_session_io_pool, copy_context().run, run)
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(worker)
            break
        except asyncio.CancelledError as error:
            if worker.done():
                # A replay callback can itself raise CancelledError. An executor
                # Future records that as an exception rather than a cancellation.
                if not worker.cancelled():
                    worker.exception()
                raise
            cancellation = error
        except BaseException:
            if cancellation is not None:
                raise cancellation
            raise
    if cancellation is not None:
        raise cancellation
    return result


async def run_session_write(store: object, operation: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
    """Run file work in order for this log and wait for its durability barrier."""
    if not isinstance(store, SessionLog):
        # Custom synchronous stores keep their original calling convention.
        return operation(*args, **kwargs)
    async with store._write_lock:
        return await run_session_io(operation, *args, **kwargs)


def _timed_fsync(fd: int) -> None:
    started = time.perf_counter()
    try:
        os.fsync(fd)
    finally:
        _log.debug("session/fsync duration_ms=%.3f", (time.perf_counter() - started) * 1000)


SESSION_LOG_VERSION = 1
TOOL_NOT_STARTED = "TOOL_NOT_STARTED"
TOOL_OUTCOME_UNKNOWN = "TOOL_OUTCOME_UNKNOWN"
KNOWN_EVENT_TYPES = frozenset(
    {
        "turn/start",
        "turn/binding",
        "history/revise",
        "turn/end",
        "step/start",
        "step/end",
        "user/message",
        "input/contract",
        "tools/contract",
        "input/adopted",
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


class SessionLogRevisionCorrupted(SessionLogCorrupted):
    """A revision cannot be safely replayed or replaced with an empty log."""


class SessionLogIncompatible(SessionLogCorrupted):
    """A valid format requires a different reader; recovery must not reset it."""


class SessionLogDurabilityError(OSError):
    """A canonical Session Log write or fsync failed."""


class SessionLogInUseError(RuntimeError):
    """Another writer owns this Session Log."""


class SessionLogWorkspaceMismatch(ValueError):
    """The requested cwd does not own this immutable Session."""


class SessionLogReplayError(RuntimeError):
    """A committed surface could not be reconstructed for live execution."""


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


def _context_usage_hash(message: Message, metadata: dict[str, Any]) -> str:
    payload = {
        "message": message.model_dump(mode="json", exclude_none=True),
        "contextUsage": {key: value for key, value in metadata.items() if key != "sha256"},
    }
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _context_usage_metadata(message: Message) -> dict[str, Any] | None:
    usage = message.usage
    snapshot = usage._request_snapshot if usage is not None else None
    if message.role != "assistant" or snapshot is None or usage.context_tokens <= 0:
        return None
    metadata = {
        "version": 1,
        "route": snapshot.route,
        "history": list(snapshot.history),
        "tools": dict(snapshot.tools),
        "requestOnly": list(snapshot.request_only),
        "contextTokens": usage.context_tokens,
        "outputTokens": usage.output_tokens or usage.completion_tokens,
    }
    metadata["sha256"] = _context_usage_hash(message, metadata)
    return metadata


def _restore_context_usage(message: Message, metadata: Any) -> None:
    """Restore optional measured context without changing public usage fields."""
    if message.role != "assistant" or message.usage is None:
        return
    usage = message.usage
    # Public token totals do not identify the provider route after replay.
    usage._request_route_unverified = True
    if not isinstance(metadata, dict):
        return
    if type(metadata.get("version")) is not int or metadata["version"] != 1:
        return

    def is_hash(value: Any, prefix: str = "") -> bool:
        return isinstance(value, str) and re.fullmatch(prefix + r"[0-9a-f]{64}", value) is not None

    history, tools = metadata.get("history"), metadata.get("tools")
    request_only = metadata.get("requestOnly")
    context_tokens, output_tokens = metadata.get("contextTokens"), metadata.get("outputTokens")
    if (not is_hash(metadata.get("route"), "sha256:")
            or not isinstance(history, list) or not all(is_hash(value) for value in history)
            or not isinstance(tools, dict)
            or not all(isinstance(name, str) and is_hash(value, "sha256:") for name, value in tools.items())
            or not isinstance(request_only, list) or not all(is_hash(value, "sha256:") for value in request_only)
            or type(context_tokens) is not int or type(output_tokens) is not int
            or not 0 <= output_tokens <= context_tokens or context_tokens <= 0):
        return
    try:
        if metadata.get("sha256") != _context_usage_hash(message, metadata):
            return
    except (TypeError, ValueError, UnicodeError):
        return
    usage._request_snapshot = RequestUsageSnapshot(
        route=metadata["route"], history=tuple(history), tools=dict(tools), request_only=tuple(request_only),
    )
    usage._request_route_unverified = False
    # Provider-specific input/cache fields are excluded from public serialization.
    # Restore their aggregate so context_tokens remains identical after replay.
    usage.input_tokens = context_tokens - output_tokens
    usage.output_tokens = output_tokens
    usage.cache_creation_input_tokens = usage.cache_read_input_tokens = 0


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


_SURFACE_MESSAGE_TYPES = {"user/message", "assistant/message", "tool/result"}


def _compaction_format(event: dict[str, Any], current: int | None) -> int | None:
    kind = event["type"]
    if kind == "compaction/start":
        return 2 if event["data"].get("format") == 2 else None
    if kind == "compaction/summary" and current != 2:
        # Old writers only started emitting copies after a successful summary.
        return 1
    if kind in ("compaction/end", "surface/reset", "turn/start"):
        return None
    return current


def _apply_surface_message(
    surface: list[tuple[int, Message]], event: dict[str, Any], *, compaction_format: int | None,
) -> Message:
    data = event["data"]
    message_data = data if event["type"] == "user/message" else data.get("message")
    seq = event["seq"]
    try:
        message = Message.model_validate(message_data)
    except Exception as exc:
        raise SessionLogCorrupted(f"session event {seq} has an invalid message") from exc

    message._surface_seq = seq
    node = (seq, message)
    operation = event.get("surfaceOp")
    if operation == "append":
        if (compaction_format == 1 and message.role == "user" and message.source == "runtime"
                and (message.name in ("current_date", "tool_descriptions") or message.runtime_kind == "tool_descriptions")):
            message._legacy_compaction_append = True
        surface.append(node)
        return message
    if not isinstance(operation, dict) or operation.get("op") != "replace":
        raise SessionLogCorrupted(f"surface event {seq} has an invalid surfaceOp")
    start = operation.get("start")
    end = operation.get("end")
    positions = {node_seq: index for index, (node_seq, _) in enumerate(surface)}
    if start not in positions or end not in positions:
        raise SessionLogCorrupted(f"surface replacement {seq} names a missing node")
    start_index = positions[start]
    end_index = positions[end]
    if start_index > end_index:
        raise SessionLogCorrupted(f"surface replacement {seq} has a reversed range")
    surface[start_index : end_index + 1] = [node]
    if message.compaction_node in ("summary_head", "fallback_head"):
        # Measurements on the retained nodes describe the previous prefix.
        # Only the projection changes; the original events remain auditable.
        for _, retained in surface[start_index + 1:]:
            retained.usage = None
            retained.request_only_input_tokens = 0
    return message


def _acquire_writer_lock(path: Path) -> BinaryIO | WindowsSessionLock:
    if os.name == "nt":
        try:
            return WindowsSessionLock(path)
        except OSError as exc:
            if getattr(exc, "winerror", None) in WINDOWS_LOCK_CONTENTION:
                raise SessionLogInUseError("session already has an active writer") from exc
            raise

    import fcntl

    handle = path.open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise SessionLogInUseError("session already has an active writer") from exc
            raise
    except BaseException:
        handle.close()
        raise
    return handle


def _release_writer_lock(handle: BinaryIO | WindowsSessionLock) -> None:
    if isinstance(handle, WindowsSessionLock):
        handle.close()
        return

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


class SessionLog:
    """Own JSONL encoding, ordered append, durability, and replay."""

    @staticmethod
    def has_session_directory(root: Path, session_id: str) -> bool:
        return _session_dir(root, session_id).is_dir()

    def __init__(
        self,
        path: Path,
        header: dict[str, Any],
        events: list[dict[str, Any]],
        handle: BinaryIO,
        lock_handle: BinaryIO | WindowsSessionLock,
    ) -> None:
        self.path = path
        self.header = dict(header)
        self._events = events
        self._durable_event_count = len(events)
        self._surface: list[tuple[int, Message]] = []
        self._surface_cursor: int | None = None if events else 0
        self._surface_compaction_format: int | None = None
        self._handle = handle
        self._write_lock = asyncio.Lock()
        self._lock_handle = lock_handle
        self._closed = False
        self._failed = False
        self._newly_created = False
        self._bound_tool_interface_version: int | None = None
        # Keep the actual objects alive until commit; an id-only map could
        # attach an abandoned image to an unrelated Message after id reuse.
        self._pending_message_metadata: dict[int, tuple[Message, dict[str, Any]]] = {}
        self.recovery_source: Path | None = None
        recovery_name = header.get("recoverySource")
        if isinstance(recovery_name, str) and Path(recovery_name).name == recovery_name:
            self.recovery_source = path.parent / recovery_name

    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        """Return detached committed and pending events in log order."""

        return tuple(deepcopy(self._events))

    def resolve_tool_interface_version(self, requested: int) -> int:
        """Read the protocol without publishing state during construction."""
        if type(requested) is not int or requested not in (1, 2):
            raise ValueError("unsupported tool interface version")
        contracts = [event["data"].get("version") for event in self._events
                     if event["type"] == "tools/contract"]
        if contracts:
            if any(type(version) is not int or version not in (1, 2)
                   for version in contracts) or len(set(contracts)) != 1:
                raise SessionLogIncompatible("session tool interface version is unsupported")
            return contracts[0]
        if self._bound_tool_interface_version is not None:
            return self._bound_tool_interface_version
        return requested if self._newly_created else 1

    def bind_tool_interface_version(self, requested: int) -> int:
        """Pin v2 before its first request; legacy logs remain backward readable."""
        version = self.resolve_tool_interface_version(requested)
        if version == 2 and not any(event["type"] == "tools/contract" for event in self._events):
            self.append("tools/contract", {"version": version})
            self.flush()
        self._bound_tool_interface_version = version
        return version

    def current_revision(self) -> str:
        """Read the revision without exposing or copying the event history."""
        from .session_history import current_revision

        return current_revision(self._events)

    def turn_binding(self, turn_id: str) -> dict[str, Any] | None:
        """Read one request reservation without copying the session history."""
        return next((dict(event["data"]) for event in self._events
                     if event["type"] == "turn/binding" and event["data"].get("turn_id") == turn_id), None)

    def has_committed_input(self, input_id: str) -> bool:
        # A retry may arrive on the ACP loop while the writer is inside fsync.
        # Only the successfully flushed prefix can acknowledge the earlier input.
        return any(event["type"] == "user/message" and
                   (event["data"].get("input_facts") or {}).get("id") == input_id
                   for event in self._active_events(through_seq=self._durable_event_count - 1))

    def has_turn_binding(self, turn_id: str) -> bool:
        """Check request idempotency without copying unrelated history."""
        return any(event["type"] == "turn/binding"
                   and event["data"].get("turn_id") == turn_id
                   for event in self._events)

    @property
    def event_count(self) -> int:
        """Return a stable exclusive cursor without copying session history."""
        return len(self._events)

    def event_page(self, start: int, *, limit: int = 64) -> tuple[dict[str, Any], ...]:
        """Read a detached bounded page; callers own their end cursor and flush."""
        if (type(start) is not int or not 0 <= start <= len(self._events)
                or type(limit) is not int or not 1 <= limit <= 256):
            raise ValueError("invalid session event page")
        if self._closed or self._failed:
            raise SessionLogDurabilityError("session log unavailable")
        return tuple(deepcopy(self._events[start:start + limit]))

    def events_of_type(self, event_type: str):
        """Iterate detached events of one type without copying unrelated payloads."""
        if self._closed or self._failed:
            raise SessionLogDurabilityError("session log unavailable")
        end = len(self._events)
        for index in range(end):
            event = self._events[index]
            if event["type"] == event_type:
                yield deepcopy(event)

    def next_turn_number(self) -> int:
        """Inspect recorded starts without copying history or reserving a turn."""

        turns = (
            event["data"].get("turn")
            for event in self._events
            if event["type"] == "turn/start"
        )
        return max((turn for turn in turns if isinstance(turn, int)), default=0) + 1

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
                _timed_fsync(handle.fileno())
            except BaseException:
                handle.close()
                raise
        except BaseException:
            _release_writer_lock(lock_handle)
            raise
        session = cls(path, header, [], handle, lock_handle)
        session._newly_created = True
        return session

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
        session, _ = cls._open(
            root, session_id=session_id, cwd=cwd, recover=recover, project=recover,
        )
        return session

    @classmethod
    def open_with_projection(
        cls,
        root: str | Path,
        *,
        session_id: str,
        cwd: str | Path,
        recover: bool = False,
    ) -> tuple["SessionLog", SessionProjection]:
        """Return the validated recovery state without replaying it a second time."""
        session, projection = cls._open(
            root, session_id=session_id, cwd=cwd, recover=recover, project=True,
        )
        assert projection is not None
        return session, projection

    @classmethod
    def _open(
        cls,
        root: str | Path,
        *,
        session_id: str,
        cwd: str | Path,
        recover: bool,
        project: bool,
    ) -> tuple["SessionLog", SessionProjection | None]:
        directory = _session_dir(Path(root), session_id)
        path = directory / "session.jsonl"
        lock_handle = _acquire_writer_lock(directory / ".writer.lock")
        try:
            handle = path.open("r+b")
            workspace_verified = False
            try:
                header_line = handle.readline()
                if not header_line.endswith(b"\n"):
                    raise SessionLogCorrupted(
                        "session log has no complete header"
                    )
                try:
                    header = json.loads(header_line)
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
                    raise SessionLogIncompatible("session log version is unsupported")
                committed_end = None
                events: list[dict[str, Any]] = []
                offset = len(header_line)
                for index, line in enumerate(handle, start=1):
                    if not line.endswith(b"\n"):
                        committed_end = offset
                        break
                    offset += len(line)
                    try:
                        event = json.loads(line)
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise SessionLogCorrupted(
                            f"session log record {index} is invalid JSON"
                        ) from exc
                    seq = index - 1
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
                        raise SessionLogIncompatible(
                            f"session log contains unknown required event {event_type!r}"
                        )
                    if not isinstance(event.get("data"), dict):
                        raise SessionLogCorrupted(
                            f"session log record {seq + 1} has invalid event data"
                        )
                    if event_type == "tools/contract":
                        version = event["data"].get("version")
                        if type(version) is not int or version not in (1, 2):
                            raise SessionLogIncompatible("session tool interface version is unsupported")
                        if any(previous["type"] == "tools/contract" and
                               previous["data"]["version"] != version for previous in events):
                            raise SessionLogIncompatible("session tool interface version cannot change")
                    events.append(event)
                session = cls(path, header, events, handle, lock_handle)
                projection = session.replay() if project else None
                if committed_end is not None:
                    handle.truncate(committed_end)
                    handle.flush()
                    _timed_fsync(handle.fileno())
                handle.seek(0, os.SEEK_END)
            except BaseException as exc:
                handle.close()
                if (recover and workspace_verified and isinstance(exc, SessionLogCorrupted)
                        and not isinstance(
                            exc, (SessionLogRevisionCorrupted, SessionLogIncompatible)
                        )):
                    session = cls._recover_log(path, session_id, cwd, lock_handle)
                    try:
                        return session, session.replay() if project else None
                    except BaseException:
                        session._handle.close()
                        raise
                raise
        except BaseException:
            _release_writer_lock(lock_handle)
            raise
        return session, projection

    @classmethod
    def _recover_log(
        cls,
        path: Path,
        session_id: str,
        cwd: str | Path,
        lock_handle: BinaryIO | WindowsSessionLock,
    ) -> "SessionLog":
        """Keep the original bytes before replacing a damaged runtime log.

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
        with path.open("rb") as source, archive.open("xb") as backup:
            shutil.copyfileobj(source, backup, length=1024 * 1024)
            backup.flush()
            _timed_fsync(backup.fileno())
        try:
            with temporary.open("xb") as replacement:
                replacement.write(_encode_record(header))
                replacement.flush()
                _timed_fsync(replacement.fileno())
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
        timestamp_ms: int | None = None,
    ) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("session log is closed")
        if self._failed:
            raise SessionLogDurabilityError("session log is unusable after an I/O failure")
        event: dict[str, Any] = {
            "type": event_type,
            "seq": len(self._events),
            "time": int(time.time() * 1000) if timestamp_ms is None else timestamp_ms,
            "data": data,
        }
        if ignorable:
            event["ignorable"] = True
        if surface_op is not None:
            event["surfaceOp"] = surface_op
        if source_event_seqs is not None:
            event["sourceEventSeqs"] = source_event_seqs
        # Keep facts used by the incremental Surface independent of callers.
        stored_event = event
        if event_type in _SURFACE_MESSAGE_TYPES or event_type == "history/revise":
            stored_event = deepcopy(event)
        encoded = _encode_record(stored_event)
        try:
            self._handle.write(encoded)
        except OSError as exc:
            self._failed = True
            raise SessionLogDurabilityError("session log append failed") from exc
        self._events.append(stored_event)
        if event_type == "history/revise":
            # Revisions can restore nodes removed by earlier replacements.
            self._surface_cursor = None
        return dict(event)

    def flush(self) -> None:
        if self._closed:
            raise RuntimeError("session log is closed")
        event_count = len(self._events)
        try:
            self._handle.flush()
            _timed_fsync(self._handle.fileno())
        except OSError as exc:
            self._failed = True
            raise SessionLogDurabilityError("session log flush failed") from exc
        self._durable_event_count = event_count

    def _message_metadata(self, message: Message) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("session log is closed")
        if self._failed:
            raise SessionLogDurabilityError("session log is unusable after an I/O failure")
        entry = self._pending_message_metadata.get(id(message))
        if entry is None:
            entry = (message, {})
            self._pending_message_metadata[id(message)] = entry
        return entry[1]

    def queue_message_images(self, message: Message, refs: list[dict[str, Any]]) -> None:
        """Attach references to this message's next event, outside its content."""
        if message.role not in {"user", "tool"}:
            raise ValueError("image references require a user or tool message")
        copied = deepcopy(refs)
        for ref in copied:
            if (not isinstance(ref, dict) or "data" in ref
                    or not isinstance(ref.get("imageId"), str) or not ref["imageId"]):
                raise ValueError("image references need an imageId and cannot contain inline data")
        if copied:
            metadata = self._message_metadata(message)
            queued = {ref["imageId"]: ref for ref in metadata.get("images", [])}
            for ref in copied:
                if ref["imageId"] in queued and queued[ref["imageId"]] != ref:
                    raise ValueError("conflicting image occurrence metadata")
                queued[ref["imageId"]] = ref
            metadata["images"] = list(queued.values())

    def mark_model_response(self, message: Message, round_number: int) -> None:
        """Persist the accepted inference round together with its response."""
        if message.role != "assistant":
            raise ValueError("model rounds require an assistant message")
        if isinstance(round_number, bool) or not isinstance(round_number, int) or round_number < 0:
            raise ValueError("model round must be a nonnegative integer")
        self._message_metadata(message)["modelRound"] = round_number

    def _append_message_event(
        self, event_type: str, data: dict[str, Any], *, message: Message,
        surface_op: str | dict[str, Any], source_event_seqs: list[int] | None = None,
    ) -> dict[str, Any]:
        pending = self._pending_message_metadata.get(id(message))
        from .input_context import requires_input_contract
        if (message.input_facts and requires_input_contract(message.input_facts)
                and not any(event["type"] == "input/contract" for event in self._events)):
            # Required feature marker: an old runtime must reject this log,
            # rather than silently dropping resource facts it cannot project.
            self.append("input/contract", {"schemaVersion": 1})
        if pending is not None:
            data.update(deepcopy(pending[1]))
        if event_type == "tool/result" and "result" not in data:
            from .kernel.tool_messages import is_interrupted_tool_reply

            if is_interrupted_tool_reply(message):
                data["error"] = {"name": "ToolOutcomeUnknownError", "code": TOOL_OUTCOME_UNKNOWN}
        context_usage = _context_usage_metadata(message)
        if context_usage is not None:
            data["contextUsage"] = context_usage
        event = self.append(
            event_type, data, surface_op=surface_op, source_event_seqs=source_event_seqs,
        )
        message._surface_seq = event["seq"]
        self._pending_message_metadata.pop(id(message), None)
        return event

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
        """Prepare an immutable snapshot, falling back to inline request content.

        After this succeeds, callers append the returned reference plus its
        request/range metadata to ``request/context`` and flush that event
        before calling the provider. Neither a trace nor the original Skill
        file is needed to reconstruct that request's reference text.
        Sidecar I/O or integrity failures return inlineContent for the same request/context
        commit; they must not poison the canonical session log.
        """

        if self._closed:
            raise RuntimeError("session log is closed")
        if self._failed:
            raise SessionLogDurabilityError("session log is unusable after an I/O failure")
        encoded = content.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        ref = {"contentRef": f"skill-references/{digest}.txt", "sha256": digest}
        temporary: Path | None = None
        try:
            path = self._skill_reference_path(ref)
            path.parent.mkdir(mode=0o700, exist_ok=True)
            if path.exists():
                # Never overwrite a corrupt snapshot or change an existing
                # inode. Re-sync a valid file before acknowledging reuse.
                self.read_skill_reference(ref)
                if os.name != "nt":
                    # Windows cannot fsync a read-only handle. This immutable
                    # file was already synced before its original publication.
                    with path.open("rb") as handle:
                        _timed_fsync(handle.fileno())
            else:
                fd, name = tempfile.mkstemp(prefix=".skill-reference-", dir=path.parent)
                temporary = Path(name)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    _timed_fsync(handle.fileno())
                try:
                    # Linking publishes an already-fsynced inode atomically
                    # without replacing any snapshot that appeared meanwhile.
                    os.link(temporary, path)
                except FileExistsError:
                    self.read_skill_reference(ref)
                    if os.name != "nt":
                        with path.open("rb") as handle:
                            _timed_fsync(handle.fileno())
                temporary.unlink()
                temporary = None
            if os.name != "nt":
                # Persist both the snapshot name and the newly-created
                # namespace. Windows does not support opening directory fds.
                for directory in (path.parent, self.path.parent):
                    directory_fd = os.open(directory, os.O_RDONLY)
                    try:
                        _timed_fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
        except (OSError, SessionLogCorrupted):
            _log.warning(
                "Skill reference snapshot unavailable; using inline request/context content",
                exc_info=True,
            )
            return {"inlineContent": content, "sha256": digest}
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
        Stored message events use the same validation as ``replay()``. Request
        measurements and other recovery state are not needed to compare messages.
        """

        nodes = self._surface_nodes()
        persisted_payloads = [
            message.model_dump(mode="json", exclude_none=True)
            for _, message in nodes
        ]
        live_payloads = [
            message.model_dump(mode="json", exclude_none=True) for message in messages
        ]
        if live_payloads[: len(persisted_payloads)] != persisted_payloads:
            if not messages:
                _log.error(
                    "session_log/surface_diverged session_id=%s persisted=%d live=0 "
                    "action=retain_persisted",
                    self.header.get("id"),
                    len(persisted_payloads),
                )
                return []
            _log.error(
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

        # Some callers supply newly projected objects for already logged inputs.
        for (seq, saved), message in zip(nodes, messages):
            message._surface_seq = seq
            if saved._legacy_compaction_append:
                message._legacy_compaction_append = True
        appended: list[dict[str, Any]] = []
        for message, payload in zip(
            messages[len(persisted_payloads) :],
            live_payloads[len(persisted_payloads) :],
        ):
            if message.role == "user":
                payload["source"] = message.source
                appended.append(
                    self._append_message_event(
                        "user/message",
                        payload,
                        message=message,
                        surface_op="append",
                    )
                )
            elif message.role == "assistant":
                appended.append(
                    self._append_message_event(
                        "assistant/message",
                        {"turn": turn, "step": step, "message": payload},
                        message=message,
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
                    self._append_message_event(
                        "tool/result",
                        data,
                        message=message,
                        surface_op="append",
                    )
                )
        return appended

    def _surface_nodes(self) -> list[tuple[int, Message]]:
        end = len(self._events)
        if self._surface_cursor == end:
            return self._surface
        if self._surface_cursor is None:
            surface: list[tuple[int, Message]] = []
            events = self._active_events()
            compaction_format = None
        else:
            surface = self._surface
            events = self._events[self._surface_cursor:end]
            compaction_format = self._surface_compaction_format

        # A partial refresh after corruption must not be reused.
        self._surface_cursor = None
        for event in events:
            compaction_format = _compaction_format(event, compaction_format)
            if event["type"] == "surface/reset":
                surface.clear()
            elif event["type"] in _SURFACE_MESSAGE_TYPES:
                _apply_surface_message(surface, event, compaction_format=compaction_format)
        self._surface = surface
        self._surface_cursor = end
        self._surface_compaction_format = compaction_format
        return surface

    def history_messages(
        self, *, events: list[dict[str, Any]] | None = None,
        checkpoint: Callable[[], None] | None = None,
    ) -> list[tuple[int, Message, dict[str, Any]]]:
        """Replay original effective history, excluding model compaction copies."""
        history = []
        compaction_format = None
        if events is None:
            events = self._active_events(checkpoint=checkpoint)
        for event in events:
            if checkpoint is not None:
                checkpoint()
            kind, data = event["type"], event["data"]
            compaction_format = _compaction_format(event, compaction_format)
            if kind == "surface/reset":
                history.clear()
            elif (compaction_format != 1 and kind in _SURFACE_MESSAGE_TYPES
                  and event.get("surfaceOp") == "append" and not data.get("surfaceDerived")):
                data = deepcopy(data)
                payload = data if kind == "user/message" else data["message"]
                if payload.get("compaction_node") is None:
                    history.append((event["seq"], Message.model_validate(payload), data))
        return history

    def surface_sequences(self, messages: list[Message]) -> tuple[int, ...]:
        """Resolve a projected history prefix to durable seqs, or omit an ambiguous mapping."""
        nodes = self._surface_nodes()
        if len(nodes) > len(messages):
            return ()
        for (_, saved), live in zip(nodes, messages):
            if saved.role != live.role or saved.source != live.source:
                return ()
            if saved.input_facts and saved.input_facts == live.input_facts:
                continue  # Host projection can render the same immutable user input differently.
            if saved.model_dump() != live.model_dump():
                return ()
        return tuple(seq for seq, _ in nodes)

    def surface_message_sequence(self, messages: list[Message], target: Message) -> int | None:
        """Resolve a live message by position, never by matching its text alone."""
        positions = [index for index, message in enumerate(messages) if message is target]
        if len(positions) != 1:
            raise ValueError("message is not uniquely present in the live surface")
        nodes = self._surface_nodes()
        common = min(len(nodes), len(messages))
        if len(nodes) > len(messages) or any(
            nodes[index][1].model_dump(mode="json", exclude_none=True)
            != messages[index].model_dump(mode="json", exclude_none=True)
            for index in range(common)
        ):
            raise ValueError("session surface diverged")
        index = positions[0]
        return nodes[index][0] if index < len(nodes) else None

    def _append_surface_message(
        self,
        message: Message,
        *,
        turn: int,
        step: int,
        surface_op: str | dict[str, Any],
        source_event_seqs: list[int] | None = None,
        surface_derived: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = message.model_dump(mode="json", exclude_none=True)
        metadata = dict(metadata or {})
        if surface_derived:
            metadata["surfaceDerived"] = True
        if message.role == "user":
            payload["source"] = message.source
            return self._append_message_event(
                "user/message",
                {**payload, **metadata},
                message=message,
                surface_op=surface_op,
                source_event_seqs=source_event_seqs,
            )
        if message.role == "assistant":
            return self._append_message_event(
                "assistant/message",
                {"turn": turn, "step": step, "message": payload, **metadata},
                message=message,
                surface_op=surface_op,
                source_event_seqs=source_event_seqs,
            )
        if message.role == "tool":
            return self._append_message_event(
                "tool/result",
                {"turn": turn, "step": step, "message": payload, **metadata},
                message=message,
                surface_op=surface_op,
                source_event_seqs=source_event_seqs,
            )
        raise ValueError(f"role {message.role!r} cannot enter the Session Surface")

    def replace_surface_message(
        self, index: int, message: Message, *, turn: int, step: int, edit: dict[str, Any],
    ) -> dict[str, Any]:
        """Replace one replay-visible node while keeping the original in host history."""
        nodes = self._surface_nodes()
        if type(index) is not int or not 0 <= index < len(nodes):
            raise SessionLogCorrupted(f"surface edit index {index!r} is outside the active surface")
        return self._append_surface_message(
            message, turn=turn, step=step, **self._surface_edit_args(nodes[index], message, edit),
        )

    def _surface_edit_args(
        self, node: tuple[int, Message], message: Message, edit: dict[str, Any],
    ) -> dict[str, Any]:
        seq, previous = node
        original = self._events[seq]
        event_types = {"user": "user/message", "assistant": "assistant/message", "tool": "tool/result"}
        if (original["type"] != event_types.get(message.role) or previous.role != message.role
                or previous.tool_call_id != message.tool_call_id or previous.name != message.name):
            raise SessionLogCorrupted(f"surface edit does not match node {seq}")
        # Images and context measurements belong to the message. Execution
        # facts such as raw results and accepted model rounds stay on their
        # original event, where replay processes each occurrence exactly once.
        data = original["data"]
        metadata = {key: data[key] for key in ("images", "contextUsage") if key in data}
        return {
            "surface_op": {"op": "replace", "start": seq, "end": seq},
            "source_event_seqs": [seq], "surface_derived": True, "metadata": {**metadata, "edit": edit},
        }

    def commit_compaction(self, plan: CompactionPlan, *, turn: int, step: int) -> None:
        """Append validated edits, a single prefix replacement, and generated messages."""
        nodes = self._surface_nodes()
        seqs = tuple(seq for seq, _ in nodes)
        if seqs != plan.source_seqs:
            raise SessionLogCorrupted("compaction source does not match the current surface")
        # The plan indexes the original history, including its system message.
        if type(plan.cut_index) is not int or not 2 <= plan.cut_index <= len(nodes) + 1:
            raise SessionLogCorrupted("compaction cut must cover a nonempty surface prefix")
        cut = plan.cut_index - 1
        if plan.cut_seq != (seqs[cut] if cut < len(nodes) else None):
            raise SessionLogCorrupted("compaction cut does not name the first retained node")
        if plan.head.role != "user" or plan.head.compaction_node not in ("summary_head", "fallback_head"):
            raise SessionLogCorrupted("compaction replacement has no valid head")
        if any(msg.compaction_node != "appended" or msg.role not in ("user", "assistant", "tool")
               for msg in plan.appends):
            raise SessionLogCorrupted("invalid compaction append")
        # Validate every edit before writing the first one. seq order can
        # differ from surface order after earlier single-node replacements.
        retained = dict(nodes[cut:])
        if not plan.rewrites.keys() <= retained.keys():
            raise SessionLogCorrupted("compaction rewrite targets a node outside the retained suffix")
        edits = [
            (msg, self._surface_edit_args((seq, retained[seq]), msg, {"reason": "compaction"}))
            for seq, msg in plan.rewrites.items()
        ]
        for message, args in edits:
            self._append_surface_message(message, turn=turn, step=step, **args)
        self._append_surface_message(
            plan.head, turn=turn, step=step,
            surface_op={"op": "replace", "start": seqs[0], "end": seqs[cut - 1]},
            source_event_seqs=list(seqs[:cut]),
        )
        for message in plan.appends:
            self._append_surface_message(message, turn=turn, step=step, surface_op="append")

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
        # Match durable fields. Submission markers and restored usage estimates
        # are private, and may differ between a live message and its log node.
        old_payloads = [(msg.source, msg.model_dump()) for _, msg in old_nodes]

        def retained(message: Message) -> bool:
            return (message.source, message.model_dump()) in old_payloads

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
                    surface_derived=retained(messages[0]),
                )
            )
            remaining = messages[1:]
        else:
            remaining = messages
        # Older store adapters can delegate a whole compacted view here. Its
        # receipts and expired usage still describe copies of original UI rows.
        compacted_tail = (messages[0]._compacted_tail_length or 0) if old_nodes else 0
        for index, message in enumerate(remaining):
            appended.append(
                self._append_surface_message(
                    message,
                    turn=turn,
                    step=step,
                    surface_op="append",
                    surface_derived=index < compacted_tail or retained(message),
                )
            )
        return appended

    def reset_surface(self, *, reason: str) -> dict[str, Any]:
        """Persist a logical conversation reset without deleting audit history."""

        event = self.append("surface/reset", {"reason": reason})
        self.flush()
        self._pending_message_metadata.clear()
        return event

    def _active_events(
        self, *, through_seq: int | None = None,
        checkpoint: Callable[[], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Return the effective timeline, optionally at an earlier checkpoint."""
        from .session_history import effective_events

        events = self._events if through_seq is None else self._events[:through_seq + 1]
        try:
            return effective_events(events, checkpoint=checkpoint)
        except (KeyError, TypeError, ValueError) as exc:
            raise SessionLogRevisionCorrupted("invalid history revision") from exc

    def replay(self, *, through_seq: int | None = None) -> SessionProjection:
        surface: list[tuple[int, Message]] = []
        goal: dict[str, Any] | None = None
        plan: dict[str, Any] | None = None
        todos: list[dict[str, Any]] = []
        skills: list[dict[str, Any]] = []
        images: dict[str, dict[str, Any]] = {}
        model_round = 0
        latest_image_request: tuple[int, list[str], list[str]] | None = None
        compaction_format = None
        for event in self._active_events(through_seq=through_seq):
            event_type = event.get("type")
            data = event["data"]
            compaction_format = _compaction_format(event, compaction_format)
            if event_type == "surface/reset":
                surface.clear()
                images.clear()
                latest_image_request = None
                continue
            if event_type == "request/header":
                mark_submitted_tool_results(message for _, message in reversed(surface))
                continue
            if event_type in {"user/message", "tool/result"}:
                references = data.get("images")
                if event_type == "tool/result" and "images" not in data:
                    # Old CUA logs already carry structured, content-addressed
                    # references. Never infer attachments from message paths.
                    result = data.get("result")
                    raw_output = result.get("rawOutput") if isinstance(result, dict) else None
                    legacy = raw_output.get("mcp_image_references") if isinstance(raw_output, dict) else None
                    references = []
                    for index, ref in enumerate(legacy if isinstance(legacy, list) else ()):
                        if not isinstance(ref, dict) or not isinstance(ref.get("contentRef"), str):
                            continue
                        copied = {key: ref[key] for key in (
                            "contentRef", "sha256", "media_type", "source_bytes", "width", "height",
                        ) if key in ref}
                        copied.update(imageId=f"legacy-cua-{event['seq']}-{index}", source="cua",
                                      createdRound=model_round + 1,
                                      eligible=ref.get("image_context_eligible", True) is True)
                        size = copied.get("source_bytes")
                        if type(size) is int and size >= 0:
                            copied["base64Bytes"] = 4 * ((size + 2) // 3)
                        message_data = data.get("message")
                        if isinstance(message_data, dict):
                            copied["toolCallId"] = message_data.get("tool_call_id")
                            copied["toolName"] = message_data.get("name")
                        references.append(copied)
                for ref in references if isinstance(references, list) else ():
                    if not isinstance(ref, dict):
                        continue
                    image_id = ref.get("imageId")
                    if not isinstance(image_id, str) or not image_id or image_id in images:
                        continue
                    was_evicted = ref.get("evicted") is True
                    images[image_id] = {
                        **deepcopy(ref), "active": (ref.get("eligible", True) is not False
                                                   and ref.get("active", True) is not False
                                                   and not was_evicted),
                        "evicted": was_evicted, "messageSeq": event["seq"],
                        "delivered": ref.get("delivered") is True,
                    }
            if event_type == "assistant/message":
                recorded_round = data.get("modelRound")
                if (isinstance(recorded_round, int) and not isinstance(recorded_round, bool)
                        and recorded_round >= 0):
                    if (recorded_round > model_round and latest_image_request is not None
                            and latest_image_request[0] == recorded_round):
                        for image_id in latest_image_request[1]:
                            if image_id in images:
                                images[image_id]["delivered"] = True
                                images[image_id].setdefault("firstDeliveredRound", recorded_round)
                                images[image_id]["lastDeliveredRound"] = recorded_round
                        for image_id in latest_image_request[2]:
                            if image_id in images:
                                images[image_id]["noticeDelivered"] = True
                    model_round = max(model_round, recorded_round)
                    latest_image_request = None
            if event_type == "request/context":
                image_context = data.get("imageContext")
                is_summary = isinstance(image_context, dict) and image_context.get("purpose") == "summary"
                if not is_summary:
                    latest_image_request = None
                if isinstance(image_context, dict) and not is_summary:
                    baseline = image_context.get("modelRoundBaseline")
                    if (image_context.get("phase") == "continuation" and model_round == 0
                            and type(baseline) is int and baseline > 0):
                        model_round = baseline
                    request_round = image_context.get(
                        "requestRound", image_context.get("round", image_context.get("modelRound")),
                    )
                    selected = image_context.get("selected")
                    notices = image_context.get("noticeImageIds")
                    if (isinstance(request_round, int) and not isinstance(request_round, bool)
                            and (isinstance(selected, list) or isinstance(notices, list))):
                        selected_ids = [
                            item.get("imageId", item.get("id")) if isinstance(item, dict) else item
                            for item in (selected if isinstance(selected, list) else ())
                        ]
                        latest_image_request = (
                            request_round, [value for value in selected_ids if isinstance(value, str)],
                            [value for value in notices if isinstance(value, str)]
                            if isinstance(notices, list) else [],
                        )
                evicted = image_context.get("evicted") if isinstance(image_context, dict) else None
                for item in evicted if isinstance(evicted, list) else ():
                    image_id = item.get("imageId", item.get("id")) if isinstance(item, dict) else item
                    if isinstance(image_id, str) and image_id in images:
                        images[image_id].update({"active": False, "evicted": True})
                        if isinstance(item, dict) and isinstance(item.get("reason"), str):
                            images[image_id]["evictionReason"] = item["reason"]
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
            if event_type in _SURFACE_MESSAGE_TYPES:
                message = _apply_surface_message(surface, event, compaction_format=compaction_format)
                _restore_context_usage(message, data.get("contextUsage"))
        messages = [message for _, message in surface]
        latest_snapshot_seen = False
        for message in reversed(messages):
            if message.usage is not None and message.usage._request_snapshot is not None:
                if latest_snapshot_seen:
                    message.usage._request_snapshot = None
                    message.usage._request_route_unverified = True
                latest_snapshot_seen = True
        return SessionProjection(
            messages=messages,
            goal=goal,
            plan=plan,
            todos=todos,
            skills=skills,
            images=list(images.values()),
            model_round=model_round,
        )

    def repair_interrupted_turn(self) -> list[dict[str, Any]]:
        """Append deterministic normal events that close one crash-open turn."""

        open_turn: int | None = None
        open_step: int | None = None
        pending_calls: dict[str, dict[str, Any]] = {}
        for event in self._active_events():
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

    def prepare_resume_projection(self, projection: SessionProjection) -> SessionProjection:
        """Reuse validated state unless crash repair appended new session facts."""
        appended = self.prepare_resume()
        if any(event["type"] not in {"step/end", "turn/end", "session/end-seed"}
               for event in appended):
            return self.replay()
        return projection

    def close(self) -> None:
        if self._closed:
            return
        self._pending_message_metadata.clear()
        try:
            self._handle.close()
        finally:
            try:
                _release_writer_lock(self._lock_handle)
            finally:
                self._closed = True

"""Best-effort per-session JSONL execution traces.

The trace path is deliberately independent from the human-readable agent log.
Trace failures are swallowed so observability can never change agent behavior.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

from .llm.debug_logging import sanitize_for_logging

from box_agent.user_paths import state_path

logger = logging.getLogger(__name__)

_TRACE_SCHEMA_VERSION = "box-agent-session-trace/v1"
_TRACE_CONTEXT: ContextVar[tuple["SessionTraceWriter", str] | None] = ContextVar(
    "box_agent_session_trace", default=None
)
_WRITE_LOCK = threading.Lock()
_RETENTION_LOCK = threading.Lock()
_LAST_RETENTION_CHECK: dict[str, float] = {}
_FALSE_VALUES = {"0", "false", "no", "off"}
_SAFE_FILE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_T = TypeVar("_T")

_DEFAULT_RETENTION_DAYS = 7
_DEFAULT_MAX_TOTAL_BYTES = 512 * 1024 * 1024
_DEFAULT_CLEANUP_INTERVAL_SECONDS = 6 * 60 * 60
_PROTECTED_RECENT_FILES = 2
_ACTIVE_FILE_GRACE_SECONDS = 24 * 60 * 60
_SECONDS_PER_DAY = 24 * 60 * 60


def session_trace_enabled() -> bool:
    """Return whether durable session traces are enabled.

    Tracing is on by default for product diagnostics. It can be disabled for
    privacy-sensitive environments with ``BOX_AGENT_SESSION_TRACE_ENABLED=0``.
    """

    value = os.environ.get("BOX_AGENT_SESSION_TRACE_ENABLED")
    return value is None or value.strip().lower() not in _FALSE_VALUES


def default_session_trace_dir() -> Path:
    configured = os.environ.get("BOX_AGENT_SESSION_TRACE_DIR", "").strip()
    if configured:
        return state_path("log/sessions", configured)
    return state_path('log/sessions')


def session_trace_retention_enabled() -> bool:
    """Return whether best-effort trace retention is enabled."""

    value = os.environ.get("BOX_AGENT_SESSION_TRACE_RETENTION_ENABLED")
    return value is None or value.strip().lower() not in _FALSE_VALUES


def _non_negative_env_int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return max(0, int(value))
    except ValueError:
        logger.debug("invalid session trace retention setting: %s=%r", name, value)
        return default


@dataclass(frozen=True)
class _TraceFileSnapshot:
    path: Path
    size: int
    mtime: float
    mtime_ns: int
    inode: int


def _trace_file_snapshots(trace_dir: Path) -> list[_TraceFileSnapshot]:
    snapshots: list[_TraceFileSnapshot] = []
    try:
        candidates = list(trace_dir.iterdir())
    except OSError:
        return snapshots

    for candidate in candidates:
        try:
            if candidate.is_symlink() or not candidate.is_file() or candidate.suffix != ".jsonl":
                continue
            stat = candidate.stat()
        except OSError:
            continue
        snapshots.append(
            _TraceFileSnapshot(
                path=candidate,
                size=stat.st_size,
                mtime=stat.st_mtime,
                mtime_ns=stat.st_mtime_ns,
                inode=stat.st_ino,
            )
        )
    return snapshots


def _unlink_unchanged_trace(snapshot: _TraceFileSnapshot) -> bool:
    """Delete a trace only if it has not changed since the cleanup scan."""

    try:
        current = snapshot.path.stat()
        if (
            current.st_ino != snapshot.inode
            or current.st_size != snapshot.size
            or current.st_mtime_ns != snapshot.mtime_ns
        ):
            return False
        snapshot.path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        logger.debug("session trace cleanup failed for %s", snapshot.path, exc_info=True)
        return False


def cleanup_session_traces(
    trace_dir: str | Path,
    *,
    current_file: str | Path | None = None,
    now: float | None = None,
    retention_days: int | None = None,
    max_total_bytes: int | None = None,
    active_file_grace_seconds: int = _ACTIVE_FILE_GRACE_SECONDS,
    protected_recent_files: int = _PROTECTED_RECENT_FILES,
) -> list[Path]:
    """Remove whole inactive trace files without changing the JSONL contract.

    The current append target, recently modified files, and the newest product
    sessions are protected. A file is re-checked immediately before deletion so
    a concurrent append causes cleanup to skip it.
    """

    directory = Path(trace_dir)
    snapshots = _trace_file_snapshots(directory)
    if not snapshots:
        return []

    current_time = time.time() if now is None else now
    configured_days = (
        _non_negative_env_int(
            "BOX_AGENT_SESSION_TRACE_RETENTION_DAYS", _DEFAULT_RETENTION_DAYS
        )
        if retention_days is None
        else max(0, retention_days)
    )
    configured_max_bytes = (
        _non_negative_env_int(
            "BOX_AGENT_SESSION_TRACE_MAX_TOTAL_BYTES", _DEFAULT_MAX_TOTAL_BYTES
        )
        if max_total_bytes is None
        else max(0, max_total_bytes)
    )
    newest_first = sorted(snapshots, key=lambda item: (-item.mtime_ns, item.path.name))
    protected_paths = {
        item.path for item in newest_first[: max(0, protected_recent_files)]
    }
    if current_file is not None:
        protected_paths.add(Path(current_file))
    if active_file_grace_seconds > 0:
        active_cutoff = current_time - active_file_grace_seconds
        protected_paths.update(item.path for item in snapshots if item.mtime >= active_cutoff)

    deleted: list[Path] = []
    deleted_paths: set[Path] = set()

    def delete(snapshot: _TraceFileSnapshot) -> None:
        if snapshot.path in protected_paths or snapshot.path in deleted_paths:
            return
        if _unlink_unchanged_trace(snapshot):
            deleted.append(snapshot.path)
            deleted_paths.add(snapshot.path)

    oldest_first = list(reversed(newest_first))
    if configured_days > 0:
        expiry_cutoff = current_time - configured_days * _SECONDS_PER_DAY
        for snapshot in oldest_first:
            if snapshot.mtime < expiry_cutoff:
                delete(snapshot)

    if configured_max_bytes > 0:
        remaining_bytes = sum(
            snapshot.size for snapshot in snapshots if snapshot.path not in deleted_paths
        )
        for snapshot in oldest_first:
            if remaining_bytes <= configured_max_bytes:
                break
            if snapshot.path in protected_paths or snapshot.path in deleted_paths:
                continue
            if _unlink_unchanged_trace(snapshot):
                deleted.append(snapshot.path)
                deleted_paths.add(snapshot.path)
                remaining_bytes -= snapshot.size

    return deleted


def _maybe_cleanup_session_traces(*, trace_dir: Path, current_file: Path) -> None:
    if not session_trace_retention_enabled():
        return
    try:
        interval = _non_negative_env_int(
            "BOX_AGENT_SESSION_TRACE_CLEANUP_INTERVAL_SECONDS",
            _DEFAULT_CLEANUP_INTERVAL_SECONDS,
        )
        key = str(trace_dir.absolute())
        checked_at = time.monotonic()
        with _RETENTION_LOCK:
            previous = _LAST_RETENTION_CHECK.get(key)
            if previous is not None and interval > 0 and checked_at - previous < interval:
                return
            _LAST_RETENTION_CHECK[key] = checked_at
            deleted = cleanup_session_traces(trace_dir, current_file=current_file)
        if deleted:
            logger.debug("removed %d expired session trace files", len(deleted))
    except Exception:
        logger.debug("session trace retention failed", exc_info=True)


def safe_session_trace_name(session_id: str) -> str:
    """Build a stable filename without changing the recorded session id."""

    normalized = session_id.strip() or "unknown-session"
    safe = _SAFE_FILE_CHARS.sub("_", normalized).strip("._-")
    if not safe:
        safe = "session"
    if safe != normalized or len(safe) > 120:
        safe = f"{safe[:100]}-{sha256(normalized.encode('utf-8')).hexdigest()[:12]}"
    return f"{safe}.jsonl"


def _trace_value(value: Any) -> Any:
    """Return a full JSON-safe value while redacting credential-like keys."""

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            result.update(sanitize_for_logging({str(key): _trace_value(item)}))
        return result
    if isinstance(value, list | tuple):
        return [_trace_value(item) for item in value]
    if hasattr(value, "model_dump"):
        try:
            return sanitize_for_logging(value.model_dump(exclude_none=True))
        except Exception:
            pass
    if hasattr(value, "to_schema"):
        try:
            return sanitize_for_logging(value.to_schema())
        except Exception:
            pass
    return sanitize_for_logging(value)


class SessionTraceWriter:
    """Append standalone JSON records to one file for one product session."""

    def __init__(
        self,
        *,
        session_id: str,
        acp_session_id: str,
        trace_dir: str | Path | None = None,
        enabled: bool | None = None,
    ) -> None:
        self.session_id = session_id.strip() or acp_session_id.strip() or "unknown-session"
        self.acp_session_id = acp_session_id.strip()
        self.enabled = session_trace_enabled() if enabled is None else enabled
        self.trace_dir = Path(trace_dir) if trace_dir is not None else default_session_trace_dir()
        self.file_path = self.trace_dir / safe_session_trace_name(self.session_id)

    def write(
        self,
        event: str,
        *,
        turn_id: str = "",
        step: int | None = None,
        llm_call_id: str | None = None,
        tool_call_id: str | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        """Append one trace record; never raise into the caller."""

        if not self.enabled:
            return
        try:
            record: dict[str, Any] = {
                "schema_version": _TRACE_SCHEMA_VERSION,
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
                    "+00:00", "Z"
                ),
                "event": event,
                "session_id": self.session_id,
                "acp_session_id": self.acp_session_id,
                "turn_id": turn_id,
            }
            if step is not None:
                record["step"] = step
            if llm_call_id:
                record["llm_call_id"] = llm_call_id
            if tool_call_id:
                record["tool_call_id"] = tool_call_id
            if data is not None:
                record["data"] = _trace_value(dict(data))

            line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)
            with _WRITE_LOCK:
                self.trace_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                descriptor = os.open(
                    self.file_path,
                    os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                    0o600,
                )
                with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                    handle.flush()
            _maybe_cleanup_session_traces(
                trace_dir=self.trace_dir,
                current_file=self.file_path,
            )
        except Exception:
            logger.debug("session trace write failed", exc_info=True)


def set_session_trace_writer(
    writer: SessionTraceWriter,
    *,
    turn_id: str,
) -> Token[tuple[SessionTraceWriter, str] | None]:
    """Bind a writer to the current turn and inherited async tasks."""

    return _TRACE_CONTEXT.set((writer, turn_id))


def reset_session_trace_writer(token: Token[tuple[SessionTraceWriter, str] | None]) -> None:
    _TRACE_CONTEXT.reset(token)


@dataclass
class SessionTraceTurn:
    """The observed result of a host-owned turn, not execution policy."""

    content: str = ""
    stop_reason: str | None = None
    turn_id: str = ""


@contextmanager
def traced_session_turn(
    writer: SessionTraceWriter,
    *,
    content: str,
) -> Iterator[SessionTraceTurn]:
    """Record one turn around awaited work without changing its result.

    The caller supplies the final content and internal stop reason. Bind in
    the task that owns this scope (before creating any run task), never across
    an async-generator yield. Escaped exceptions keep their original behavior;
    normal cancellation is not reported as an execution error.
    """

    turn_id = f"turn-{uuid4().hex}"
    result = SessionTraceTurn(turn_id=turn_id)
    started_at = time.perf_counter()
    writer.write("turn.input", turn_id=turn_id, data={"content": content})
    token = set_session_trace_writer(writer, turn_id=turn_id)
    error: dict[str, Any] | None = None
    try:
        yield result
    except (asyncio.CancelledError, KeyboardInterrupt):
        result.stop_reason = "cancelled"
        raise
    except BaseException as exc:
        result.stop_reason = "error"
        error = {"message": str(exc), "error_type": type(exc).__name__, "unexpected": True}
        raise
    finally:
        # Reset even when rendering, cancellation or an unexpected host error
        # exits the turn. A surrounding caller's context must remain intact.
        reset_session_trace_writer(token)
        if result.stop_reason == "error":
            writer.write(
                "turn.error", turn_id=turn_id,
                data=error or {"message": result.content.strip() or "Agent execution failed."},
            )
        writer.write(
            "turn.output", turn_id=turn_id,
            data={"content": result.content, "stop_reason": result.stop_reason},
        )
        writer.write(
            "turn.end", turn_id=turn_id,
            data={
                "stop_reason": result.stop_reason,
                "duration_ms": max(0, int((time.perf_counter() - started_at) * 1000)),
            },
        )


def emit_session_trace(
    event: str,
    *,
    turn_id: str = "",
    step: int | None = None,
    llm_call_id: str | None = None,
    tool_call_id: str | None = None,
    data: Mapping[str, Any] | None = None,
) -> None:
    """Write through the active trace context, if one exists."""

    context = _TRACE_CONTEXT.get()
    if context is None:
        return
    writer, context_turn_id = context
    writer.write(
        event,
        turn_id=turn_id.strip() or context_turn_id,
        step=step,
        llm_call_id=llm_call_id,
        tool_call_id=tool_call_id,
        data=data,
    )


async def scoped_session_trace(
    events: AsyncIterator[_T],
    *,
    writer: SessionTraceWriter,
    turn_id: str,
) -> AsyncIterator[_T]:
    """Iterate events with tracing bound, preserving the original event stream.

    Bind only while advancing or closing the wrapped iterator.  Keeping a
    ``ContextVar`` token active across ``yield`` is unsafe because Python may
    finalize an abandoned async generator in a different task context.
    """

    iterator = events.__aiter__()
    try:
        while True:
            token = set_session_trace_writer(writer, turn_id=turn_id)
            try:
                event = await anext(iterator)
            except StopAsyncIteration:
                return
            finally:
                reset_session_trace_writer(token)
            yield event
    finally:
        close = getattr(iterator, "aclose", None)
        if close is not None:
            token = set_session_trace_writer(writer, turn_id=turn_id)
            try:
                await close()
            finally:
                reset_session_trace_writer(token)

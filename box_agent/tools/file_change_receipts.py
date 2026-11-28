"""Receipts for bytes committed by Box-owned writers, never workspace observations."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar, Token
import hashlib
import os
import stat
import sys
import tempfile
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .base import ToolInvocationContext, ToolInvocationRejected, ToolResult, check_tool_cancellation
from ..user_paths import state_path

MAX_CHECKPOINT_FILE_BYTES = 50 * 1024 * 1024
_CHECKPOINT_IGNORED_SEGMENTS = frozenset({'.box-agent', '.box-agent-scratch'})
_CHECKPOINT_IGNORED_ROOT_SEGMENTS = frozenset({'.box-agent-staging'})


@dataclass(frozen=True, slots=True)
class FileWriteInvocationContext(ToolInvocationContext):
    validate_write_target: Callable[[Path], ToolResult | None] | None = None


def _is_case_insensitive_dir(directory: Path) -> bool:
    if os.path.normcase('A') == 'a':
        return True
    if sys.platform != 'darwin':
        return False
    name = directory.name
    swapped = name.swapcase()
    if swapped == name:
        return True
    try:
        return os.path.samefile(directory, directory.with_name(swapped))
    except OSError:
        return False


@dataclass
class ChangeCheckpointTarget:
    """Host-provided per-turn checkpoint directory for on-demand before-write snapshots."""

    checkpoint_dir: Path
    workspace_dir: Path
    max_file_bytes: int = MAX_CHECKPOINT_FILE_BYTES
    case_insensitive: bool = False
    recorded_paths: set[str] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)


_ACTIVE_CHECKPOINT: ContextVar[ChangeCheckpointTarget | None] = ContextVar(
    'box_agent_change_checkpoint',
    default=None,
)


def set_change_checkpoint(
    target: ChangeCheckpointTarget | None,
) -> Token[ChangeCheckpointTarget | None]:
    return _ACTIVE_CHECKPOINT.set(target)


def reset_change_checkpoint(token: Token[ChangeCheckpointTarget | None]) -> None:
    _ACTIVE_CHECKPOINT.reset(token)


def get_change_checkpoint() -> ChangeCheckpointTarget | None:
    return _ACTIVE_CHECKPOINT.get()


def parse_change_checkpoint_target_with_reason(
    meta: Any,
    workspace_dir: str | Path,
) -> tuple[ChangeCheckpointTarget | None, str | None]:
    """Validate optional host-supplied change_checkpoint metadata for a single turn.

    Returns ``(target, reject_reason)``. ``reject_reason`` is ``None`` when
    ``change_checkpoint`` is omitted or valid, and a non-sensitive reason code
    when an explicitly supplied ``change_checkpoint`` payload is rejected.
    """
    if not isinstance(meta, dict) or 'change_checkpoint' not in meta or meta['change_checkpoint'] is None:
        return None, None
    raw = meta.get('change_checkpoint')
    if not isinstance(raw, dict):
        return None, 'invalid_payload'
    if type(raw.get('version')) is not int or raw['version'] != 1:
        return None, 'unsupported_version'
    raw_dir = raw.get('checkpoint_dir')
    if not isinstance(raw_dir, str) or not raw_dir.strip():
        return None, 'empty_dir'
    try:
        candidate = Path(raw_dir)
        if not candidate.is_absolute():
            return None, 'relative_dir'
        if candidate.is_symlink():
            return None, 'symlink_dir'
        if not candidate.is_dir():
            return None, 'missing_or_not_dir'
        resolved_checkpoint = candidate.resolve()
        resolved_workspace = Path(workspace_dir).resolve()
        if (
            resolved_checkpoint == resolved_workspace
            or resolved_checkpoint.is_relative_to(resolved_workspace)
            or resolved_workspace.is_relative_to(resolved_checkpoint)
        ):
            return None, 'overlaps_workspace'
    except (OSError, RuntimeError, ValueError):
        return None, 'resolve_failed'
    raw_max = raw.get('max_file_bytes')
    max_file_bytes = (
        raw_max
        if type(raw_max) is int and 0 < raw_max <= MAX_CHECKPOINT_FILE_BYTES
        else MAX_CHECKPOINT_FILE_BYTES
    )
    return (
        ChangeCheckpointTarget(
            checkpoint_dir=resolved_checkpoint,
            workspace_dir=resolved_workspace,
            max_file_bytes=max_file_bytes,
            case_insensitive=_is_case_insensitive_dir(resolved_workspace),
        ),
        None,
    )


def parse_change_checkpoint_target(
    meta: Any,
    workspace_dir: str | Path,
) -> ChangeCheckpointTarget | None:
    """Validate optional host-supplied change_checkpoint metadata for a single turn."""
    target, _ = parse_change_checkpoint_target_with_reason(meta, workspace_dir)
    return target


def _checkpoint_relative_path(checkpoint: ChangeCheckpointTarget, target: Path) -> str | None:
    try:
        resolved = target.resolve()
        rel = resolved.relative_to(checkpoint.workspace_dir)
    except (OSError, RuntimeError, ValueError):
        return None
    parts = rel.parts
    if (
        not parts
        or parts[0] in _CHECKPOINT_IGNORED_ROOT_SEGMENTS
        or any(part in ('', '.', '..') or part in _CHECKPOINT_IGNORED_SEGMENTS for part in parts)
    ):
        return None
    return rel.as_posix()


def _checkpoint_path_key(checkpoint: ChangeCheckpointTarget, relative: str) -> str:
    return relative.casefold() if checkpoint.case_insensitive else os.path.normcase(relative)


def _prepare_before_destination(checkpoint: ChangeCheckpointTarget, relative: str) -> Path | None:
    before_root = checkpoint.checkpoint_dir / 'before'
    if before_root.is_symlink():
        return None
    before_root.mkdir(exist_ok=True)
    resolved_before_root = before_root.resolve()
    parts = relative.split('/')
    current = before_root
    for segment in parts[:-1]:
        current = current / segment
        if current.is_symlink():
            return None
        current.mkdir(exist_ok=True)
    if current.resolve() != resolved_before_root and not current.resolve().is_relative_to(resolved_before_root):
        return None
    dest = current / parts[-1]
    if dest.is_symlink() or dest.exists():
        return None
    return dest


def _commit_temp_before_file(temp_path: Path, dest: Path) -> None:
    try:
        os.link(temp_path, dest)
    except FileExistsError:
        pass
    except OSError:
        if not dest.exists():
            os.replace(temp_path, dest)
    finally:
        temp_path.unlink(missing_ok=True)


def _capture_before_bytes_sync(
    checkpoint: ChangeCheckpointTarget,
    target: Path,
    original: bytes | None,
) -> None:
    relative = _checkpoint_relative_path(checkpoint, target)
    if relative is None:
        return
    key = _checkpoint_path_key(checkpoint, relative)
    with checkpoint.lock:
        if key in checkpoint.recorded_paths:
            return
    if original is None or len(original) > checkpoint.max_file_bytes:
        with checkpoint.lock:
            checkpoint.recorded_paths.add(key)
        return
    temp_path: Path | None = None
    try:
        dest = _prepare_before_destination(checkpoint, relative)
        if dest is not None:
            fd, name = tempfile.mkstemp(prefix='.before-', dir=dest.parent)
            temp_path = Path(name)
            with os.fdopen(fd, 'wb') as output:
                output.write(original)
                output.flush()
            _commit_temp_before_file(temp_path, dest)
            temp_path = None
    except OSError:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    finally:
        with checkpoint.lock:
            checkpoint.recorded_paths.add(key)


def capture_before_bytes(target: Path, original: bytes | None) -> None:
    """Best-effort on-demand before-write snapshot from already-loaded bytes.

    First write to a workspace path in a turn wins; later edits preserve the
    pre-turn state (or absence if the file was created during this turn).
    """
    checkpoint = _ACTIVE_CHECKPOINT.get()
    if checkpoint is None:
        return
    _capture_before_bytes_sync(checkpoint, target, original)


async def acapture_before_bytes(target: Path, original: bytes | None) -> None:
    """Async wrapper keeping checkpoint file I/O off the shared event loop."""
    checkpoint = _ACTIVE_CHECKPOINT.get()
    if checkpoint is None:
        return
    await asyncio.to_thread(_capture_before_bytes_sync, checkpoint, target, original)


def _hash_and_capture_before(checkpoint: ChangeCheckpointTarget, target: Path) -> str | None:
    relative = _checkpoint_relative_path(checkpoint, target)
    if relative is None:
        return file_hash(target)
    key = _checkpoint_path_key(checkpoint, relative)
    with checkpoint.lock:
        if key in checkpoint.recorded_paths:
            return file_hash(target)
    temp_path: Path | None = None
    temp_stream = None
    dest: Path | None = None
    try:
        try:
            fd = os.open(
                target,
                os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0),
            )
        except FileNotFoundError:
            return None
        digest = hashlib.sha256()
        with os.fdopen(fd, 'rb') as stream:
            st = os.fstat(stream.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise OSError('File version is unavailable for a non-regular file')
            if st.st_size <= checkpoint.max_file_bytes:
                try:
                    dest = _prepare_before_destination(checkpoint, relative)
                    if dest is not None:
                        temp_fd, name = tempfile.mkstemp(prefix='.before-', dir=dest.parent)
                        temp_path = Path(name)
                        temp_stream = os.fdopen(temp_fd, 'wb')
                except OSError:
                    if temp_stream is not None:
                        temp_stream.close()
                        temp_stream = None
                    if temp_path is not None:
                        temp_path.unlink(missing_ok=True)
                        temp_path = None
            total_bytes = 0
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
                total_bytes += len(chunk)
                if temp_stream is not None:
                    if total_bytes > checkpoint.max_file_bytes:
                        temp_stream.close()
                        temp_stream = None
                        if temp_path is not None:
                            temp_path.unlink(missing_ok=True)
                            temp_path = None
                    else:
                        try:
                            temp_stream.write(chunk)
                        except OSError:
                            temp_stream.close()
                            temp_stream = None
                            if temp_path is not None:
                                temp_path.unlink(missing_ok=True)
                                temp_path = None
        if temp_stream is not None:
            try:
                temp_stream.flush()
                temp_stream.close()
                temp_stream = None
                if temp_path is not None and dest is not None:
                    _commit_temp_before_file(temp_path, dest)
                    temp_path = None
            except OSError:
                pass
        return digest.hexdigest()
    finally:
        with checkpoint.lock:
            checkpoint.recorded_paths.add(key)
        if temp_stream is not None:
            try:
                temp_stream.close()
            except OSError:
                pass
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def file_hash(target: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        # A FIFO must not block the event loop while collecting optional evidence.
        fd = os.open(target, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise OSError('File version is unavailable for a non-regular file')
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()
    except FileNotFoundError:
        return None


_UNKNOWN_BEFORE = object()


def _before_write_sync(target: Path, checkpoint: ChangeCheckpointTarget | None) -> str | None | object:
    # Failure to read an old version must not remove existing write capability.
    try:
        if checkpoint is not None:
            return _hash_and_capture_before(checkpoint, target)
        return file_hash(target)
    except OSError:
        return _UNKNOWN_BEFORE


def before_write(target: Path) -> str | None | object:
    return _before_write_sync(target, _ACTIVE_CHECKPOINT.get())


async def abefore_write(target: Path) -> str | None | object:
    """Async version of before_write that keeps hashing and checkpoint I/O off the event loop."""
    return await asyncio.to_thread(_before_write_sync, target, _ACTIVE_CHECKPOINT.get())


def file_change_result(result: ToolResult, workspace_dir: str | Path, target: Path,
                       before: str | None, after: str, *, verify_current: bool = False) -> ToolResult:
    """Call only after committing the bytes whose digest is `after`."""
    if before is _UNKNOWN_BEFORE:
        return result
    root = Path(workspace_dir).resolve()
    try:
        target = target.resolve()
        relative = target.relative_to(root).as_posix()
        if verify_current and file_hash(target) != after:
            return result
    except (OSError, RuntimeError, ValueError):
        return result  # Full-access writes outside cwd retain their old behavior.
    if not result.success or before == after:
        return result
    raw = dict(result.raw_output or {})
    raw.update(file_changes_version=2, file_changes=[{
        'path': relative, 'before_sha256': before, 'after_sha256': after,
    }])
    return result.model_copy(update={'raw_output': raw})


@asynccontextmanager
async def destination_lock(target: Path):
    """Cross-process coordination for this destination only, including aliases.

    Lock files remain stable: unlinking a lock inode could let a third process
    bypass a waiter on the old inode. No profile/workspace writer barrier exists.
    """
    records = state_path('file-commit-locks')
    records.mkdir(parents=True, exist_ok=True)
    streams = []
    try:
        while True:
            lexical = target.parent.resolve() / target.name
            keys = sorted({str(lexical), str(target.resolve())})
            for key in keys:
                lock_path = records / (hashlib.sha256(os.fsencode(key)).hexdigest() + '.lock')
                stream = lock_path.open('a+b')
                streams.append(stream)
                if lock_path.stat().st_size == 0:
                    stream.write(b'\0')
                    stream.flush()
                while True:
                    check_tool_cancellation()
                    try:
                        if os.name == 'nt':
                            import msvcrt
                            stream.seek(0)
                            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except (BlockingIOError, PermissionError):
                        await asyncio.sleep(0.01)
                check_tool_cancellation()
            current = sorted({str(target.parent.resolve() / target.name), str(target.resolve())})
            if current == keys:
                break
            for stream in reversed(streams):
                stream.close()
            streams.clear()
        yield
    finally:
        for stream in reversed(streams):
            stream.close()


async def persist_file_change(workspace_dir: str, target: Path | None,
                              persist: Callable[[Path | None], ToolResult]) -> ToolResult:
    """Persist browser payloads under a short destination lock, after network I/O."""
    if target is None:
        return persist(None)
    root = Path(workspace_dir).resolve()
    try:
        while True:
            resolved = target.resolve()
            async with destination_lock(resolved):
                if target.resolve() != resolved:
                    continue
                if not resolved.is_relative_to(root):
                    raise ValueError('browser persistence target must stay inside the session workspace')
                before = await abefore_write(resolved)
                check_tool_cancellation()
                result = persist(resolved)
                digest = result._written_file_sha256
                return file_change_result(result, root, resolved, before, digest, verify_current=True) if digest else result
    except ToolInvocationRejected as exc:
        return exc.to_result()
    except (OSError, RuntimeError, ValueError) as exc:
        return ToolResult(success=False, error=f'BROWSER_PERSISTENCE_OUTPUT_PATH_INVALID: {exc}')


class FileChangeReceiptMixin:
    """Coordinate direct file tools without holding locks during script/model I/O."""

    def _direct_file_target(self, arguments: dict) -> Path:
        from .file_tools import _resolve_from_active_root
        target = _resolve_from_active_root(arguments['path'], workspace_dir=self.workspace_dir,
                                           relative_root_dir=self.relative_root_dir)
        if self.name == 'edit_file' and not target.exists() and not Path(arguments['path']).is_absolute():
            candidate = self.workspace_dir / arguments['path']
            if candidate.exists():
                target = candidate.resolve()
        return target.resolve()

    async def _invoke_validated(self, arguments: dict, *, context: ToolInvocationContext | None):
        while True:
            target = self._direct_file_target(arguments)
            async with destination_lock(target):
                if self._direct_file_target(arguments) != target:
                    continue
                if isinstance(context, FileWriteInvocationContext) and context.validate_write_target:
                    if error := context.validate_write_target(target):
                        return error
                check_tool_cancellation()
                return await super()._invoke_validated(arguments, context=context)

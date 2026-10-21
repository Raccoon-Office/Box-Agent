"""Receipts for bytes committed by Box-owned writers, never workspace observations."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from .base import ToolInvocationContext, ToolInvocationRejected, ToolResult, check_tool_cancellation
from ..user_paths import state_path


@dataclass(frozen=True, slots=True)
class FileWriteInvocationContext(ToolInvocationContext):
    validate_write_target: Callable[[Path], ToolResult | None] | None = None


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


def before_write(target: Path) -> str | None | object:
    # Failure to read an old version must not remove existing write capability.
    try:
        return file_hash(target)
    except OSError:
        return _UNKNOWN_BEFORE


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
                before = before_write(resolved)
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

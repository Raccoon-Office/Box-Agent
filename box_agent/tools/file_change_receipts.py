"""Per-invocation file receipts captured under a shared workspace execution lock."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
import os
import stat
from pathlib import Path
from contextlib import asynccontextmanager
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from inspect import isawaitable

from .base import ToolInvocationContext, ToolResult
from ..artifact_publication import SUFFIX
from ..user_paths import state_path


@dataclass(frozen=True, slots=True)
class FileWriteInvocationContext(ToolInvocationContext):
    """Recheck delegated write authority against the destination under its lease."""

    validate_write_target: Callable[[Path], ToolResult | None] | None = None


CHANGED_FILES_PARAMETER = {
    "type": "array",
    "items": {"type": "string", "minLength": 1},
    "maxItems": 256,
    "uniqueItems": True,
    "description": (
        "Optional optimization for large workspaces: declare ALL exact target file paths "
        "before execution (both paths for a rename). Omit for automatic workspace capture. "
        "Paths are relative to the session workspace, regardless of inline cd. "
        "Include script-generated outputs of any file type; never list read-only inputs, "
        "directories, glob patterns, or another task's files. Use [] for read-only calls. "
        "Only files whose bytes change receive task-bound file change receipts. "
        "Background execution cannot produce these receipts."
    ),
}


def _file_hash(target: Path) -> str | None:
    try:
        if not stat.S_ISREG(target.stat().st_mode):
            raise ValueError('changed_files must contain regular files')
        with target.open('rb') as stream:
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()
    except FileNotFoundError:
        return None


def _workspace_hashes(root: Path) -> dict[str, str] | None:
    from .engine.artifact_results import _snapshot_workspace

    files = _snapshot_workspace(str(root), include_all_regular_files=True)
    if files is None:
        return None
    # Match the desktop's default snapshot budget. Partial scans cannot claim
    # deletions or additions; callers retain explicit-target fallback instead.
    hashes = {}
    total_bytes = 0
    try:
        for target in sorted(files):
            is_metadata = (
                target.name.startswith('.') and target.name.endswith(SUFFIX)
                and len(target.name) > len(SUFFIX) + 1
            )
            if target.resolve() != target or is_metadata:
                continue
            total_bytes += target.stat().st_size
            if len(hashes) >= 2000 or total_bytes > 512 * 1024 * 1024:
                return None
            digest = _file_hash(target)
            if digest is None:
                return None
            hashes[target.relative_to(root).as_posix()] = digest
    except (OSError, RuntimeError, ValueError):
        return None
    return hashes


def _has_background_writer(root: Path) -> bool:
    from .bash_tool import BackgroundShellManager

    for shell in BackgroundShellManager._shells.values():
        # Shell permissions can include any workspace in this profile.
        if shell.process.returncode is None:
            return True
        if os.name != 'nt':
            try:
                # A wrapper may exit while its child service still writes.
                os.killpg(shell.process.pid, 0)
                return True
            except ProcessLookupError:
                pass
            except OSError:
                return True
    return _has_shared_background_writer(root)


def _acquire_file_lock(stream) -> None:
    if os.name == 'nt':
        import msvcrt

        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release_file_lock(stream) -> None:
    if os.name == 'nt':
        import msvcrt

        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@asynccontextmanager
async def _registry_lock(path: Path):
    with path.open('a+b') as stream:
        if path.stat().st_size == 0:
            stream.write(b'\0')
            stream.flush()
        while True:
            try:
                _acquire_file_lock(stream)
                break
            except (BlockingIOError, PermissionError):
                await asyncio.sleep(0.05)
        try:
            yield
        finally:
            _release_file_lock(stream)


def _overlapping_lease(root: Path | None, records: Path) -> bool:
    # Called under the registry lock. The first byte is reserved for the OS
    # lock; Windows readers can inspect the remaining metadata while it is held.
    for path in records.glob('*.lease'):
        try:
            with path.open('r+b') as stream:
                try:
                    _acquire_file_lock(stream)
                except (BlockingIOError, PermissionError):
                    stream.seek(1)
                    value = json.load(stream)
                    # A null root is an unrestricted script's profile-wide lease.
                    if root is None or value is None:
                        return True
                    other = Path(value)
                    if root.is_relative_to(other) or other.is_relative_to(root):
                        return True
                    continue
                else:
                    _release_file_lock(stream)
            # No owner holds this lease, including after an Agent crash.
            path.unlink(missing_ok=True)
        except FileNotFoundError:
            continue
        except (OSError, ValueError, TypeError):
            return True  # Unknown ownership must not permit overlapping writes.
    return False


def _target_lease_roots(target: Path) -> set[Path]:
    target = target.expanduser().absolute()
    roots = {target.resolve().parent}
    pending = [target]
    seen = set()
    while pending:
        path = pending.pop()
        for alias in (path, *path.parents):
            if alias in seen:
                continue
            seen.add(alias)
            if not alias.is_symlink():
                continue
            roots.add(alias.parent.resolve())
            try:
                referent = alias.readlink()
            except FileNotFoundError:
                continue
            pending.append(referent if referent.is_absolute() else alias.parent / referent)
    return roots


@asynccontextmanager
async def _workspace_execution_lock(
    root: Path, *additional_roots: Path, target_paths: tuple[Path, ...] = (),
    exclusive: bool = False,
):
    """Acquire all affected roots atomically, without nested-lock deadlocks."""
    requested_roots = (root, *additional_roots)
    records = state_path('file-change-locks')
    records.mkdir(parents=True, exist_ok=True)
    leases = []
    try:
        while not leases:
            async with _registry_lock(records / 'registry.lock'):
                # A cooperating writer can retarget an alias while we wait.
                # Resolve the original paths again on every acquisition attempt.
                target_roots = []
                for target in target_paths:
                    # Lease the entries that select the destination as well as
                    # its referent, including aliases reached through aliases.
                    target_roots.extend(_target_lease_roots(target))
                roots = tuple(dict.fromkeys([
                    *(path.expanduser().resolve() for path in requested_roots),
                    *target_roots,
                ]))
                roots = tuple(path for path in roots if not any(
                    path != other and path.is_relative_to(other) for other in roots
                ))
                # Known destinations can run concurrently; scripts may write
                # anywhere, so their lease conflicts with every active writer.
                lease_roots = (None,) if exclusive else roots
                if not any(_overlapping_lease(root, records) for root in lease_roots):
                    for root in lease_roots:
                        path = records / f'{uuid.uuid4().hex}.lease'
                        stream = path.open('w+b')
                        leases.append((path, stream))
                        stream.write(b'\0' + json.dumps(str(root) if root is not None else None).encode())
                        stream.flush()
                        _acquire_file_lock(stream)
            if not leases:
                await asyncio.sleep(0.05)
        yield
    finally:
        for path, stream in leases:
            stream.close()  # The OS also releases the lease on process exit.
            try:
                path.unlink(missing_ok=True)
            except OSError:
                # A Windows registry reader may still have the file open.
                # Its next scan can reclaim the now-unlocked lease.
                pass


def register_shell_writer(workspace_dir: str, pid: int) -> None:
    """Publish shell liveness before returning its process to the invocation."""
    records = state_path('file-change-background-writers')
    records.mkdir(parents=True, exist_ok=True)
    path = records / f'{uuid.uuid4().hex}.json'
    temporary = path.with_suffix('.tmp')
    try:
        temporary.write_text(json.dumps({
            'workspace': str(Path(workspace_dir).expanduser().resolve()), 'pid': pid,
        }), encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _background_process_alive(pid: int) -> bool:
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes

        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x00100000, False, pid)
        if not handle:
            return ctypes.get_last_error() != 87  # Only an absent PID is safe.
        try:
            return api.WaitForSingleObject(handle, 0) != 0
        finally:
            api.CloseHandle(handle)
    try:
        os.killpg(pid, 0)  # Wrapper exit does not imply its child service exited.
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _has_shared_background_writer(root: Path) -> bool:
    records = state_path('file-change-background-writers')
    try:
        for path in records.glob('*.json'):
            try:
                record = json.loads(path.read_text(encoding='utf-8'))
                other = Path(record['workspace'])
                pid = record['pid']
                if not isinstance(pid, int) or pid <= 0 or not other.is_absolute():
                    return True
                if not _background_process_alive(pid):
                    path.unlink(missing_ok=True)
                else:
                    return True
            except FileNotFoundError:
                continue
            except (OSError, ValueError, TypeError, KeyError):
                return True
    except OSError:
        return True
    return False


async def persist_file_change(workspace_dir: str, target: Path | None,
                              persist: Callable[[Path | None], ToolResult]) -> ToolResult:
    """Capture Box-owned browser persistence at its actual write seam."""
    if target is None:
        return persist(None)
    root = Path(workspace_dir).expanduser().resolve()
    try:
        async with _workspace_execution_lock(root, target_paths=(target,)):
            target = target.resolve()
            if not target.is_relative_to(root):
                raise ValueError('browser persistence target must stay inside the session workspace')
            return await capture_file_change(root, target, lambda: persist(target))
    except (OSError, RuntimeError, ValueError) as exc:
        return ToolResult(success=False, error=f'BROWSER_PERSISTENCE_OUTPUT_PATH_INVALID: {exc}')


async def capture_file_change(root: Path, target: Path,
                              persist: Callable[[], ToolResult | Awaitable[ToolResult]]) -> ToolResult:
    """Capture one target while the caller holds its workspace/destination leases.

    Keep the final component unresolved when persistence replaces a symlink.
    """
    relative = None
    try:
        if not _has_background_writer(root):
            relative = target.relative_to(root).as_posix()
            before = await asyncio.to_thread(_file_hash, target)
    except (OSError, RuntimeError, ValueError):
        relative = None
    result = persist()
    if isawaitable(result):
        result = await result
    if relative is None:
        return result
    try:
        if target.resolve() != target:
            return result
        after = await asyncio.to_thread(_file_hash, target)
    except (OSError, RuntimeError, ValueError):
        return result
    if before == after:
        return result
    raw = dict(result.raw_output or {})
    raw.update(file_changes_version=1, file_changes=[
        {'path': relative, 'before_sha256': before, 'after_sha256': after},
    ])
    return result.model_copy(update={'raw_output': raw})


class FileChangeReceiptMixin:
    """Add receipts at the validated tool seam without changing legacy execute calls.

    Other cooperating foreground tools cannot write during capture. Hosts must
    still corroborate both hashes against their own round snapshots; unmatched
    versions remain unconfirmed when other writers share a path between calls.
    """

    def _direct_file_target(self, arguments: dict) -> Path | None:
        if self.name not in {'write_file', 'append_file', 'edit_file'}:
            return None
        from .file_tools import _resolve_from_active_root

        target = _resolve_from_active_root(arguments['path'], workspace_dir=self.workspace_dir,
                                           relative_root_dir=self.relative_root_dir)
        if self.name == 'edit_file' and not target.exists() and not Path(arguments['path']).is_absolute():
            candidate = self.workspace_dir / arguments['path']
            if candidate.exists():
                target = candidate
        return target

    def _additional_file_change_targets(self, arguments: dict) -> tuple[Path, ...]:
        return ()

    async def _invoke_validated(self, arguments: dict, *, context: ToolInvocationContext | None):
        target = self._direct_file_target(arguments)
        script = self.name in {'bash', 'execute_code'}
        if self.workspace_dir or script:
            # Also serialize legacy calls without declarations: otherwise they
            # could change a tracked target during another tool's receipt window.
            target_paths = (
                ((target,) if target is not None else ())
                + self._additional_file_change_targets(arguments)
            )
            async with _workspace_execution_lock(
                Path(self.workspace_dir or os.getcwd()), target_paths=target_paths, exclusive=script,
            ):
                return await self._invoke_with_receipts(arguments, context=context, target=target)
        return await self._invoke_with_receipts(arguments, context=context, target=target)

    async def _invoke_with_receipts(self, arguments: dict, *, context: ToolInvocationContext | None,
                                  target: Path | None = None):
        arguments = dict(arguments)
        targets = arguments.pop('changed_files', None)
        direct_file_tool = target is not None
        if direct_file_tool:
            if isinstance(context, FileWriteInvocationContext) and context.validate_write_target:
                target = target.resolve()
                if error := context.validate_write_target(target):
                    return error
                # Keep authorization, receipt capture and execution on one path.
                arguments['path'] = str(target)
            targets = [str(target)]
        if arguments.get('run_in_background'):
            if targets:
                return ToolResult(success=False, error='FILE_CHANGES_REQUIRE_FOREGROUND: changed_files cannot track background commands')
            return await super()._invoke_validated(arguments, context=context)
        if not self.workspace_dir:
            if not targets:
                return await super()._invoke_validated(arguments, context=context)
            return ToolResult(success=False, error='FILE_CHANGES_REQUIRE_WORKSPACE: configure an explicit workspace for changed_files')
        root = Path(self.workspace_dir).expanduser().resolve()
        if _has_background_writer(root):
            # Background commands and shells surviving an Agent crash can
            # outlive their workspace lease. Never adopt their writes.
            return await super()._invoke_validated(arguments, context=context)
        if targets is None:
            before = await asyncio.to_thread(_workspace_hashes, root)
            result = await super()._invoke_validated(arguments, context=context)
            if result.permission_request or before is None:
                return result
            after = await asyncio.to_thread(_workspace_hashes, root)
            if after is None:
                return result
            raw = dict(result.raw_output or {})
            raw.update(file_changes_version=1, file_changes=[
                {'path': name, 'before_sha256': before.get(name), 'after_sha256': after.get(name)}
                for name in sorted(before.keys() | after.keys()) if before.get(name) != after.get(name)
            ])
            return result.model_copy(update={'raw_output': raw})
        if not targets:
            return await super()._invoke_validated(arguments, context=context)
        resolved = {}
        try:
            for value in targets:
                target = Path(value).expanduser()
                target = (target if target.is_absolute() else root / target).resolve()
                relative = target.relative_to(root).as_posix()
                if target == root or target.is_dir():
                    raise ValueError('changed_files must contain exact files')
                resolved[relative] = target
            before = await asyncio.to_thread(lambda: {name: _file_hash(p) for name, p in resolved.items()})
        except (OSError, RuntimeError, ValueError) as exc:
            # Outside-workspace direct writes retain their established permissions.
            if direct_file_tool:
                return await super()._invoke_validated(arguments, context=context)
            return ToolResult(success=False, error=f'INVALID_CHANGED_FILES: {exc}')
        result = await super()._invoke_validated(arguments, context=context)
        if result.permission_request:
            return result
        changes = []
        for name, target in resolved.items():
            try:
                # A script can replace a path with an escaping symlink while running.
                if target.resolve() != target:
                    continue
                after = await asyncio.to_thread(_file_hash, target)
            except (OSError, RuntimeError, ValueError):
                continue
            if before[name] != after:
                changes.append({'path': name, 'before_sha256': before[name], 'after_sha256': after})
        raw = dict(result.raw_output or {})
        raw.update(file_changes_version=1, file_changes=changes)
        return result.model_copy(update={'raw_output': raw})

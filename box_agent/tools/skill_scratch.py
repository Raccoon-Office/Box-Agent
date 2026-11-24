"""Session-scoped scratch storage for subprocess-backed Skills."""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import shutil
import stat
import sys
import tempfile
import threading
from collections.abc import Callable
from contextvars import copy_context
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar
from uuid import uuid4
from weakref import WeakValueDictionary

from ..artifact_publication import ChatReferenceScanError, retained_chat_file_in


SKILL_SCRATCH_DIR_NAME = ".box-agent-scratch"
SKILL_SCRATCH_ENV = "BOX_AGENT_SCRATCH_DIR"
_PREPARE_TIMEOUT_SECONDS = 5.0
_CLEANUP_TIMEOUT_SECONDS = 10.0
_DIAGNOSE_TIMEOUT_SECONDS = 2.0
_T = TypeVar("_T")


def _settle(future: asyncio.Future, result: object, error: BaseException | None) -> None:
    if future.done():  # the waiter timed out or was cancelled
        return
    if error is not None:
        future.set_exception(error)
    else:
        future.set_result(result)


async def _run_scratch_io(operation: Callable[..., _T], /, *args, **kwargs) -> _T:
    """Run optional scratch file I/O on its own daemon thread.

    A shared pool would let a hung cleanup starve preparation, and pool
    workers are joined at interpreter exit. A daemon thread per operation
    keeps a stuck Windows file call from blocking other sessions or shutdown;
    callers bound the wait with their own timeouts.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[_T] = loop.create_future()
    context = copy_context()

    def worker() -> None:
        result: object = None
        error: BaseException | None = None
        try:
            result = context.run(operation, *args, **kwargs)
        except BaseException as exc:  # delivered to the awaiting task
            error = exc
        try:
            loop.call_soon_threadsafe(_settle, future, result, error)
        except RuntimeError:  # event loop already closed
            pass

    threading.Thread(target=worker, name="skill-scratch-io", daemon=True).start()
    return await future


@dataclass(frozen=True)
class SkillScratchDirectory:
    path: Path
    device: int
    inode: int
    _protected_references: set[Path] = field(default_factory=set, init=False, repr=False, compare=False)
    _cleanup_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False, compare=False)


_active_scratch_dirs: WeakValueDictionary[Path, SkillScratchDirectory] = WeakValueDictionary()


def _scratch_owner(path: Path) -> SkillScratchDirectory | None:
    for parent in (path, *path.parents):
        scratch = _active_scratch_dirs.get(parent)
        if scratch is not None:
            return scratch
    return None


def protect_scratch_reference(path: Path) -> None:
    """Preserve a chat path during scratch cleanup."""
    if (scratch := _scratch_owner(path)) is not None:
        scratch._protected_references.add(path)


def release_scratch_reference(path: Path) -> None:
    """Release a scratch path after a confirmed chat-reference deletion."""
    if (scratch := _scratch_owner(path)) is not None:
        scratch._protected_references.discard(path)


class SkillScratchUnavailable(RuntimeError):
    code = "SKILL_SCRATCH_UNAVAILABLE"

    def __init__(self, path: Path, reason: str, *, path_validated: bool = False):
        super().__init__(f"{self.code}: {path}: {reason}")
        # True only when every existing component was checked to be a real
        # directory inside the workspace, so a command may still be given it.
        self.path_validated = path_validated


_log = logging.getLogger(__name__)

# Windows folders that Defender "Controlled folder access" protects by default.
_PROTECTED_USER_FOLDERS = ("Documents", "Desktop", "Pictures", "Videos", "Music", "Favorites")


def _describe_path(path: Path) -> str:
    """One-word state of a path for diagnostics; never raises."""
    try:
        stats = path.lstat()
    except FileNotFoundError:
        return "missing"
    except OSError as exc:
        return f"unreadable({type(exc).__name__}:{getattr(exc, 'winerror', None) or exc.errno})"
    kind = "link" if stat.S_ISLNK(stats.st_mode) else "dir" if stat.S_ISDIR(stats.st_mode) else "file"
    attributes = getattr(stats, "st_file_attributes", None)
    return kind if attributes is None else f"{kind}(attrs=0x{attributes:x})"


def _location_hints(workspace: Path) -> list[str]:
    """Name likely Windows causes of access-denied for this workspace location."""
    hints: list[str] = []
    try:
        absolute = _absolute_without_resolving(workspace)
        text = os.path.normcase(str(absolute))
        for variable in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
            root = os.environ.get(variable)
            if root and text.startswith(os.path.normcase(root)):
                hints.append("onedrive")
                break
        profile = os.environ.get("USERPROFILE") or str(Path.home())
        for folder in _PROTECTED_USER_FOLDERS:
            if text.startswith(os.path.normcase(os.path.join(profile, folder))):
                hints.append(f"user_{folder.lower()}")
        if absolute.anchor.startswith("\\\\"):
            hints.append("network_share")
    except Exception:  # Diagnostics must never fail preparation handling.
        pass
    return hints


def _diagnose_failure(workspace: Path, planned: Path, error: BaseException) -> dict[str, object]:
    """Collect what is needed to tell ACL, Defender, delete-pending and lock cases apart."""
    fields: dict[str, object] = {
        "error_type": type(error).__name__,
        "winerror": getattr(error, "winerror", None),
        "errno": getattr(error, "errno", None),
        "error": str(error),
        "failed_path": getattr(error, "filename", None),
        "workspace": str(workspace),
        "planned_path": str(planned),
        "os": os.name,
    }
    try:
        fields["workspace_state"] = _describe_path(workspace)
        fields["scratch_root_state"] = _describe_path(workspace / SKILL_SCRATCH_DIR_NAME)
        fields["planned_state"] = _describe_path(planned)
        fields["hints"] = ",".join(_location_hints(workspace)) or "none"
    except Exception:
        pass
    return fields


def _format_fields(fields: dict[str, object]) -> str:
    return " ".join(f"{key}={value!r}" for key, value in fields.items())


class SessionSkillScratch:
    """Prepare one session-owned directory on first use by a tool."""

    def __init__(self, workspace: Path):
        self._workspace = workspace
        self._path = workspace / SKILL_SCRATCH_DIR_NAME / uuid4().hex
        self.prepared: SkillScratchDirectory | None = None
        self._preparing: asyncio.Task[SkillScratchDirectory | SkillScratchUnavailable] | None = None
        self._cleanup: asyncio.Future[list[str]] | None = None
        self._retired: list[SkillScratchDirectory] = []
        # Log each distinct failure once per session, then recovery once.
        self._logged_failure: tuple[object, ...] | None = None

    @property
    def path(self) -> Path:
        return self.prepared.path if self.prepared is not None else self._path

    async def ensure(self) -> Path:
        if self.prepared is not None:
            return self.prepared.path
        task = self._preparing
        if task is None or task.done():
            task = self._preparing = asyncio.create_task(self._prepare())
        try:
            result = await asyncio.wait_for(asyncio.shield(task), _PREPARE_TIMEOUT_SECONDS)
        # Python 3.10's asyncio.TimeoutError is not the builtin TimeoutError.
        except asyncio.TimeoutError:
            self._log_failure(("timeout",), {
                "error_type": "Timeout", "timeout_seconds": _PREPARE_TIMEOUT_SECONDS,
                "workspace": str(self._workspace), "planned_path": str(self._path),
                "hints": ",".join(_location_hints(self._workspace)) or "none",
            })
            # Unknown state: preparation has not validated the path yet.
            raise SkillScratchUnavailable(self._path, "timed out preparing the directory") from None
        if isinstance(result, SkillScratchUnavailable):
            raise result
        return result.path

    def _log_failure(self, key: tuple[object, ...], fields: dict[str, object]) -> None:
        if key == self._logged_failure:
            return
        self._logged_failure = key
        _log.warning("skill_scratch/unavailable %s", _format_fields(fields))

    async def _prepare(self) -> SkillScratchDirectory | SkillScratchUnavailable:
        # A cancelled or timed-out tool can leave the OS call running. Keep it
        # shared so retries cannot start duplicate preparation while it runs.
        try:
            directory = await _run_scratch_io(
                prepare_skill_scratch_dir, self._workspace, scratch_root_dir=self._path,
            )
        except Exception as exc:
            try:
                fields = await asyncio.wait_for(
                    _run_scratch_io(_diagnose_failure, self._workspace, self._path, exc),
                    _DIAGNOSE_TIMEOUT_SECONDS,
                )
            except Exception:  # including a diagnosis that hangs on the same file system
                fields = {"error_type": type(exc).__name__, "error": str(exc),
                          "workspace": str(self._workspace), "planned_path": str(self._path),
                          "diagnostics": "unavailable"}
            # prepare_skill_scratch_dir raises RuntimeError for its safety checks
            # (links, reparse points, non-directories, workspace escape). Only a
            # plain OS error leaves the checked components trustworthy.
            path_validated = isinstance(exc, OSError)
            fields["path_validated"] = path_validated
            self._log_failure(
                (type(exc).__name__, getattr(exc, "winerror", None), getattr(exc, "errno", None),
                 getattr(exc, "filename", None)),
                fields,
            )
            return SkillScratchUnavailable(
                self._path, str(exc) or type(exc).__name__, path_validated=path_validated,
            )
        if self._logged_failure is not None:
            _log.info("skill_scratch/recovered path=%r", str(directory.path))
            self._logged_failure = None
        self.prepared = directory
        return directory


async def try_prepare_scratch(scratch: SessionSkillScratch) -> bool:
    """Prepare scratch for a general-purpose tool without making it a hard dependency.

    After a plain OS error (for example access denied) Bash and Python still
    run: the env var keeps pointing at the planned session path (never unset,
    so ``$VAR/x`` cannot expand to ``/x``), a command that writes there fails
    on its own, and nothing extra is added to the result. The failure is
    logged with diagnostics by ``SessionSkillScratch``.

    Raises ``SkillScratchUnavailable`` when the path was rejected by a safety
    check or its state is unknown (timeout): handing it to a command could
    follow a link out of the workspace, so the tool must fail instead.
    """
    try:
        await scratch.ensure()
    except SkillScratchUnavailable as exc:
        if not exc.path_validated:
            raise
        return False
    return True


def _absolute_without_resolving(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _is_link_or_reparse_point(stats: os.stat_result) -> bool:
    if stat.S_ISLNK(stats.st_mode):
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(getattr(stats, "st_file_attributes", 0) & reparse_flag)


def _prepare_real_directory_chain(root: Path, relative_path: Path) -> os.stat_result:
    current = root
    stats = root.lstat()
    if _is_link_or_reparse_point(stats) or not stat.S_ISDIR(stats.st_mode):
        raise RuntimeError(f"Skill scratch workspace must be a real directory: {root}")
    for part in relative_path.parts:
        current /= part
        try:
            stats = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            stats = current.lstat()
        if _is_link_or_reparse_point(stats) or not stat.S_ISDIR(stats.st_mode):
            raise RuntimeError(
                f"Skill scratch path must contain only real directories: {current}"
            )
    return stats


def prepare_skill_scratch_dir(
    workspace_dir: Path,
    *,
    scratch_root_dir: str | Path | None = None,
) -> SkillScratchDirectory:
    """Create a workspace-contained Skill scratch root without accepting links."""
    workspace_path = _absolute_without_resolving(workspace_dir)
    workspace_root = workspace_path.resolve(strict=True)
    requested_path = (
        _absolute_without_resolving(Path(scratch_root_dir))
        if scratch_root_dir is not None
        else workspace_path / SKILL_SCRATCH_DIR_NAME
    )
    try:
        relative_path = requested_path.relative_to(workspace_path)
    except ValueError as error:
        raise RuntimeError(
            f"Skill scratch root must stay within the workspace: {requested_path}"
        ) from error
    if not relative_path.parts:
        raise RuntimeError("Skill scratch root must not be the workspace root")

    scratch_dir = workspace_root / relative_path
    stats = _prepare_real_directory_chain(workspace_root, relative_path)
    if os.name != "nt":
        scratch_dir.chmod(0o700)
    scratch = SkillScratchDirectory(
        path=scratch_dir,
        device=stats.st_dev,
        inode=stats.st_ino,
    )
    _active_scratch_dirs[scratch_dir] = scratch
    return scratch


def _prepared_scratch(scratch: SkillScratchDirectory | SessionSkillScratch) -> SkillScratchDirectory | None:
    return scratch.prepared if isinstance(scratch, SessionSkillScratch) else scratch


class _CleanupLease:
    """The waiter can revoke live-path moves without waiting for filesystem I/O."""

    def __init__(self) -> None:
        self.stopped = False
        self._lock = threading.Lock()
        self._moving_from: Path | None = None

    def revoke(self) -> Path | None:
        with self._lock:
            self.stopped = True
            return self._moving_from

    def clean(self, prepared: SkillScratchDirectory) -> list[str]:
        if not prepared._cleanup_lock.acquire(blocking=False):
            return []
        try:
            return _clean_prepared_scratch(prepared, self)
        finally:
            prepared._cleanup_lock.release()

    def detach(self, entry: Path, isolated: Path) -> bool:
        # Order the handoff against the live rename without holding the lock
        # across the filesystem call. The event loop can always revoke it.
        with self._lock:
            if self.stopped:
                return False
            self._moving_from = entry.parent
        try:
            entry.rename(isolated)
        finally:
            with self._lock:
                self._moving_from = None
                expired = self.stopped
        if expired:
            # A late move might have claimed an explicitly reused absolute path.
            try:
                _restore_scratch_entry(isolated, entry)
            except OSError:
                protect_scratch_reference(isolated)
                _log.warning("skill_scratch/cleanup_restore_failed path=%r isolated=%r",
                             str(entry), str(isolated), exc_info=True)
        return not expired


def _rename_directory_no_replace(source: Path, target: Path) -> None:
    """Use the native exclusive rename when restoring a POSIX directory."""
    import ctypes

    if sys.platform == "darwin":
        symbol, at_fdcwd, flags = "renameatx_np", -2, 0x4  # RENAME_EXCL
    elif sys.platform.startswith("linux"):
        symbol, at_fdcwd, flags = "renameat2", -100, 1  # RENAME_NOREPLACE
    else:
        raise OSError(errno.ENOTSUP, "Exclusive directory rename unavailable", str(target))
    rename = getattr(ctypes.CDLL(None, use_errno=True), symbol, None)
    if rename is None:
        raise OSError(errno.ENOTSUP, "Exclusive directory rename unavailable", str(target))
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    if rename(at_fdcwd, os.fsencode(source), at_fdcwd, os.fsencode(target), flags):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(target))


def _restore_scratch_entry(isolated: Path, entry: Path) -> None:
    """Return an unconfirmed claim without overwriting a newly written entry."""
    try:
        if os.name == "nt":
            isolated.rename(entry)
        elif stat.S_ISDIR(isolated.lstat().st_mode):
            _rename_directory_no_replace(isolated, entry)
        else:
            os.link(isolated, entry, follow_symlinks=False)
            isolated.unlink()
    except FileExistsError:
        pass


def cleanup_skill_scratch_dir(scratch: SkillScratchDirectory | SessionSkillScratch) -> list[str]:
    """Detach disposable entries before deleting them, preserving their public paths for reuse."""
    prepared = _prepared_scratch(scratch)
    return _CleanupLease().clean(prepared) if prepared is not None else []


def _clean_prepared_scratch(prepared: SkillScratchDirectory, lease: _CleanupLease) -> list[str]:
    scratch_dir = prepared.path
    try:
        stats = scratch_dir.lstat()
    except FileNotFoundError:
        return []
    if (
        stat.S_ISLNK(stats.st_mode)
        or not stat.S_ISDIR(stats.st_mode)
        or stats.st_dev != prepared.device
        or stats.st_ino != prepared.inode
    ):
        raise RuntimeError(f"Skill scratch root must be a real directory: {scratch_dir}")

    references = prepared._protected_references
    for path in tuple(references):
        if lease.stopped:
            return []
        try:
            path.stat()
        except (FileNotFoundError, NotADirectoryError):
            if not lease.stopped:
                references.discard(path)
        except OSError:
            # An inaccessible preview must stay protected for later cleanups.
            continue
    # Other sessions can add references from their session I/O workers.
    protected = references.copy()
    if scratch_dir in protected:
        return []
    protected_entries = {path.relative_to(scratch_dir).parts[0] for path in protected}
    removed: list[str] = []
    quarantine = None
    try:
        for entry in scratch_dir.iterdir():
            if lease.stopped:
                break
            if entry == quarantine or entry.name in protected_entries:
                continue
            try:
                retained = retained_chat_file_in(entry)
            except ChatReferenceScanError:
                continue
            if retained is not None or lease.stopped:
                continue
            if quarantine is None:
                quarantine = Path(tempfile.mkdtemp(prefix=".cleanup-", dir=scratch_dir))
            isolated = quarantine / entry.name
            if not lease.detach(entry, isolated):
                break
            try:
                entry_mode = isolated.lstat().st_mode
                if stat.S_ISDIR(entry_mode) and not stat.S_ISLNK(entry_mode):
                    shutil.rmtree(isolated)
                else:
                    isolated.unlink()
            except OSError:
                if not lease.stopped:
                    _restore_scratch_entry(isolated, entry)
                raise
            removed.append(str(entry))
    finally:
        if quarantine is not None:
            # A failed deletion leaves its private entry for a later cleanup.
            try:
                quarantine.rmdir()
            except OSError as exc:
                if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                    raise
    return removed


async def acleanup_skill_scratch_dir(scratch: SkillScratchDirectory | SessionSkillScratch) -> list[str]:
    """Clean scratch off the event loop, waiting at most ``_CLEANUP_TIMEOUT_SECONDS``.

    A timed-out or cancelled waiter revokes further moves from the live directory.
    """
    prepared = _prepared_scratch(scratch)
    session = scratch if isinstance(scratch, SessionSkillScratch) else None
    directories = [prepared] if prepared is not None else []
    if session is not None:
        directories.extend(session._retired)
    if not directories:
        return []
    if session is not None and session._cleanup is not None and not session._cleanup.done():
        _log.warning("skill_scratch/cleanup_skipped reason='previous cleanup still running' path=%r",
                     str(directories[0].path))
        return []
    lease = _CleanupLease()

    async def clean_directories() -> list[str]:
        removed = []
        for directory in directories:
            if lease.stopped:
                break
            removed.extend(await _run_scratch_io(lease.clean, directory))
            if session is not None and directory in session._retired and not lease.stopped:
                session._retired.remove(directory)
        return removed

    task = asyncio.create_task(clean_directories())
    if session is not None:
        session._cleanup = task
    try:
        return await asyncio.wait_for(asyncio.shield(task), _CLEANUP_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        _log.warning("skill_scratch/cleanup_timeout timeout_seconds=%s path=%r",
                     _CLEANUP_TIMEOUT_SECONDS, str(directories[0].path))
        return []
    finally:
        moving_from = lease.revoke()
        if session is not None and prepared is not None and moving_from == prepared.path:
            # An already-issued rename cannot be cancelled on a stuck file
            # system. New tools must never reuse its source namespace.
            session._retired.append(prepared)
            session._path = session._path.with_name(uuid4().hex)
            session.prepared = None
            session._preparing = None
        if not task.done():
            task.add_done_callback(lambda done: done.cancelled() or done.exception())


async def close_skill_scratch_dir(scratch: SkillScratchDirectory | SessionSkillScratch) -> None:
    try:
        await acleanup_skill_scratch_dir(scratch)
    except Exception:
        logging.getLogger(__name__).warning("Could not clean session scratch directory", exc_info=True)

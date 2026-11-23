"""Session-scoped scratch storage for subprocess-backed Skills."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import stat
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TypeVar
from uuid import uuid4

from ..artifact_publication import ChatReferenceScanError, retained_chat_file_in


SKILL_SCRATCH_DIR_NAME = ".box-agent-scratch"
SKILL_SCRATCH_ENV = "BOX_AGENT_SCRATCH_DIR"
_PREPARE_TIMEOUT_SECONDS = 5.0
# Optional scratch I/O must not occupy the workers that serve ACP or session logs.
_scratch_io_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="skill-scratch")
_T = TypeVar("_T")


async def _run_scratch_io(operation: Callable[..., _T], /, *args, **kwargs) -> _T:
    return await asyncio.get_running_loop().run_in_executor(
        _scratch_io_pool, copy_context().run, partial(operation, *args, **kwargs),
    )


@dataclass(frozen=True)
class SkillScratchDirectory:
    path: Path
    device: int
    inode: int


class SkillScratchUnavailable(RuntimeError):
    code = "SKILL_SCRATCH_UNAVAILABLE"

    def __init__(self, path: Path, reason: str):
        super().__init__(f"{self.code}: {path}: {reason}")


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
        self._preparing: asyncio.Task[SkillScratchDirectory | str] | None = None
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
            raise SkillScratchUnavailable(self._path, "timed out preparing the directory") from None
        if isinstance(result, str):
            raise SkillScratchUnavailable(self._path, result)
        return result.path

    def _log_failure(self, key: tuple[object, ...], fields: dict[str, object]) -> None:
        if key == self._logged_failure:
            return
        self._logged_failure = key
        _log.warning("skill_scratch/unavailable %s", _format_fields(fields))

    async def _prepare(self) -> SkillScratchDirectory | str:
        # A cancelled or timed-out tool can leave the OS call running. Keep it
        # shared so retries cannot start duplicate preparation while it runs.
        try:
            directory = await _run_scratch_io(
                prepare_skill_scratch_dir, self._workspace, scratch_root_dir=self._path,
            )
        except Exception as exc:
            try:
                fields = await _run_scratch_io(_diagnose_failure, self._workspace, self._path, exc)
            except Exception:
                fields = {"error_type": type(exc).__name__, "error": str(exc)}
            self._log_failure(
                (type(exc).__name__, getattr(exc, "winerror", None), getattr(exc, "errno", None),
                 getattr(exc, "filename", None)),
                fields,
            )
            return str(exc) or type(exc).__name__
        if self._logged_failure is not None:
            _log.info("skill_scratch/recovered path=%r", str(directory.path))
            self._logged_failure = None
        self.prepared = directory
        return directory


async def try_prepare_scratch(scratch: SessionSkillScratch) -> bool:
    """Prepare scratch for a general-purpose tool without making it a hard dependency.

    Bash and Python still run when scratch is unavailable: a command that
    writes there fails on its own and the model adapts, so no extra warning is
    added to the result. The env var keeps pointing at the planned session
    path (never unset, so ``$VAR/x`` cannot expand to ``/x``). The failure is
    logged with diagnostics by ``SessionSkillScratch``.
    """
    try:
        await scratch.ensure()
    except SkillScratchUnavailable:
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
    return SkillScratchDirectory(
        path=scratch_dir,
        device=stats.st_dev,
        inode=stats.st_ino,
    )


def _prepared_scratch(scratch: SkillScratchDirectory | SessionSkillScratch) -> SkillScratchDirectory | None:
    return scratch.prepared if isinstance(scratch, SessionSkillScratch) else scratch


def cleanup_skill_scratch_dir(scratch: SkillScratchDirectory | SessionSkillScratch) -> list[str]:
    """Remove all entries from a reserved scratch root without following links."""
    prepared = _prepared_scratch(scratch)
    if prepared is None:
        return []
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

    removed: list[str] = []
    for entry in scratch_dir.iterdir():
        try:
            retained = retained_chat_file_in(entry)
        except ChatReferenceScanError:
            continue
        if retained is not None:
            continue
        entry_mode = entry.lstat().st_mode
        if stat.S_ISDIR(entry_mode) and not stat.S_ISLNK(entry_mode):
            shutil.rmtree(entry)
        else:
            entry.unlink()
        removed.append(str(entry))
    return removed


async def acleanup_skill_scratch_dir(scratch: SkillScratchDirectory | SessionSkillScratch) -> list[str]:
    prepared = _prepared_scratch(scratch)
    if prepared is None:
        return []
    return await _run_scratch_io(cleanup_skill_scratch_dir, prepared)


async def close_skill_scratch_dir(scratch: SkillScratchDirectory | SessionSkillScratch) -> None:
    try:
        await acleanup_skill_scratch_dir(scratch)
    except Exception:
        logging.getLogger(__name__).warning("Could not clean session scratch directory", exc_info=True)

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


class SessionSkillScratch:
    """Prepare one session-owned directory on first use by a tool."""

    def __init__(self, workspace: Path):
        self._workspace = workspace
        self._path = workspace / SKILL_SCRATCH_DIR_NAME / uuid4().hex
        self.prepared: SkillScratchDirectory | None = None
        self._preparing: asyncio.Task[SkillScratchDirectory | str] | None = None

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
        except TimeoutError:
            raise SkillScratchUnavailable(self._path, "timed out preparing the directory") from None
        if isinstance(result, str):
            raise SkillScratchUnavailable(self._path, result)
        return result.path

    async def _prepare(self) -> SkillScratchDirectory | str:
        # A cancelled or timed-out tool can leave the OS call running. Keep it
        # shared so retries cannot start duplicate preparation while it runs.
        try:
            directory = await _run_scratch_io(
                prepare_skill_scratch_dir, self._workspace, scratch_root_dir=self._path,
            )
        except Exception as exc:
            return str(exc) or type(exc).__name__
        self.prepared = directory
        return directory


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

"""Session-scoped scratch storage for subprocess-backed Skills."""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from ..artifact_publication import ChatReferenceScanError, retained_chat_file_in


SKILL_SCRATCH_DIR_NAME = ".box-agent-scratch"


@dataclass(frozen=True)
class SkillScratchDirectory:
    path: Path
    device: int
    inode: int


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


def cleanup_skill_scratch_dir(scratch: SkillScratchDirectory) -> list[str]:
    """Remove all entries from a reserved scratch root without following links."""
    scratch_dir = scratch.path
    try:
        stats = scratch_dir.lstat()
    except FileNotFoundError:
        return []
    if (
        stat.S_ISLNK(stats.st_mode)
        or not stat.S_ISDIR(stats.st_mode)
        or stats.st_dev != scratch.device
        or stats.st_ino != scratch.inode
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

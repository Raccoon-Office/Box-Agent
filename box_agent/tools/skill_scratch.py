"""Session-scoped scratch storage for subprocess-backed Skills."""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path


SKILL_SCRATCH_DIR_NAME = ".box-agent-scratch"


@dataclass(frozen=True)
class SkillScratchDirectory:
    path: Path
    device: int
    inode: int


def prepare_skill_scratch_dir(workspace_dir: Path) -> SkillScratchDirectory:
    """Create the reserved Skill scratch root without accepting links."""
    scratch_dir = workspace_dir.resolve() / SKILL_SCRATCH_DIR_NAME
    try:
        stats = scratch_dir.lstat()
    except FileNotFoundError:
        scratch_dir.mkdir(mode=0o700)
        stats = scratch_dir.lstat()
    else:
        if stat.S_ISLNK(stats.st_mode) or not stat.S_ISDIR(stats.st_mode):
            raise RuntimeError(f"Skill scratch root must be a real directory: {scratch_dir}")
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
        entry_mode = entry.lstat().st_mode
        if stat.S_ISDIR(entry_mode) and not stat.S_ISLNK(entry_mode):
            shutil.rmtree(entry)
        else:
            entry.unlink()
        removed.append(str(entry))
    return removed

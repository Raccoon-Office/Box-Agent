"""Session-scoped scratch storage for subprocess-backed Skills."""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from weakref import WeakValueDictionary

from ..artifact_metadata import _profile_directory
from ..artifact_publication import ChatReferenceScanError, WORKSPACE_SCRATCH_PATH, retained_chat_file_in


SKILL_SCRATCH_DIR_NAME = ".box-agent-scratch"


@dataclass(frozen=True)
class SkillScratchDirectory:
    path: Path
    device: int
    inode: int
    _protected_references: set[Path] = field(default_factory=set, init=False, repr=False, compare=False)


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


def _absolute_without_resolving(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _is_link_or_reparse_point(stats: os.stat_result) -> bool:
    if stat.S_ISLNK(stats.st_mode):
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(getattr(stats, "st_file_attributes", 0) & reparse_flag)


def workspace_scratch_root(workspace: Path) -> Path:
    profile = _profile_directory()
    if workspace.resolve() / ".box-agent" == profile:
        try:
            if _is_link_or_reparse_point(profile.lstat()):
                # Keep scratch workspace-local when the global profile was relocated.
                return workspace / SKILL_SCRATCH_DIR_NAME
        except FileNotFoundError:
            pass
    return workspace / WORKSPACE_SCRATCH_PATH


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

    references = scratch._protected_references
    for path in tuple(references):
        try:
            path.stat()
        except (FileNotFoundError, NotADirectoryError):
            references.discard(path)
        except OSError:
            # An inaccessible preview must stay protected for later cleanups.
            continue
    if scratch_dir in references:
        return []
    protected_entries = {path.relative_to(scratch_dir).parts[0] for path in references}
    removed: list[str] = []
    for entry in scratch_dir.iterdir():
        if entry.name in protected_entries:
            continue
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

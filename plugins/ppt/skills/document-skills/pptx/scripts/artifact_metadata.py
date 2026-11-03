"""Portable per-path artifact records; standalone producers may copy this module."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import uuid
from typing import Iterator

SUFFIX = ".artifact.json"
SCOPE_FILE = ".artifact-delivery.json"
MAX_RECORD_BYTES = 16384


def canonical_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def resolve_workspace(path: str | Path, workspace: str | Path | None = None) -> Path:
    if workspace or os.environ.get("BOX_AGENT_WORKSPACE_DIR"):
        return canonical_path(workspace or os.environ["BOX_AGENT_WORKSPACE_DIR"])
    target = canonical_path(path)
    for parent in target.parents:
        if (parent / ".box-agent").exists() or (parent / ".box-agent").is_symlink() or (parent / ".git").exists():
            return parent
    cwd = Path.cwd().resolve()
    if target.is_relative_to(cwd):
        for parent in (cwd, *cwd.parents):
            if (parent / ".box-agent").exists() or (parent / ".git").exists():
                return parent
    return target.parent


def legacy_metadata_path(path: str | Path) -> Path:
    path = Path(path)
    return path.with_name(f".{path.name}{SUFFIX}")


def metadata_path(path: str | Path, workspace: str | Path | None = None) -> Path:
    target = canonical_path(path)
    return resolve_workspace(target, workspace) / ".box-agent/artifact-metadata/files" / (hashlib.sha256(str(target).encode("utf-8")).hexdigest() + ".json")


def scope_path(directory: str | Path, workspace: str | Path | None = None) -> Path:
    target = canonical_path(directory)
    return resolve_workspace(target / "_scope", workspace) / ".box-agent/artifact-metadata/scopes" / (hashlib.sha256(str(target).encode("utf-8")).hexdigest() + ".json")


def retention_directory() -> Path:
    """Shared derived references, independent of a session's workspace/cwd."""
    profile = Path(os.environ.get("BOX_AGENT_HOME") or Path.home() / ".box-agent").expanduser()
    return profile.parent.resolve() / profile.name / "artifact-metadata/retained"


def iter_retention_records(target: str | Path | None = None) -> Iterator[Path]:
    directory = retention_directory()
    _check_storage(directory)
    if target is not None:
        directory /= hashlib.sha256(str(canonical_path(target)).encode("utf-8")).hexdigest()
    pending = [(directory, target is None)]
    count = 0
    while pending:
        folder, descend = pending.pop()
        try:
            if _redirected(folder):
                raise OSError(f"Retention storage is redirected: {folder}")
            with os.scandir(folder) as entries:
                for entry in entries:
                    count += 1
                    if count > 10000:
                        raise OSError("Retention scan incomplete; preserve files")
                    if entry.is_symlink():
                        raise OSError(f"Retention record is redirected: {entry.path}")
                    yield Path(entry.path)
                    if descend and entry.is_dir(follow_symlinks=False):
                        pending.append((Path(entry.path), False))
        except FileNotFoundError:
            continue


def read_retention_record(marker: Path) -> tuple[Path, Path] | None:
    directory = retention_directory()
    if marker.suffix != ".json" or marker.parent.parent != directory:
        return None
    _check_storage(directory)
    try:
        if _redirected(marker.parent) or marker.is_symlink():
            raise OSError(f"Retention record is redirected: {marker}")
    except FileNotFoundError:
        return None
    value = _read_json(marker, strict=True)
    target, workspace = value.get("path"), value.get("workspace")
    if (not isinstance(target, str) or not Path(target).is_absolute()
            or not isinstance(workspace, str) or not Path(workspace).is_absolute()):
        return None
    if (marker.parent.name != hashlib.sha256(target.encode("utf-8")).hexdigest()
            or marker.name != hashlib.sha256(workspace.encode("utf-8")).hexdigest() + ".json"):
        return None
    return Path(target), Path(workspace)


def write_retention(path: str | Path, workspace: str | Path | None = None) -> None:
    target = canonical_path(path)
    root = resolve_workspace(target, workspace)
    directory = retention_directory()
    _check_storage(directory)
    profile = directory.parent.parent
    profile.mkdir(parents=True, exist_ok=True)
    marker = directory / hashlib.sha256(str(target).encode("utf-8")).hexdigest() / (
        hashlib.sha256(str(root).encode("utf-8")).hexdigest() + ".json")
    # Independent owner records avoid read/modify/write races between chats.
    if read_retention_record(marker) is None:
        _write(marker, {"path": str(target), "workspace": str(root)}, profile)


def _redirected(path: Path) -> bool:
    value = path.lstat()
    return stat.S_ISLNK(value.st_mode) or bool(getattr(value, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _check_storage(directory: Path, *, create: bool = False) -> None:
    # Do not resolve these components: resolving would silently accept redirects.
    for path in (directory.parent.parent, directory.parent, directory):
        if create:
            path.mkdir(exist_ok=True)
        try:
            if _redirected(path) or not path.is_dir():
                raise OSError(f"Artifact metadata storage is redirected: {path}")
        except FileNotFoundError:
            return


def _read_json(path: Path, limit: int = MAX_RECORD_BYTES, *, strict: bool = False) -> dict:
    try:
        if path.stat().st_size > limit:
            return {}
        with path.open(encoding="utf-8") as stream:
            value = json.loads(stream.read(limit + 1))
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        return {}
    except OSError:
        if strict:
            raise
        return {}
    except ValueError:
        return {}


def is_metadata_record(path: Path) -> bool:
    return (len(path.parts) >= 4 and path.parts[-4:-1] == (".box-agent", "artifact-metadata", "files")
            and path.suffix == ".json")


def read_record(marker: Path) -> tuple[Path, dict] | None:
    """Validate a central record before trusting either its path or metadata."""
    _check_storage(marker.parent)
    if marker.is_symlink():
        raise OSError(f"Artifact metadata record is redirected: {marker}")
    value = _read_json(marker, strict=True)
    path, metadata = value.get("path"), value.get("metadata")
    if not isinstance(path, str) or not Path(path).is_absolute() or not isinstance(metadata, dict):
        return None
    if marker.name != hashlib.sha256(path.encode("utf-8")).hexdigest() + ".json":
        return None
    if len(json.dumps(metadata, ensure_ascii=False).encode("utf-8")) > 4096:
        return None
    return Path(path), metadata


def _nested_record_paths(target: Path, workspace: str | Path, *, scope: bool = False) -> Iterator[Path]:
    root = canonical_path(workspace)
    parents = (target, *target.parents) if scope else target.parents
    for parent in parents:
        if parent == root:
            continue
        yield scope_path(target, parent) if scope else metadata_path(target, parent)


def _nested_records(target: Path, workspace: str | Path, *, scope: bool = False) -> Iterator[tuple[Path, dict]]:
    for marker in _nested_record_paths(target, workspace, scope=scope):
        record = read_record(marker)
        if record:
            yield record


def remove_metadata(path: str | Path, workspace: str | Path | None = None) -> None:
    """Release derived records only after the caller durably releases a path."""
    target = canonical_path(path)
    root = resolve_workspace(target, workspace)
    retained = list(iter_retention_records(target))
    owners = {record[1] for marker in retained if (record := read_retention_record(marker))}
    markers = {metadata_path(target, owner) for owner in owners | {root}}
    markers.update(_nested_record_paths(target, root))
    for marker in markers:
        read_record(marker)  # Reject redirects before removing a derived cache.
        marker.unlink(missing_ok=True)
    for marker in retained:
        if read_retention_record(marker):
            marker.unlink(missing_ok=True)
    for folder in {marker.parent for marker in retained}:
        try:
            folder.rmdir()
        except OSError:
            pass  # Preserve other owners or concurrent new references.


def read_metadata(path: str | Path, workspace: str | Path | None = None) -> dict:
    target = canonical_path(path)
    root = resolve_workspace(target, workspace)
    record = read_record(metadata_path(target, root))
    values = [record[1]] if record and record[0] == target else []
    values.extend(value for original, value in _nested_records(target, root) if original == target)
    values.append(_read_json(legacy_metadata_path(target), 4096))
    value = next((value for value in values if value), {})
    # Retention is monotonic across standalone, runtime and legacy stores.
    if any(value.get("retain_for_chat") is True for value in values):
        return {**value, "retain_for_chat": True}
    return value


def read_scope(directory: str | Path, workspace: str | Path | None = None) -> dict:
    target = canonical_path(directory)
    root = resolve_workspace(target / "_scope", workspace)
    record = read_record(scope_path(target, root))
    if record is None:
        record = next(_nested_records(target, root, scope=True), None)
    return record[1] if record and record[0] == target else _read_json(target / SCOPE_FILE, 4096)


def _exclude_internal_directory(root: Path) -> None:
    """Use Git's local excludes, including gitfiles and linked worktrees."""
    for parent in (root, *root.parents):
        marker = parent / ".git"
        if marker.is_symlink():
            return
        if marker.is_dir():
            git_dir = marker
        elif marker.is_file():
            text = marker.read_text(encoding="utf-8").strip()
            if not text.startswith("gitdir:"):
                return
            git_dir = (parent / text.partition(":")[2].strip()).resolve()
        else:
            continue
        common = git_dir / "commondir"
        if common.is_file():
            git_dir = (git_dir / common.read_text(encoding="utf-8").strip()).resolve()
        info = git_dir / "info"
        if info.is_symlink():
            return
        info.mkdir(exist_ok=True)
        exclude = info / "exclude"
        if exclude.is_symlink():
            return
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if ".box-agent/" not in existing.splitlines():
            with exclude.open("a", encoding="utf-8") as stream:
                stream.write(("\n" if existing and not existing.endswith("\n") else "") + ".box-agent/\n")
        return


def _write(path: Path, value: dict, root: Path) -> None:
    if path.is_symlink():
        raise OSError(f"Artifact metadata record is redirected: {path}")
    data = json.dumps(value, ensure_ascii=False) + "\n"
    temporary = ".artifact-" + uuid.uuid4().hex + ".tmp"
    # Holding the directory descriptor prevents a swapped ancestor from
    # redirecting the atomic write on systems supporting descriptor-relative IO.
    if os.open in os.supports_dir_fd and os.rename in os.supports_dir_fd:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(root, flags)
        try:
            for part in path.parent.relative_to(root).parts:
                try:
                    os.mkdir(part, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child
            out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd)
            try:
                with os.fdopen(out, "w", encoding="utf-8") as stream:
                    stream.write(data)
                _check_storage(path.parent)
                os.replace(temporary, path.name, src_dir_fd=fd, dst_dir_fd=fd)
            finally:
                try:
                    os.unlink(temporary, dir_fd=fd)
                except FileNotFoundError:
                    pass
        finally:
            os.close(fd)
    else:
        _check_storage(path.parent, create=True)
        temp = path.parent / temporary
        try:
            with temp.open("x", encoding="utf-8") as stream:
                stream.write(data)
            _check_storage(path.parent)
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
    try:
        _exclude_internal_directory(root)
    except OSError:
        pass  # Read-only Git administration must not discard artifact records.


def write_metadata(path: str | Path, value: dict, workspace: str | Path | None = None) -> None:
    target = canonical_path(path)
    if read_metadata(target, workspace).get("retain_for_chat") is True:
        value = {**value, "retain_for_chat": True}
    root = resolve_workspace(target, workspace)
    _write(metadata_path(target, root), {"path": str(target), "metadata": value}, root)


def write_scope(directory: str | Path, value: dict, workspace: str | Path | None = None) -> None:
    target = canonical_path(directory)
    root = resolve_workspace(target / "_scope", workspace)
    _write(scope_path(target, root), {"path": str(target), "metadata": value}, root)


def iter_metadata_records(workspace: str | Path) -> Iterator[Path]:
    """Read only this workspace's central file store; callers bound traversal."""
    directory = canonical_path(workspace) / ".box-agent/artifact-metadata/files"
    _check_storage(directory)
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name.endswith(".json"):
                    if entry.is_symlink():
                        raise OSError(f"Artifact metadata record is redirected: {entry.path}")
                    if entry.is_file(follow_symlinks=False):
                        yield Path(entry.path)
    except FileNotFoundError:
        return

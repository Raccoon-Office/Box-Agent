"""File-owned publication metadata, retained when an asset is renamed/copied."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
from typing import Iterable, Iterator
from time import monotonic

from . import artifact_metadata as storage

SUFFIX = ".artifact.json"
SCOPE_FILE = ".artifact-delivery.json"
STAGING_DIRECTORY = ".box-agent-staging"
MAX_HASH_BYTES = 64 * 1024 * 1024
_REFERENCE_SCAN_MAX_ENTRIES = 10_000
_REFERENCE_SCAN_TIMEOUT_SECONDS = 0.5


class ChatReferenceScanError(RuntimeError):
    """An incomplete protection check must never authorize deletion."""


def is_publication_sidecar(path: Path) -> bool:
    name = path.name
    return name.startswith(".") and name.endswith(SUFFIX) and len(name) > len(SUFFIX) + 1


def is_publication_metadata(path: Path, workspace: str | Path | None = None) -> bool:
    """Reserved publication records are protocol data, never deliverables."""
    if path.name == SCOPE_FILE or is_publication_sidecar(path):
        return True
    if workspace:
        try:
            relative = path.absolute().relative_to(Path(workspace).absolute())
            if relative.parts and relative.parts[0] == ".box-agent":
                return True
        except ValueError:
            pass
    return (any(path.parts[index:index + 2] == (".box-agent", "artifact-metadata")
                for index in range(len(path.parts) - 1))
            or path.absolute().is_relative_to(storage.retention_directory()))


def is_staging_file(path: Path, workspace: str | Path) -> bool:
    """Publication transaction inputs are internal, even with artifact sidecars."""
    try:
        relative = path.resolve().relative_to(Path(workspace).resolve())
        return bool(relative.parts) and relative.parts[0] == STAGING_DIRECTORY
    except (OSError, RuntimeError, ValueError):
        return False


def metadata_path(path: Path, workspace: str | Path | None = None) -> Path:
    return storage.metadata_path(path, workspace)


def read_metadata(path: Path) -> dict:
    """Compatibility reader for legacy markers and centralized records."""
    if is_publication_sidecar(path):
        return storage.read_metadata(path.with_name(path.name[1:-len(SUFFIX)]))
    if path.name == SCOPE_FILE:
        return storage.read_scope(path.parent)
    record = storage.read_record(path)
    return record[1] if record else {}


def content_fingerprint(path: Path) -> tuple[int, str] | None:
    try:
        if not path.is_file():
            return None
        size = path.stat().st_size
        if size > MAX_HASH_BYTES:
            return None
        digest = hashlib.sha256()
        total = 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_HASH_BYTES:
                    return None
                digest.update(chunk)
        return (total, digest.hexdigest()) if total == size else None
    except OSError:
        return None


def write_metadata(path: Path, value: dict, workspace: str | Path | None = None) -> None:
    storage.write_metadata(path, value, workspace)


def retain_chat_file(path: Path, workspace: str | Path | None = None) -> None:
    """Cache retention without promoting a file's delivery placement."""
    if is_publication_metadata(path, workspace) or not path.resolve().name:
        return
    value = storage.read_metadata(path, workspace)
    if (value.get("retain_for_chat") is not True
            or not storage.metadata_path(path, workspace).exists()):
        write_metadata(path, {**value, "retain_for_chat": True}, workspace)
    storage.write_retention(path, workspace)


def retained_chat_files_in(path: Path, workspace: str | Path | None = None) -> Iterator[Path]:
    """Check legacy and central caches with bounded, non-following scans."""
    if not path.name:
        return
    deadline = monotonic() + _REFERENCE_SCAN_TIMEOUT_SECONDS
    count = 0

    def checked():
        nonlocal count
        count += 1
        if count > _REFERENCE_SCAN_MAX_ENTRIES or monotonic() >= deadline:
            raise ChatReferenceScanError(
                f"CHAT_REFERENCE_PROTECTED: retention scan incomplete for {path}; preserve it or use a narrower cleanup target"
            )

    try:
        original = path.with_name(path.name[1:-len(SUFFIX)]) if is_publication_sidecar(path) else path
        for candidate in (original, *original.parents):
            if not candidate.name:
                continue
            if storage.read_metadata(candidate, workspace).get("retain_for_chat") is True:
                yield candidate
            for marker in storage.iter_retention_records(candidate):
                checked()
                record = storage.read_retention_record(marker)
                if record and record[0] == candidate.resolve():
                    yield record[0]
        if storage.is_metadata_record(path):
            record = storage.read_record(path)
            if record and record[1].get("retain_for_chat") is True:
                yield record[0]
        if record := storage.read_retention_record(path):
            yield record[0]
        if path.is_dir() and not path.is_symlink():
            # Publication history is unrelated to retention. Only the shared
            # reference cache and the affected subtree need enumeration.
            for marker in storage.iter_retention_records():
                checked()
                record = storage.read_retention_record(marker)
                if record and (record[0].is_relative_to(original.resolve())
                               or marker.is_relative_to(path)):
                    yield record[0]
            pending = [path]
            while pending:
                with os.scandir(pending.pop()) as entries:
                    for entry in entries:
                        checked()
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(Path(entry.path))
                        elif is_publication_sidecar(Path(entry.path)):
                            if read_metadata(Path(entry.path)).get("retain_for_chat") is True:
                                yield Path(entry.path).with_name(entry.name[1:-len(SUFFIX)])
                        elif storage.is_metadata_record(Path(entry.path)):
                            record = storage.read_record(Path(entry.path))
                            if record and record[1].get("retain_for_chat") is True:
                                yield record[0]
                        elif entry.is_symlink() and entry.name in {".box-agent", "artifact-metadata", "files"}:
                            raise OSError("redirected retention storage")
    except (OSError, RuntimeError) as exc:
        if isinstance(exc, ChatReferenceScanError):
            raise
        raise ChatReferenceScanError(f"CHAT_REFERENCE_PROTECTED: cannot check retention for {path}; preserve it") from exc


def retained_chat_file_in(path: Path, workspace: str | Path | None = None) -> Path | None:
    """Find the first retained file, rejecting incomplete directory scans."""
    return next(retained_chat_files_in(path, workspace), None)


def intermediate_fingerprints(metadata_files: Iterable[Path], workspace: str | Path | None = None) -> dict[int, set[str]]:
    """Build a discovery-local index from sidecars, never process-global state.

    Enrich older producer markers while the original file is still available.
    Orphan markers retain provenance after a shell rename or copy/delete.
    """
    result: dict[int, set[str]] = {}
    for marker in metadata_files:
        value = read_metadata(marker)
        if value.get("type") != "intermediate_asset":
            continue
        size, digest = value.get("size_bytes"), value.get("sha256")
        if not (isinstance(size, int) and 0 <= size <= MAX_HASH_BYTES
                and isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)):
            record = storage.read_record(marker) if not is_publication_sidecar(marker) else None
            original = record[0] if record else marker.with_name(marker.name[1:-len(SUFFIX)])
            fingerprint = content_fingerprint(original)
            if fingerprint is None:
                continue
            size, digest = fingerprint
            try:
                write_metadata(original, {**value, "size_bytes": size, "sha256": digest}, workspace)
            except OSError:
                pass
        result.setdefault(size, set()).add(digest)
    return result


def is_intermediate(path: Path, fingerprints: dict[int, set[str]], workspace: str | None = None) -> bool:
    if is_publication_metadata(path, workspace):
        return True
    if workspace and is_staging_file(path, workspace):
        return True
    value = storage.read_metadata(path, workspace)
    if value.get("type") == "intermediate_asset":
        return True
    if value.get("type") == "artifact":
        return False  # Explicit publication of this path overrides inherited suppression.
    if workspace:
        if delivery_scope(path, workspace) is not None:
            fingerprint = content_fingerprint(path)
            metadata = {"type": "intermediate_asset"}
            if fingerprint:
                metadata.update(size_bytes=fingerprint[0], sha256=fingerprint[1])
            try:
                write_metadata(path, metadata, workspace)
            except OSError:
                pass
            return True
    try:
        if path.stat().st_size not in fingerprints:
            return False
    except OSError:
        return False
    fingerprint = content_fingerprint(path)
    if not fingerprint or fingerprint[1] not in fingerprints.get(fingerprint[0], set()):
        return False
    size, digest = fingerprint
    try:
        write_metadata(path, {"type": "intermediate_asset", "size_bytes": size, "sha256": digest}, workspace)
    except OSError:
        pass
    return True


def delivery_scope(path: Path, workspace: str | Path) -> Path | None:
    """Find a declared scope without inferring intent from directory names."""
    try:
        root = Path(workspace).resolve()
        target = path.resolve()
        target.relative_to(root)
        if target == root:
            return None
        for parent in target.parents:
            declaration = storage.read_scope(parent, root)
            if declaration.get("schema_version") == 1 and declaration.get("default") == "intermediate":
                return parent
            if parent == root:
                break
    except (OSError, ValueError, RuntimeError):
        pass
    return None


def is_unpublished_scoped_file(path: Path, workspace: str | Path) -> bool:
    if delivery_scope(path, workspace) is None:
        return False
    return storage.read_metadata(path, workspace).get("type") != "artifact"


def apply_delivery_policy(raw_output: dict | None, workspace: str | None) -> dict | None:
    """Guard structured tool outputs as well as the file-discovery channel."""
    if not workspace or not raw_output or raw_output.get("type") != "artifact":
        return raw_output
    value = raw_output.get("abs_path") or raw_output.get("absolute_path") or raw_output.get("path")
    if not isinstance(value, str) or not value.strip():
        return raw_output
    path = Path(value)
    if not path.is_absolute():
        path = Path(workspace) / path
    if is_publication_metadata(path, workspace) or is_staging_file(path, workspace):
        return {**raw_output, "type": "intermediate_asset"}
    if is_unpublished_scoped_file(path, workspace):
        if path.is_file():
            is_intermediate(path, {}, workspace)  # Persist identity before a later move out of scope.
        return {**raw_output, "type": "intermediate_asset"}
    return raw_output

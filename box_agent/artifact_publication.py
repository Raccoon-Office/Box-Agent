"""File-owned publication metadata, retained when an asset is renamed/copied."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Iterable, Iterator
from time import monotonic

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


def is_publication_metadata(path: Path) -> bool:
    """Reserved publication records are protocol data, never deliverables."""
    return path.name == SCOPE_FILE or is_publication_sidecar(path)


def is_staging_file(path: Path, workspace: str | Path) -> bool:
    """Publication transaction inputs are internal, even with artifact sidecars."""
    try:
        relative = path.resolve().relative_to(Path(workspace).resolve())
        return bool(relative.parts) and relative.parts[0] == STAGING_DIRECTORY
    except (OSError, RuntimeError, ValueError):
        return False


def metadata_path(path: Path) -> Path:
    return path.with_name(f".{path.name}{SUFFIX}")


def read_metadata(path: Path) -> dict:
    try:
        if path.stat().st_size > 4096:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


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


def write_metadata(path: Path, value: dict) -> None:
    """Atomic sidecar writes also work for concurrent independent producers."""
    target = metadata_path(path)
    if read_metadata(target).get("retain_for_chat") is True:
        value = {**value, "retain_for_chat": True}
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                     prefix=".artifact-", suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, ensure_ascii=False)
            stream.write("\n")
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def retain_chat_file(path: Path) -> None:
    """Cache retention without promoting a file's delivery placement."""
    if is_publication_metadata(path):
        return
    value = read_metadata(metadata_path(path))
    if value.get("retain_for_chat") is not True:
        write_metadata(path, {**value, "retain_for_chat": True})


def retained_chat_files_in(path: Path) -> Iterator[Path]:
    """Read retention markers with a bounded, non-symlink-following walk."""
    if not path.name:
        return
    marker = path if path.name.startswith(".") and path.name.endswith(SUFFIX) else metadata_path(path)
    if read_metadata(marker).get("retain_for_chat") is True:
        yield path
    for parent in path.parents:
        if parent.name and read_metadata(metadata_path(parent)).get("retain_for_chat") is True:
            yield parent
    if path.is_dir() and not path.is_symlink():
        deadline = monotonic() + _REFERENCE_SCAN_TIMEOUT_SECONDS
        pending, count = [path], 0
        try:
            while pending:
                with os.scandir(pending.pop()) as entries:
                    for entry in entries:
                        count += 1
                        if count > _REFERENCE_SCAN_MAX_ENTRIES or monotonic() >= deadline:
                            raise ChatReferenceScanError(
                                f"CHAT_REFERENCE_PROTECTED: retention scan incomplete for {path}; preserve it or use a narrower cleanup target"
                            )
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(Path(entry.path))
                        elif entry.name.startswith(".") and entry.name.endswith(SUFFIX):
                            marker = Path(entry.path)
                            if read_metadata(marker).get("retain_for_chat") is True:
                                yield marker.with_name(marker.name[1:-len(SUFFIX)])
        except OSError as exc:
            raise ChatReferenceScanError(f"CHAT_REFERENCE_PROTECTED: cannot check retention for {path}; preserve it") from exc


def retained_chat_file_in(path: Path) -> Path | None:
    """Find the first retained file, rejecting incomplete directory scans."""
    return next(retained_chat_files_in(path), None)


def intermediate_fingerprints(metadata_files: Iterable[Path]) -> dict[int, set[str]]:
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
            original = marker.with_name(marker.name[1:-len(SUFFIX)])
            fingerprint = content_fingerprint(original)
            if fingerprint is None:
                continue
            size, digest = fingerprint
            try:
                write_metadata(original, {**value, "size_bytes": size, "sha256": digest})
            except OSError:
                pass
        result.setdefault(size, set()).add(digest)
    return result


def is_intermediate(path: Path, fingerprints: dict[int, set[str]], workspace: str | None = None) -> bool:
    if is_publication_metadata(path):
        return True
    if workspace and is_staging_file(path, workspace):
        return True
    value = read_metadata(metadata_path(path))
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
                write_metadata(path, metadata)
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
        write_metadata(path, {"type": "intermediate_asset", "size_bytes": size, "sha256": digest})
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
            declaration = read_metadata(parent / SCOPE_FILE)
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
    return read_metadata(metadata_path(path)).get("type") != "artifact"


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
    if is_publication_metadata(path) or is_staging_file(path, workspace):
        return {**raw_output, "type": "intermediate_asset"}
    if is_unpublished_scoped_file(path, workspace):
        if path.is_file():
            is_intermediate(path, {}, workspace)  # Persist identity before a later move out of scope.
        return {**raw_output, "type": "intermediate_asset"}
    return raw_output

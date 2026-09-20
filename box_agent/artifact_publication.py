"""File-owned publication metadata, retained when an asset is renamed/copied."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Iterable

SUFFIX = ".artifact.json"
MAX_HASH_BYTES = 64 * 1024 * 1024


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


SCOPE_FILE = ".artifact-delivery.json"


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
    if is_unpublished_scoped_file(path, workspace):
        if path.is_file():
            is_intermediate(path, {}, workspace)  # Persist identity before a later move out of scope.
        return {**raw_output, "type": "intermediate_asset"}
    return raw_output

"""Content-addressed installation of trusted, non-executing resource packages."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

from .manifest import PluginError, ResourceManifest, json_bytes, load_manifest

MAX_PACKAGE_BYTES = 512 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_PACKAGE_FILES = 20_000
_OMIT_DIRECTORIES = frozenset({".git", ".hg", ".svn", "__pycache__", ".pytest_cache", ".venv", "node_modules"})


def _inventory(root: Path) -> list[Path]:
    files: list[Path] = []
    size = 0
    for directory, subdirs, filenames in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in list(subdirs):
            target = base / name
            if target.is_symlink():
                raise PluginError("invalid_path", "Packages may not contain symbolic links",
                                  {"path": target.relative_to(root).as_posix()})
            if name in _OMIT_DIRECTORIES:
                subdirs.remove(name)
        for name in filenames:
            path = base / name
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise PluginError("invalid_path", "Packages may contain regular files only",
                                  {"path": path.relative_to(root).as_posix()})
            if name.endswith((".pyc", ".pyo")):
                continue
            if info.st_size > MAX_FILE_BYTES:
                raise PluginError("package_too_large", "Package file exceeds 64 MiB",
                                  {"path": path.relative_to(root).as_posix()})
            size += info.st_size
            files.append(path)
            if len(files) > MAX_PACKAGE_FILES or size > MAX_PACKAGE_BYTES:
                raise PluginError("package_too_large", "Package exceeds 20,000 files or 512 MiB")
    return sorted(files, key=lambda p: p.relative_to(root).as_posix())


def package_digest(root: Path) -> str:
    """Hash names, execution bits and content; ignore only defined build caches."""
    digest = hashlib.sha256()
    for path in _inventory(root):
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise PluginError("invalid_path", "Package resource is not a regular file")
            content = hashlib.sha256()
            count = 0
            while chunk := stream.read(1024 * 1024):
                count += len(chunk)
                if count > MAX_FILE_BYTES:
                    raise PluginError("package_too_large", "Package file exceeds 64 MiB")
                content.update(chunk)
        descriptor_data = [path.relative_to(root).as_posix(), bool(info.st_mode & 0o111), count, content.hexdigest()]
        digest.update(json_bytes(descriptor_data) + b"\n")
    return digest.hexdigest()


def _remove_staging(path: Path) -> None:
    if not path.exists():
        return
    for directory, _, files in os.walk(path):
        os.chmod(directory, 0o700)
        for name in files:
            os.chmod(Path(directory) / name, 0o600)
    shutil.rmtree(path)


class PackageStore:
    def __init__(self, root: Path, *, host_version: str = "1.0.0") -> None:
        self.root = Path(root).resolve()
        self.host_version = host_version

    def install(self, source: Path) -> dict[str, Any]:
        source = Path(source).expanduser()
        if source.is_symlink() or not source.is_dir():
            raise PluginError("invalid_path", "Install source must be a real package directory")
        source = source.resolve()
        if self.root.is_relative_to(source) or source.is_relative_to(self.root):
            raise PluginError("invalid_path", "Install source must be outside the managed package store")
        load_manifest(source, host_version=self.host_version)
        original_digest = package_digest(source)
        self.root.mkdir(parents=True, exist_ok=True)
        staged = Path(tempfile.mkdtemp(prefix=".install-", dir=self.root))
        try:
            for path in _inventory(source):
                relative = path.relative_to(source)
                destination = staged / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                with os.fdopen(os.open(path, flags), "rb") as reader, destination.open("xb") as writer:
                    info = os.fstat(reader.fileno())
                    if not stat.S_ISREG(info.st_mode):
                        raise PluginError("invalid_path", "Package changed while being installed")
                    count = 0
                    while chunk := reader.read(1024 * 1024):
                        count += len(chunk)
                        if count > MAX_FILE_BYTES:
                            raise PluginError("package_too_large", "Package file grew beyond its size limit")
                        writer.write(chunk)
                    writer.flush()
                    os.fsync(writer.fileno())
                os.chmod(destination, 0o555 if info.st_mode & 0o111 else 0o444)
            manifest = load_manifest(staged, host_version=self.host_version)
            digest = package_digest(staged)
            if digest != original_digest or package_digest(source) != original_digest:
                raise PluginError("package_changed", "Package changed during installation; retry from a fixed source")
            destination = self.root / digest
            for directory, _, _ in os.walk(staged, topdown=False):
                # macOS requires the directory being renamed to remain writable.
                # Its children can already be read-only before publication.
                if Path(directory) != staged:
                    os.chmod(directory, 0o555)
            if destination.exists():
                if destination.is_symlink() or package_digest(destination) != digest:
                    raise PluginError("package_corrupt", "Existing installed package does not match its content hash")
            else:
                try:
                    staged.rename(destination)
                except OSError:
                    # POSIX directory rename may report ENOTEMPTY rather than
                    # EEXIST when another process has published the same hash.
                    if not destination.is_dir() or destination.is_symlink() or package_digest(destination) != digest:
                        raise PluginError("package_corrupt", "Concurrent installation produced invalid package content")
                os.chmod(destination, 0o555)
            if os.name != "nt":
                descriptor = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            return {"package_id": manifest.data["id"], "package_version": manifest.data["version"],
                    "package_digest": digest, "schema_digest": manifest.schema_digest,
                    "package_root": str(destination)}
        finally:
            _remove_staging(staged)

    def load(self, digest: str, *, verify: bool = True) -> ResourceManifest:
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise PluginError("invalid_package", "Invalid installed package digest")
        root = self.root / digest
        if root.is_symlink() or not root.is_dir():
            raise PluginError("package_unavailable", "Installed package is unavailable", {"package_digest": digest})
        if verify and package_digest(root) != digest:
            raise PluginError("package_corrupt", "Installed package content changed", {"package_digest": digest})
        return load_manifest(root, host_version=self.host_version)

"""Session-local proof for cleaning newly created, unpublished regular files."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shlex
import stat

from .shell_inspection import inspect_shell_command


class OwnedFileCleanup:
    def __init__(self, workspace: str | Path):
        self.root = Path(workspace).resolve()
        self.files: dict[Path, tuple[tuple[int, ...], str]] = {}

    def path(self, value: str, cwd: Path | None = None) -> Path:
        path = Path(value)
        if ".." in path.parts:
            raise ValueError("temporary file path must not traverse parents")
        if not path.is_absolute():
            path = (cwd or self.root) / path
        path = Path(os.path.abspath(path))
        relative = path.relative_to(self.root)
        if not relative.parts:
            raise ValueError("workspace root is not a temporary file")
        current = self.root
        for part in ("", *relative.parts):
            if part:
                current /= part
            try:
                stats = current.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(stats.st_mode) or (
                getattr(stats, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            ):
                raise ValueError("temporary file path contains a symlink")
        return path

    def fingerprint(self, path: Path) -> tuple[tuple[int, ...], str]:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 64 * 1024 * 1024:
            raise ValueError("not a bounded, privately created regular file")
        signature = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        after = path.lstat()
        if signature != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("file changed while recording ownership")
        return signature, digest

    def record(self, value: str, *, sha256: str | None = None) -> None:
        """Called only with proof that a producer created a previously absent path."""
        try:
            path = self.path(value)
            fingerprint = self.fingerprint(path)
            if sha256 is None or fingerprint[1] == sha256:
                self.files[path] = fingerprint
        except (OSError, ValueError):
            return

    def reserve(self, values: list[str]) -> dict:
        """Exclusively reserve declared new outputs before running a script."""
        reservations = {}
        try:
            for value in values:
                path = self.path(value)
                with path.open("xb"):
                    pass
                reservations[path] = self.fingerprint(path)
            return reservations
        except (OSError, ValueError):
            self.discard_empty(reservations)
            raise

    def finish(self, reservations: dict) -> None:
        for path, (signature, _) in reservations.items():
            try:
                current = self.fingerprint(self.path(str(path)))
                if current[0][:2] == signature[:2]:
                    self.files[path] = current
            except (OSError, ValueError):
                continue

    def discard_empty(self, reservations: dict) -> None:
        for path, expected in reservations.items():
            try:
                if path not in self.files and self.fingerprint(self.path(str(path))) == expected:
                    path.unlink()
            except (OSError, ValueError):
                continue

    def may_remove(self, value: str, cwd: Path) -> bool:
        try:
            path = self.path(value, cwd)
            # Published or metadata-bearing files require the ordinary approval
            # flow. Never infer disposability from a QA/tmp filename.
            metadata = path.with_name(f".{path.name}.artifact.json")
            if metadata.exists() or metadata.is_symlink():
                return False
            return path in self.files and self.fingerprint(path) == self.files[path]
        except (OSError, ValueError):
            return False

    def targets(self, command: str) -> list[Path]:
        """Accept exact rm targets with optional cd/ls joined only by &&.

        Dynamic paths, recursive deletion, wrappers and other operations stay
        on the normal approval path; they cannot inherit this exception.
        """
        inspection = inspect_shell_command(command)
        if inspection.substitutions or inspection.redirections or inspection.ambiguous_regions:
            return []
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>\n")
            lexer.whitespace = " \t\r"
            lexer.whitespace_split = True
            lexer.commenters = ""
            words = list(lexer)
            if any(word and all(char in ";&|()<>\n" for char in word) and word != "&&" for word in words):
                return []
            cwd = self.root
            targets = []
            for invocation in inspection.invocations:
                if invocation.prefix or invocation.indirect or invocation.dynamic_executable_sources:
                    return []
                args = list(invocation.arguments)
                if any(any(char in arg for char in "$`*?[]{}~") for arg in args):
                    return []
                if invocation.executable == "cd":
                    if len(args) != 1 or args[0].startswith("-"):
                        return []
                    # Bare relative cd may be redirected by the shell's CDPATH.
                    if ".." in Path(args[0]).parts or not (Path(args[0]).is_absolute() or args[0].startswith("./")):
                        return []
                    candidate = Path(os.path.abspath(cwd / args[0]))
                    cwd = self.root if candidate == self.root else self.path(args[0], cwd)
                    if not cwd.is_dir():
                        return []
                elif invocation.executable == "ls":
                    if any(arg not in {"-l", "-a", "-la", "-al"} for arg in args):
                        return []
                elif invocation.executable == "rm":
                    if args and args[0] == "-f":
                        args.pop(0)
                    if args and args[0] == "--":
                        args.pop(0)
                    if not args or any(arg.startswith("-") or not self.may_remove(arg, cwd) for arg in args):
                        return []
                    targets.extend(self.path(arg, cwd) for arg in args)
                else:
                    return []
            return targets
        except (OSError, ValueError):
            return []

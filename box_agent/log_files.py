"""File sinks for diagnostics that can contain private conversation data."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from typing import Literal, TextIO


def make_private_log_dir(path: Path) -> None:
    """Create an owned log directory and tighten existing POSIX permissions."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if info.st_uid != os.geteuid():
            raise PermissionError("Diagnostic directory is owned by another user")
        if stat.S_IMODE(info.st_mode) & 0o077:
            os.fchmod(descriptor, stat.S_IMODE(info.st_mode) & 0o700)
    finally:
        os.close(descriptor)


def _private_log_descriptor(path: str, flags: int) -> int:
    try:
        descriptor = os.open(
            path, flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), 0o600,
        )
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PermissionError("Diagnostic file is a symbolic link") from exc
        raise
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PermissionError("Diagnostic destination must be a regular file with one link")
        if os.name == "posix":
            if info.st_uid != os.geteuid():
                raise PermissionError("Diagnostic file is owned by another user")
            if stat.S_IMODE(info.st_mode) & 0o077:
                # Older readers can keep using an open descriptor after chmod.
                raise PermissionError("Diagnostic file has group or other permissions")
        elif os.name == "nt":
            # The CRT ignores POSIX mode bits on Windows. Check the inherited
            # ACL on the actual open file before passing it to a writer.
            import msvcrt

            from .desktop_memory.windows_files import WindowsFileAPI, validate_private_acl

            api = WindowsFileAPI()
            try:
                validate_private_acl(*api.security(msvcrt.get_osfhandle(descriptor)), api.user_sid())
            except (OSError, ValueError) as exc:
                raise PermissionError("Could not verify a private diagnostic file ACL") from exc
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def open_private_log(
    path: str | Path, mode: Literal["a", "x"], *, buffering: int = -1, errors: str | None = None,
) -> TextIO:
    """Open a diagnostic file after verifying access on the open descriptor."""
    return open(path, mode, encoding="utf-8", buffering=buffering, errors=errors, opener=_private_log_descriptor)

"""Exclusive file locks shared by presentation data writers and admission slots."""

import errno
import os
import time


def lock_file(handle, blocking: bool = True) -> None:
    """Lock a file; Windows waits at most 30 seconds for byte-zero ownership."""
    if os.name != "nt":
        import fcntl

        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        fcntl.flock(handle.fileno(), flags)
        return

    import msvcrt

    handle.flush()
    if os.fstat(handle.fileno()).st_size == 0:
        handle.write(b"\0" if "b" in handle.mode else "\0")
        handle.flush()
    deadline = time.monotonic() + 30
    while True:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError as exc:
            winerror = getattr(exc, "winerror", None)
            busy = (winerror == 33 if winerror is not None else
                    exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK})
            if not busy:
                raise
            if not blocking or time.monotonic() >= deadline:
                raise BlockingIOError(errno.EAGAIN, "Presentation file lock is busy") from exc
            time.sleep(0.05)


def unlock_file(handle) -> None:
    """Release the same lock range without changing ownership of other files."""
    if os.name != "nt":
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return

    import msvcrt

    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

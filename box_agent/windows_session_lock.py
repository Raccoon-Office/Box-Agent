"""Preserve the Win32 result of acquiring the one-byte session writer lock."""

from __future__ import annotations

import ctypes
from functools import lru_cache
from pathlib import Path


ERROR_ACCESS_DENIED = 5
WINDOWS_LOCK_CONTENTION = frozenset({32, 33})  # sharing and byte-range violations

_DWORD = ctypes.c_uint32
_HANDLE = ctypes.c_void_p
_INVALID_HANDLE = _HANDLE(-1).value
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_SHARE_READ_WRITE = 3
_OPEN_EXISTING = 3
_OPEN_ALWAYS = 4
_FILE_ATTRIBUTE_NORMAL = 0x80
_EXCLUSIVE_NO_WAIT = 3


class _Overlapped(ctypes.Structure):
    _fields_ = [
        ("internal", ctypes.c_size_t),
        ("internal_high", ctypes.c_size_t),
        ("offset", _DWORD),
        ("offset_high", _DWORD),
        ("event", _HANDLE),
    ]


@lru_cache(maxsize=1)
def _kernel32():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = [
        ctypes.c_wchar_p, _DWORD, _DWORD, ctypes.c_void_p, _DWORD, _DWORD, _HANDLE,
    ]
    api.CreateFileW.restype = _HANDLE
    api.LockFileEx.argtypes = [
        _HANDLE, _DWORD, _DWORD, _DWORD, _DWORD, ctypes.POINTER(_Overlapped),
    ]
    api.LockFileEx.restype = ctypes.c_int
    api.UnlockFileEx.argtypes = [_HANDLE, _DWORD, _DWORD, _DWORD, ctypes.POINTER(_Overlapped)]
    api.UnlockFileEx.restype = ctypes.c_int
    api.CloseHandle.argtypes = [_HANDLE]
    api.CloseHandle.restype = ctypes.c_int
    return api


def _last_error(path: Path) -> OSError:
    error = ctypes.WinError(ctypes.get_last_error())
    error.filename = str(path)
    return error


class WindowsSessionLock:
    """Own a non-inheritable handle to the same byte used by msvcrt.locking."""

    def __init__(self, path: Path) -> None:
        self._api = _kernel32()
        self._path = path
        try:
            handle = self._open(_GENERIC_READ | _GENERIC_WRITE, _OPEN_ALWAYS)
        except OSError as error:
            if getattr(error, "winerror", None) != ERROR_ACCESS_DENIED:
                raise
            # The marker need not be writable to lock byte zero. OPEN_EXISTING
            # keeps creation subject to the directory's normal write access.
            try:
                handle = self._open(_GENERIC_READ, _OPEN_EXISTING)
            except OSError as fallback_error:
                if getattr(fallback_error, "winerror", None) in WINDOWS_LOCK_CONTENTION:
                    raise
                raise error from None

        if not self._api.LockFileEx(handle, _EXCLUSIVE_NO_WAIT, 0, 1, 0, ctypes.byref(_Overlapped())):
            error = _last_error(path)
            self._api.CloseHandle(handle)
            raise error
        self._handle = handle

    def _open(self, access: int, disposition: int) -> int:
        handle = self._api.CreateFileW(
            str(self._path), access, _SHARE_READ_WRITE, None, disposition, _FILE_ATTRIBUTE_NORMAL, None,
        )
        if handle in (None, _INVALID_HANDLE):
            raise _last_error(self._path)
        return handle

    def close(self) -> None:
        try:
            if not self._api.UnlockFileEx(self._handle, 0, 1, 0, ctypes.byref(_Overlapped())):
                raise _last_error(self._path)
        finally:
            self._api.CloseHandle(self._handle)

"""Windows handle-bound bootstrap reads. No DLL is loaded on other platforms."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import ntpath
import os
from pathlib import PurePath
import struct
from typing import Protocol


_REPARSE = 0x400
_DIRECTORY = 0x10
_MAX_ACL = 65535
_SYSTEM_SID = bytes.fromhex("010100000000000512000000")
_ADMINISTRATORS_SID = bytes.fromhex("01020000000000052000000020020000")


def _sid(value: bytes) -> bytes:
    if (not isinstance(value, bytes) or len(value) < 8 or value[0] != 1
            or value[1] > 15 or len(value) != 8 + value[1] * 4):
        raise ValueError("invalid private SID")
    return value


def validate_private_acl(owner: bytes, dacl: bytes | None, user: bytes) -> None:
    """Only the current user and OS privileged principals may have allow ACEs."""
    if _sid(owner) != _sid(user) or not isinstance(dacl, bytes) or not 8 <= len(dacl) <= _MAX_ACL:
        raise ValueError("invalid private ACL")
    revision, reserved, size, count, reserved2 = struct.unpack_from("<BBHHH", dacl)
    if revision not in {2, 4} or reserved or reserved2 or size != len(dacl) or count > 1024:
        raise ValueError("invalid private ACL")
    allowed = {user, _SYSTEM_SID, _ADMINISTRATORS_SID}
    offset, user_access = 8, False
    for _ in range(count):
        if offset + 8 > size:
            raise ValueError("invalid private ACE")
        kind, flags, length, mask = struct.unpack_from("<BBHI", dacl, offset)
        if kind not in {0, 1} or flags & ~0x1F or length < 16 or offset + length > size:
            raise ValueError("unsupported private ACE")
        principal = _sid(dacl[offset + 8:offset + length])
        # Inherit-only grants to other users are unsafe for newly created children too.
        if kind == 0:
            if principal not in allowed:
                raise ValueError("non-private allow ACE")
            if principal == user and mask and not flags & 0x08:
                user_access = True
        offset += length
    if not user_access:
        raise ValueError("missing private user access")


@dataclass(frozen=True)
class FileInfo:
    attributes: int
    links: int
    size: int
    volume: int
    file_id: int
    write_time: int


class FileAPI(Protocol):
    def user_sid(self) -> bytes: ...
    def open(self, path: str, directory: bool) -> int: ...
    def info(self, handle: int) -> FileInfo: ...
    def security(self, handle: int) -> tuple[bytes, bytes | None]: ...
    def final_path(self, handle: int) -> str: ...
    def read(self, handle: int, maximum: int) -> bytes: ...
    def close(self, handle: int) -> None: ...


def read_private_bootstrap(path: PurePath, maximum: int, *, api: FileAPI | None = None) -> bytes:
    """Validate a pinned parent and read/verify the same non-shared-write file handle."""
    if (not path.is_absolute() or not path.name or ":" in path.name
            or type(maximum) is not int or not 0 < maximum <= 65536):
        raise ValueError("invalid private path")
    api = api if api is not None else WindowsFileAPI()
    user = api.user_sid()
    parent = api.open(str(path.parent), True)
    try:
        info = api.info(parent)
        if info.attributes & _REPARSE or not info.attributes & _DIRECTORY:
            raise ValueError("invalid private directory")
        validate_private_acl(*api.security(parent), user)
        descriptor = api.open(str(path), False)
        try:
            before = api.info(descriptor)
            if (before.attributes & (_REPARSE | _DIRECTORY) or before.links != 1
                    or not 0 <= before.size <= maximum):
                raise ValueError("invalid private file")
            validate_private_acl(*api.security(descriptor), user)
            parent_path = ntpath.normcase(ntpath.normpath(api.final_path(parent)))
            file_path = ntpath.normcase(ntpath.normpath(api.final_path(descriptor)))
            if ntpath.dirname(file_path) != parent_path or ntpath.basename(file_path) != ntpath.normcase(path.name):
                raise ValueError("private parent changed")
            body = api.read(descriptor, maximum + 1)
            if not isinstance(body, bytes) or len(body) != before.size or len(body) > maximum:
                raise ValueError("invalid private size")
            if api.info(descriptor) != before:
                raise ValueError("private file changed")
            return body
        finally:
            api.close(descriptor)
    finally:
        api.close(parent)


# Fixed Windows widths also make structure/decoder tests portable on LP64 hosts.
_DWORD, _BOOL, _HANDLE = ctypes.c_uint32, ctypes.c_int32, ctypes.c_void_p


class _FileInformation(ctypes.Structure):
    _fields_ = [(name, _DWORD) for name in (
        "attributes", "created_low", "created_high", "access_low", "access_high",
        "write_low", "write_high", "volume", "size_high", "size_low", "links", "index_high", "index_low",
    )]


class WindowsFileAPI:
    """Documented Win32 APIs; handles are non-inheritable and never converted to CRT FDs."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows private files unavailable")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        security = ctypes.WinDLL("advapi32", use_last_error=True)

        def bind(library, name, result, *arguments):
            function = getattr(library, name)
            function.restype, function.argtypes = result, list(arguments)
            return function

        pointer = ctypes.c_void_p
        self._process = bind(kernel, "GetCurrentProcess", _HANDLE)
        self._open_token = bind(security, "OpenProcessToken", _BOOL, _HANDLE, _DWORD, ctypes.POINTER(_HANDLE))
        self._token_info = bind(security, "GetTokenInformation", _BOOL, _HANDLE, _DWORD, pointer, _DWORD, ctypes.POINTER(_DWORD))
        self._create = bind(kernel, "CreateFileW", _HANDLE, ctypes.c_wchar_p, _DWORD, _DWORD, pointer, _DWORD, _DWORD, _HANDLE)
        self._type = bind(kernel, "GetFileType", _DWORD, _HANDLE)
        self._information = bind(kernel, "GetFileInformationByHandle", _BOOL, _HANDLE, ctypes.POINTER(_FileInformation))
        self._security = bind(security, "GetSecurityInfo", _DWORD, _HANDLE, _DWORD, _DWORD,
                              ctypes.POINTER(pointer), pointer, ctypes.POINTER(pointer), pointer, ctypes.POINTER(pointer))
        self._valid_sid = bind(security, "IsValidSid", _BOOL, pointer)
        self._sid_length = bind(security, "GetLengthSid", _DWORD, pointer)
        self._final = bind(kernel, "GetFinalPathNameByHandleW", _DWORD, _HANDLE, ctypes.c_wchar_p, _DWORD, _DWORD)
        self._read = bind(kernel, "ReadFile", _BOOL, _HANDLE, pointer, _DWORD, ctypes.POINTER(_DWORD), pointer)
        self._close = bind(kernel, "CloseHandle", _BOOL, _HANDLE)
        self._free = bind(kernel, "LocalFree", pointer, pointer)

    @staticmethod
    def _require(result) -> None:
        if not result:
            raise OSError("Windows private files unavailable")

    def _copy_sid(self, pointer: int | None) -> bytes:
        if not pointer or not self._valid_sid(pointer):
            raise ValueError("invalid private SID")
        size = self._sid_length(pointer)
        if not 8 <= size <= 68:
            raise ValueError("invalid private SID")
        return _sid(ctypes.string_at(pointer, size))

    def user_sid(self) -> bytes:
        token, needed = _HANDLE(), _DWORD()
        self._require(self._open_token(self._process(), 0x0008, ctypes.byref(token)))
        try:
            self._token_info(token, 1, None, 0, ctypes.byref(needed))
            if not 8 <= needed.value <= 65536:
                raise ValueError("invalid private token")
            data = ctypes.create_string_buffer(needed.value)
            self._require(self._token_info(token, 1, data, len(data), ctypes.byref(needed)))
            return self._copy_sid(ctypes.cast(data, ctypes.POINTER(_HANDLE)).contents.value)
        finally:
            self.close(token)

    def open(self, path: str, directory: bool) -> int:
        # Deny deletion of the pinned directory; deny file writes/deletes while reading.
        access = 0x20080 if directory else 0x80020000  # READ_CONTROL, attributes / GENERIC_READ
        flags = 0x00200000 | (0x02000000 if directory else 0)  # OPEN_REPARSE_POINT, BACKUP_SEMANTICS
        handle = self._create(path, access, 3 if directory else 1, None, 3, flags, None)
        if handle in {None, 0, ctypes.c_void_p(-1).value}:
            raise OSError("Windows private files unavailable")
        return handle

    def info(self, handle: int) -> FileInfo:
        if self._type(handle) != 1:  # FILE_TYPE_DISK
            raise ValueError("invalid private file type")
        value = _FileInformation()
        self._require(self._information(handle, ctypes.byref(value)))
        return FileInfo(value.attributes, value.links, (value.size_high << 32) | value.size_low,
                        value.volume, (value.index_high << 32) | value.index_low,
                        (value.write_high << 32) | value.write_low)

    def security(self, handle: int) -> tuple[bytes, bytes | None]:
        owner, acl, descriptor = _HANDLE(), _HANDLE(), _HANDLE()
        result = self._security(handle, 1, 0x05, ctypes.byref(owner), None, ctypes.byref(acl), None, ctypes.byref(descriptor))
        try:
            if result != 0:
                raise OSError("Windows private files unavailable")
            owner_sid = self._copy_sid(owner.value)
            if not acl.value:
                return owner_sid, None
            header = ctypes.string_at(acl.value, 8)
            size = struct.unpack_from("<H", header, 2)[0]
            if not 8 <= size <= _MAX_ACL:
                raise ValueError("invalid private ACL")
            return owner_sid, ctypes.string_at(acl.value, size)
        finally:
            if descriptor.value:
                self._free(descriptor)

    def final_path(self, handle: int) -> str:
        size = self._final(handle, None, 0, 0)
        if not 0 < size <= 32768:
            raise ValueError("invalid private final path")
        value = ctypes.create_unicode_buffer(size + 1)
        used = self._final(handle, value, len(value), 0)
        if not 0 < used < len(value):
            raise ValueError("invalid private final path")
        return value.value

    def read(self, handle: int, maximum: int) -> bytes:
        data, used = ctypes.create_string_buffer(maximum), _DWORD()
        self._require(self._read(handle, data, len(data), ctypes.byref(used), None))
        if used.value > maximum:
            raise ValueError("invalid private read")
        return data.raw[:used.value]

    def close(self, handle: int) -> None:
        self._require(self._close(handle))

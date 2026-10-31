"""Private Windows reads: binary ACL vectors, handle lifecycle and platform-gated native checks."""

from dataclasses import replace
import ctypes
import json
import os
from pathlib import PureWindowsPath
import struct
from types import SimpleNamespace

import pytest

from box_agent.desktop_memory import transport
from box_agent.desktop_memory import windows_files as module


def sid(*parts, authority=5):
    return bytes([1, len(parts)]) + authority.to_bytes(6, "big") + struct.pack("<" + "I" * len(parts), *parts)


USER = sid(21, 100, 200, 300, 1001)
OTHER = sid(21, 100, 200, 300, 1002)
SYSTEM, ADMIN = sid(18), sid(32, 544)
EVERYONE = sid(0, authority=1)
PATH = PureWindowsPath("C:/private/synthetic-user/lease/bootstrap.json")


def ace(principal=USER, *, kind=0, flags=0, mask=0x1F01FF):
    return struct.pack("<BBHI", kind, flags, len(principal) + 8, mask) + principal


def acl(*entries, revision=2):
    data = b"".join(entries or [ace()])
    return struct.pack("<BBHHH", revision, 0, len(data) + 8, len(entries) or 1, 0) + data


@pytest.mark.parametrize("entries", [
    (ace(),), (ace(), ace(SYSTEM), ace(ADMIN)),
    (ace(flags=0x13),), (ace(EVERYONE, kind=1), ace()),
])
def test_acl_accepts_only_owner_and_os_privileged_allow_principals(entries):
    module.validate_private_acl(USER, acl(*entries), USER)


@pytest.mark.parametrize("owner,dacl", [
    (OTHER, acl()), (SYSTEM, acl()), (USER, None),
    (USER, struct.pack("<BBHHH", 2, 0, 8, 0, 0)),
    (USER, acl(ace(), ace(EVERYONE))), (USER, acl(ace(), ace(OTHER))),
    (USER, acl(ace(), ace(EVERYONE, flags=0x0B))),
    (USER, acl(ace(kind=5))), (USER, acl(ace(kind=9))),
    (USER, acl(ace(flags=0x40))), (USER, acl(ace(flags=0x08))),
    (USER, acl(ace(mask=0))), (USER, acl(revision=1)),
    (USER, acl()[:-1]), (USER, acl() + b"\0"),
    (USER, b"\2" + b"\0" * 7),
])
def test_acl_rejects_other_owners_null_or_malformed_and_untrusted_grants(owner, dacl):
    with pytest.raises(ValueError):
        module.validate_private_acl(owner, dacl, USER)


class FileAPI:
    def __init__(self, body=b'{"synthetic":true}'):
        self.body = body
        self.events = []
        self.owners = {10: USER, 20: USER}
        self.acls = {10: acl(), 20: acl()}
        self.information = {10: module.FileInfo(0x10, 1, 0, 7, 9, 11),
                            20: module.FileInfo(0x80, 1, len(body), 7, 10, 11)}
        self.paths = {10: "\\\\?\\C:\\private\\synthetic-user\\lease",
                      20: "\\\\?\\C:\\private\\synthetic-user\\lease\\bootstrap.json"}
        self.mutate = False
        self.open_error = False
        self.read_error = False

    def user_sid(self):
        self.events.append(("user",))
        return USER

    def open(self, path, directory):
        self.events.append(("open", directory))
        assert path == str(PATH.parent if directory else PATH)
        if self.open_error and not directory:
            raise OSError("synthetic missing")
        return 10 if directory else 20

    def info(self, handle):
        self.events.append(("info", handle))
        value = self.information[handle]
        return replace(value, write_time=12) if self.mutate and ("read", 20, 65537) in self.events else value

    def security(self, handle):
        self.events.append(("security", handle))
        return self.owners[handle], self.acls[handle]

    def final_path(self, handle):
        self.events.append(("path", handle))
        return self.paths[handle]

    def read(self, handle, maximum):
        self.events.append(("read", handle, maximum))
        if self.read_error:
            raise OSError("synthetic read failure")
        return self.body

    def close(self, handle):
        self.events.append(("close", handle))


def test_only_validated_same_file_handle_is_read_and_all_handles_are_released():
    api = FileAPI()
    assert module.read_private_bootstrap(PATH, 65536, api=api) == api.body
    assert api.events == [("user",), ("open", True), ("info", 10), ("security", 10),
                          ("open", False), ("info", 20), ("security", 20), ("path", 10),
                          ("path", 20), ("read", 20, 65537), ("info", 20), ("close", 20), ("close", 10)]


@pytest.mark.parametrize("case", ["parent_reparse", "parent_file", "parent_owner", "parent_acl",
    "file_reparse", "file_directory", "hardlink", "oversize", "file_owner", "file_acl",
    "null_acl", "sibling_path", "different_name", "missing", "read_error", "short_read", "changed"])
def test_invalid_file_material_never_returns_bytes_and_releases_every_opened_handle(case):
    api = FileAPI()
    if case == "parent_reparse": api.information[10] = replace(api.information[10], attributes=0x410)
    elif case == "parent_file": api.information[10] = replace(api.information[10], attributes=0x80)
    elif case == "parent_owner": api.owners[10] = OTHER
    elif case == "parent_acl": api.acls[10] = acl(ace(), ace(EVERYONE))
    elif case == "file_reparse": api.information[20] = replace(api.information[20], attributes=0x480)
    elif case == "file_directory": api.information[20] = replace(api.information[20], attributes=0x10)
    elif case == "hardlink": api.information[20] = replace(api.information[20], links=2)
    elif case == "oversize": api.information[20] = replace(api.information[20], size=65537)
    elif case == "file_owner": api.owners[20] = OTHER
    elif case == "file_acl": api.acls[20] = acl(ace(), ace(EVERYONE))
    elif case == "null_acl": api.acls[20] = None
    elif case == "sibling_path": api.paths[20] = "\\\\?\\C:\\private\\synthetic-user\\lease-sibling\\bootstrap.json"
    elif case == "different_name": api.paths[20] = api.paths[20].replace("bootstrap.json", "other.json")
    elif case == "missing": api.open_error = True
    elif case == "read_error": api.read_error = True
    elif case == "short_read": api.body = b""
    elif case == "changed": api.mutate = True
    with pytest.raises((ValueError, OSError)):
        module.read_private_bootstrap(PATH, 65536, api=api)
    parent_only = case.startswith("parent_") or case == "missing"
    assert [event for event in api.events if event[0] == "close"] == ([('close', 10)] if parent_only else [('close', 20), ('close', 10)])
    if case not in {"read_error", "short_read", "changed"}:
        assert not any(event[0] == "read" for event in api.events)


@pytest.mark.parametrize("path,maximum", [(PureWindowsPath("relative.json"), 65536),
    (PureWindowsPath("C:/private/bootstrap.json:stream"), 65536), (PATH, 0), (PATH, 65537), (PATH, True)])
def test_invalid_path_and_limits_are_rejected_before_native_access(path, maximum):
    api = FileAPI()
    with pytest.raises(ValueError):
        module.read_private_bootstrap(path, maximum, api=api)
    assert api.events == []


@pytest.mark.parametrize("change", [None, "extra", "epoch", "origin"])
def test_windows_bytes_use_the_common_schema_and_identity_validator(monkeypatch, change):
    epoch = "11111111-1111-4111-8111-111111111111"
    data = {"protocol": 2, "base_url": "http://127.0.0.1:1234/memory/v2", "token": "a" * 43,
            "control_token": "b" * 43, "identity_epoch": epoch, "profile_id": "22222222-2222-4222-8222-222222222222"}
    if change == "extra": data["arbitrary_url"] = "https://invalid.test"
    elif change == "epoch": data["identity_epoch"] = data["profile_id"]
    elif change == "origin": data["base_url"] = "http://localhost:1234/memory/v2"
    api = FileAPI(json.dumps(data).encode())
    monkeypatch.setattr(transport, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(module, "WindowsFileAPI", lambda: api)
    if change:
        with pytest.raises(transport.MemoryProxyError, match="MEMORY_BOOTSTRAP_UNAVAILABLE"):
            transport._read_bootstrap(PATH, epoch)
    else:
        result = transport._read_bootstrap(PATH, epoch)
        assert result.base_url == data["base_url"] and result.identity_epoch == epoch
    assert api.events[-2:] == [("close", 20), ("close", 10)]


def test_native_file_information_uses_fixed_win32_widths_and_preserves_high_bits():
    assert ctypes.sizeof(module._FileInformation) == 52
    api = module.WindowsFileAPI.__new__(module.WindowsFileAPI)
    api._type = lambda _: 1
    def information(handle, output):
        assert handle == 20
        ctypes.memmove(output, struct.pack("<13I", 0x80, 0, 0, 0, 0, 8, 9, 7, 1, 2, 1, 3, 4), 52)
        return 1
    api._information = information
    assert api.info(20) == module.FileInfo(0x80, 1, (1 << 32) | 2, 7, (3 << 32) | 4, (9 << 32) | 8)


@pytest.mark.parametrize("directory,access,sharing,flags", [(True, 0x20080, 3, 0x02200000), (False, 0x80020000, 1, 0x00200000)])
def test_native_open_has_no_follow_no_inheritance_and_required_share_restrictions(directory, access, sharing, flags):
    api = module.WindowsFileAPI.__new__(module.WindowsFileAPI)
    calls = []
    api._create = lambda *arguments: calls.append(arguments) or 20
    assert api.open(str(PATH), directory) == 20
    assert calls == [(str(PATH), access, sharing, None, 3, flags, None)]


@pytest.mark.parametrize("kind", [0, 2, 3])
def test_non_disk_native_handles_are_rejected_before_information_or_read(kind):
    api = module.WindowsFileAPI.__new__(module.WindowsFileAPI)
    api._type = lambda _: kind
    api._information = lambda *_: pytest.fail("non-disk handle")
    with pytest.raises(ValueError): api.info(20)


@pytest.mark.parametrize("failure", [None, "query", "owner", "acl"])
def test_security_descriptor_allocation_is_released_on_success_and_every_failure(failure):
    api = module.WindowsFileAPI.__new__(module.WindowsFileAPI)
    owner, permissions, descriptor = ctypes.create_string_buffer(USER), ctypes.create_string_buffer(acl()), ctypes.create_string_buffer(1)
    def security(_handle, kind, flags, owner_out, _group, acl_out, _sacl, descriptor_out):
        assert kind == 1 and flags == 5
        ctypes.cast(owner_out, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(owner)
        ctypes.cast(acl_out, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(permissions)
        ctypes.cast(descriptor_out, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(descriptor)
        return 5 if failure == "query" else 0
    api._security = security
    api._valid_sid = lambda _: failure != "owner"
    api._sid_length = lambda _: len(USER)
    if failure == "acl": ctypes.memmove(permissions, b"\2\0\1\0" + b"\0" * 4, 8)
    freed = []
    api._free = lambda pointer: freed.append(pointer.value)
    if failure:
        with pytest.raises((OSError, ValueError)): api.security(20)
    else:
        assert api.security(20) == (USER, acl())
    assert freed == [ctypes.addressof(descriptor)]


@pytest.mark.parametrize("failure", [None, "size", "query", "sid"])
def test_token_query_copies_the_sid_before_releasing_the_token_on_all_paths(failure):
    api = module.WindowsFileAPI.__new__(module.WindowsFileAPI)
    api._process = lambda: 99
    def open_token(process, access, output):
        assert process == 99 and access == 8
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = 123
        return 1
    def token_info(token, kind, output, size, needed):
        assert token.value == 123 and kind == 1
        ctypes.cast(needed, ctypes.POINTER(ctypes.c_uint32))[0] = 0 if failure == "size" else 16 + len(USER)
        if not size: return 0
        if failure == "query": return 0
        pointer = ctypes.addressof(output) + 16
        ctypes.memmove(pointer, USER, len(USER))
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = pointer
        return 1
    api._open_token, api._token_info = open_token, token_info
    api._valid_sid = lambda _: failure != "sid"
    api._sid_length = lambda _: len(USER)
    closed = []
    api._close = lambda handle: closed.append(handle.value) or 1
    if failure:
        with pytest.raises((OSError, ValueError)): api.user_sid()
    else: assert api.user_sid() == USER
    assert closed == [123]


@pytest.mark.parametrize("failure", [None, "read", "oversize"])
def test_native_read_only_returns_the_reported_bounded_bytes(failure):
    api = module.WindowsFileAPI.__new__(module.WindowsFileAPI)
    def read(handle, output, limit, used, overlapped):
        assert handle == 20 and limit == 64 and overlapped is None
        ctypes.memmove(output, b'{}', 2)
        ctypes.cast(used, ctypes.POINTER(ctypes.c_uint32))[0] = 65 if failure == "oversize" else 2
        return 0 if failure == "read" else 1
    api._read = read
    if failure:
        with pytest.raises((OSError, ValueError)): api.read(20, 64)
    else: assert api.read(20, 64) == b'{}'


def native_fixture(tmp_path, body=b'{"synthetic":true}'):
    """Create only owned Windows test files with explicit owner/protected DACL at creation."""
    api = module.WindowsFileAPI()
    user = api.user_sid()
    user_text = "S-1-" + str(int.from_bytes(user[2:8], "big")) + "".join(
        "-" + str(value) for value in struct.unpack("<" + "I" * user[1], user[8:]))
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = security.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.restype = ctypes.c_int32
    convert.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    create_directory = kernel.CreateDirectoryW
    create_directory.restype = ctypes.c_int32
    create_directory.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p]
    write = kernel.WriteFile
    write.restype = ctypes.c_int32
    write.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
    set_security = security.SetFileSecurityW
    set_security.restype = ctypes.c_int32
    set_security.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_void_p]

    class Attributes(ctypes.Structure):
        _fields_ = [("length", ctypes.c_uint32), ("descriptor", ctypes.c_void_p), ("inherit", ctypes.c_int32)]

    descriptor = ctypes.c_void_p()
    sddl = f"O:{user_text}D:P(A;OICI;FA;;;{user_text})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
    assert convert(sddl, 1, ctypes.byref(descriptor), None)
    directory = tmp_path / "private-unicode-用户"
    filename = directory / "bootstrap.json"
    try:
        attributes = Attributes(ctypes.sizeof(Attributes), descriptor.value, 0)
        assert create_directory(str(directory), ctypes.byref(attributes))
        handle = api._create(str(filename), 0x40000000, 1, ctypes.byref(attributes), 1, 0x80, None)
        assert handle not in {None, 0, ctypes.c_void_p(-1).value}
        try:
            data, used = ctypes.create_string_buffer(body), ctypes.c_uint32()
            assert write(handle, data, len(body), ctypes.byref(used), None)
            assert used.value == len(body)
        finally:
            api.close(handle)
    finally:
        api._free(descriptor)

    def grant_world(target):
        descriptor = ctypes.c_void_p()
        assert convert(f"O:{user_text}D:P(A;;FA;;;{user_text})(A;;FR;;;WD)", 1, ctypes.byref(descriptor), None)
        try:
            assert set_security(str(target), 0x80000004, descriptor)
        finally:
            api._free(descriptor)

    return filename, api, grant_world


@pytest.mark.skipif(os.name != "nt", reason="requires real Windows security descriptors and handles")
def test_native_windows_private_unicode_file_is_read_through_verified_handles(tmp_path):
    path, api, _ = native_fixture(tmp_path)
    assert module.read_private_bootstrap(path, 65536, api=api) == b'{"synthetic":true}'


@pytest.mark.skipif(os.name != "nt", reason="requires real Windows DACL enforcement")
@pytest.mark.parametrize("target", ["parent", "file"])
def test_native_windows_everyone_read_grant_is_rejected_before_payload_read(tmp_path, target):
    path, api, grant_world = native_fixture(tmp_path)
    grant_world(path.parent if target == "parent" else path)
    api.read = lambda *_: pytest.fail("untrusted DACL must not expose payload")
    with pytest.raises((ValueError, OSError)):
        module.read_private_bootstrap(path, 65536, api=api)


@pytest.mark.skipif(os.name != "nt", reason="requires Windows file link metadata")
def test_native_windows_hardlinked_bootstrap_is_rejected(tmp_path):
    path, api, _ = native_fixture(tmp_path)
    os.link(path, path.with_name("second-link.json"))
    api.read = lambda *_: pytest.fail("hardlink must not expose payload")
    with pytest.raises((ValueError, OSError)):
        module.read_private_bootstrap(path, 65536, api=api)


@pytest.mark.skipif(os.name != "nt", reason="requires Windows reparse-point handles")
@pytest.mark.parametrize("target", ["parent", "file"])
def test_native_windows_symbolic_links_are_rejected_without_following_them(tmp_path, target):
    path, api, _ = native_fixture(tmp_path)
    link = tmp_path / "linked-parent" if target == "parent" else path.with_name("linked.json")
    try:
        link.symlink_to(path.parent if target == "parent" else path, target_is_directory=target == "parent")
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:
            pytest.skip("Windows account has no symbolic-link privilege or Developer Mode")
        raise
    api.read = lambda *_: pytest.fail("reparse point must not expose payload")
    with pytest.raises((ValueError, OSError)):
        module.read_private_bootstrap(link / path.name if target == "parent" else link, 65536, api=api)

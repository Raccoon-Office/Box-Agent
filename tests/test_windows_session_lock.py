import os
import stat
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from box_agent import session_log
from box_agent.session_log import SessionLog, SessionLogInUseError
from box_agent import windows_session_lock


def winerror(code, path):
    error = PermissionError(13, "Windows file open failed", str(path))
    error.winerror = code
    return error


def fake_api(monkeypatch, *, opens, errors=(), locks=(True,)):
    api = Mock()
    api.CreateFileW.side_effect = opens
    api.LockFileEx.side_effect = locks
    api.UnlockFileEx.return_value = True
    monkeypatch.setattr(windows_session_lock, "_kernel32", lambda: api)
    last_errors = iter(errors)
    monkeypatch.setattr(windows_session_lock, "_last_error", lambda path: winerror(next(last_errors), path))
    monkeypatch.setattr(session_log, "os", SimpleNamespace(name="nt"))
    return api


def test_windows_read_only_marker_still_takes_the_original_byte_lock(monkeypatch, tmp_path):
    path = tmp_path / ".writer.lock"
    api = fake_api(monkeypatch, opens=[windows_session_lock._INVALID_HANDLE, 41], errors=[5])
    lock = session_log._acquire_writer_lock(path)
    create, reopen = api.CreateFileW.call_args_list
    assert create.args[:5] == (str(path), 0xC0000000, 3, None, 4)
    assert reopen.args[:5] == (str(path), 0x80000000, 3, None, 3)
    lock_args = api.LockFileEx.call_args.args
    assert lock_args[:5] == (41, 3, 0, 1, 0)
    assert lock_args[5]._obj.offset == 0
    session_log._release_writer_lock(lock)
    api.CloseHandle.assert_called_once_with(41)
    assert api.UnlockFileEx.call_args.args[:4] == (41, 0, 1, 0)


@pytest.mark.parametrize("code", (32, 33))
def test_windows_reports_actual_sharing_and_byte_lock_contention(monkeypatch, tmp_path, code):
    invalid = windows_session_lock._INVALID_HANDLE
    api = fake_api(
        monkeypatch, opens=[invalid if code == 32 else 41], errors=[code], locks=[False],
    )
    with pytest.raises(SessionLogInUseError) as caught:
        session_log._acquire_writer_lock(tmp_path / ".writer.lock")
    assert caught.value.__cause__.winerror == code
    if code == 33:
        api.CloseHandle.assert_called_once_with(41)
    else:
        api.CloseHandle.assert_not_called()


def test_windows_unreadable_marker_is_access_failure(monkeypatch, tmp_path):
    invalid = windows_session_lock._INVALID_HANDLE
    api = fake_api(monkeypatch, opens=[invalid, invalid], errors=[5, 5])
    with pytest.raises(PermissionError) as caught:
        session_log._acquire_writer_lock(tmp_path / ".writer.lock")
    assert caught.value.winerror == 5
    api.LockFileEx.assert_not_called()


@pytest.mark.skipif(os.name != "nt", reason="requires the native Windows lock APIs")
def test_windows_no_share_handle_is_distinct_from_a_byte_lock(tmp_path):
    session = SessionLog.create(tmp_path, session_id="old", cwd=tmp_path)
    marker = session.path.with_name(".writer.lock")
    session.close()
    api = windows_session_lock._kernel32()
    handle = api.CreateFileW(str(marker), 0x80000000, 0, None, 3, 0x80, None)
    assert handle not in (None, windows_session_lock._INVALID_HANDLE)
    try:
        with pytest.raises(SessionLogInUseError) as caught:
            SessionLog.open(tmp_path, session_id="old", cwd=tmp_path)
        assert caught.value.__cause__.winerror == 32
    finally:
        api.CloseHandle(handle)


@pytest.mark.skipif(os.name != "nt", reason="requires the native Windows lock APIs")
def test_read_only_marker_and_legacy_writer_are_compatible_on_windows(tmp_path):
    import msvcrt

    session = SessionLog.create(tmp_path, session_id="old", cwd=tmp_path)
    marker = session.path.with_name(".writer.lock")
    session.close()
    marker.write_bytes(b"\0")
    with marker.open("r+b") as legacy:
        msvcrt.locking(legacy.fileno(), msvcrt.LK_NBLCK, 1)
        with pytest.raises(SessionLogInUseError):
            SessionLog.open(tmp_path, session_id="old", cwd=tmp_path)
        msvcrt.locking(legacy.fileno(), msvcrt.LK_UNLCK, 1)
    marker.chmod(stat.S_IREAD)
    try:
        resumed = SessionLog.open(tmp_path, session_id="old", cwd=tmp_path)
        try:
            with pytest.raises(SessionLogInUseError):
                SessionLog.open(tmp_path, session_id="old", cwd=tmp_path)
        finally:
            resumed.close()
    finally:
        marker.chmod(stat.S_IREAD | stat.S_IWRITE)

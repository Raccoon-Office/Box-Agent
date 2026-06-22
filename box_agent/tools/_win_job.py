"""
Windows Job Object guard: kernel subprocesses die when box-agent dies.

On Windows, child processes survive parent termination by default — there is no
Unix-style process-group kill.  This module creates one Job Object per
box-agent process with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.  Every kernel PID
registered here is automatically terminated by the OS when box-agent's last
handle to the job closes, whether via normal sys.exit() or a hard
TerminateProcess() call (e.g. Stop-Process -Force in PowerShell).

Normal shutdown path (shutdown_kernel / shutdown_all) is unaffected: the
kernel is already dead before box-agent exits, so the job cleanup is a no-op.

Safe to import on all platforms.  assign_pid_to_job() is a no-op everywhere
except win32.  All errors are swallowed — a best-effort guard must never crash
kernel startup.
"""

from __future__ import annotations

import platform

__all__ = ["assign_pid_to_job"]

if platform.system() != "Windows":
    def assign_pid_to_job(pid: int) -> None:
        """No-op on non-Windows platforms."""
        return

else:
    import ctypes
    import ctypes.wintypes
    from typing import Optional

    _k32 = ctypes.windll.kernel32

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JobObjectBasicLimitInformation = 2
    _PROCESS_ALL_ACCESS = 0x1F0FFF

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        # Matches the Windows SDK layout on both 32-bit and 64-bit.
        # ctypes applies natural alignment (same rules as MSVC), so the two
        # implicit padding gaps (after LimitFlags and after ActiveProcessLimit)
        # are inserted automatically.
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),  # LARGE_INTEGER
            ("PerJobUserTimeLimit",     ctypes.c_longlong),  # LARGE_INTEGER
            ("LimitFlags",             ctypes.c_uint32),     # DWORD
            ("MinimumWorkingSetSize",  ctypes.c_size_t),     # SIZE_T
            ("MaximumWorkingSetSize",  ctypes.c_size_t),     # SIZE_T
            ("ActiveProcessLimit",     ctypes.c_uint32),     # DWORD
            ("Affinity",               ctypes.c_size_t),     # ULONG_PTR
            ("PriorityClass",         ctypes.c_uint32),      # DWORD
            ("SchedulingClass",       ctypes.c_uint32),      # DWORD
        ]

    _job_handle: Optional[int] = None

    def _get_or_create_job() -> Optional[int]:
        """Return (creating if needed) the module-level Job Object handle.

        The handle intentionally lives for the full process lifetime and is
        never explicitly closed.  When the process exits the OS reclaims it,
        triggering KILL_ON_JOB_CLOSE for all member processes.
        """
        global _job_handle
        if _job_handle is not None:
            return _job_handle

        handle = _k32.CreateJobObjectW(None, None)
        if not handle:
            return None

        info = _JOBOBJECT_BASIC_LIMIT_INFORMATION()
        info.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = _k32.SetInformationJobObject(
            handle,
            _JobObjectBasicLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            _k32.CloseHandle(handle)
            return None

        _job_handle = handle
        return handle

    def assign_pid_to_job(pid: int) -> None:
        """Add *pid* to the box-agent kill-on-close Job Object.

        Silently ignored when:
        - pid is 0 / invalid
        - the Job Object cannot be created (resource exhaustion)
        - the process is already in a different, non-nested Job Object
          (Windows 7; Win 8+ supports nested jobs so this rarely fails)
        - any other OS error
        """
        if not pid:
            return
        try:
            job = _get_or_create_job()
            if job is None:
                return
            proc = _k32.OpenProcess(_PROCESS_ALL_ACCESS, False, pid)
            if not proc:
                return
            try:
                _k32.AssignProcessToJobObject(job, proc)
            finally:
                _k32.CloseHandle(proc)
        except Exception:
            pass

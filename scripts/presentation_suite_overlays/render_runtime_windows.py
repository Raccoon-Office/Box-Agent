"""Native Windows ownership for one PPT render invocation.

The existing supervisor loop still owns deadlines, retries and result receipts.
A private Job Object replaces Unix process groups; the worker cannot launch
Playwright until it is assigned to that job and receives the START handshake.
No global browser scan, shell wrapper, inherited job handle or new dependency.
"""

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import secrets
import selectors
import socket
import subprocess
import sys
import tempfile
import time

from render_runtime import CLEANUP_SECONDS, RenderCleanupError, _Supervisor, _positive, _send


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IOCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IOCounters)] + [
        (name, ctypes.c_size_t) for name in (
            "ProcessMemoryLimit", "JobMemoryLimit", "PeakProcessMemoryUsed", "PeakJobMemoryUsed")]


class _Accounting(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int64) for name in (
        "TotalUserTime", "TotalKernelTime", "ThisPeriodTotalUserTime", "ThisPeriodTotalKernelTime")]
    _fields_ += [(name, wintypes.DWORD) for name in (
        "TotalPageFaultCount", "TotalProcesses", "ActiveProcesses", "TotalTerminatedProcesses")]


class WindowsJob:
    """A non-inheritable, per-render kill-on-close Job Object."""

    def __init__(self):
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            "QueryInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes = arguments
            function.restype = result
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            self._raise("CreateJobObjectW")
        try:
            limits = _ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE; no breakaway.
            if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                self._raise("SetInformationJobObject")
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _raise(operation):
        error = ctypes.get_last_error()
        raise OSError(error, f"{operation} failed: {ctypes.FormatError(error).strip()}")

    def assign(self, process):
        # The worker is blocked on START and cannot create children yet.
        handle = self.api.OpenProcess(0x0100 | 0x0001, False, process.pid)  # SET_QUOTA | TERMINATE
        if not handle:
            self._raise("OpenProcess")
        try:
            if not self.api.AssignProcessToJobObject(self.handle, handle):
                self._raise("AssignProcessToJobObject")
        finally:
            self.api.CloseHandle(handle)

    def active_processes(self):
        accounting = _Accounting()
        if not self.api.QueryInformationJobObject(self.handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
            self._raise("QueryInformationJobObject")
        return accounting.ActiveProcesses

    def terminate(self):
        if not self.api.TerminateJobObject(self.handle, 1):
            self._raise("TerminateJobObject")

    def close(self):
        if self.handle:
            handle, self.handle = self.handle, None
            if not self.api.CloseHandle(handle):
                self._raise("CloseHandle")


def acquire_render_slot(deadline=None):
    from file_lock import lock_file

    limit = int(os.environ.get("RENDER_GLOBAL_LIMIT", "0") or "0")
    if limit < 0:
        raise ValueError("RENDER_GLOBAL_LIMIT must be nonnegative")
    if not limit:
        return None
    wait = _positive(os.environ.get("RENDER_SLOT_TIMEOUT", "900"), "RENDER_SLOT_TIMEOUT")
    deadline = min(deadline or float("inf"), time.monotonic() + wait)
    directory = Path(os.environ.get("RENDER_LOCK_DIR", str(Path(tempfile.gettempdir()) / "ppt_render_slots")))
    directory.mkdir(parents=True, exist_ok=True)
    while time.monotonic() < deadline:
        for index in range(limit):
            handle = (directory / f"slot_{index}.lock").open("a+b")
            try:
                lock_file(handle, blocking=False)
                return handle
            except BlockingIOError:
                handle.close()
            except BaseException:
                handle.close()
                raise
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    raise TimeoutError("render admission timed out; configured limit was not bypassed")


class WindowsSupervisor(_Supervisor):
    """Reuse the event/deadline loop, replacing only OS resource ownership."""

    def __init__(self, renderer, args, directory, deadline, stdout, stderr, slot=None):
        import psutil

        self.deadline = deadline
        budget = max(0, deadline - time.monotonic())
        self.force_deadline = deadline - min(3, budget / 5)
        self.work_deadline = self.force_deadline - min(CLEANUP_SECONDS, budget / 5)
        self.stage_deadline = self.work_deadline
        self.stage = "startup"
        self.token = secrets.token_hex(24)
        self.slot = slot
        self.peers = {}
        self.worker_done = False
        self.cleanup_failed = False
        self.process = None
        self.job = WindowsJob()
        self.selector = selectors.DefaultSelector()
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.listener.bind(("127.0.0.1", 0))
            self.listener.listen()
            self.listener.setblocking(False)
            self.selector.register(self.listener, selectors.EVENT_READ)
            environment = dict(os.environ, _PPT_RENDER_SOCKET=json.dumps(self.listener.getsockname()),
                               _PPT_RENDER_TOKEN=self.token, _PPT_RENDER_DIRECTORY=str(directory),
                               PYTHONIOENCODING="utf-8")
            runtime = Path(__file__).with_name("render_runtime.py")
            self.process = subprocess.Popen([sys.executable, str(runtime), "--worker", renderer, *args],
                                            env=environment, stdout=stdout, stderr=stderr)
            self.job.assign(self.process)
            self.worker_identity = psutil.Process(self.process.pid)
            self.worker_identity.create_time()
        except BaseException:
            # Constructor failure must not lose ownership before run_renderer
            # receives a supervisor object. The unadmitted worker has no children.
            self.listener.close()
            self.selector.close()
            self.job.close()
            if self.process is not None:
                if self.process.poll() is None:
                    self.process.kill()
                self.process.wait(timeout=5)
            raise

    def _message(self, connection, message):
        peer = self.peers[connection]
        event = message.get("event")
        if event == "register" and "identity" not in peer:
            if (message.get("token") != self.token or message.get("role") != "worker"
                    or message.get("pid") != self.process.pid or not self.worker_identity.is_running()):
                raise RuntimeError("invalid render process registration")
            peer.update(identity=self.worker_identity, role="worker")
            _send(connection, {"event": "start"})
        elif event == "phase" and peer.get("role") == "worker":
            self.stage = str(message["name"])
            end = self.force_deadline if message.get("cleanup") else self.work_deadline
            self.stage_deadline = min(end, time.monotonic() + _positive(message["seconds"], "phase timeout"))
        elif event == "done" and peer.get("role") == "worker":
            # Job accounting includes orphaned grandchildren, unlike a PID tree
            # walk after the driver/worker exits. The worker waits for RELEASE.
            limit = min(self.force_deadline, time.monotonic() + 1)
            while self.job.active_processes() > 1 and time.monotonic() < limit:
                time.sleep(0.02)
            if not message.get("clean") or self.job.active_processes() != 1:
                raise RenderCleanupError("render worker retained driver processes")
            self.worker_done = True
            _send(connection, {"event": "release"})
            peer["released"] = True
        else:
            raise RuntimeError("unexpected render control message")

    def close(self):
        self.listener.close()
        try:
            for connection in list(self.peers):
                self._remove(connection)
            self.job.terminate()
            limit = max(time.monotonic() + 0.05, self.deadline)
            while self.job.active_processes() and time.monotonic() < limit:
                time.sleep(0.02)
            if self.job.active_processes():
                raise RenderCleanupError("render Job Object retained live processes after cleanup")
            self.process.wait(timeout=max(0.05, self.deadline - time.monotonic()))
        finally:
            # The parent exclusively owns this handle. A hard parent exit also
            # invokes KILL_ON_JOB_CLOSE without depending on Python callbacks.
            self.job.close()
            self.selector.close()

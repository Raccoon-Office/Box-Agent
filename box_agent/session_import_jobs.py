"""Own bounded, isolated history-import processes for one ACP connection."""
from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import json
import os
import sys
from uuid import uuid4

from .session_import import _uuid


class HistoryImportJobError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def worker_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--history-import-worker"]
    return [sys.executable, "-m", "box_agent.session_import_worker"]


def _worker_launch(command: list[str]) -> tuple[list[str], dict[str, str] | None]:
    # Match multiprocessing's Windows venv launch: avoid the redirector so the
    # process handle, Job membership and PPID all identify the actual worker.
    # __PYVENV_LAUNCHER__ retains venv site-packages and .pth initialization.
    if sys.platform == "win32" and not getattr(sys, "frozen", False):
        base = getattr(sys, "_base_executable", None)
        if (base and os.path.normcase(command[0]) == os.path.normcase(sys.executable)
                and os.path.normcase(base) != os.path.normcase(sys.executable)):
            return [base, *command[1:]], os.environ | {"__PYVENV_LAUNCHER__": sys.executable}
    return command, None


class _WindowsImportJob:
    """One kill-on-close Job, armed before its child receives write authority."""

    def __init__(self, pid: int):
        import ctypes as c
        api = c.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([c.c_void_p, c.c_wchar_p], c.c_void_p),
            "SetInformationJobObject": ([c.c_void_p, c.c_int, c.c_void_p, c.c_uint32], c.c_int),
            "OpenProcess": ([c.c_uint32, c.c_int, c.c_uint32], c.c_void_p),
            "AssignProcessToJobObject": ([c.c_void_p, c.c_void_p], c.c_int),
            "CloseHandle": ([c.c_void_p], c.c_int),
        }
        for name, (args, result) in signatures.items():
            function = getattr(api, name)
            function.argtypes, function.restype = args, result
        self.api, self.handle = api, api.CreateJobObjectW(None, None)
        if not self.handle:
            raise c.WinError(c.get_last_error())
        try:
            limits = c.create_string_buffer(144)
            c.c_uint32.from_buffer(limits, 16).value = 0x2000  # KILL_ON_JOB_CLOSE
            if not api.SetInformationJobObject(self.handle, 9, limits, len(limits)):
                raise c.WinError(c.get_last_error())
            process = api.OpenProcess(0x101, False, pid)
            if not process:
                raise c.WinError(c.get_last_error())
            try:
                if not api.AssignProcessToJobObject(self.handle, process):
                    raise c.WinError(c.get_last_error())
            finally:
                api.CloseHandle(process)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


@dataclass
class _ImportJob:
    token: str
    attempt: str
    result: asyncio.Future
    process: asyncio.subprocess.Process | None = None
    windows_job: _WindowsImportJob | None = None
    spawned: asyncio.Event = field(default_factory=asyncio.Event)
    exited: asyncio.Event = field(default_factory=asyncio.Event)
    progress_ready: asyncio.Event = field(default_factory=asyncio.Event)
    latest_progress: dict | None = None
    stopping: bool = False
    task: asyncio.Task | None = None
    stop_task: asyncio.Task | None = None


class HistoryImportJobs:
    """Keep write ownership until exit, even after the caller stops waiting."""

    def __init__(self, notify: Callable[[dict], Awaitable[None]] | None = None, *,
                 command: Callable[[], list[str]] | None = None,
                 grace_seconds: float = 2, kill_seconds: float = 1):
        self._notify = notify
        self._command = command or worker_command
        self._grace_seconds, self._kill_seconds = grace_seconds, kill_seconds
        self._jobs: dict[str, _ImportJob] = {}
        self._closing = False

    async def run(self, token: str, attempt_id: str | None = None) -> dict:
        token = _uuid(token)
        attempt = _uuid(attempt_id) if attempt_id is not None else None
        if self._closing:
            raise HistoryImportJobError("AGENT_CLOSING")
        current = self._jobs.get(token)
        if (current is not None and current.result.done() and current.process is not None
                and current.process.returncode is not None):
            # Only in-flight calls are coalesced. A new import must re-run the
            # existing-history verification and durability barrier.
            self._jobs.pop(token)
            current = None
        if current is not None:
            if current.stopping:
                raise HistoryImportJobError("HISTORY_IMPORT_PENDING_CONFIRMATION")
            if attempt is not None and attempt != current.attempt:
                raise HistoryImportJobError("HISTORY_IMPORT_BUSY")
            return await asyncio.shield(current.result)
        if sum(job.stopping for job in self._jobs.values()) >= 2:
            raise HistoryImportJobError("HISTORY_IMPORT_CAPACITY")
        if any(not job.stopping for job in self._jobs.values()):
            raise HistoryImportJobError("HISTORY_IMPORT_BUSY")
        result = asyncio.get_running_loop().create_future()
        # A disconnected waiter must not cause an unobserved exception warning.
        result.add_done_callback(lambda value: None if value.cancelled() else value.exception())
        job = self._jobs[token] = _ImportJob(token, attempt or str(uuid4()), result)
        job.task = asyncio.create_task(self._execute(job, progress=attempt is not None))
        return await asyncio.shield(result)

    async def _progress(self, job: _ImportJob) -> None:
        if self._notify is None:
            return
        while not job.exited.is_set():
            await job.progress_ready.wait()
            job.progress_ready.clear()
            if job.stopping or job.latest_progress is None:
                continue
            value, job.latest_progress = job.latest_progress, None
            try:
                await asyncio.wait_for(self._notify({"token": job.token,
                    "attemptId": job.attempt, **value}), 1)
            except Exception:
                pass  # Display progress cannot own process or protocol liveness.
            await asyncio.sleep(.25)

    async def _execute(self, job: _ImportJob, *, progress: bool) -> None:
        reporter = asyncio.create_task(self._progress(job)) if progress else None
        response = None
        try:
            command, env = _worker_launch(self._command())
            process = await asyncio.create_subprocess_exec(
                *command, env=env, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=None, limit=65536,
            )
            job.process = process
            if os.name == "nt":
                job.windows_job = _WindowsImportJob(process.pid)
            job.spawned.set()
            if job.stopping:
                process.kill()
            else:
                process.stdin.write((json.dumps({"token": job.token, "attemptId": job.attempt,
                                                "parentPid": os.getpid()}) + "\n").encode())
            while line := await process.stdout.readline():
                if len(line) > 16384 or not line.endswith(b"\n"):
                    raise ValueError("Invalid import worker frame")
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("Invalid import worker message")
                update = value.get("progress")
                if isinstance(update, dict):
                    if (isinstance(update.get("stage"), str) and len(update["stage"]) <= 64
                            and type(update.get("completed")) is int and update["completed"] >= 0):
                        job.latest_progress = update
                        job.progress_ready.set()
                elif "result" in value or "error" in value:
                    response = value
            await process.wait()
            if not job.result.done():
                if job.stopping:
                    raise HistoryImportJobError("HISTORY_IMPORT_CANCELLED")
                if process.returncode != 0 or not isinstance(response, dict):
                    raise HistoryImportJobError("HISTORY_IMPORT_FAILED")
                if "error" in response:
                    code = response["error"]
                    if code not in {"HISTORY_IMPORT_CANCELLED", "HISTORY_IMPORT_BUSY"}:
                        code = "HISTORY_IMPORT_FAILED"
                    raise HistoryImportJobError(code)
                result = response.get("result")
                expected = "acp-v1-" + base64.urlsafe_b64encode(job.token.encode()).decode().rstrip("=")
                if not isinstance(result, dict) or result.get("sessionId") != expected:
                    raise HistoryImportJobError("HISTORY_IMPORT_FAILED")
                job.result.set_result(result)
        except BaseException as error:
            if not job.result.done():
                job.result.set_exception(error if isinstance(error, HistoryImportJobError)
                                         else HistoryImportJobError("HISTORY_IMPORT_FAILED"))
            if job.process is not None and job.process.returncode is None:
                job.stopping = True
                try:
                    job.process.kill()
                except ProcessLookupError:
                    pass
                # Retain this task and token while the OS still owns the writer.
                if not isinstance(error, asyncio.CancelledError):
                    await job.process.wait()
        finally:
            job.spawned.set()
            if reporter is not None:
                reporter.cancel()
                await asyncio.gather(reporter, return_exceptions=True)
            if job.process is None or job.process.returncode is not None:
                job.exited.set()
                if job.windows_job is not None:
                    job.windows_job.close()
                if job.process is not None and job.process.stdin is not None:
                    job.process.stdin.close()
                if self._jobs.get(job.token) is job:
                    self._jobs.pop(job.token)
                if progress and self._notify is not None:
                    try:
                        await asyncio.wait_for(self._notify({"token": job.token,
                            "attemptId": job.attempt, "exited": True}), 1)
                    except Exception:
                        pass

    async def cancel(self, token: str, attempt_id: str) -> dict:
        token, attempt = _uuid(token), _uuid(attempt_id)
        job = self._jobs.get(token)
        if job is None or job.attempt != attempt:
            return {"state": "not_found"}
        if job.stop_task is None:
            job.stopping = True
            job.stop_task = asyncio.create_task(self._stop(job))
        return await asyncio.shield(job.stop_task)

    async def _stop(self, job: _ImportJob) -> dict:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._grace_seconds
        try:
            await asyncio.wait_for(job.spawned.wait(), self._grace_seconds)
            process = job.process
            if process is not None and process.returncode is None and process.stdin is not None:
                try:
                    process.stdin.write((json.dumps({"cancel": job.attempt}) + "\n").encode())
                except (BrokenPipeError, ConnectionResetError):
                    pass
            await asyncio.wait_for(job.exited.wait(), max(0, deadline - loop.time()))
        except asyncio.TimeoutError:
            if job.process is not None and job.process.returncode is None:
                try:
                    job.process.kill()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(job.exited.wait(), self._kill_seconds)
            except asyncio.TimeoutError:
                if not job.result.done():
                    job.result.set_exception(HistoryImportJobError("HISTORY_IMPORT_PENDING_CONFIRMATION"))
                return {"state": "pending_confirmation"}
        return {"state": "stopped"}

    async def aclose(self) -> None:
        self._closing = True
        await asyncio.gather(*(self.cancel(job.token, job.attempt)
                               for job in tuple(self._jobs.values())))

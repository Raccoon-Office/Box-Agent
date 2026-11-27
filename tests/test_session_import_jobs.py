"""Isolated import processes preserve other sessions and retain writer ownership."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from uuid import uuid4

import pytest

from box_agent.session_import_jobs import (
    HistoryImportJobError, HistoryImportJobs, _worker_launch, worker_command,
)
from tests.acp_host.probe import AcpHostProbe, _send


STALLED_WORKER = """
import base64, json, os, sys, time
from pathlib import Path
request = json.loads(sys.stdin.readline())
root = Path(os.environ['IMPORT_JOB_TEST_ROOT'])
(root / str(os.getpid())).touch()
print(json.dumps({'progress': {'stage': 'read', 'completed': 1}}), flush=True)
while not (root / 'release').exists(): time.sleep(.01)
handle = 'acp-v1-' + base64.urlsafe_b64encode(request['token'].encode()).decode().rstrip('=')
print(json.dumps({'result': {'sessionId': handle}}), flush=True)
"""


@pytest.mark.skipif(sys.platform != "win32", reason="Windows venv process identity")
@pytest.mark.asyncio
async def test_windows_worker_retains_venv_dependencies_and_direct_parent_identity():
    script = f"""
import json, os, sys, pywintypes
from box_agent.session_import_worker import _watch_parent
_watch_parent({os.getpid()})
print(json.dumps({{'pid': os.getpid(), 'ppid': os.getppid(),
    'prefix': sys.prefix, 'executable': sys.executable, 'dependency': pywintypes.__file__}}))
"""
    command, env = _worker_launch([sys.executable, "-c", script])
    child = await asyncio.create_subprocess_exec(
        *command, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        output, error = await asyncio.wait_for(child.communicate(), 10)
        assert child.returncode == 0, error.decode(errors="replace")
        identity = json.loads(output)
        assert identity["pid"] == child.pid
        assert identity["ppid"] == os.getpid()
        assert Path(identity["prefix"]) == Path(sys.prefix)
        assert Path(identity["executable"]) == Path(sys.executable)
        assert Path(identity["dependency"]).is_file()
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()


def test_worker_rejects_an_unrelated_parent():
    from box_agent.session_import_worker import _watch_parent

    with pytest.raises(RuntimeError, match="Import parent changed before startup"):
        _watch_parent(os.getpid())


def test_frozen_worker_keeps_its_runtime_entry_and_environment(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    command, env = _worker_launch(worker_command())
    assert command == [sys.executable, "--history-import-worker"]
    assert env is None


@pytest.mark.parametrize("stage", ["read", "parse", "write", "file_sync", "verify", "directory_sync"])
@pytest.mark.asyncio
async def test_real_import_can_be_retired_at_each_io_stage_and_retried(tmp_path, monkeypatch, stage):
    if stage == "directory_sync" and os.name == "nt":
        pytest.skip("Windows does not sync directories")
    profile = tmp_path / "profile"
    monkeypatch.setenv("BOX_AGENT_HOME", str(profile))
    token, attempt = str(uuid4()), str(uuid4())
    source = profile / "history-import" / token
    source.mkdir(parents=True)
    (source / "snapshot.json").write_text(json.dumps({"version": 1, "id": token,
        "workspace": str(tmp_path), "messages": [
            {"role": "user", "text": "retained history", "timestamp": 1, "turnId": str(uuid4())},
        ]}))
    stall = tmp_path / "stall"
    stall.touch()
    worker = f"""
import json, sys, time
from pathlib import Path
import box_agent.session_import as importer
output = sys.stdout
original = importer._checkpoint
def checkpoint(stage, completed=0):
    if stage == {stage!r} and Path({str(stall)!r}).exists():
        print(json.dumps({{'progress': {{'stage': 'blocked', 'completed': 1}}}}), file=output, flush=True)
        while True: time.sleep(.01)
    original(stage, completed)
importer._checkpoint = checkpoint
from box_agent.session_import_worker import main
main()
"""
    entered = asyncio.Event()

    async def notify(value):
        if value.get("stage") == "blocked":
            entered.set()

    jobs = HistoryImportJobs(notify, command=lambda: [sys.executable, "-c", worker],
                             grace_seconds=.05, kill_seconds=1)
    pending = asyncio.create_task(jobs.run(token, attempt))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert await jobs.cancel(token, attempt) == {"state": "stopped"}
        with pytest.raises(HistoryImportJobError, match="CANCELLED"):
            await pending
        stall.unlink()
        assert (await jobs.run(token, str(uuid4())))["sessionId"].startswith("acp-v1-")
        assert (source / "snapshot.json").exists()
    finally:
        await jobs.aclose()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_duplicate_attempts_share_a_worker_and_old_cancellation_cannot_stop_it(tmp_path, monkeypatch):
    monkeypatch.setenv("IMPORT_JOB_TEST_ROOT", str(tmp_path))
    entered = asyncio.Event()

    async def notify(value):
        if value.get("stage") == "read":
            entered.set()

    jobs = HistoryImportJobs(notify, command=lambda: [sys.executable, "-c", STALLED_WORKER],
                             grace_seconds=.05, kill_seconds=1)
    token, attempt = str(uuid4()), str(uuid4())
    first = asyncio.create_task(jobs.run(token, attempt))
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        second = asyncio.create_task(jobs.run(token, attempt))
        await asyncio.sleep(0)
        assert len(list(tmp_path.iterdir())) == 1
        assert await jobs.cancel(token, str(uuid4())) == {"state": "not_found"}
        with pytest.raises(HistoryImportJobError, match="BUSY"):
            await jobs.run(token, str(uuid4()))
        with pytest.raises(HistoryImportJobError, match="BUSY"):
            await jobs.run(str(uuid4()), str(uuid4()))
        assert not first.done() and not second.done()
        assert await jobs.cancel(token, attempt) == {"state": "stopped"}
        for task in (first, second):
            with pytest.raises(HistoryImportJobError, match="CANCELLED"):
                await task
        (tmp_path / "release").touch()
        assert (await jobs.run(token, str(uuid4())))["sessionId"].startswith("acp-v1-")
    finally:
        await jobs.aclose()
        await asyncio.gather(*(task for task in (first, second) if task), return_exceptions=True)


@pytest.mark.asyncio
async def test_unconfirmed_exits_fence_tokens_and_bound_unreaped_workers(tmp_path, monkeypatch):
    monkeypatch.setenv("IMPORT_JOB_TEST_ROOT", str(tmp_path))
    ready = asyncio.Queue()

    async def notify(value):
        if value.get("stage") == "read":
            ready.put_nowait(value)

    jobs = HistoryImportJobs(notify, command=lambda: [sys.executable, "-c", STALLED_WORKER],
                             grace_seconds=.02, kill_seconds=.02)
    retained = []
    try:
        for _ in range(2):
            token, attempt = str(uuid4()), str(uuid4())
            request = asyncio.create_task(jobs.run(token, attempt))
            await asyncio.wait_for(ready.get(), 5)
            job = jobs._jobs[token]
            real_kill = job.process.kill
            monkeypatch.setattr(job.process, "kill", lambda: None)
            retained.append((job, real_kill))
            assert await jobs.cancel(token, attempt) == {"state": "pending_confirmation"}
            with pytest.raises(HistoryImportJobError, match="PENDING_CONFIRMATION"):
                await request
            with pytest.raises(HistoryImportJobError, match="PENDING_CONFIRMATION"):
                await jobs.run(token, str(uuid4()))
        with pytest.raises(HistoryImportJobError, match="CAPACITY"):
            await jobs.run(str(uuid4()), str(uuid4()))
    finally:
        for job, kill in retained:
            kill()
        await asyncio.wait_for(asyncio.gather(*(job.exited.wait() for job, _ in retained)), 5)
        await jobs.aclose()
    assert not jobs._jobs


@pytest.mark.asyncio
async def test_stalled_import_can_be_cancelled_without_stopping_another_acp_session():
    repository = Path(__file__).resolve().parents[1]
    with TemporaryDirectory(dir=repository / "workspace", prefix="import-isolation-") as tmp:
        child = (
            "import sys\nimport box_agent.session_import_jobs as jobs\n"
            f"jobs.worker_command = lambda: [sys.executable, '-c', {STALLED_WORKER!r}]\n"
            "from tests.desktop_runtime_fixture import main\nmain()\n"
        )
        probe = AcpHostProbe(command=[sys.executable, "-c", child], cwd=repository,
            env={"BOX_AGENT_DESKTOP_TEST_ROOT": tmp, "IMPORT_JOB_TEST_ROOT": tmp,
                 "BOX_AGENT_DESKTOP_TEST_FILE_INPUTS": "1"}, timeout_s=10)
        await probe.start()
        try:
            response = await probe.initialize()
            assert response["_meta"]["history_import_control"]["protocol_version"] == 1
            token, attempt = str(uuid4()), str(uuid4())
            await _send(probe._proc, probe._protocol, {"jsonrpc": "2.0", "id": 900,
                "method": "_history/import", "params": {"token": token, "attemptId": attempt}})
            async def started():
                while True:
                    eof, message = await probe._reader.read_frame(deadline=None)
                    assert not eof
                    if message and message.get("method") == "_history/import_progress":
                        return
            await asyncio.wait_for(started(), 5)
            other = await probe.session_new(cwd=tmp)
            assert (await probe.session_prompt(other, "fixture:file_inputs other"))["stopReason"] == "end_turn"
            cancelled = await probe.ext_request("history/import_cancel", {"token": token, "attemptId": attempt})
            assert cancelled["state"] == "stopped"
            assert (await probe.session_prompt(other, "fixture:file_inputs again"))["stopReason"] == "end_turn"
            assert not probe._protocol.parse_errors
        finally:
            await probe.stop()


@pytest.mark.skipif(sys.platform not in {"darwin", "win32"}, reason="Desktop parent-death guard")
@pytest.mark.asyncio
async def test_import_worker_exits_when_its_acp_parent_is_killed(tmp_path, monkeypatch):
    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path / "profile"))
    script = """
import asyncio, sys
from box_agent.session_import_jobs import HistoryImportJobs
worker = '''
import os, threading
import box_agent.session_import as importer
def blocked(token):
    importer._checkpoint("worker_pid", os.getpid())
    threading.Event().wait()
importer._read_snapshot = blocked
from box_agent.session_import_worker import main
main()
'''
async def report_worker_pid(update):
    if update.get("stage") == "worker_pid":
        print(update["completed"], flush=True)

async def run():
    jobs = HistoryImportJobs(report_worker_pid, command=lambda: [sys.executable, '-c', worker])
    await jobs.run(sys.argv[1], sys.argv[2])
asyncio.run(run())
"""
    # Retain the actual owner handle, not a Windows venv redirector's handle.
    command, env = _worker_launch([sys.executable, "-c", script, str(uuid4()), str(uuid4())])
    parent = await asyncio.create_subprocess_exec(*command, env=env, stdout=asyncio.subprocess.PIPE)
    pid = None
    child_handle = None
    kernel = None
    try:
        # Both interpreters cold-start before the worker holds the write lock and enters the read.
        try:
            ready = await asyncio.wait_for(parent.stdout.readline(), timeout=30)
        except asyncio.TimeoutError:
            pytest.fail("Import worker did not reach the snapshot read within 30 seconds")
        if not ready:
            code = await asyncio.wait_for(parent.wait(), timeout=5)
            pytest.fail(f"Import parent exited with code {code} before the worker reached the snapshot read")
        pid = int(ready)
        if os.name == "nt":
            import ctypes as c
            kernel = c.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [c.c_uint32, c.c_int, c.c_uint32]
            kernel.OpenProcess.restype = c.c_void_p
            kernel.WaitForSingleObject.argtypes = [c.c_void_p, c.c_uint32]
            kernel.WaitForSingleObject.restype = c.c_uint32
            kernel.TerminateProcess.argtypes = [c.c_void_p, c.c_uint32]
            kernel.CloseHandle.argtypes = [c.c_void_p]
            child_handle = kernel.OpenProcess(0x100001, False, pid)
            assert child_handle, "Could not retain the exact child process handle"
        parent.kill()
        await parent.wait()
        for _ in range(100):
            if kernel is not None:
                stopped = kernel.WaitForSingleObject(child_handle, 0) == 0
            else:
                state = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True).stdout.strip()
                stopped = not state or state.startswith("Z")
            if stopped:
                pid = None
                break
            await asyncio.sleep(.02)
        else:
            pytest.fail("history import outlived its parent")
    finally:
        if parent.returncode is None:
            parent.kill()
            await parent.wait()
        if kernel is not None and child_handle:
            if pid:
                kernel.TerminateProcess(child_handle, 1)
            kernel.CloseHandle(child_handle)
        elif pid:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass


@pytest.mark.skipif(not os.environ.get("BOX_AGENT_IMPORT_TEST_RUNTIME"), reason="Requires a built runtime")
@pytest.mark.asyncio
async def test_frozen_runtime_import_restart_and_continuation(tmp_path):
    from box_agent.config import ToolsConfig
    observed = []

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            observed.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for delta, finish in [({"role": "assistant", "content": "Continued imported history."}, None), ({}, "stop")]:
                value = {"id": "fixture", "object": "chat.completion.chunk", "created": 1, "model": "fixture",
                         "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                self.wfile.write(("data: " + json.dumps(value) + "\n\n").encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    profile, cwd = tmp_path / "profile", tmp_path / "files"
    config = profile / "config"
    config.mkdir(parents=True)
    cwd.mkdir()
    (config / "config.yaml").write_text(json.dumps({
        "api_key": "synthetic-local-test-key", "api_base": f"http://127.0.0.1:{server.server_port}/v1",
        "provider": "openai", "model": "fixture", "max_output_tokens": 512, "retry": {"enabled": False},
        "agent": {"workspace_dir": str(profile / "workspace"), "enable_memory": False,
                  "enable_memory_extraction": False, "memory_maintainer_enabled": False,
                  "memory_promotion_proposal_enabled": False},
        "tools": {name: False for name in ToolsConfig.model_fields if name.startswith("enable_")},
    }))
    token = str(uuid4())
    stage = profile / "history-import" / token
    stage.mkdir(parents=True)
    (stage / "snapshot.json").write_text(json.dumps({
        "version": 1, "id": token, "workspace": str(cwd), "messages": [
            {"role": "user", "text": "frozen-import-marker", "timestamp": 1, "turnId": str(uuid4())},
        ],
    }))
    def probe():
        return AcpHostProbe(command=[os.environ["BOX_AGENT_IMPORT_TEST_RUNTIME"]],
            cwd=Path(__file__).resolve().parents[1], env={"BOX_AGENT_HOME": str(profile)}, timeout_s=30)
    first, second = probe(), probe()
    try:
        await first.start()
        initialized = await first.initialize()
        assert initialized["_meta"]["history_import_control"]["protocol_version"] == 1
        imported = await first.ext_request("history/import", {"token": token, "attemptId": str(uuid4())})
        await first.stop()
        await second.start()
        await second.initialize()
        await second.request("session/resume", {"sessionId": imported["sessionId"], "cwd": str(cwd), "mcpServers": []})
        assert (await second.session_prompt(imported["sessionId"], "continue"))["stopReason"] == "end_turn"
        assert any("frozen-import-marker" in str(message.get("content"))
                   for request in observed for message in request.get("messages", []))
        assert not first._protocol.parse_errors and not second._protocol.parse_errors
    finally:
        await first.stop()
        await second.stop()
        server.shutdown()
        server.server_close()

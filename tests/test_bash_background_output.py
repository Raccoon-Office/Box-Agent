"""Regression tests for lossless, consumable background Bash output."""

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import box_agent.tools.bash_tool as bash


@pytest.fixture
def manager(monkeypatch):
    monkeypatch.setattr(bash.BackgroundShellManager, "_shells", {})
    monkeypatch.setattr(bash.BackgroundShellManager, "_monitor_tasks", {})
    yield bash.BackgroundShellManager
    for shell in bash.BackgroundShellManager._shells.values():
        shell._output.close()
    assert not bash.BackgroundShellManager._monitor_tasks


def make_shell(bash_id="shell", *, owner="owner", returncode=None):
    process = SimpleNamespace(returncode=returncode, pid=123456)
    return bash.BackgroundShell(bash_id, "test", process, 0, owner_id=owner, lifetime="runtime")


def test_unterminated_utf8_line_survives_chunk_boundaries_and_polls(monkeypatch):
    monkeypatch.setattr(bash, "_BACKGROUND_MEMORY_BYTES", 64)
    buffer = bash._BackgroundOutputBuffer()
    expected = "中文🙂" * 30_000
    payload = expected.encode()
    try:
        for start in range(0, len(payload), 65537):
            buffer.append(payload[start:start + 65537])
            assert buffer.take_lines() == []
        stream = buffer._stream
        assert stream._rolled
        buffer.finish()
        assert buffer.take_lines() == [expected]
        assert buffer.take_lines() == []
        assert buffer.size == 0
        assert stream.closed
        assert buffer._stream is None
    finally:
        buffer.close()


def test_poll_preserves_large_incomplete_tail_and_line_filter_semantics(monkeypatch):
    monkeypatch.setattr(bash, "_BACKGROUND_MEMORY_BYTES", 64)
    shell = make_shell()
    tail = b"z" * 100_000
    try:
        shell._output.append(b"first\r\n\n" + tail)
        old_stream = shell._output._stream
        assert shell.get_new_output() == ["first\r", ""]
        assert old_stream.closed
        assert shell._output.size == len(tail)
        assert shell._output._stream._rolled
        shell._output.append(b"\nkeep\nskip\n")
        assert shell.get_new_output("keep") == ["keep"]
        assert shell.get_new_output() == []
        assert shell._output.size == 0
        shell.add_output("invalid regex returns all lines")
        assert shell.get_new_output("[") == ["invalid regex returns all lines"]
    finally:
        shell._output.close()


@pytest.mark.asyncio
async def test_repeated_reads_release_consumed_output_without_exposing_peer_data(manager):
    shell = make_shell()
    manager.add(shell)
    owner = bash.BashOutputTool("owner")
    peer = bash.BashOutputTool("peer")
    for batch in range(8):
        expected = f"batch-{batch}:" + "x" * (512 * 1024)
        shell.add_output(expected)
        stream = shell._output._stream
        assert stream._rolled
        rejected = await peer.execute(shell.bash_id)
        assert not rejected.success
        result = await owner.execute(shell.bash_id)
        assert result.success
        assert expected in result.persistence_content
        assert len(result.stdout) <= bash.MAX_BASH_OUTPUT_CHARS + 500
        assert result.raw_output["original_stdout_chars"] == len(expected)
        assert stream.closed
        assert shell._output.size == 0
        assert shell._output._stream is None
        assert (await owner.execute(shell.bash_id)).stdout == ""


@pytest.mark.asyncio
async def test_read_failure_is_reported_once_without_retrying_or_losing_prior_output(manager):
    shell = make_shell()
    shell._output.append(b"partial")
    shell.process.stdout = SimpleNamespace(read=AsyncMock(side_effect=OSError("read failed")))
    shell.process.wait = AsyncMock(return_value=0)
    manager.add(shell)
    await manager.start_monitor(shell.bash_id)
    await asyncio.wait_for(manager._monitor_tasks[shell.bash_id], 5)
    result = await bash.BashOutputTool("owner").execute(shell.bash_id)
    assert not result.success
    assert "read failed" in result.error
    assert result.stdout == "partial"
    assert shell.status == "error"
    assert manager.get(shell.bash_id) is shell
    shell.process.stdout.read.assert_awaited_once()
    shell.process.wait.assert_not_awaited()


@pytest.mark.asyncio
async def test_stdout_eof_exposes_partial_line_while_process_is_still_running(manager):
    shell = make_shell()
    shell.process.stdout = SimpleNamespace(read=AsyncMock(side_effect=[b"last line", b""]))
    waiting = asyncio.Event()
    finish = asyncio.Event()

    async def wait():
        waiting.set()
        await finish.wait()
        return 0

    shell.process.wait = wait
    manager.add(shell)
    await manager.start_monitor(shell.bash_id)
    monitor = manager._monitor_tasks[shell.bash_id]
    try:
        await asyncio.wait_for(waiting.wait(), 5)
        result = await bash.BashOutputTool("owner").execute(shell.bash_id)
        assert result.success
        assert result.stdout == "last line"
        assert shell.status == "running"
    finally:
        finish.set()
        await asyncio.wait_for(monitor, 5)


@pytest.mark.asyncio
async def test_spool_write_failure_is_reported_and_does_not_retry(manager, monkeypatch):
    shell = make_shell()
    shell.process.stdout = SimpleNamespace(read=AsyncMock(return_value=b"line\n"))
    shell.process.wait = AsyncMock()
    manager.add(shell)

    def disk_full(*args, **kwargs):
        raise OSError("no space left")

    monkeypatch.setattr(bash.tempfile, "SpooledTemporaryFile", disk_full)
    await manager.start_monitor(shell.bash_id)
    await asyncio.wait_for(manager._monitor_tasks[shell.bash_id], 5)
    result = await bash.BashOutputTool("owner").execute(shell.bash_id)
    assert not result.success
    assert "no space left" in result.error
    shell.process.stdout.read.assert_awaited_once()


@pytest.mark.asyncio
async def test_completed_history_expires_only_after_output_is_consumed(manager, monkeypatch):
    monkeypatch.setattr(bash, "_MAX_RETAINED_COMPLETED_SHELLS", 2)

    def no_group(*args):
        raise ProcessLookupError

    monkeypatch.setattr(bash.os, "killpg", no_group, raising=False)
    for index in range(5):
        shell = make_shell(str(index), returncode=0)
        shell.add_output(f"result-{index}")
        shell.update_status(False, 0)
        manager.add(shell)
    assert len(manager._shells) == 5  # Unread results never expire.
    for index in range(5):
        result = await bash.BashOutputTool("owner").execute(str(index))
        assert result.success
        assert result.stdout == f"result-{index}"
    assert manager.get_available_ids("owner") == ["3", "4"]
    expired = await bash.BashOutputTool("owner").execute("0")
    assert not expired.success
    assert "Shell not found" in expired.error
    assert (await bash.BashOutputTool("owner").execute("4")).success


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group liveness")
def test_history_pruning_preserves_live_groups_readers_and_failed_reads(manager, monkeypatch):
    monkeypatch.setattr(bash, "_MAX_RETAINED_COMPLETED_SHELLS", 0)
    group = make_shell("child-group", returncode=0)
    group.update_status(False, 0)
    monkeypatch.setattr(bash.os, "killpg", lambda *args: None)
    manager.add(group)
    running = make_shell("running")
    manager.add(running)
    failed = make_shell("read-error", returncode=0)
    failed.output_error = "disk full"
    failed.update_status(False, 0)
    manager.add(failed)
    reader = make_shell("draining", returncode=0)
    reader.update_status(False, 0)
    manager._monitor_tasks[reader.bash_id] = object()
    manager.add(reader)
    assert set(manager._shells) == {"child-group", "running", "read-error", "draining"}
    manager._monitor_tasks.clear()


@pytest.mark.asyncio
async def test_kill_drains_final_partial_output_and_closes_spool(manager):
    shell = make_shell()
    killed = asyncio.Event()
    expected = "final:" + "x" * (512 * 1024)

    async def final_read(size):
        await killed.wait()
        shell.process.stdout.read.side_effect = None
        shell.process.stdout.read.return_value = b""
        return expected.encode()

    async def terminate():
        shell.status = "terminated"
        shell.process.returncode = shell.exit_code = -15
        killed.set()

    shell.process.stdout = SimpleNamespace(read=AsyncMock(side_effect=final_read))
    shell.process.wait = AsyncMock(return_value=-15)
    shell.terminate = terminate
    manager.add(shell)
    await manager.start_monitor(shell.bash_id)
    result = await asyncio.wait_for(bash.BashKillTool("owner").execute(shell.bash_id), 5)
    assert result.success
    assert expected in result.persistence_content
    assert len(result.stdout) <= bash.MAX_BASH_OUTPUT_CHARS + 500
    assert result.exit_code == -15
    assert shell.status == "terminated"
    assert shell._output._stream is None
    assert not manager._shells
    assert not manager._monitor_tasks


@pytest.mark.asyncio
async def test_cleanup_closes_unread_spool_without_materializing_output(manager, monkeypatch):
    shell = make_shell()
    shell.add_output("x" * (512 * 1024))
    stream = shell._output._stream
    shell.terminate = AsyncMock()
    manager.add(shell)

    def unexpected_read(*args):
        pytest.fail("cleanup must not materialize unread output")

    monkeypatch.setattr(shell, "get_new_output", unexpected_read)
    assert await manager.terminate_owner("owner") == [shell.bash_id]
    assert stream.closed
    assert shell._output.size == 0


@pytest.mark.asyncio
async def test_blocked_reader_has_bounded_stop_and_explicit_drain_failure(manager):
    shell = make_shell()
    waiting = asyncio.Event()

    async def read(size):
        waiting.set()
        await asyncio.Event().wait()

    shell.process.stdout = SimpleNamespace(read=AsyncMock(side_effect=read))
    shell.process._transport = Mock()
    shell.terminate = AsyncMock()
    manager.add(shell)
    await manager.start_monitor(shell.bash_id)
    await asyncio.wait_for(waiting.wait(), 5)
    result = await asyncio.wait_for(bash.BashKillTool("owner").execute(shell.bash_id), 5)
    assert not result.success
    assert "Timed out draining" in result.error
    assert not manager._monitor_tasks
    assert not manager._shells
    shell.process._transport.close.assert_called_once()


@pytest.mark.asyncio
async def test_cancelled_drain_still_closes_process_transport(manager):
    shell = make_shell()
    waiting = asyncio.Event()
    terminated = asyncio.Event()

    async def read(size):
        waiting.set()
        await asyncio.Event().wait()

    async def terminate():
        terminated.set()

    shell.process.stdout = SimpleNamespace(read=AsyncMock(side_effect=read))
    shell.process._transport = Mock()
    shell.terminate = terminate
    manager.add(shell)
    await manager.start_monitor(shell.bash_id)
    await asyncio.wait_for(waiting.wait(), 5)
    stopped = asyncio.create_task(manager.terminate(shell.bash_id))
    try:
        await asyncio.wait_for(terminated.wait(), 5)
        stopped.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(stopped, 5)
        shell.process._transport.close.assert_called_once()
        assert not manager._monitor_tasks
        assert not manager._shells
    finally:
        stopped.cancel()
        await asyncio.gather(stopped, return_exceptions=True)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX subprocess integration")
@pytest.mark.asyncio
@pytest.mark.parametrize("stop_scope", ["kill", "owner", "runtime"])
async def test_stop_after_spool_failure_closes_paused_pipe_without_gc(
    manager, tmp_path, monkeypatch, stop_scope,
):
    script = tmp_path / "flood.py"
    script.write_text(
        "import sys,time\n"
        "sys.stdout.buffer.write(b'x' * (1024 * 1024))\n"
        "sys.stdout.buffer.flush()\ntime.sleep(30)\n"
    )
    tool = bash.BashTool(process_owner_id="owner")
    spool_factory = bash.tempfile.SpooledTemporaryFile

    class FailingSpool:
        def __init__(self, *args, **kwargs):
            self.stream = spool_factory(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def write(self, data):
            if self.stream.tell() + len(data) > 256 * 1024:
                raise OSError(28, "synthetic disk full")
            return self.stream.write(data)

    async def create_process(*args, **kwargs):
        return await asyncio.create_subprocess_exec(
            sys.executable, str(script), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, start_new_session=True,
        )

    monkeypatch.setattr(tool, "_create_subprocess", create_process)
    monkeypatch.setattr(bash.tempfile, "SpooledTemporaryFile", FailingSpool)
    started = await tool.execute("synthetic flood", run_in_background=True, lifetime="runtime")
    assert started.success
    shell = manager.get(started.bash_id)
    transport = shell.process._transport
    pipe = transport.get_pipe_transport(1)
    pipe_file = pipe.get_extra_info("pipe")

    async def wait_for_backpressure():
        while shell.status != "error" or not shell.process.stdout._paused:
            await asyncio.sleep(.01)

    try:
        await asyncio.wait_for(wait_for_backpressure(), 5)
        assert shell.bash_id not in manager._monitor_tasks
        assert not pipe.is_closing()
        assert not pipe_file.closed
        if stop_scope == "kill":
            result = await asyncio.wait_for(bash.BashKillTool("owner").execute(shell.bash_id), 5)
            assert not result.success
            assert "synthetic disk full" in result.error
            assert result.persistence_content is not None
        elif stop_scope == "owner":
            assert await asyncio.wait_for(manager.terminate_owner("owner"), 5) == [shell.bash_id]
        else:
            assert await asyncio.wait_for(manager.terminate_all(), 5) == [shell.bash_id]
        # Keep strong references so GC cannot make an incomplete cleanup pass.
        assert shell.process.returncode is not None
        assert pipe.is_closing()
        assert pipe_file.closed
        assert shell._output._stream is None
        assert not manager._shells
        assert not manager._monitor_tasks
    finally:
        await tool._kill_process_tree(shell.process)
        transport.close()
        await asyncio.sleep(0)
        await tool.cleanup_background_processes()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX subprocess integration")
@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["x" * 65537, "x" * 1048576, "中文🙂" * 100000],
                         ids=["64k-plus-one", "1m", "utf8"])
async def test_live_process_long_line_is_complete_before_exit(manager, tmp_path, monkeypatch, text):
    payload = tmp_path / "payload"
    payload.write_bytes(text.encode() + b"\n")
    release = tmp_path / "release"
    script = tmp_path / "producer.py"
    script.write_text(
        "import sys,time\nfrom pathlib import Path\n"
        f"sys.stdout.buffer.write(Path({str(payload)!r}).read_bytes())\n"
        "sys.stdout.buffer.flush()\n"
        "for _ in range(1000):\n"
        f"    if Path({str(release)!r}).exists(): break\n"
        "    time.sleep(.01)\n"
        "sys.stdout.write('END')\nsys.stdout.flush()\n"
    )
    tool = bash.BashTool(process_owner_id="owner")

    async def create_process(*args, **kwargs):
        return await asyncio.create_subprocess_exec(
            sys.executable, str(script), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, start_new_session=True,
        )

    monkeypatch.setattr(tool, "_create_subprocess", create_process)
    try:
        started = await tool.execute("synthetic producer", run_in_background=True, lifetime="runtime")
        assert started.success
        shell = manager.get(started.bash_id)
        for _ in range(250):
            result = await bash.BashOutputTool("owner").execute(started.bash_id)
            if result.stdout:
                break
            await asyncio.sleep(.01)
        assert result.success
        assert shell.process.returncode is None
        assert text in result.persistence_content
        assert result.raw_output["original_stdout_chars"] == len(text)
        assert shell._output.size == 0
        monitor = manager._monitor_tasks[started.bash_id]
        release.touch()
        await asyncio.wait_for(asyncio.shield(monitor), 5)
        final = await bash.BashOutputTool("owner").execute(started.bash_id)
        assert final.success
        assert final.stdout == "END"
        assert final.exit_code == 0
    finally:
        release.touch()
        await tool.cleanup_background_processes()

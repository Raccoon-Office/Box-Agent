"""EOF notification must preserve framing and the platform stdio bridge."""

import asyncio
import io
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from types import SimpleNamespace

import pytest

from box_agent.acp import stdio_compat


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [128 * 1024, stdio_compat._READ_LIMIT, stdio_compat._READ_LIMIT + 1])
async def test_sdk_transport_enforces_complete_frame_limit(size):
    closed = asyncio.Event()
    reader = stdio_compat._StdioStreamReader()
    transport = stdio_compat.StdioTransport(reader, None, on_disconnect=closed.set)
    envelope = b'{"text":"' + b"x" * (size - len(b'{"text":""}')) + b'"}\n'
    # Deliberately split the oversized frame: the SDK default accumulates chunks.
    reader.feed_data(envelope[:65536])
    receiving = asyncio.create_task(transport.receive())
    await asyncio.sleep(0)
    reader.feed_data(envelope[65536:])
    if size > stdio_compat._READ_LIMIT:
        with pytest.raises(ValueError, match="limit"):
            await receiving
        assert closed.is_set()
    else:
        assert await receiving == json.loads(envelope)
        assert not closed.is_set()
        reader.feed_eof()
        assert await transport.receive() is None
        assert closed.is_set()


@pytest.mark.asyncio
async def test_sdk_transport_skips_malformed_frames_and_reports_io_failure():
    closed = asyncio.Event()
    reader = stdio_compat._StdioStreamReader()
    transport = stdio_compat.StdioTransport(reader, None, on_disconnect=closed.set)
    reader.feed_data(b'\nnot-json\n{"id":1,"result":null}\n')
    assert await transport.receive() == {"id": 1, "result": None}
    assert not closed.is_set()
    reader.set_exception(OSError("fixture read failure"))
    with pytest.raises(OSError, match="fixture read failure"):
        await transport.receive()
    assert closed.is_set()


def test_windows_stdout_stays_pinned_and_propagates_broken_pipe(monkeypatch):
    output, redirected = io.BytesIO(), io.BytesIO()
    monkeypatch.setattr(stdio_compat.sys, "stdout", SimpleNamespace(buffer=output))
    transport = stdio_compat._StdoutTransport()
    monkeypatch.setattr(stdio_compat.sys, "stdout", SimpleNamespace(buffer=redirected))
    transport.write(b'protocol\n')
    assert output.getvalue() == b'protocol\n'
    assert redirected.getvalue() == b''
    output.close()
    with pytest.raises(ValueError):
        transport.write(b'broken pipe')
    transport.close()
    with pytest.raises(ConnectionError):
        transport.write(b'closed')


@pytest.mark.asyncio
async def test_windows_stdout_backpressure_keeps_reads_available_and_preserves_frame_order(monkeypatch):
    entered, release = Event(), Event()

    class SlowOutput(io.BytesIO):
        def write(self, data):
            if data == b'{"id":1}\n':
                entered.set()
                assert release.wait(5)
            return super().write(data)

    output = SlowOutput()
    monkeypatch.setattr(stdio_compat.sys, "stdout", SimpleNamespace(buffer=output))
    reader = stdio_compat._StdioStreamReader()
    writer = SimpleNamespace(transport=stdio_compat._StdoutTransport())
    transport = stdio_compat.StdioTransport(reader, writer)
    first = asyncio.create_task(transport.send({"id": 1}))
    second = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        reader.feed_data(b'{"id":3}\n')
        assert await asyncio.wait_for(transport.receive(), 1) == {"id": 3}
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(transport.send({"id": 2}))
        await asyncio.sleep(0)
        assert output.getvalue() == b''
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)
        if second is not None:
            await second
        await transport.close()
    assert output.getvalue() == b'{"id":1}\n{"id":2}\n'


@pytest.mark.asyncio
async def test_windows_stdout_writes_do_not_queue_behind_a_saturated_default_executor(monkeypatch):
    # Tools and session I/O share the loop's default executor. A protocol frame
    # queued behind them would hit the session update timeout and fail the turn.
    loop = asyncio.get_running_loop()
    release = Event()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=2))
    output = io.BytesIO()
    monkeypatch.setattr(stdio_compat.sys, "stdout", SimpleNamespace(buffer=output))
    transport = stdio_compat.StdioTransport(
        asyncio.StreamReader(), SimpleNamespace(transport=stdio_compat._StdoutTransport()),
    )
    blockers = [loop.run_in_executor(None, release.wait, 5) for _ in range(2)]
    try:
        await asyncio.wait_for(transport.send({"id": 1}), 1)
        assert output.getvalue() == b'{"id":1}\n'
    finally:
        release.set()
        await asyncio.gather(*blockers)
        await transport.close()


@pytest.mark.asyncio
async def test_windows_stdout_normal_close_retires_the_write_thread(monkeypatch):
    output = io.BytesIO()
    monkeypatch.setattr(stdio_compat.sys, "stdout", SimpleNamespace(buffer=output))
    stdout_transport = stdio_compat._StdoutTransport()
    transport = stdio_compat.StdioTransport(asyncio.StreamReader(), SimpleNamespace(transport=stdout_transport))
    await transport.send({"id": 1})
    workers = tuple(stdout_transport._executor._threads)
    assert workers
    # The SDK's Connection.close() reaches only StdioTransport.close().
    await transport.close()
    for worker in workers:
        await asyncio.to_thread(worker.join, 2)
        assert not worker.is_alive()
    assert output.getvalue() == b'{"id":1}\n'
    with pytest.raises(ConnectionError):
        await stdout_transport.write_frame(b'{"id":2}\n')


@pytest.mark.asyncio
async def test_windows_stdout_cancelled_close_under_backpressure_keeps_the_loop_responsive(monkeypatch):
    # A stalled pipe write holds the buffer lock; flush() would wait for it.
    entered, release = Event(), Event()
    buffer_lock = Lock()

    class BackpressuredOutput(io.BytesIO):
        def write(self, data):
            with buffer_lock:
                entered.set()
                assert release.wait(5)
                return super().write(data)

        def flush(self):
            with buffer_lock:
                super().flush()

    output = BackpressuredOutput()
    monkeypatch.setattr(stdio_compat.sys, "stdout", SimpleNamespace(buffer=output))
    stdout_transport = stdio_compat._StdoutTransport()
    transport = stdio_compat.StdioTransport(asyncio.StreamReader(), SimpleNamespace(transport=stdout_transport))
    sending = asyncio.create_task(transport.send({"id": 1}))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        sending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sending
        loop = asyncio.get_running_loop()
        started = loop.time()
        ticked = loop.create_future()
        loop.call_later(0.1, lambda: ticked.done() or ticked.set_result(loop.time()))
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(transport.close(), 0.05)
        assert loop.time() - started < 0.3
        assert await asyncio.wait_for(ticked, 1) - started < 0.3
        assert stdout_transport.is_closing()
    finally:
        release.set()
    # The cancelled frame still completes on its own thread, then it exits.
    for worker in tuple(stdout_transport._executor._threads):
        await asyncio.to_thread(worker.join, 2)
        assert not worker.is_alive()
    assert output.getvalue() == b'{"id":1}\n'


@pytest.mark.asyncio
@pytest.mark.parametrize("stall", ["executor", "write", "flush", "send_lock"])
async def test_windows_stdout_reports_the_stalled_phase_and_late_completion(monkeypatch, tmp_path, stall):
    trace_id = "d1ec4f70-4800-4b8d-9687-105c2a0edeaa"
    log_path = tmp_path / stdio_compat._STDOUT_TIMING_LOG_NAME
    monkeypatch.setenv(
        "BOX_AGENT_ACP_STDIO_TIMING",
        json.dumps({"traceId": trace_id, "writeLogPath": str(log_path)}),
    )
    loop = asyncio.get_running_loop()
    submitted, writing = asyncio.Event(), asyncio.Event()
    diagnostics = asyncio.Queue()
    release = Event()
    monkeypatch.setattr(
        stdio_compat._StdoutTimingLog, "_write",
        lambda _self, record: loop.call_soon_threadsafe(diagnostics.put_nowait, record),
    )

    class ObservedExecutor(ThreadPoolExecutor):
        def submit(self, fn, /, *args, **kwargs):
            future = super().submit(fn, *args, **kwargs)
            submitted.set()
            return future

    class BlockedOutput(io.BytesIO):
        flush_blocked = False

        def write(self, data):
            if b'"id":1,' in data:
                if stall in {"write", "send_lock"}:
                    loop.call_soon_threadsafe(writing.set)
                    assert release.wait(5)
                self.flush_blocked = stall == "flush"
            return super().write(data)

        def flush(self):
            if self.flush_blocked:
                loop.call_soon_threadsafe(writing.set)
                assert release.wait(5)
                self.flush_blocked = False
            super().flush()

    output = BlockedOutput()
    monkeypatch.setattr(stdio_compat.sys, "stdout", SimpleNamespace(buffer=output))
    stdout_transport = stdio_compat._StdoutTransport()
    stdout_transport._executor.shutdown()
    stdout_transport._executor = ObservedExecutor(max_workers=1)
    transport = stdio_compat.StdioTransport(
        asyncio.StreamReader(), SimpleNamespace(transport=stdout_transport),
    )

    await transport.send({"id": 0})
    frame_start = len(output.getvalue())
    assert diagnostics.empty()
    payload = {"id": 1, "result": "private fixture data"}
    blocker = (
        loop.run_in_executor(stdout_transport._executor, release.wait, 5) if stall == "executor" else None
    )
    submitted.clear()
    sending = asyncio.create_task(transport.send(payload))
    waiter = sending

    try:
        ready = submitted if stall == "executor" else writing
        await asyncio.wait_for(ready.wait(), 2)
        if stall == "send_lock":
            waiter = asyncio.create_task(transport.send({"id": 9}))
            await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        pending = await asyncio.wait_for(diagnostics.get(), 2)
        assert pending["event"] == "wait_cancelled"
        assert pending["waitAt"] == ("send_lock" if stall == "send_lock" else "write_completion")
        assert pending["traceId"] == trace_id
        assert pending["frameStartByte"] == frame_start
        assert pending["submittedAfterMs"] is not None
        assert (pending["threadStartedAfterMs"] is None) == (stall == "executor")
        assert (pending["writeReturnedAfterMs"] is not None) == (stall == "flush")
        assert pending["flushReturnedAfterMs"] is None
    finally:
        release.set()
        if blocker is not None:
            await blocker
        await asyncio.gather(sending, waiter, return_exceptions=True)
        await asyncio.wait_for(transport.close(), 2)

    settled = await asyncio.wait_for(diagnostics.get(), 2)
    assert settled["event"] == "write_settled"
    assert settled["outcome"] == "completed"
    assert settled["frameStartByte"] == frame_start
    assert settled["frameEndByte"] == pending["frameEndByte"] == len(output.getvalue())
    assert (
        settled["submittedAfterMs"]
        <= settled["threadStartedAfterMs"]
        <= settled["writeReturnedAfterMs"]
        <= settled["flushReturnedAfterMs"]
        <= settled["observedAfterMs"]
    )
    assert "private fixture data" not in json.dumps([pending, settled])
    assert [json.loads(line) for line in output.getvalue().splitlines()] == [{"id": 0}, payload]


@pytest.mark.asyncio
async def test_stdout_timing_queue_stays_bounded_when_the_file_writer_is_blocked(monkeypatch, tmp_path):
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    written = asyncio.Queue()
    release = Event()

    def blocked_write(_sink, record):
        if record["sequence"] == 0:
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5)
        loop.call_soon_threadsafe(written.put_nowait, record["sequence"])

    monkeypatch.setattr(stdio_compat._StdoutTimingLog, "_write", blocked_write)
    sink = stdio_compat._StdoutTimingLog(tmp_path / stdio_compat._STDOUT_TIMING_LOG_NAME)
    capacity = stdio_compat._STDOUT_TIMING_QUEUE_LIMIT
    try:
        sink.emit({"sequence": 0})
        await asyncio.wait_for(entered.wait(), 2)
        for sequence in range(1, capacity + 4):
            sink.emit({"sequence": sequence})
        assert sink._records.qsize() == capacity
        sink.close()
        assert written.empty()
    finally:
        sink.close()
        release.set()

    assert [
        await asyncio.wait_for(written.get(), 2) for _ in range(capacity + 1)
    ] == list(range(capacity + 1))


@pytest.mark.asyncio
async def test_timing_file_failure_leaves_stdout_order_and_stderr_intact(
    monkeypatch, tmp_path, capfd,
):
    # Opening a directory as a log fails on every supported platform.
    log_path = tmp_path / stdio_compat._STDOUT_TIMING_LOG_NAME
    log_path.mkdir()
    monkeypatch.setenv("BOX_AGENT_ACP_STDIO_TIMING", json.dumps({
        "traceId": "d1ec4f70-4800-4b8d-9687-105c2a0edeaa",
        "writeLogPath": str(log_path),
    }))
    loop = asyncio.get_running_loop()
    writing = asyncio.Event()
    attempts = asyncio.Queue()
    release = Event()
    write_timing = stdio_compat._StdoutTimingLog._write

    def observe_write(sink, record):
        try:
            write_timing(sink, record)
        finally:
            loop.call_soon_threadsafe(attempts.put_nowait, record["event"])

    class Output(io.BytesIO):
        def write(self, data):
            if data == b'{"id":1}\n':
                loop.call_soon_threadsafe(writing.set)
                assert release.wait(5)
            return super().write(data)

    monkeypatch.setattr(stdio_compat._StdoutTimingLog, "_write", observe_write)
    output = Output()
    monkeypatch.setattr(stdio_compat.sys, "stdout", SimpleNamespace(buffer=output))
    transport = stdio_compat.StdioTransport(
        asyncio.StreamReader(), SimpleNamespace(transport=stdio_compat._StdoutTransport()),
    )
    first = asyncio.create_task(transport.send({"id": 1}))
    try:
        await asyncio.wait_for(writing.wait(), 2)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert await asyncio.wait_for(attempts.get(), 2) == "wait_cancelled"
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)

    try:
        await asyncio.wait_for(transport.send({"id": 2}), 2)
        assert await asyncio.wait_for(attempts.get(), 2) == "write_settled"
    finally:
        await asyncio.wait_for(transport.close(), 2)
    assert output.getvalue() == b'{"id":1}\n{"id":2}\n'
    assert "ACP_STDOUT_TIMING" not in capfd.readouterr().err


@pytest.mark.asyncio
async def test_windows_stdin_feeder_bounds_allocation_before_async_parsing(monkeypatch):
    monkeypatch.setattr(stdio_compat, "_READ_LIMIT", 1024)
    calls = []

    class Input(io.BytesIO):
        def readline(self, size=-1):
            calls.append(size)
            return super().readline(size)

    source = Input(b"x" * 4096)
    monkeypatch.setattr(stdio_compat.sys, "stdin", SimpleNamespace(buffer=source))
    reader = stdio_compat._StdioStreamReader()
    stdio_compat._start_stdin_feeder(asyncio.get_running_loop(), reader)
    with pytest.raises(ValueError, match="limit"):
        await asyncio.wait_for(reader.readline(), 2)
    assert await asyncio.wait_for(reader.readline(), 2) == b""
    assert calls == [1025]
    assert source.tell() == 1025


@pytest.mark.asyncio
async def test_stdin_eof_notifies_after_buffered_frames_are_consumed():
    notifications = []
    reader = stdio_compat._StdioStreamReader(lambda: notifications.append("eof"))
    large_frame = b"x" * (128 * 1024) + b"\n"
    reader.feed_data(large_frame + b"\n" + b"last frame without newline")
    reader.feed_eof()
    assert await reader.readline() == large_frame
    assert await reader.readline() == b"\n"
    assert await reader.readline() == b"last frame without newline"
    assert not notifications
    assert await reader.readline() == b""
    assert await reader.readline() == b""
    assert notifications == ["eof"]


@pytest.mark.asyncio
async def test_cancelling_stdin_read_does_not_report_host_eof():
    closed = asyncio.Event()
    reader = stdio_compat._StdioStreamReader(closed.set)
    pending = asyncio.create_task(reader.readline())
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not closed.is_set()
    reader.feed_data(b"still connected\n")
    assert await reader.readline() == b"still connected\n"
    assert not closed.is_set()
    reader.feed_eof()
    assert await reader.readline() == b""
    assert closed.is_set()


@pytest.mark.asyncio
async def test_windows_stdio_feeder_uses_eof_notification(monkeypatch):
    closed = asyncio.Event()

    def feed(loop, reader):
        reader.feed_data(b"frame\n")
        reader.feed_eof()

    monkeypatch.setattr(stdio_compat, "_start_stdin_feeder", feed)
    monkeypatch.setattr(stdio_compat.platform, "system", lambda: "Windows")
    reader, writer = await stdio_compat.stdio_streams_largebuf(on_eof=closed.set)
    try:
        assert await reader.readline() == b"frame\n"
        assert not closed.is_set()
        assert await reader.readline() == b""
        assert closed.is_set()
    finally:
        writer.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", [b"\n", b""])
@pytest.mark.parametrize("feed_eof", [False, True])
async def test_oversized_stdin_frame_notifies_disconnect_once(suffix, feed_eof):
    notifications = []
    reader = stdio_compat._StdioStreamReader(lambda: notifications.append("closed"))
    reader.feed_data(b"x" * (stdio_compat._READ_LIMIT + 1) + suffix)
    if feed_eof:
        reader.feed_eof()
    with pytest.raises(ValueError, match="limit"):
        await reader.readline()
    assert notifications == ["closed"]
    if not feed_eof:
        reader.feed_eof()
    assert await reader.readline() == b""
    assert notifications == ["closed"]


@pytest.mark.asyncio
async def test_stdin_io_failure_notifies_disconnect_once():
    notifications = []
    reader = stdio_compat._StdioStreamReader(lambda: notifications.append("closed"))
    reader.set_exception(OSError("fixture read failure"))
    for _ in range(2):
        with pytest.raises(OSError, match="fixture read failure"):
            await reader.readline()
    assert notifications == ["closed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["drain", "timeout", "cancel"])
async def test_connection_shutdown_settles_sdk_tasks_and_pending_output(mode):
    from acp.connection import Connection
    from box_agent.acp import _close_acp_connection

    protocol = stdio_compat._WritePipeProtocol()
    written = asyncio.Event()
    handler_started = asyncio.Event()
    handler_closed = asyncio.Event()
    before = asyncio.all_tasks()

    class Transport(asyncio.WriteTransport):
        aborted = False
        payload = b""

        def write(self, data):
            self.payload += data
            protocol.pause_writing()
            written.set()

        def is_closing(self):
            return self.aborted

        def abort(self):
            self.aborted = True

        def close(self):
            self.abort()

    transport = Transport()
    reader = asyncio.StreamReader()
    writer = asyncio.StreamWriter(transport, protocol, None, asyncio.get_running_loop())

    async def handle(method, params, is_notification):
        handler_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            handler_closed.set()

    connection = Connection(handle, stdio_compat.StdioTransport(reader, writer))
    reader.feed_data(b'{"jsonrpc":"2.0","id":1,"method":"fixture_wait"}\n')
    await asyncio.wait_for(handler_started.wait(), 1)
    sending = asyncio.create_task(connection.send_notification("fixture_update", {"ok": True}))
    await asyncio.wait_for(written.wait(), 1)
    closing = asyncio.create_task(_close_acp_connection(
        connection, writer, timeout=0.02 if mode == "timeout" else 1,
    ))
    try:
        if mode != "timeout":
            await asyncio.sleep(0.01)
            assert not closing.done()
        if mode == "drain":
            protocol.resume_writing()
            await asyncio.wait_for(sending, 1)
            await asyncio.wait_for(closing, 1)
            assert not transport.aborted
            assert b'"method":"fixture_update"' in transport.payload
        elif mode == "timeout":
            await asyncio.wait_for(closing, 1)
            assert transport.aborted
        else:
            closing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(closing, 1)
            assert transport.aborted
        assert handler_closed.is_set()
    finally:
        sending.cancel()
        closing.cancel()
        await asyncio.gather(sending, closing, return_exceptions=True)
        writer.close()
        await connection.close()
    assert not (asyncio.all_tasks() - before)

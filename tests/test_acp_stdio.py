"""EOF notification must preserve framing and the platform stdio bridge."""

import asyncio
import io
import json
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

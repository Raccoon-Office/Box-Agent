"""Bounded ACP stdio with host-disconnect and platform lifecycle handling.

SDK 0.12.1 accepts a reader limit, but its default NDJSON transport joins
unlimited readuntil chunks, so that limit is not a message-size ceiling.
Use the SDK's message transport interface with bounded readline instead.
The SDK continues to own JSON-RPC routing, requests and response correlation.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import platform
import queue
import re
import sys
import threading
import time
from asyncio import transports as aio_transports
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

# 32 MiB. Default asyncio limit (64 KiB) is too small for ACP frames that may
# include restored conversation context, generated artifacts, or base64-inlined
# images. Keep an explicit ceiling, but leave enough room for large officev3
# resume prompts so the ACP receive loop does not die before it can respond.
_READ_LIMIT = 32 * 1024 * 1024
_STDOUT_TIMING_QUEUE_LIMIT = 32


class StdioTransport:
    """Serialize frames without a connection-long payload or sender queue."""

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
        *, on_disconnect: Callable[[], None] | None = None,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._send_lock = asyncio.Lock()
        self._closed = False
        self._on_disconnect = on_disconnect

    def _disconnect(self) -> None:
        if self._on_disconnect is not None:
            callback, self._on_disconnect = self._on_disconnect, None
            callback()

    async def send(self, message: dict) -> None:
        # Serialization errors reject only this request; a broken pipe ends
        # the connection. Keep each frame and drain under the same lock.
        frame = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        transport = getattr(self._writer, "transport", None)
        try:
            async with self._send_lock:
                if self._closed:
                    raise ConnectionError("Transport closed")
                try:
                    if isinstance(transport, _StdoutTransport):
                        await transport.write_frame(frame)
                    else:
                        self._writer.write(frame)
                        await self._writer.drain()
                except Exception:
                    self._disconnect()
                    raise
        except asyncio.CancelledError:
            # A notification can time out behind another sender holding the lock.
            if isinstance(transport, _StdoutTransport):
                transport.report_cancelled_wait("send_lock")
            raise

    async def receive(self) -> dict | None:
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    self._disconnect()
                    return None
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeError):
                    logging.exception("Error parsing JSON-RPC message")
                    continue
                if not isinstance(message, dict):
                    raise ValueError("ACP frame must be a JSON object")
                return message
        except Exception:
            self._disconnect()
            raise

    async def close(self) -> None:
        self._closed = True
        async with self._send_lock:
            transport = getattr(self._writer, "transport", None)
            if isinstance(transport, _StdoutTransport):
                try:
                    await transport.drain_frame()
                finally:
                    transport.close_timing_log()
            else:
                await self._writer.drain()


class _StdioStreamReader(asyncio.StreamReader):
    def __init__(self, on_eof: Callable[[], None] | None = None) -> None:
        super().__init__(limit=_READ_LIMIT)
        self._on_eof = on_eof

    async def readline(self) -> bytes:
        try:
            line = await super().readline()
        except Exception:
            # The SDK will not read again after a framing or I/O failure.
            # CancelledError remains a transient read cancellation, not EOF.
            self._notify_closed()
            raise
        if not line:
            self._notify_closed()
        return line

    def _notify_closed(self) -> None:
        if self._on_eof is not None:
            callback, self._on_eof = self._on_eof, None
            callback()


class _WritePipeProtocol(asyncio.BaseProtocol):
    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._paused = False
        self._drain_waiter: asyncio.Future[None] | None = None

    def pause_writing(self) -> None:  # type: ignore[override]
        self._paused = True
        if self._drain_waiter is None:
            self._drain_waiter = self._loop.create_future()

    def resume_writing(self) -> None:  # type: ignore[override]
        self._paused = False
        if self._drain_waiter is not None and not self._drain_waiter.done():
            self._drain_waiter.set_result(None)
        self._drain_waiter = None

    async def _drain_helper(self) -> None:
        if self._paused and self._drain_waiter is not None:
            await self._drain_waiter


def _start_stdin_feeder(loop: asyncio.AbstractEventLoop, reader: asyncio.StreamReader) -> None:
    def blocking_read() -> None:
        try:
            while True:
                # Bound the feeder allocation too: a Windows pipe does not use
                # asyncio's read transport or its buffer limit.
                data = sys.stdin.buffer.readline(_READ_LIMIT + 1)
                if not data:
                    break
                loop.call_soon_threadsafe(reader.feed_data, data)
                if len(data) > _READ_LIMIT and not data.endswith(b"\n"):
                    break  # Let bounded readline report the oversized frame.
        finally:
            loop.call_soon_threadsafe(reader.feed_eof)

    threading.Thread(target=blocking_read, daemon=True).start()


@dataclass(slots=True)
class _StdoutWriteTiming:
    start_byte: int
    end_byte: int
    scheduled_ns: int
    scheduled_unix_ms: int
    submitted_ns: int | None = None
    started_ns: int | None = None
    written_ns: int | None = None
    flushed_ns: int | None = None
    reported: bool = False

    def elapsed_ms(self, stamp_ns: int | None) -> float | None:
        return None if stamp_ns is None else round((stamp_ns - self.scheduled_ns) / 1_000_000, 3)


class _StderrTimingLog:
    """Best-effort diagnostics isolated from ACP and its default executor."""

    def __init__(self, fd: int) -> None:
        self._fd = fd
        self._records: queue.Queue[dict[str, object] | None] = queue.Queue(
            maxsize=_STDOUT_TIMING_QUEUE_LIMIT,
        )
        self._closed = False
        threading.Thread(target=self._run, name="acp-stdout-timing", daemon=True).start()

    def emit(self, record: dict[str, object]) -> None:
        if not self._closed:
            with contextlib.suppress(queue.Full):
                self._records.put_nowait(record)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(queue.Full):
            self._records.put_nowait(None)

    def _run(self) -> None:
        while (record := self._records.get()) is not None:
            with contextlib.suppress(OSError):
                self._write(record)
            if self._closed and self._records.empty():
                return

    def _write(self, record: dict[str, object]) -> None:
        # Avoid a shared logging handler or buffered stream lock while the pipe
        # is stalled. The daemon can also outlive a bounded ACP shutdown.
        body = json.dumps(record, separators=(",", ":"))
        remaining = memoryview(f"ACP_STDOUT_TIMING {body}\n".encode("ascii"))
        while remaining:
            written = os.write(self._fd, remaining)
            if not written:
                return
            remaining = remaining[written:]


class _StdoutTransport(asyncio.BaseTransport):
    def __init__(self) -> None:
        self._is_closing = False
        # run_acp_server redirects sys.stdout to stderr after constructing us.
        # Keep protocol bytes on the original pipe, including on Windows.
        self._stdout = sys.stdout.buffer
        self._frame: asyncio.Task[None] | None = None
        trace_id = os.environ.get("BOX_AGENT_ACP_STDIO_TRACE_ID", "")
        self._trace_id = (
            trace_id if re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", trace_id) else ""
        )
        self._timing_log: _StderrTimingLog | None = None
        if self._trace_id:
            with contextlib.suppress(AttributeError, OSError, RuntimeError, ValueError):
                self._timing_log = _StderrTimingLog(sys.stderr.fileno())
        # Offsets are from process launch and include the NDJSON newline.
        self._next_byte = 0
        self._timing: _StdoutWriteTiming | None = None

    async def write_frame(self, data: bytes) -> None:
        # Windows pipe writes can block even with the asyncio send lock held.
        # A cancelled sender must leave its frame ordered ahead of the next one.
        await self.drain_frame()
        if self._timing_log is not None:
            timing = _StdoutWriteTiming(
                self._next_byte, self._next_byte + len(data),
                time.monotonic_ns(), time.time_ns() // 1_000_000,
            )
            self._next_byte = timing.end_byte
            self._timing = timing
            self._frame = asyncio.create_task(self._write_timed_frame(data, timing))
        else:
            self._frame = asyncio.create_task(asyncio.to_thread(self.write, data))
        await self.drain_frame()

    async def _write_timed_frame(self, data: bytes, timing: _StdoutWriteTiming) -> None:
        timing.submitted_ns = time.monotonic_ns()
        await asyncio.to_thread(self.write, data, timing)

    async def drain_frame(self) -> None:
        frame = self._frame
        if frame is None:
            return
        try:
            await asyncio.shield(frame)
        except asyncio.CancelledError:
            self.report_cancelled_wait("write_completion")
            raise
        finally:
            if frame.done():
                self._frame = None
                self._timing = None

    def report_cancelled_wait(self, wait_at: str) -> None:
        frame, timing = self._frame, self._timing
        if frame is None or timing is None or timing.reported:
            return
        timing.reported = True
        self._report_timing(timing, "wait_cancelled", wait_at=wait_at)
        frame.add_done_callback(lambda done: self._report_settled_timing(timing, done))

    def _report_settled_timing(self, timing: _StdoutWriteTiming, frame: asyncio.Task[None]) -> None:
        if frame.cancelled():
            outcome = "cancelled"
        else:
            outcome = "failed" if frame.exception() is not None else "completed"
        self._report_timing(timing, "write_settled", outcome=outcome)

    def _report_timing(
        self, timing: _StdoutWriteTiming, event: str, *,
        outcome: str | None = None, wait_at: str | None = None,
    ) -> None:
        timing_log = self._timing_log
        if timing_log is None:
            return
        # Read latest phases first, so the snapshot always includes prerequisites
        # even if the worker thread advances while we read its timestamps.
        flushed, written = timing.flushed_ns, timing.written_ns
        started, submitted = timing.started_ns, timing.submitted_ns
        observed_ns = time.monotonic_ns()
        record: dict[str, object] = {
            "event": event,
            "outcome": outcome,
            "waitAt": wait_at,
            "traceId": self._trace_id,
            "frameStartByte": timing.start_byte,
            "frameEndByte": timing.end_byte,
            "scheduledAtUnixMs": timing.scheduled_unix_ms,
            "observedAtUnixMs": time.time_ns() // 1_000_000,
            "observedAfterMs": timing.elapsed_ms(observed_ns),
            "submittedAfterMs": timing.elapsed_ms(submitted),
            "threadStartedAfterMs": timing.elapsed_ms(started),
            "writeReturnedAfterMs": timing.elapsed_ms(written),
            "flushReturnedAfterMs": timing.elapsed_ms(flushed),
        }
        timing_log.emit(record)

    def write(self, data: bytes, timing: _StdoutWriteTiming | None = None) -> None:  # type: ignore[override]
        if self._is_closing:
            raise ConnectionError("stdout closed")
        if timing is not None:
            timing.started_ns = time.monotonic_ns()
        self._stdout.write(data)
        if timing is not None:
            timing.written_ns = time.monotonic_ns()
        self._stdout.flush()
        if timing is not None:
            timing.flushed_ns = time.monotonic_ns()

    def can_write_eof(self) -> bool:  # type: ignore[override]
        return False

    def is_closing(self) -> bool:  # type: ignore[override]
        return self._is_closing

    def close_timing_log(self) -> None:
        if self._timing_log is not None:
            self._timing_log.close()

    def close(self) -> None:  # type: ignore[override]
        self._is_closing = True
        self.close_timing_log()
        with contextlib.suppress(Exception):
            self._stdout.flush()

    def abort(self) -> None:  # type: ignore[override]
        self.close()

    def get_extra_info(self, name: str, default=None):  # type: ignore[override]
        return default


async def _windows_stdio_streams(
    loop: asyncio.AbstractEventLoop,
    on_eof: Callable[[], None] | None = None,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader = _StdioStreamReader(on_eof)
    _ = asyncio.StreamReaderProtocol(reader)

    _start_stdin_feeder(loop, reader)

    write_protocol = _WritePipeProtocol()
    transport = _StdoutTransport()
    writer = asyncio.StreamWriter(
        cast(aio_transports.WriteTransport, transport), write_protocol, None, loop
    )
    return reader, writer


async def _posix_stdio_streams(
    loop: asyncio.AbstractEventLoop,
    on_eof: Callable[[], None] | None = None,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader = _StdioStreamReader(on_eof)
    reader_protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: reader_protocol, sys.stdin)

    write_protocol = _WritePipeProtocol()
    transport, _ = await loop.connect_write_pipe(lambda: write_protocol, sys.stdout)
    writer = asyncio.StreamWriter(transport, write_protocol, None, loop)
    return reader, writer


async def stdio_streams_largebuf(
    *, on_eof: Callable[[], None] | None = None,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """ACP stdio with a 32 MiB limit; notify on consumed EOF or fatal read error."""
    loop = asyncio.get_running_loop()
    if platform.system() == "Windows":
        return await _windows_stdio_streams(loop, on_eof)
    return await _posix_stdio_streams(loop, on_eof)

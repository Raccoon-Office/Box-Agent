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
import platform
import sys
import threading
from asyncio import transports as aio_transports
from collections.abc import Callable
from typing import cast

# 32 MiB. Default asyncio limit (64 KiB) is too small for ACP frames that may
# include restored conversation context, generated artifacts, or base64-inlined
# images. Keep an explicit ceiling, but leave enough room for large officev3
# resume prompts so the ACP receive loop does not die before it can respond.
_READ_LIMIT = 32 * 1024 * 1024


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
        async with self._send_lock:
            if self._closed:
                raise ConnectionError("Transport closed")
            try:
                self._writer.write(frame)
                await self._writer.drain()
            except Exception:
                self._disconnect()
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


class _StdoutTransport(asyncio.BaseTransport):
    def __init__(self) -> None:
        self._is_closing = False
        # run_acp_server redirects sys.stdout to stderr after constructing us.
        # Keep protocol bytes on the original pipe, including on Windows.
        self._stdout = sys.stdout.buffer

    def write(self, data: bytes) -> None:  # type: ignore[override]
        if self._is_closing:
            raise ConnectionError("stdout closed")
        self._stdout.write(data)
        self._stdout.flush()

    def can_write_eof(self) -> bool:  # type: ignore[override]
        return False

    def is_closing(self) -> bool:  # type: ignore[override]
        return self._is_closing

    def close(self) -> None:  # type: ignore[override]
        self._is_closing = True
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

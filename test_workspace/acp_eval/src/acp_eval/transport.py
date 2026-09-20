"""Shared stdio framing and response exchange for ACP evaluation and probes.

The evaluator keeps its permissive capture policy; probes can request strict
frames; reverse RPC keeps the evaluator's deny-by-default permission policy.
"""
from __future__ import annotations

import asyncio
from time import monotonic
from typing import Any, Mapping

from acp_eval.protocol import ACPAccumulator, ProtocolRecorder
from acp_eval.lifecycle import ProcessRecorder

class _ACPStdoutReader:
    """Persist arbitrary chunks before incrementally extracting JSONL frames."""

    def __init__(self, stream: asyncio.StreamReader, protocol: ProtocolRecorder) -> None:
        self.stream = stream
        self.protocol = protocol
        self.buffer = bytearray()
        self.buffer_offset = 0
        self.eof = False

    async def read_frame(
        self, deadline: float | None = None
    ) -> tuple[bool, dict[str, Any] | None]:
        while True:
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                frame_size = newline + 1
                raw_line = bytes(self.buffer[:frame_size])
                byte_offset = self.buffer_offset
                del self.buffer[:frame_size]
                self.buffer_offset += frame_size
                return False, self.protocol.record_persisted_received(
                    raw_line, byte_offset
                )

            if self.eof:
                if self.buffer:
                    raw_data = bytes(self.buffer)
                    byte_offset = self.buffer_offset
                    self.buffer.clear()
                    self.buffer_offset += len(raw_data)
                    self.protocol.record_incomplete_received(raw_data, byte_offset)
                return True, None

            read = self.stream.read(64 * 1024)
            if deadline is None:
                chunk = await read
            else:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                chunk = await asyncio.wait_for(read, timeout=remaining)
            if not chunk:
                self.eof = True
                continue
            chunk_offset = self.protocol.record_received_chunk(chunk)
            if not self.buffer:
                self.buffer_offset = chunk_offset
            self.buffer.extend(chunk)


async def _send(
    process: asyncio.subprocess.Process,
    recorder: ProtocolRecorder,
    message: Mapping[str, Any],
) -> None:
    if process.stdin is None:
        raise RuntimeError("ACP stdin is unavailable")
    raw = recorder.record_sent(message)
    process.stdin.write(raw)
    await process.stdin.drain()


def _permission_reply(message: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": message["id"],
        "result": {"outcome": {"outcome": "cancelled"}},
    }


async def _read_until_response(
    process: asyncio.subprocess.Process,
    stdout_reader: _ACPStdoutReader,
    protocol: ProtocolRecorder,
    accumulator: ACPAccumulator,
    process_recorder: ProcessRecorder,
    state: Any,
    expected_id: int,
    deadline: float,
    *,
    strict: bool = False,
) -> dict[str, Any]:
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError
        eof, message = await stdout_reader.read_frame(deadline=deadline)
        if strict and protocol.parse_errors:
            raise ValueError("Invalid ACP stdout frame")
        if eof:
            process_recorder.write("stream.eof", stream="stdout")
            state.stdout_terminal = True
            raise EOFError("ACP process closed stdout")
        if message is None:
            continue
        accumulator.consume(message)
        if message.get("id") == expected_id and "method" not in message:
            return message
        if "id" in message and "method" in message:
            if message.get("method") == "session/request_permission":
                reply = _permission_reply(message)
            else:
                reply = {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32601, "message": "Unsupported reverse RPC"},
                }
            await _send(process, protocol, reply)

"""Regression coverage for the manual ACP streaming client's framing."""

import asyncio
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time

import pytest


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts/test_acp_streaming.py"
_SPEC = importlib.util.spec_from_file_location("acp_streaming_script", _SCRIPT)
client = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(client)


def make_reader(stream, tmp_path):
    recorder = client.ProtocolRecorder(
        tmp_path, lambda: datetime.now(timezone.utc), time.monotonic_ns
    )
    return client._ACPStdoutReader(stream, recorder)


@pytest.mark.asyncio
async def test_large_fragmented_frame_survives_timeout_and_keeps_next_frame(tmp_path):
    stream = asyncio.StreamReader()  # Keep the default 64 KiB buffer limit.
    reader = make_reader(stream, tmp_path)
    message = {"jsonrpc": "2.0", "id": 1, "result": "研究" * 30_000}
    following = {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "second"}}
    raw = json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"
    # Split inside a UTF-8 character, after already exceeding 64 KiB.
    split = raw.index("研".encode("utf-8")) + 6 * 12_000 + 1
    stream.feed_data(raw[:split])
    with pytest.raises(asyncio.TimeoutError):
        await client.read_line(reader, timeout=0.01)

    stream.feed_data(raw[split:] + json.dumps(following).encode() + b"\n")
    stream.feed_eof()
    assert await client.read_line(reader) == message
    assert await client.read_line(reader) == following
    assert await client.read_line(reader) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [b"not json\n", b"[]\n", b"\xff\n", b'{"id": 1}'])
async def test_invalid_or_unterminated_frame_is_reported_as_error(tmp_path, raw):
    stream = asyncio.StreamReader()
    reader = make_reader(stream, tmp_path)
    stream.feed_data(raw)
    stream.feed_eof()
    with pytest.raises(ValueError, match="Invalid ACP stdout frame"):
        await client.read_line(reader)


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout, completes", [(20, True), (1, False)])
async def test_script_reads_large_stdio_result_until_response_or_deadline(
    tmp_path, monkeypatch, capsys, timeout, completes,
):
    # A real stdio peer reproduces the oversized web-search result without
    # depending on network access, model credentials, or provider rate limits.
    peer = r'''
import json, sys, time

def receive():
    return json.loads(sys.stdin.buffer.readline())

def send(value):
    raw = json.dumps(value, ensure_ascii=False).encode("utf-8") + b"\n"
    for offset in range(0, len(raw), 8192):
        sys.stdout.buffer.write(raw[offset:offset + 8192])
        sys.stdout.buffer.flush()

request = receive()
assert request["method"] == "initialize"
send({"jsonrpc": "2.0", "id": request["id"], "result": {"protocolVersion": 1}})
request = receive()
assert request["method"] == "session/new"
send({"jsonrpc": "2.0", "id": request["id"], "result": {"sessionId": "test-session"}})
request = receive()
assert request["method"] == "session/prompt"
send({"jsonrpc": "2.0", "method": "session/update", "params": {
    "sessionId": "test-session", "update": {
        "sessionUpdate": "tool_call_update", "toolCallId": "search-1",
        "status": "completed", "rawOutput": "x" * (5 * 1024 * 1024),
    },
}})
time.sleep(11)
send({"jsonrpc": "2.0", "id": request["id"], "result": {"stopReason": "end_turn"}})
sys.stdin.buffer.read()
'''
    create_process = asyncio.create_subprocess_exec
    processes = []

    async def start_peer(*command, **kwargs):
        assert command == (sys.executable, "-m", "box_agent.acp.server")
        proc = await create_process(sys.executable, "-u", "-c", peer, **kwargs)
        processes.append(proc)
        return proc

    monkeypatch.setattr(client.asyncio, "create_subprocess_exec", start_peer)
    monkeypatch.setattr(
        sys, "argv", [str(_SCRIPT), "--prompt", "hello", "--timeout", str(timeout)]
    )
    monkeypatch.setattr(
        client, "TemporaryDirectory", lambda **kw: TemporaryDirectory(dir=tmp_path, **kw)
    )
    await asyncio.wait_for(client.main(), timeout=25)

    output = capsys.readouterr().out
    assert "tool_call_update" in output
    if completes:
        assert "stopReason: end_turn" in output
        assert "TIMEOUT" not in output
    else:
        assert f"TIMEOUT after {timeout}s" in output
        assert "stopReason: end_turn" not in output
    assert "assuming done" not in output
    assert all(proc.returncode is not None for proc in processes)
    assert not list(tmp_path.glob("acp-streaming-*"))

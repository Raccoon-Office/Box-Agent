"""Desktop-compatible Volc ASR framing, authentication, timing and lifecycle."""

from __future__ import annotations

import asyncio
import gzip
import json
import struct

import pytest
from websockets.asyncio.server import serve

from box_agent.tools import video_transcription as module
from box_agent.tools.video_transcription import (
    TranscriptionError, VolcAsrConfig, _packet, _parse_response, _transcript, transcribe_pcm,
)


def response(payload, *, final=True, compressed=True, sequence=True):
    body = json.dumps(payload, ensure_ascii=False).encode()
    if compressed:
        body = gzip.compress(body)
    flags = int(sequence) | (2 if final else 0)
    return (bytes((0x11, 0x90 | flags, 0x10 | int(compressed), 0))
            + (struct.pack(">i", -1 if final else 1) if sequence else b"")
            + struct.pack(">I", len(body)) + body)


@pytest.mark.parametrize("env,header,value", [
    ({"VOLC_ASR_API_KEY": "api"}, "X-Api-Key", "api"),
    ({"DOUBAO_ASR_API_KEY": "alias"}, "X-Api-Key", "alias"),
    ({"VOLC_ASR_APPID": "app", "VOLC_ASR_ACCESS_TOKEN": "legacy"}, "X-Api-Access-Key", "legacy"),
    ({"DOUBAO_ASR_APP_ID": "app", "DOUBAO_ASR_AT": "alias"}, "X-Api-Access-Key", "alias"),
])
def test_same_desktop_environment_auth_modes(env, header, value):
    config = VolcAsrConfig.from_env(env)
    assert config.headers()[header] == value
    assert config.url.endswith("/sauc/bigmodel_async")
    assert config.resource_id == "volc.seedasr.sauc.duration"


def test_auth_is_not_in_repr_and_api_key_has_desktop_precedence():
    config = VolcAsrConfig.from_env({"VOLC_ASR_API_KEY": "private-value", "VOLC_ASR_APP_ID": "app",
                                   "VOLC_ASR_ACCESS_KEY": "private-legacy"})
    assert "X-Api-App-Key" not in config.headers()
    assert "private" not in repr(config)
    assert VolcAsrConfig.from_env({}) is None
    assert VolcAsrConfig.from_env({"VOLC_ASR_APP_ID": "app"}) is None


@pytest.mark.parametrize("url", ["http://bad", "ws://bad", "wss://user:secret@host", "wss://["])
def test_bad_asr_endpoints_are_sanitized(url):
    with pytest.raises(TranscriptionError) as error:
        VolcAsrConfig.from_env({"VOLC_ASR_API_KEY": "key", "VOLC_ASR_WS_URL": url})
    assert "secret" not in str(error.value)


def test_request_packets_match_desktop_binary_protocol():
    request = _packet(b'{"audio":{"rate":16000}}')
    assert request[:4] == bytes.fromhex("11101100")
    assert struct.unpack(">I", request[4:8])[0] == len(request[8:])
    assert json.loads(gzip.decompress(request[8:]))["audio"]["rate"] == 16000
    audio = _packet(b"\0\1\0\2", audio=True)
    assert audio[:4] == bytes.fromhex("11200100")
    assert gzip.decompress(audio[8:]) == b"\0\1\0\2"
    assert _packet(b"", audio=True, last=True) == bytes.fromhex("1122000000000000")


@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("sequence", [False, True])
@pytest.mark.parametrize("final", [False, True])
def test_server_response_flags_compression_and_unicode(compressed, sequence, final):
    payload = {"result": {"text": "你好"}}
    assert _parse_response(response(payload, compressed=compressed, sequence=sequence, final=final)) == (final, payload)


@pytest.mark.parametrize("data", [b"", b"notasrdata", bytes.fromhex("1193110000000001"),
                                   response({})[:-1], response({}) + b"extra",
                                   bytes.fromhex("1192110000000004") + b"xxxx"])
def test_malformed_server_packets_fail_closed(data):
    with pytest.raises(TranscriptionError):
        _parse_response(data)


def test_server_error_does_not_echo_payload_and_gzip_expansion_is_bounded():
    secret = b"private credential and request data"
    error = bytes.fromhex("11f01000") + struct.pack(">II", 45000001, len(secret)) + secret
    with pytest.raises(TranscriptionError, match="45000001") as captured:
        _parse_response(error)
    assert "private" not in str(captured.value)
    with pytest.raises(TranscriptionError):
        _parse_response(response({"result": {"text": "x" * 1_100_000}}))


def test_transcript_offsets_utterance_milliseconds_into_original_video_time():
    payload = {"result": {"text": "Hello world", "utterances": [
        {"start_time": 200, "end_time": 1800, "text": "Hello world"},
        {"start_time": -100, "end_time": 100, "text": "bad"},
        {"start_time": "200", "end_time": 300, "text": "bad"},
    ]}}
    transcript = _transcript(payload, 30, 5)
    assert transcript["segments"] == [{"start_seconds": 30.2, "end_seconds": 31.8, "text": "Hello world"}]
    assert transcript["scope"] == "speech_only"
    assert _transcript({"result": {"text": ""}}, 0, 1)["text"] == ""
    with pytest.raises(TranscriptionError):
        _transcript({"result": {}}, 0, 1)


async def test_local_websocket_service_receives_pcm_and_returns_final_timestamped_transcript(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "_CHUNK_SECONDS", 0)
    pcm = tmp_path / "speech.pcm"
    pcm.write_bytes(bytes(range(256)) * 30)
    received = {"audio": b""}

    async def handler(socket):
        received["key"] = socket.request.headers["X-Api-Key"]
        initial = await socket.recv()
        received["initial"] = json.loads(gzip.decompress(initial[8:]))
        await socket.send(response({"result": {"text": "partial"}}, final=False))
        while True:
            packet = await socket.recv()
            if packet[1] & 2:
                break
            received["audio"] += gzip.decompress(packet[8:])
        await socket.send(response({"result": {"text": "实际转写", "utterances": [
            {"start_time": 20, "end_time": 200, "text": "实际转写"}]}}))

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        result = await transcribe_pcm(pcm, config=VolcAsrConfig(url=f"ws://127.0.0.1:{port}", api_key="test"),
                                      start_seconds=7, duration=1)
    assert received["audio"] == pcm.read_bytes()
    assert received["key"] == "test"
    assert received["initial"]["audio"] == {"format": "pcm", "codec": "raw", "rate": 16000, "bits": 16, "channel": 1}
    assert received["initial"]["request"]["show_utterances"] is True
    assert result["text"] == "实际转写"
    assert result["segments"][0]["start_seconds"] == 7.02


async def test_disconnect_after_partial_transcript_is_not_success(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "_CHUNK_SECONDS", 0)
    pcm = tmp_path / "speech.pcm"
    pcm.write_bytes(b"\0\0" * 32)

    async def handler(socket):
        await socket.recv()
        await socket.send(response({"result": {"text": "partial"}}, final=False))
        await socket.close()

    async with serve(handler, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        with pytest.raises(TranscriptionError, match="connection failed"):
            await transcribe_pcm(pcm, config=VolcAsrConfig(url=url), start_seconds=0, duration=1)


async def test_cancellation_closes_asr_connection(tmp_path):
    pcm = tmp_path / "speech.pcm"
    pcm.write_bytes(b"\0\0" * 16000)
    opened, closed = asyncio.Event(), asyncio.Event()

    async def handler(socket):
        await socket.recv()
        opened.set()
        await socket.wait_closed()
        closed.set()

    async with serve(handler, "127.0.0.1", 0) as server:
        config = VolcAsrConfig(url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
        task = asyncio.create_task(transcribe_pcm(pcm, config=config, start_seconds=0, duration=1))
        await asyncio.wait_for(opened.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(closed.wait(), 3)


async def test_missing_final_response_times_out_and_closes_connection(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "_CHUNK_SECONDS", 0)
    monkeypatch.setattr(module, "_FINAL_TIMEOUT_SECONDS", 0.05)
    pcm = tmp_path / "speech.pcm"
    pcm.write_bytes(b"\0\0" * 16)
    closed = asyncio.Event()

    async def handler(socket):
        await socket.recv()
        await socket.wait_closed()
        closed.set()

    async with serve(handler, "127.0.0.1", 0) as server:
        config = VolcAsrConfig(url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
        with pytest.raises(TranscriptionError, match="timed out"):
            await transcribe_pcm(pcm, config=config, start_seconds=0, duration=0.01)
        await asyncio.wait_for(closed.wait(), 3)

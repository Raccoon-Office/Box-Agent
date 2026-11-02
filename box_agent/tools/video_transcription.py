"""Volc streaming ASR, using the desktop voice client's environment and wire protocol."""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import math
import os
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit
from uuid import uuid4

from websockets.asyncio.client import connect

from .base import ToolInvocationRejected, check_tool_cancellation


_DEFAULT_URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
_DEFAULT_RESOURCE = "volc.seedasr.sauc.duration"
_MAX_RESPONSE_BYTES = 1_048_576
_CHUNK_BYTES = 6_400  # 200 ms of mono 16 kHz, signed 16-bit PCM.
_CHUNK_SECONDS = 0.2
_FINAL_TIMEOUT_SECONDS = 60


class TranscriptionError(ValueError):
    """Sanitized ASR failure; never contains credentials or server payloads."""


@dataclass(frozen=True)
class VolcAsrConfig:
    url: str = _DEFAULT_URL
    resource_id: str = _DEFAULT_RESOURCE
    api_key: str = field(default="", repr=False)
    app_id: str = field(default="", repr=False)
    access_key: str = field(default="", repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> VolcAsrConfig | None:
        source = os.environ if env is None else env

        def first(*names: str) -> str:
            return next((source[n].strip() for n in names if source.get(n, "").strip()), "")

        api_key = first("VOLC_ASR_API_KEY", "DOUBAO_ASR_API_KEY")
        app_id = first("VOLC_ASR_APP_ID", "VOLC_ASR_APPID", "DOUBAO_ASR_APP_ID", "DOUBAO_ASR_APPID")
        access_key = first(
            "VOLC_ASR_ACCESS_KEY", "VOLC_ASR_ACCESS_TOKEN", "VOLC_ASR_AT", "VOLC_ASR_TOKEN",
            "DOUBAO_ASR_ACCESS_KEY", "DOUBAO_ASR_ACCESS_TOKEN", "DOUBAO_ASR_AT", "DOUBAO_ASR_TOKEN",
        )
        if not api_key and not (app_id and access_key):
            return None
        url = first("VOLC_ASR_WS_URL") or _DEFAULT_URL
        try:
            parsed = urlsplit(url)
        except ValueError:
            raise TranscriptionError("Invalid VOLC_ASR_WS_URL") from None
        if parsed.scheme != "wss" or not parsed.hostname or parsed.username or parsed.password:
            raise TranscriptionError("VOLC_ASR_WS_URL must be a secure WebSocket URL without credentials")
        return cls(url, first("VOLC_ASR_RESOURCE_ID") or _DEFAULT_RESOURCE, api_key, app_id, access_key)

    def headers(self) -> dict[str, str]:
        request_id = str(uuid4())
        headers = {
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Request-Id": request_id,
            "X-Api-Connect-Id": request_id,
            "X-Api-Sequence": "-1",
        }
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        else:
            headers.update({"X-Api-App-Key": self.app_id, "X-Api-Access-Key": self.access_key})
        return headers


def _packet(payload: bytes, *, audio: bool = False, last: bool = False) -> bytes:
    compressed = bool(payload)
    body = gzip.compress(payload) if compressed else payload
    header = bytes((0x11, (0x20 if audio else 0x10) | (2 if last else 0),
                    (0 if audio else 0x10) | int(compressed), 0))
    return header + struct.pack(">I", len(body)) + body


def _parse_response(data: bytes) -> tuple[bool, dict[str, Any]]:
    if not isinstance(data, bytes) or len(data) < 8 or data[0] >> 4 != 1:
        raise TranscriptionError("Invalid ASR response header")
    offset = (data[0] & 15) * 4
    message_type, flags = data[1] >> 4, data[1] & 15
    serialization, compression = data[2] >> 4, data[2] & 15
    if offset < 4 or flags > 3 or compression not in (0, 1):
        raise TranscriptionError("Invalid ASR response header")
    if message_type == 15:
        if len(data) < offset + 8:
            raise TranscriptionError("Invalid ASR error response")
        code = struct.unpack_from(">I", data, offset)[0]
        raise TranscriptionError(f"Volc ASR service error (code {code})")
    if message_type != 9 or serialization != 1:
        raise TranscriptionError("Unsupported ASR response type")
    if flags & 1:
        offset += 4
    if len(data) < offset + 4:
        raise TranscriptionError("Truncated ASR response")
    size = struct.unpack_from(">I", data, offset)[0]
    body = data[offset + 4:]
    if len(body) != size or size > _MAX_RESPONSE_BYTES:
        raise TranscriptionError("Invalid ASR response size")
    try:
        if compression:
            with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:
                body = stream.read(_MAX_RESPONSE_BYTES + 1)
        if len(body) > _MAX_RESPONSE_BYTES:
            raise TranscriptionError("ASR response exceeds the size limit")
        payload = json.loads(body)
    except (OSError, EOFError, ValueError, zlib.error):
        raise TranscriptionError("Invalid ASR response payload") from None
    if not isinstance(payload, dict):
        raise TranscriptionError("Invalid ASR response payload")
    return bool(flags & 2), payload


def _transcript(payload: dict[str, Any], start: float, duration: float) -> dict[str, Any]:
    result = payload.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("text"), str):
        raise TranscriptionError("ASR final response has no transcript")
    text = result["text"]
    if len(text) > 60_000:
        raise TranscriptionError("ASR transcript exceeds the text limit")
    segments = []
    for item in result.get("utterances", []) or []:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            continue
        begin, end = item.get("start_time"), item.get("end_time")
        if not all(isinstance(t, (int, float)) and not isinstance(t, bool) and math.isfinite(t)
                   for t in (begin, end)):
            continue
        if not 0 <= begin <= min(end, duration * 1000) or end > duration * 1000 + 1000:
            continue
        segments.append({"start_seconds": round(start + begin / 1000, 3),
                         "end_seconds": round(start + min(end / 1000, duration), 3),
                         "text": item["text"]})
    return {"status": "transcribed", "text": text, "segments": segments,
            "start_seconds": start, "end_seconds": start + duration,
            "provider": "volc", "scope": "speech_only"}


async def transcribe_pcm(
    path: Path, *, config: VolcAsrConfig, start_seconds: float, duration: float,
    on_progress: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Send paced PCM concurrently with receiving; require a terminal server result."""
    async def exchange() -> dict[str, Any]:
        async with connect(config.url, additional_headers=config.headers(),
                           compression=None, open_timeout=15, close_timeout=5,
                           max_size=_MAX_RESPONSE_BYTES) as socket:
            await socket.send(_packet(json.dumps({
                "user": {"uid": "box-agent-read-video"},
                "audio": {"format": "pcm", "codec": "raw", "rate": 16000,
                          "bits": 16, "channel": 1},
                "request": {"model_name": "bigmodel", "enable_itn": True,
                            "enable_punc": True, "enable_ddc": True,
                            "enable_nonstream": True, "show_utterances": True,
                            "result_type": "full", "end_window_size": 800},
            }).encode()))

            async def send_audio() -> None:
                sent_bytes = 0
                next_progress = 10 * 32000
                with path.open("rb") as stream:
                    while chunk := stream.read(_CHUNK_BYTES):
                        check_tool_cancellation()
                        await socket.send(_packet(chunk, audio=True))
                        sent_bytes += len(chunk)
                        if on_progress is not None and sent_bytes >= next_progress:
                            on_progress(sent_bytes / 32000)
                            next_progress += 10 * 32000
                        await asyncio.sleep(_CHUNK_SECONDS)
                await socket.send(_packet(b"", audio=True, last=True))

            sender = asyncio.create_task(send_audio())

            async def receive() -> dict[str, Any]:
                while True:
                    check_tool_cancellation()
                    final, payload = _parse_response(await socket.recv())
                    if final:
                        # An early terminal response must not claim full audio coverage.
                        if not sender.done():
                            raise TranscriptionError("ASR ended before all audio was sent")
                        await sender
                        return _transcript(payload, start_seconds, duration)

            receiver = asyncio.create_task(receive())
            try:
                _, transcript = await asyncio.gather(sender, receiver)
                return transcript
            finally:
                sender.cancel()
                receiver.cancel()
                await asyncio.gather(sender, receiver, return_exceptions=True)

    try:
        # Desktop streaming ASR expects real-time pacing. Allow finalization time.
        return await asyncio.wait_for(exchange(), timeout=duration + _FINAL_TIMEOUT_SECONDS)
    except (TranscriptionError, ToolInvocationRejected, asyncio.CancelledError):
        raise
    except asyncio.TimeoutError:
        raise TranscriptionError("Volc ASR timed out before a final transcript") from None
    except Exception:
        raise TranscriptionError("Volc ASR connection failed; check desktop ASR configuration and connectivity") from None

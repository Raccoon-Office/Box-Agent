"""Cancel actual SDK request/response reads using an in-memory HTTP transport."""

import asyncio
import json

import httpx
import pytest

from box_agent.kernel.stream_controller import stream_with_activity
from box_agent.llm.openai_client import OpenAIClient
from box_agent.schema import Message


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["headers", "first_chunk", "next_chunk"])
@pytest.mark.parametrize("cleanup_error", [False, True])
async def test_cancel_interrupts_openai_http_wait_and_closes_response(phase, cleanup_error):
    started, closed, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    requests = 0

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            if phase == "next_chunk":
                chunk = {
                    "id": "response-1", "object": "chat.completion.chunk",
                    "created": 0, "model": "test-model",
                    "choices": [{"index": 0, "delta": {"content": "partial"},
                                 "finish_reason": None}],
                }
                yield f"data: {json.dumps(chunk)}\n\n".encode()
            started.set()
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()
            if cleanup_error:
                raise OSError("fixture response close failure")

    async def handler(request):
        nonlocal requests
        requests += 1
        if phase == "headers":
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                closed.set()
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Body())

    client = OpenAIClient(api_key="test", api_base="https://example.com/v1", model="test-model")
    await client.client.close()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client.client = client.client.with_options(http_client=http_client)

        async def collect():
            return [event async for event in stream_with_activity(
                client.generate_stream([Message(role="user", content="hello")]),
                stale_seconds=300, activity_interval_seconds=15,
                wait_cancelled=cancelled.wait,
            )]

        pending = asyncio.create_task(collect())
        try:
            await asyncio.wait_for(started.wait(), 2)
            cancelled.set()
            events = await asyncio.wait_for(asyncio.shield(pending), 1)
            assert closed.is_set(), "cancellation must release the HTTP response"
            assert requests == 1, "user cancellation must not retry the model request"
            assert [event.delta for event in events if event.type == "text"] == (
                ["partial"] if phase == "next_chunk" else []
            )
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

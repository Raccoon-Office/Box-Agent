"""Token-limit compatibility at the actual OpenAI SDK HTTP boundary."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from openai import AsyncOpenAI, BadRequestError

from box_agent.llm.openai_client import OpenAIClient
from box_agent.retry import RetryConfig
from box_agent.schema import Message


MODEL = "gpt-6-sol/azure_L/qwb"
TOKEN_ERROR = {
    "message": "Unsupported parameter: 'max_tokens' is not supported with this model. "
    "Use 'max_completion_tokens' instead.",
    "type": "invalid_request_error",
    "param": "max_tokens",
    "code": "unsupported_parameter",
}


@asynccontextmanager
async def wire_client(respond):
    requests = []

    def handle(request):
        payload = json.loads(request.content)
        requests.append(payload)
        rejection = respond(payload)
        if rejection is not None:
            return rejection
        choice = {"index": 0, "finish_reason": "stop"}
        body = {"id": "test", "created": 1, "model": payload["model"], "choices": [choice]}
        if payload.get("stream"):
            choice["delta"] = {"content": "OK"}
            body["object"] = "chat.completion.chunk"
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"},
                text="data: " + json.dumps(body) + "\n\ndata: [DONE]\n\n",
            )
        choice["message"] = {"role": "assistant", "content": "OK"}
        body["object"] = "chat.completion"
        return httpx.Response(200, json=body)

    client = OpenAIClient(
        api_key="test", api_base="https://inference.example/v1", model=MODEL,
        max_output_tokens=64000, retry_config=RetryConfig(enabled=False),
    )
    original = client.client
    client.client = AsyncOpenAI(
        api_key="test", base_url=client.api_base, max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    try:
        yield client, requests
    finally:
        await client.client.close()
        await original.close()


async def generate(client, stream):
    messages = [Message(role="user", content="hi")]
    if stream:
        events = [event async for event in client.generate_stream(messages, thinking_enabled=True)]
        assert events[-1].type == "finish"
        assert "".join(event.delta for event in events if event.type == "text") == "OK"
    else:
        assert (await client.generate(messages, thinking_enabled=True)).content == "OK"


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("model", [MODEL, "private-deployment"])
async def test_explicit_token_rejection_retries_with_same_budget_and_remembers_binding(stream, model):
    def respond(payload):
        if "max_tokens" in payload:
            return httpx.Response(400, json={"error": TOKEN_ERROR})

    async with wire_client(respond) as (client, requests):
        client.model = model
        client.set_ephemeral_max_output_tokens(80000)
        await generate(client, stream)
        assert len(requests) == 2
        assert requests[0]["max_tokens"] == 80000
        expected = dict(requests[0])
        expected["max_completion_tokens"] = expected.pop("max_tokens")
        assert requests[1] == expected

        await generate(client, not stream)
        assert len(requests) == 3
        assert requests[2]["max_completion_tokens"] == 64000
        assert "max_tokens" not in requests[2]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_token_fallback_works_without_sdk_raw_response_wrapper(stream):
    def respond(payload):
        if "max_tokens" in payload:
            return httpx.Response(400, json={"error": TOKEN_ERROR})

    async with wire_client(respond) as (client, requests):
        sdk = client.client
        client.client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=sdk.chat.completions.create),
        ))
        try:
            await generate(client, stream)
            assert len(requests) == 2
            assert requests[1]["max_completion_tokens"] == 64000
        finally:
            client.client = sdk


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_legacy_endpoint_keeps_max_tokens_without_retry(stream):
    async with wire_client(lambda payload: None) as (client, requests):
        await generate(client, stream)
        assert len(requests) == 1
        assert requests[0]["max_tokens"] == 64000
        assert "max_completion_tokens" not in requests[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("error", [
    {**TOKEN_ERROR, "param": "temperature"},
    {**TOKEN_ERROR, "code": "context_length_exceeded"},
    {**TOKEN_ERROR, "message": "Unsupported parameter: 'max_tokens'."},
    {"message": "Malformed request"},
])
async def test_other_bad_requests_are_not_rewritten_or_retried(stream, error):
    async with wire_client(lambda payload: httpx.Response(400, json={"error": error})) as (client, requests):
        with pytest.raises(BadRequestError):
            await generate(client, stream)
        assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_second_rejection_stops_after_one_parameter_retry(stream):
    async with wire_client(lambda payload: httpx.Response(400, json={"error": TOKEN_ERROR})) as (client, requests):
        client.retry_config = RetryConfig(max_retries=2, initial_delay=0, max_delay=0)
        with pytest.raises(BadRequestError):
            await generate(client, stream)
        assert len(requests) == 2
        assert "max_tokens" in requests[0]
        assert "max_tokens" not in requests[1]


@pytest.mark.asyncio
async def test_token_parameter_discovery_is_scoped_to_endpoint_and_model():
    def respond(payload):
        if payload["model"] == MODEL and "max_tokens" in payload:
            return httpx.Response(400, json={"error": TOKEN_ERROR})

    async with wire_client(respond) as (client, requests):
        await generate(client, False)
        client.model = "legacy-model"
        await generate(client, False)
        assert requests[-1]["max_tokens"] == 64000
        assert "max_completion_tokens" not in requests[-1]

        client.model = MODEL
        client.api_base = "https://another.example/v1"
        client.client.base_url = client.api_base
        before = len(requests)
        await generate(client, False)
        assert len(requests) == before + 2
        assert requests[before]["max_tokens"] == 64000

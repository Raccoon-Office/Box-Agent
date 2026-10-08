"""OpenAI-compatible provider cache usage parsing."""

import json

import httpx
import pytest

from box_agent.llm import OpenAIClient
from box_agent.llm.openai_client import _token_usage_from_openai
from box_agent.schema import Message


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_openai_cache_reads_are_reported_without_increasing_prompt_total(stream):
    usage = {
        "prompt_tokens": 1200,
        "completion_tokens": 30,
        "total_tokens": 1230,
        "prompt_tokens_details": {"cached_tokens": 896},
    }

    def handler(_request):
        if stream:
            chunks = [
                {
                    "id": "response-1", "object": "chat.completion.chunk",
                    "created": 0, "model": "test-model",
                    "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}],
                },
                {
                    "id": "response-1", "object": "chat.completion.chunk",
                    "created": 0, "model": "test-model", "choices": [], "usage": usage,
                },
            ]
            body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  text=body + "data: [DONE]\n\n")
        return httpx.Response(200, json={
            "id": "response-1", "object": "chat.completion", "created": 0,
            "model": "test-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": usage,
        })

    client = OpenAIClient(api_key="test", api_base="https://example.com/v1", model="test-model")
    await client.client.close()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client.client = client.client.with_options(http_client=http_client)
        messages = [Message(role="user", content="hello")]
        if stream:
            events = [event async for event in client.generate_stream(messages)]
            result_usage = events[-1].usage
        else:
            result_usage = (await client.generate(messages)).usage

    assert result_usage.prompt_tokens == 1200
    assert result_usage.total_tokens == 1230
    assert result_usage.input_tokens == 304
    assert result_usage.cache_read_input_tokens == 896
    assert result_usage.context_tokens == 1230
    assert result_usage.reported_usage()["cached_tokens"] == 896


@pytest.mark.parametrize("details", [None, {"cached_tokens": 1201}, {"cached_tokens": "896"}])
def test_openai_missing_or_invalid_cache_details_remain_unknown(details):
    provider_usage = {
        "prompt_tokens": 1200, "completion_tokens": 30, "total_tokens": 1230,
    }
    if details is not None:
        provider_usage["prompt_tokens_details"] = details
    usage = _token_usage_from_openai(provider_usage)

    assert usage.context_tokens == 1230
    assert "cached_tokens" not in usage.reported_usage()


def test_openai_explicit_zero_cache_read_is_reported():
    usage = _token_usage_from_openai({
        "prompt_tokens": 1200, "completion_tokens": 30, "total_tokens": 1230,
        "prompt_tokens_details": {"cached_tokens": 0},
    })

    assert usage.reported_usage()["cached_tokens"] == 0

"""Context-selected images retain their exact bytes through the SDK boundary."""

import base64
import io
import json

import httpx
from openai import AsyncOpenAI
from PIL import Image
import pytest

from box_agent.llm.image_payload import RequestBodyTooLargeError
from box_agent.llm.llm_wrapper import LLMClient, SessionBoundLLM
from box_agent.llm.openai_client import OpenAIClient
from box_agent.schema import LLMProvider, Message


def _message() -> Message:
    image = Image.new("RGB", (173, 99), "navy")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return Message(role="user", content=[
        {"type": "text", "text": "历史截图，请保留坐标。"},
        {"type": "input_image", "media_type": "image/png",
         "data": base64.b64encode(output.getvalue()).decode("ascii"),
         "width": 173, "height": 99, "preserve_image_bytes": True},
    ])


async def _client(limit, captured):
    async def handle(request):
        body = json.loads(request.content)
        captured.append((len(request.content), body))
        stream = body.get("stream", False)
        response = {
            "id": "test", "created": 1, "model": "vision",
            "object": "chat.completion.chunk" if stream else "chat.completion",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "delta" if stream else "message": {"role": "assistant", "content": "seen"}}],
        }
        if stream:
            return httpx.Response(200, text="data: " + json.dumps(response) + "\n\ndata: [DONE]\n\n",
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=response)

    client = OpenAIClient(api_key="test", api_base="https://example.test/v1", model="vision")
    await client.client.close()
    client.max_request_body_bytes = limit
    client.client = AsyncOpenAI(api_key="test", base_url=client.api_base, max_retries=0,
                               http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_preflight_matches_wire_without_consuming_override_or_changing_images(stream):
    captured = []
    client = await _client(100_000, captured)
    message = _message()
    original = message.model_dump()
    tools = [{"name": "probe", "description": "完整工具描述",
              "input_schema": {"type": "object", "properties": {}}}]
    client.set_ephemeral_max_output_tokens(123456)
    try:
        first = client.preflight_image_request([message], tools, stream=stream)
        assert first == client.preflight_image_request([message], tools, stream=stream)
        assert client._ephemeral_max_output_tokens == 123456
        assert captured == []
        assert first["fits"] and first["transforms"] == []
        if stream:
            _ = [event async for event in client.generate_stream([message], tools)]
        else:
            await client.generate([message], tools)
    finally:
        await client.client.close()
    size, body = captured[0]
    assert size == first["body_bytes"]
    assert body["max_tokens"] == 123456
    assert body["messages"][0]["content"][1] == {
        "type": "image_url", "image_url": {
            "url": "data:image/png;base64," + message.content[1]["data"]},
    }
    assert message.model_dump() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_selected_images_are_rejected_instead_of_reencoded_when_body_still_exceeds_limit(stream):
    captured = []
    client = await _client(300, captured)
    message = _message()
    try:
        metrics = client.preflight_image_request([message], stream=stream)
        assert not metrics["fits"]
        assert metrics["image_count"] == 1
        with pytest.raises(RequestBodyTooLargeError) as error:
            if stream:
                _ = [event async for event in client.generate_stream([message])]
            else:
                await client.generate([message])
        assert error.value.metrics["transforms"] == []
    finally:
        await client.client.close()
    assert captured == []


@pytest.mark.asyncio
async def test_session_bound_wrapper_exposes_preflight_and_token_fallback_reserve():
    llm = LLMClient(api_key="test", api_base="https://example.test/v1", model="vision",
                    provider=LLMProvider.OPENAI, max_request_body_bytes=100_000)
    try:
        bound = SessionBoundLLM(llm)
        first = bound.preflight_image_request([_message()])
        assert first["required_body_bytes"] > first["body_bytes"]
        llm._client._max_completion_token_bindings.add((llm.api_base, llm.model))
        retry = bound.preflight_image_request([_message()])
        assert retry["body_bytes"] == first["required_body_bytes"]
        assert retry["token_parameter_reserve_bytes"] == 0
    finally:
        await llm.aclose()

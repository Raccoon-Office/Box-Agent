"""Anthropic preflight and transport share exact UTF-8 request-body budgets."""

import base64
import io
import json

import anthropic
import httpx
from PIL import Image
import pytest

from box_agent.llm.anthropic_client import AnthropicClient
from box_agent.llm.image_payload import RequestBodyTooLargeError
from box_agent.llm.llm_wrapper import LLMClient, SessionBoundLLM
from box_agent.schema import LLMProvider, Message


def _messages():
    output = io.BytesIO()
    Image.new("RGB", (173, 99), "navy").save(output, format="PNG")
    return [Message(role="system", content="保留截图坐标。"),
            Message(role="user", content="Inspect this image"),
            Message(role="user", source="runtime", content=[
                {"type": "text", "text": "Image one | size=173x99"},
                {"type": "input_image", "media_type": "image/png",
                 "data": base64.b64encode(output.getvalue()).decode("ascii"),
                 "width": 173, "height": 99, "preserve_image_bytes": True},
            ])]


async def _client(limit, captured):
    async def handle(request):
        body = json.loads(request.content)
        captured.append((len(request.content), body))
        message = {"id": "msg_test", "type": "message", "role": "assistant",
                   "model": "claude-test", "content": [], "stop_reason": None,
                   "stop_sequence": None, "usage": {"input_tokens": 12, "output_tokens": 1}}
        if body.get("stream"):
            events = [
                {"type": "message_start", "message": message},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "seen"}},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                 "usage": {"output_tokens": 2}},
                {"type": "message_stop"},
            ]
            return httpx.Response(200, text="".join(
                f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events),
                headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={**message, "content": [{"type": "text", "text": "seen"}],
                                         "stop_reason": "end_turn"})

    client = AnthropicClient(api_key="test", api_base="https://example.test", model="claude-test",
                             max_output_tokens=16384, max_request_body_bytes=limit)
    await client.client.close()
    client.client = anthropic.AsyncAnthropic(
        api_key="test", base_url=client.api_base, max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("thinking", [False, True])
async def test_anthropic_preflight_equals_wire_and_preserves_pixels(stream, thinking):
    captured = []
    client = await _client(100_000, captured)
    messages = _messages()
    before = [message.model_dump() for message in messages]
    tools = [{"name": "probe", "description": "含完整工具描述", "input_schema": {"type": "object"}}]
    try:
        metrics = client.preflight_image_request(messages, tools, stream=stream, thinking_enabled=thinking)
        assert metrics == client.preflight_image_request(messages, tools, stream=stream, thinking_enabled=thinking)
        assert captured == [] and metrics["fits"] and metrics["image_count"] == 1
        if stream:
            _ = [event async for event in client.generate_stream(messages, tools, thinking_enabled=thinking)]
        else:
            await client.generate(messages, tools, thinking_enabled=thinking)
    finally:
        await client.client.close()
    size, body = captured[0]
    assert size == metrics["body_bytes"]
    assert body["system"] == messages[0].content
    assert len(body["messages"]) == 1  # Adjacent users merge without losing the image overlay.
    image = body["messages"][0]["content"][-1]
    assert image["source"]["data"] == messages[-1].content[-1]["data"]
    assert image["source"]["media_type"] == "image/png"
    assert [message.model_dump() for message in messages] == before
    assert metrics["non_image_bytes"] < metrics["body_bytes"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_anthropic_oversized_body_stops_before_network_without_reencoding(stream):
    captured = []
    client = await _client(300, captured)
    try:
        metrics = client.preflight_image_request(_messages(), stream=stream)
        assert not metrics["fits"] and metrics["image_count"] == 1
        with pytest.raises(RequestBodyTooLargeError) as error:
            if stream:
                _ = [event async for event in client.generate_stream(_messages())]
            else:
                await client.generate(_messages())
        assert error.value.metrics["transforms"] == []
    finally:
        await client.client.close()
    assert captured == []


@pytest.mark.asyncio
async def test_wrapper_passes_anthropic_request_limit_to_model_views():
    llm = LLMClient(api_key="test", provider=LLMProvider.ANTHROPIC, model="claude-test",
                    max_request_body_bytes=400)
    try:
        assert llm._client.max_request_body_bytes == 400
        bound = SessionBoundLLM(llm.for_model("claude-other", max_output_tokens=8192))
        assert not bound.preflight_image_request(_messages())["fits"]
    finally:
        await llm.aclose()


@pytest.mark.parametrize("limit", [0, -1, True, "1000"])
def test_anthropic_body_limit_requires_positive_integer(limit):
    with pytest.raises(ValueError, match="positive integer"):
        AnthropicClient(api_key="test", max_request_body_bytes=limit)

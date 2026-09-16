"""Request budgets exercise the actual OpenAI SDK JSON wire representation."""

import base64
import io
import json
import random

import httpx
from openai import AsyncOpenAI
from PIL import Image
import pytest

from box_agent.llm.openai_client import OpenAIClient
from box_agent.llm.image_payload import RequestBodyTooLargeError, encode_view_image, prepare_image_request, request_body_bytes
from box_agent.schema import Message


def image_message(count=1, *, size=(1280, 1024), alpha=False):
    mode = "RGBA" if alpha else "RGB"
    rng = random.Random(11)
    image = Image.frombytes(mode, size, rng.randbytes(size[0] * size[1] * len(mode)))
    output = io.BytesIO()
    image.save(output, format="PNG")
    data = base64.b64encode(output.getvalue()).decode("ascii")
    return Message(role="user", content=[{"type": "text", "text": "检查图片。"}, *[
        {"type": "input_image", "media_type": "image/png", "data": data,
         "width": size[0], "height": size[1]} for _ in range(count)
    ]])


def image_blocks(body):
    return [block for message in body["messages"] if isinstance(message.get("content"), list)
            for block in message["content"] if block.get("type") == "image_url"]


async def recording_client(limit, requests):
    async def handle(request):
        body = json.loads(request.content)
        requests.append((len(request.content), body))
        if body.get("stream"):
            chunk = {"id": "test", "created": 1, "object": "chat.completion.chunk", "model": "vision",
                     "choices": [{"index": 0, "delta": {"content": "Checked"}, "finish_reason": "stop"}]}
            return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n",
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"id": "test", "created": 1, "object": "chat.completion",
            "model": "vision", "choices": [{"index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": "Checked"}}]})

    client = OpenAIClient(api_key="test", api_base="https://example.test/v1", model="vision")
    await client.client.close()
    client.max_request_body_bytes = limit
    client.client = AsyncOpenAI(api_key="test", base_url=client.api_base, max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_large_png_is_transcoded_without_resizing_and_wire_fits(stream):
    message = image_message()
    before = message.model_dump()
    requests = []
    client = await recording_client(4_000_000, requests)
    try:
        if stream:
            _ = [event async for event in client.generate_stream([message])]
        else:
            await client.generate([message])
    finally:
        await client.client.close()
    assert len(requests) == 1
    size, body = requests[0]
    assert size <= 3_800_000
    url = image_blocks(body)[0]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
    with Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))) as decoded:
        assert decoded.size == (1280, 1024)
    assert message.model_dump() == before


@pytest.mark.asyncio
async def test_under_budget_request_keeps_original_image_bytes():
    message = image_message(size=(50, 50))
    requests = []
    client = await recording_client(100_000, requests)
    try:
        await client.generate([message])
    finally:
        await client.client.close()
    assert image_blocks(requests[0][1])[0]["image_url"]["url"] == (
        "data:image/png;base64," + message.content[1]["data"])


@pytest.mark.asyncio
async def test_multiple_images_over_budget_are_rejected_before_network_without_resize():
    requests = []
    client = await recording_client(2_000_000, requests)
    try:
        with pytest.raises(Exception, match="REQUEST_BODY_TOO_LARGE.*fewer images"):
            await client.generate([image_message(2)])
    finally:
        await client.client.close()
    assert requests == []


@pytest.mark.asyncio
async def test_transparent_image_is_not_flattened_to_jpeg():
    requests = []
    client = await recording_client(10_000, requests)
    try:
        with pytest.raises(Exception, match="REQUEST_BODY_TOO_LARGE"):
            await client.generate([image_message(size=(100, 100), alpha=True)])
    finally:
        await client.client.close()
    assert requests == []


@pytest.mark.asyncio
async def test_single_image_can_resize_but_does_not_cross_resolution_floor():
    requests = []
    client = await recording_client(2_800_000, requests)
    try:
        await client.generate([image_message(size=(1568, 1568))])
    finally:
        await client.client.close()
    size, body = requests[0]
    assert size <= 2_660_000
    url = image_blocks(body)[0]["image_url"]["url"]
    with Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))) as decoded:
        assert 1024 <= max(decoded.size) < 1568


@pytest.mark.asyncio
async def test_text_and_tool_definitions_count_towards_request_body_budget():
    requests = []
    client = await recording_client(1000, requests)
    try:
        with pytest.raises(Exception, match="REQUEST_BODY_TOO_LARGE"):
            await client.generate([Message(role="user", content="字" * 200)], tools=[{
                "name": "demo", "description": "字" * 200,
                "input_schema": {"type": "object", "properties": {}},
            }])
    finally:
        await client.client.close()
    assert requests == []


@pytest.mark.asyncio
async def test_custom_endpoint_sends_over_ten_mb_without_implicit_transcoding():
    message = image_message(3)
    requests = []
    client = await recording_client(None, requests)
    try:
        await client.generate([message])
    finally:
        await client.client.close()
    size, body = requests[0]
    assert size > 10_000_000
    assert all(block["image_url"]["url"] == "data:image/png;base64," + message.content[1]["data"]
               for block in image_blocks(body))


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint,expected", [
    ("https://xiaohuanxiong.com/api/web/llm/v2", 10_000_000),
    ("https://code-stage.xiaohuanxiong.com/api/web/llm/v2", 10_000_000),
    ("http://192.0.2.208:8000/v1", None),
    ("https://xiaohuanxiong.com.example.test/v1", None),
])
async def test_default_body_limit_belongs_to_endpoint_not_model_name(endpoint, expected):
    from box_agent.llm.llm_wrapper import LLMClient
    from box_agent.schema import LLMProvider

    client = LLMClient(api_key="test", provider=LLMProvider.OPENAI, api_base=endpoint,
                       model="sn-sensenova-6-8-flash-lite")
    try:
        assert client._client.max_request_body_bytes == expected
        assert client.for_model("another-model")._client.max_request_body_bytes == expected
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_explicit_custom_endpoint_limit_is_retained_by_model_views():
    from box_agent.llm.llm_wrapper import LLMClient
    from box_agent.schema import LLMProvider

    client = LLMClient(api_key="test", provider=LLMProvider.OPENAI,
        api_base="http://192.0.2.208:8000/v1", max_request_body_bytes=32_000_000)
    try:
        assert client.for_model("sn-any")._client.max_request_body_bytes == 32_000_000
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_byte_measurement_matches_sdk_extra_body_merge_and_unicode():
    requests = []
    client = await recording_client(None, requests)
    params = {
        "model": "vision", "messages": [{"role": "user", "content": "被覆盖"}],
        "tools": [{"type": "function", "function": {"name": "demo", "description": "检查🔎"}}],
        "extra_body": {"messages": [{"role": "user", "content": "实际发送🖼️"}], "custom": True},
        "extra_headers": {"x-test": "header is not body"}, "timeout": 1,
    }
    try:
        await client.client.chat.completions.create(**params)
    finally:
        await client.client.close()
    assert request_body_bytes(params) == requests[0][0]
    assert requests[0][1]["messages"][0]["content"] == "实际发送🖼️"


def test_single_image_rejection_stops_at_quality_and_resolution_floor():
    message = image_message(size=(1280, 1024))
    original_url = "data:image/png;base64," + message.content[1]["data"]
    params = {"model": "vision", "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": original_url}},
    ]}]}
    with pytest.raises(RequestBodyTooLargeError) as rejected:
        prepare_image_request(params, 1000)
    error = rejected.value
    assert "high-resolution crop" in error.guidance
    assert error.metrics["transforms"]
    assert min(max(item["width"], item["height"]) for item in error.metrics["transforms"]) == 1024
    assert all(item["quality"] == 90 for item in error.metrics["transforms"])
    assert params["messages"][0]["content"][0]["image_url"]["url"] == original_url


@pytest.mark.parametrize("mode,seed,channels,format", [("CMYK", 17, 4, "JPEG"), ("I;16", 19, 2, "PNG")])
def test_non_rgb_images_are_not_converted_to_wrong_colors_or_clipped_white(mode, seed, channels, format):
    raw = random.Random(seed).randbytes(1200 * 1000 * channels)
    image = Image.frombytes(mode, (1200, 1000), raw)
    output = io.BytesIO()
    image.save(output, format=format, **({"quality": 100} if format == "JPEG" else {}))
    mime = "image/jpeg" if format == "JPEG" else "image/png"
    url = f"data:{mime};base64," + base64.b64encode(output.getvalue()).decode()
    params = {"model": "vision", "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": url}} for _ in range(4)
    ]}]}
    with pytest.raises(RequestBodyTooLargeError) as rejected:
        prepare_image_request(params, 10_000_000)
    assert rejected.value.metrics["transforms"] == []
    assert all(block["image_url"]["url"] == url for block in image_blocks(params))


@pytest.mark.parametrize("mode,pixel", [("1", 1), ("LA", (128, 255))])
def test_opaque_grayscale_conversion_keeps_grayscale_color_space(mode, pixel):
    source = Image.new(mode, (100, 100), pixel)
    encoded, _, _ = encode_view_image(source, "image/jpeg", max_long_edge=1024)
    with Image.open(io.BytesIO(encoded)) as image:
        assert image.mode == "L"
        assert abs(image.getpixel((50, 50)) - (255 if mode == "1" else 128)) <= 1

"""Verify byte limits at the actual SDK boundary, including native tool recovery."""

import base64
import hashlib
import io
import json
import random

import httpx
from openai import AsyncOpenAI
from PIL import Image
import pytest

from box_agent.llm.openai_client import OpenAIClient
from box_agent.schema import Message


def image_message(count=1, *, size=(1280, 1024), alpha=False):
    mode = "RGBA" if alpha else "RGB"
    image = Image.frombytes(mode, size, random.Random(11).randbytes(size[0] * size[1] * len(mode)))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return Message(role="user", content=[{"type": "text", "text": "检查图片。"}, *[
        {"type": "input_image", "media_type": "image/png",
         "data": base64.b64encode(output.getvalue()).decode("ascii"),
         "width": size[0], "height": size[1]} for _ in range(count)]])


def image_blocks(body):
    return [block for message in body["messages"] if isinstance(message.get("content"), list)
            for block in message["content"] if block.get("type") == "image_url"]


def reply(content="Checked", *, stream=False, tools=None):
    delta = {"tool_calls": tools} if tools else {"content": content}
    finish = "tool_calls" if tools else "stop"
    chunk = {"id": "test", "created": 1, "model": "vision",
             "object": "chat.completion.chunk" if stream else "chat.completion",
             "choices": [{"index": 0, "finish_reason": finish,
                          "delta" if stream else "message": delta if stream else {"role": "assistant", **delta}}]}
    return (httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n",
                           headers={"content-type": "text/event-stream"}) if stream
            else httpx.Response(200, json=chunk))


async def recording_client(limit, requests):
    async def handle(request):
        body = json.loads(request.content)
        requests.append((len(request.content), body))
        return reply(stream=body.get("stream", False))

    client = OpenAIClient(api_key="test", api_base="https://example.test/v1", model="vision")
    await client.client.close()
    client.max_request_body_bytes = limit
    client.client = AsyncOpenAI(api_key="test", base_url=client.api_base, max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_oversized_png_fits_wire_without_resizing_or_mutating_input(stream):
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
    size, body = requests[0]
    assert size <= 3_800_000
    url = image_blocks(body)[0]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
    with Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))) as decoded:
        assert decoded.size == (1280, 1024)
    assert message.model_dump() == before


@pytest.mark.asyncio
async def test_multiple_oversized_images_fail_before_sending_instead_of_losing_pixels():
    requests = []
    client = await recording_client(2_000_000, requests)
    try:
        with pytest.raises(Exception, match="REQUEST_BODY_TOO_LARGE.*fewer images"):
            await client.generate([image_message(2)])
    finally:
        await client.client.close()
    assert requests == []


@pytest.mark.asyncio
async def test_under_budget_keeps_bytes_and_only_hosted_endpoint_defaults_to_10mb():
    for address, expected in [("https://api.xiaohuanxiong.com/v1", 10_000_000),
                              ("http://192.0.2.208/v1", None)]:
        client = OpenAIClient(api_key="test", api_base=address, model="sn-sensenova-6-8-flash-lite")
        try:
            assert client.max_request_body_bytes == expected
        finally:
            await client.client.close()
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
@pytest.mark.parametrize("limit, alpha", [(10_000, True), (1_000, False)])
async def test_detail_floor_or_transparency_cannot_be_discarded_to_fit(limit, alpha):
    requests = []
    client = await recording_client(limit, requests)
    try:
        with pytest.raises(Exception, match="REQUEST_BODY_TOO_LARGE"):
            await client.generate([image_message(size=(100, 100), alpha=alpha)])
    finally:
        await client.client.close()
    assert requests == []


@pytest.mark.asyncio
async def test_single_oversized_image_can_resize_above_detail_floor():
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
async def test_complete_utf8_body_includes_text_and_tools():
    from box_agent.llm.image_payload import request_body_bytes
    requests = []
    client = await recording_client(30_000, requests)
    messages = [Message(role="user", content="中" * 2_000)]
    tools = [{"name": "describe", "description": "中" * 4_000,
              "input_schema": {"type": "object", "properties": {}}}]
    try:
        await client.generate(messages, tools=tools)
        size, body = requests[0]
        assert size == request_body_bytes(body)
        client.max_request_body_bytes = 12_000
        with pytest.raises(Exception, match="REQUEST_BODY_TOO_LARGE.*non-image"):
            await client.generate(messages, tools=tools)
    finally:
        await client.client.close()
    assert len(requests) == 1


def test_main_lite_config_limits_remain_separate(tmp_path, monkeypatch):
    from box_agent.config import Config, LLMConfig
    monkeypatch.delenv("BOX_AGENT_HOME", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text("api_key: test\nmax_request_body_bytes: 32000000\nlite_llm:\n  api_base: https://other.test/v1\n  api_key: test\n  model: lite\n")
    config = Config.from_yaml(path)
    assert config.llm.max_request_body_bytes == 32_000_000
    assert config.lite_llm.max_request_body_bytes is None
    for invalid in [0, -1, True, "10000000"]:
        with pytest.raises(ValueError):
            LLMConfig(max_request_body_bytes=invalid)


@pytest.mark.asyncio
@pytest.mark.parametrize("additional_batch", [False, True])
async def test_native_batches_drop_oldest_images_and_never_replay_executed_tools(tmp_path, additional_batch):
    from box_agent.agent import Agent
    from box_agent.events import ErrorEvent
    from box_agent.llm.llm_wrapper import LLMClient
    from box_agent.schema import LLMProvider
    from box_agent.session_log import SessionLog
    from box_agent.tools.image_inspection_tool import ImageInspectionTool

    raw = base64.b64decode(image_message(size=(100, 100), alpha=True).content[1]["data"])
    paths = [f"page_{i}.png" for i in range(1, 9)]
    for path in paths:
        (tmp_path / path).write_bytes(raw)
    requests, executed = [], []

    class RecordingInspectionTool(ImageInspectionTool):
        async def execute(self, image_paths, instruction, strategy=None):
            executed.append(list(image_paths))
            return await super().execute(image_paths, instruction, strategy)

    async def handle(request):
        body = json.loads(request.content)
        if not body.get("stream"):
            return reply('{"continue": false}')
        requests.append((len(request.content), body))
        next_paths = paths if len(requests) == 1 else paths[:2] if additional_batch and len(requests) == 2 else []
        calls = [{"index": i, "id": f"inspect-{len(requests)}-{i}", "type": "function",
                  "function": {"name": "inspect_images", "arguments": json.dumps({
                      "image_paths": next_paths[start:start + 4], "instruction": "Inspect all pages.",
                      "strategy": "native"})}} for i, start in enumerate(range(0, len(next_paths), 4))]
        return reply("Checked all supplied pages.", stream=True, tools=calls)

    llm = LLMClient(api_key="test", provider=LLMProvider.OPENAI,
                   api_base="https://example.test/v1", model="vision", max_request_body_bytes=160_000)
    llm.capabilities = {"image_input": True}
    await llm.aclose()
    llm._client.client = AsyncOpenAI(api_key="test", base_url=llm.api_base, max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    log = SessionLog.create(tmp_path / "sessions", session_id="batch-test", cwd=tmp_path)
    agent = Agent(llm_client=llm, system_prompt="Inspect all supplied pages.",
                  tools=[RecordingInspectionTool(llm, workspace_dir=str(tmp_path), native_supported=True)],
                  workspace_dir=str(tmp_path), max_steps=5, deferred_mcp_loading_enabled=False,
                  session_log=log)
    agent.add_user_message("Inspect all eight pages.")
    try:
        events = [event async for event in agent.run_events()]
    finally:
        await llm.aclose()
        log.close()
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert errors and all(event.error_code == "IMAGE_NOT_DELIVERED" and not event.is_fatal for event in errors)
    assert [len(image_blocks(body)) for _, body in requests] == ([0, 2, 2] if additional_batch else [0, 2])
    assert "was not delivered" in str(requests[1][1]["messages"])
    assert "provider_body_limit" in str(requests[1][1]["messages"])
    assert "Do not assume its contents were seen" in str(requests[1][1]["messages"])
    assert all(size <= 152_000 for size, _ in requests)
    assert executed == ([paths[:4], paths[4:], paths[:2]] if additional_batch else [paths[:4], paths[4:]])
    refs = [ref for event in log.events for ref in event["data"].get("images", [])]
    manifests = [event["data"]["imageContext"] for event in log.events
                 if event["type"] == "request/context" and "imageContext" in event["data"]]
    first_selection = next(item for item in manifests if item.get("evicted"))
    assert [item["imageId"] for item in first_selection["evicted"]] == [ref["imageId"] for ref in refs[:6]]
    assert [item["imageId"] for item in first_selection["selected"]] == [ref["imageId"] for ref in refs[6:8]]
    for _, body in requests:
        for block in image_blocks(body):
            encoded = block["image_url"]["url"].split(",", 1)[1]
            sent = base64.b64decode(encoded)
            digest = hashlib.sha256(sent).hexdigest()
            matching = [ref for ref in refs if ref["sha256"] == digest]
            assert matching
            assert (log.path.parent / matching[0]["contentRef"]).read_bytes() == sent
            with Image.open(io.BytesIO(sent)) as decoded:
                assert decoded.size == (matching[0]["width"], matching[0]["height"]) == (100, 100)
    assert all((tmp_path / path).read_bytes() == raw for path in paths)


@pytest.mark.asyncio
async def test_proxy_rejection_is_actionable_and_remote_413_is_not_retried(tmp_path):
    from box_agent.llm.image_payload import RequestBodyTooLargeError
    from box_agent.llm.error_messages import classify_llm_error, is_retryable_llm_error
    from box_agent.tools.image_inspection_tool import ImageInspectionTool

    class RejectingVision:
        async def generate(self, **kwargs):
            raise RequestBodyTooLargeError(body_bytes=2000, budget_bytes=1500,
                                           image_count=2, non_image_bytes=200)

    raw = base64.b64decode(image_message(size=(10, 10)).content[1]["data"])
    for path in ["one.png", "two.png"]:
        (tmp_path / path).write_bytes(raw)
    result = await ImageInspectionTool(RejectingVision(), workspace_dir=str(tmp_path)).execute(
        ["one.png", "two.png"], "Inspect both")
    assert not result.success
    assert result.raw_output["code"] == "REQUEST_BODY_TOO_LARGE"
    assert result.raw_output["request_sent"] is False
    assert "fewer images" in result.error
    error = httpx.HTTPStatusError("request body exceeds 10MB", request=httpx.Request("POST", "https://x"),
                                  response=httpx.Response(413))
    assert classify_llm_error(error).category == "request_body_too_large"
    assert not is_retryable_llm_error(error)

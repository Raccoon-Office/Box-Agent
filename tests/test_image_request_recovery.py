"""A rejected native image request resumes through real agent/tool turns."""

import base64
import json

import httpx
from openai import AsyncOpenAI
import pytest

from box_agent.agent import Agent
from box_agent.events import ErrorEvent
from box_agent.llm.llm_wrapper import LLMClient
from box_agent.llm.image_payload import RequestBodyTooLargeError
from box_agent.schema import LLMProvider
from box_agent.tools.image_inspection_tool import ImageInspectionTool
from tests.test_image_request_budget import image_message, image_blocks


@pytest.mark.asyncio
@pytest.mark.parametrize("repeat_bad_call", [False, True])
@pytest.mark.parametrize("image_count", [4, 8])
async def test_native_size_rejection_retries_explicit_batches_with_bounded_recovery(tmp_path, repeat_bad_call, image_count):
    raw = base64.b64decode(image_message(size=(100, 100), alpha=True).content[1]["data"])
    paths = [f"page_{i}.png" for i in range(1, image_count + 1)]
    for path in paths:
        (tmp_path / path).write_bytes(raw)
    requests = []
    verified = []

    async def handle(request):
        body = json.loads(request.content)
        if not body.get("stream"):
            return httpx.Response(200, json={
                "id": "judge", "created": 1, "model": "vision", "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": '{"continue": false}'},
                             "finish_reason": "stop"}],
            })
        requests.append((len(request.content), body))
        images = image_blocks(body)
        if images:
            text = str(body["messages"][-1]["content"])
            verified.extend(path for path in paths if path in text)
        if repeat_bad_call or len(requests) == 1:
            next_paths = paths
        else:
            next_paths = [path for path in paths if path not in verified][:2]
        delta = {"content": "Checked all supplied pages."}
        if next_paths:
            # The eight-image case violates Skill sequencing with two calls
            # of four in one response: the actual next request contains both.
            delta = {"tool_calls": [{"index": index, "id": f"inspect-{len(requests)}-{index}", "type": "function",
                "function": {"name": "inspect_images", "arguments": json.dumps({
                    "image_paths": next_paths[start:start + 4], "instruction": "Check small text without losing alpha.",
                    "strategy": "native"})}}
                for index, start in enumerate(range(0, len(next_paths), 4))]}
        chunk = {"id": "test", "created": 1, "model": "vision", "object": "chat.completion.chunk",
                 "choices": [{"index": 0, "delta": delta,
                              "finish_reason": "tool_calls" if next_paths else "stop"}]}
        return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n",
                              headers={"content-type": "text/event-stream"})

    llm = LLMClient(api_key="test", provider=LLMProvider.OPENAI,
        api_base="https://example.test/v1", model="vision", max_request_body_bytes=160_000)
    llm.capabilities = {"image_input": True}
    await llm.aclose()
    llm._client.client = AsyncOpenAI(api_key="test", base_url=llm.api_base, max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    tool = ImageInspectionTool(llm, workspace_dir=str(tmp_path), native_supported=True)
    agent = Agent(llm_client=llm, tools=[tool], system_prompt="Inspect all supplied pages.",
                  workspace_dir=str(tmp_path), max_steps=20, deferred_mcp_loading_enabled=False)
    agent.add_user_message(f"Inspect all {image_count} page images and report their quality.")
    try:
        events = [event async for event in agent.run_events()]
    finally:
        await llm.aclose()

    assert len(requests) > 1, "Local rejection must let the model choose a smaller batch"
    recovery = requests[1][1]["messages"]
    assert "REQUEST_BODY_TOO_LARGE" in str(recovery)
    assert "not been inspected" in str(recovery)
    assert image_blocks(requests[1][1]) == []
    assert all(size <= 152_000 for size, _ in requests)
    if repeat_bad_call:
        assert any(isinstance(event, ErrorEvent) for event in events)
        assert len(requests) <= 4
        assert verified == []
    else:
        assert not any(isinstance(event, ErrorEvent) for event in events), (
            [event.message for event in events if isinstance(event, ErrorEvent)],
            [len(image_blocks(body)) for _, body in requests], verified)
        assert sorted(verified) == paths
        assert [len(image_blocks(body)) for _, body in requests] == [0, 0] + [2] * (image_count // 2)


@pytest.mark.asyncio
async def test_proxy_exposes_actionable_size_rejection_as_tool_result(tmp_path):
    class RejectingVision:
        async def generate(self, **kwargs):
            raise RequestBodyTooLargeError(body_bytes=2000, budget_bytes=1500,
                                           image_count=2, non_image_bytes=200)

    raw = base64.b64decode(image_message(size=(10, 10)).content[1]["data"])
    for path in ("one.png", "two.png"):
        (tmp_path / path).write_bytes(raw)
    result = await ImageInspectionTool(RejectingVision(), workspace_dir=str(tmp_path)).execute(
        ["one.png", "two.png"], "Inspect both")
    assert not result.success
    assert result.raw_output["code"] == "REQUEST_BODY_TOO_LARGE"
    assert result.raw_output["request_sent"] is False
    assert "fewer images" in result.error


def test_remote_413_has_distinct_category_and_does_not_retry_unchanged_request():
    from box_agent.llm.error_messages import classify_llm_error, is_retryable_llm_error
    error = httpx.HTTPStatusError("request body exceeds 10MB", request=httpx.Request("POST", "https://x"),
                                  response=httpx.Response(413))
    assert classify_llm_error(error).category == "request_body_too_large"
    assert not is_retryable_llm_error(error)
    assert not is_retryable_llm_error(RuntimeError("request body exceeds 10MB"))

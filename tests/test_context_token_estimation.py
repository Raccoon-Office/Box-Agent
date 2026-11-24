"""Provider usage and local counter contracts for context budgeting."""

import json
import math
from types import SimpleNamespace

import httpx
import pytest

from box_agent.kernel.context_engine import (
    ContextTokenCalibration,
    _estimate_context_from_latest_response,
    _fallback_context_estimate,
    capture_request_usage,
    reported_input_tokens,
)
from box_agent.llm import AnthropicClient, OpenAIClient
from box_agent.llm.token_estimator import estimate_text_tokens
from box_agent.schema import Message, TokenUsage


@pytest.mark.parametrize(("text", "expected"), [
    ("The quick brown fox jumps over the lazy dog. Context compaction summarizes earlier turns.", 27),
    ("帮我逆向一下 claude code 的上下文压缩和上下文组装是怎么做的，需要具体一些，有一些示例", 41),
    ("function QY(e,n){let r=e-13000,g=n.testPctOverride;if(g>0&&g<=100)return "
     "Math.min(Math.floor(e*(g/100)),r);return r}", 72),
    ('{"type":"tool_result","tool_use_id":"toolu_01ABC","content":[1,2,3],"ok":true}', 47),
    ("1234567890 2026-11-24 167000 980000 0x108a34000", 23),
    ("😀" * 20, 37),
    ("漢" * 100, 107),
    ("あ" * 100, 77),
    ("한" * 100, 131),
    ("=" * 100, 16),
    ("", 0),
])
def test_local_estimator_matches_character_class_samples(text, expected):
    assert estimate_text_tokens(text) == expected


def _llm(model):
    return SimpleNamespace(model=model, provider="openai", api_base="https://example.test/v1")


def _tool(name, description):
    return SimpleNamespace(name=name, to_openai_schema=lambda: {"name": name, "description": description})


def test_same_route_usage_charges_only_changed_and_new_input():
    route = _llm("primary")
    unchanged = _tool("read", "Read a page")
    original_tool = _tool("search", "Search")
    changed_tool = _tool("search", "Search with scope")
    original = [Message(role="system", content="Read safely."),
                Message(role="user", content="较长的旧消息。" * 1_000)]
    changed = Message(role="system", content="Read safely and verify sources.")
    recent = Message(role="user", content="Show the results.")
    tools = {tool.name: tool for tool in [unchanged, changed_tool]}
    usage = TokenUsage(prompt_tokens=100, completion_tokens=10, total_tokens=110)
    usage._request_snapshot = capture_request_usage(original, len(original), [unchanged, original_tool], route)
    reply = Message(role="assistant", content="Working on it.", usage=usage)
    messages = [changed, original[1], reply, recent]

    estimate, source = _estimate_context_from_latest_response(messages, tools, llm=route)
    assert source == "usage"
    assert estimate == 110 + _fallback_context_estimate([changed, recent], {changed_tool.name: changed_tool})
    assert estimate < _fallback_context_estimate(messages, tools)

    estimate, source = _estimate_context_from_latest_response(messages, tools, llm=_llm("secondary"))
    assert (estimate, source) == (_fallback_context_estimate(messages, tools), "fallback")


def test_snapshotless_usage_keeps_anchor_and_removes_request_only_input():
    history = [Message(role="system", content="读历史记录。" * 1_000)]
    response = Message(role="assistant", content="Done.", request_only_input_tokens=150,
                       usage=TokenUsage(total_tokens=250))
    recent = Message(role="user", content="Continue.")
    assert _estimate_context_from_latest_response([*history, response, recent], None) == (
        100 + _fallback_context_estimate([recent], None), "usage",
    )


def test_total_only_usage_still_includes_assistant_tokens_after_replay(tmp_path):
    from box_agent.session_log import SessionLog

    route = _llm("primary")
    request = Message(role="user", content="Initial request.")
    usage = TokenUsage(total_tokens=100)
    usage._request_snapshot = capture_request_usage([request], 1, [], route)
    messages = [request, Message(role="assistant", content="Here is the result.", usage=usage)]
    assert _estimate_context_from_latest_response(messages, None, llm=route) == (100, "usage")

    log = SessionLog.create(tmp_path, session_id="total-only", cwd=tmp_path)
    log.append_unlogged_messages(messages, turn=1, step=1)
    try:
        assert _estimate_context_from_latest_response(log.replay().messages, None, llm=route) == (100, "usage")
    finally:
        log.close()


@pytest.mark.parametrize(("actual", "ratio"), [(10, 0.5), (150, 1.5), (260, 2.0)])
def test_calibration_uses_real_inputs_per_route_and_scales_only_delta(actual, ratio):
    route = _llm("primary")
    other = _llm("secondary")
    calibration = ContextTokenCalibration()
    calibration.record(route, actual, 100)
    calibration.record(route, 0, 100)
    assert calibration.ratio(route) == ratio
    assert calibration.ratio(other) == ContextTokenCalibration().ratio(route) == 1.0

    initial = Message(role="user", content="Initial request.")
    usage = TokenUsage(prompt_tokens=actual, completion_tokens=10, total_tokens=actual + 10)
    usage._request_snapshot = capture_request_usage([initial], 1, [], route)
    response = Message(role="assistant", content="Here is the result.", usage=usage)
    delta = Message(role="user", content="漢" * 100)
    local_delta = _fallback_context_estimate([delta], None)

    estimate, source = _estimate_context_from_latest_response(
        [initial, response, delta], None, llm=route, calibration=calibration,
    )
    assert source == "usage"
    assert estimate == actual + 10 + math.ceil(local_delta * ratio)
    assert _estimate_context_from_latest_response([initial], None, llm=route, calibration=calibration) == (
        _fallback_context_estimate([initial], None), "fallback",
    )


@pytest.mark.asyncio
async def test_main_request_records_usage_against_what_was_sent():
    from box_agent.context_input import DefaultContextEngine
    from box_agent.runtime import run_agent_loop
    from box_agent.schema import StreamEvent

    class Provider:
        model = "test-model"

        async def generate_stream(self, messages, tools, **_):
            local_input = _fallback_context_estimate(messages, {tool.name: tool for tool in tools}, llm=self)
            yield StreamEvent(type="text", delta="done")
            yield StreamEvent(type="finish", finish_reason="stop", usage=TokenUsage(
                prompt_tokens=local_input * 3, completion_tokens=1, total_tokens=local_input * 3 + 1,
            ))

    llm = Provider()
    calibration = ContextTokenCalibration()
    messages = [Message(role="system", content="system"), Message(role="user", content="request")]
    context = DefaultContextEngine(llm=llm, token_calibration=calibration)
    _ = [event async for event in run_agent_loop(llm=llm, messages=messages, tools={}, max_steps=1,
                                                 context_engine=context, token_limit=10_000)]
    assert messages[-1].usage._request_snapshot is not None
    assert calibration.ratio(llm) == 2.0


@pytest.mark.asyncio
async def test_anthropic_context_usage_includes_cache_read_and_creation_on_wire():
    def handler(_request):
        return httpx.Response(200, json={
            "id": "test", "type": "message", "role": "assistant", "model": "test-model",
            "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 100, "output_tokens": 10,
                      "cache_creation_input_tokens": 20, "cache_read_input_tokens": 30},
        })

    client = AnthropicClient(api_key="test", api_base="https://example.test", model="test-model")
    await client.client.close()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client.client = client.client.with_options(http_client=http_client)
        response = await client.generate([Message(role="user", content="hello")])

    assert response.usage.context_tokens == 160
    assert reported_input_tokens(response.usage) == 150


@pytest.mark.asyncio
@pytest.mark.parametrize("usage_fields", [
    None,
    {"total_tokens": 1_230},
    {"prompt_tokens": 1_200, "completion_tokens": 30, "total_tokens": 1_230,
     "prompt_tokens_details": {"cached_tokens": 896}},
])
async def test_openai_stream_usage_anchors_cached_and_total_only_responses(usage_fields):
    def handler(request):
        assert json.loads(request.content)["stream_options"] == {"include_usage": True}
        base = {"id": "test", "object": "chat.completion.chunk", "created": 0, "model": "test-model"}
        chunks = [{**base, "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]}]
        if usage_fields is not None:
            chunks.append({**base, "choices": [], "usage": usage_fields})
        body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body + "data: [DONE]\n\n")

    client = OpenAIClient(api_key="test", api_base="https://example.test/v1", model="test-model")
    await client.client.close()
    initial = Message(role="user", content="hello")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client.client = client.client.with_options(http_client=http_client)
        events = [event async for event in client.generate_stream([initial])]

    usage = events[-1].usage
    reply = Message(role="assistant", content="ok", usage=usage)
    if usage_fields is None:
        assert usage is None
        assert _estimate_context_from_latest_response([initial, reply], None, llm=client)[1] == "fallback"
    else:
        usage._request_snapshot = capture_request_usage([initial], 1, [], client)
        assert _estimate_context_from_latest_response([initial, reply], None, llm=client) == (1_230, "usage")
        assert reported_input_tokens(usage) == usage_fields.get("prompt_tokens", 0)

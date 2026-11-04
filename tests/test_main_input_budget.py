"""Main requests compact or stop locally before exceeding their input budget."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import AsyncOpenAI

from box_agent.acp import BoxACPAgent
from box_agent.config import derive_context_token_limit
from box_agent.events import DoneEvent, ErrorEvent, StopReason, SummarizationEvent
from box_agent.kernel.context_engine import (
    _SUMMARY_REQUEST, _fallback_context_estimate, capture_request_usage, request_input_tokens,
)
from box_agent.llm import LLMClient, SessionBoundLLM
from box_agent.retry import RetryConfig
from box_agent.runtime import run_agent_loop
from box_agent.schema import FunctionCall, LLMProvider, Message, StreamEvent, TokenUsage, ToolCall
from box_agent.tools.base import Tool, ToolResult


@asynccontextmanager
async def wire_llm(context_window=20_000):
    requests = []

    def handle(request):
        payload = json.loads(request.content)
        requests.append(payload)
        response = ("<summary>kept</summary>"
                    if payload["messages"][-1]["content"] == _SUMMARY_REQUEST else "done")
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=(
            'data: {"id":"budget","object":"chat.completion.chunk","created":0,'
            '"model":"budget-model","choices":[{"index":0,"delta":{"content":' + json.dumps(response) + '},'
            '"finish_reason":null}]}\n\n'
            'data: {"id":"budget","object":"chat.completion.chunk","created":0,'
            '"model":"budget-model","choices":[{"index":0,"delta":{},'
            '"finish_reason":"stop"}]}\n\n'
            'data: [DONE]\n\n'
        ))

    llm = LLMClient(
        api_key="test", provider=LLMProvider.OPENAI,
        api_base="https://budget.invalid/v1", model="budget-model",
        max_output_tokens=1_000, context_window=context_window,
        retry_config=RetryConfig(enabled=False),
    )
    await llm._client.client.close()
    llm._client.client = AsyncOpenAI(
        api_key="test", base_url=llm.api_base, max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    try:
        yield llm, requests
    finally:
        await llm.aclose()


@pytest.mark.asyncio
async def test_long_text_that_fits_the_estimated_window_reaches_provider_without_compaction():
    user_text = "请整理工作计划和待办。\n" * 800
    messages = [Message(role="system", content="system"), Message(role="user", content=user_text)]
    async with wire_llm() as (llm, requests):
        events = [event async for event in run_agent_loop(
            llm=llm, messages=messages, tools={},
            token_limit=derive_context_token_limit(20_000, 1_000), max_steps=1,
        )]
    assert len(requests) == 1
    assert requests[0]["messages"][-1]["content"] == user_text
    assert not any(isinstance(event, (SummarizationEvent, ErrorEvent)) for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("change_schema", [False, True])
async def test_provider_usage_guides_next_request_and_charges_changed_tools(change_schema):
    class ReadTool(Tool):
        name = "read_data"
        description = "Read data."
        parameters = {"type": "object", "properties": {}}

        async def execute(self):
            if change_schema:
                self.description = "Extra usage guidance. " * 2_000
            return ToolResult(success=True, content="工具返回的结果。\n" * 650)

    class Provider:
        estimate_text_tokens = LLMClient.estimate_text_tokens

        def __init__(self):
            self.main_requests = []

        async def generate_stream(self, messages, **kwargs):
            if kwargs.get("call_kind") == "context_summary":
                yield StreamEvent(type="text", delta="<summary>Continue.</summary>")
                yield StreamEvent(type="finish", finish_reason="stop")
                return
            self.main_requests.append(list(messages))
            if len(self.main_requests) == 1:
                yield StreamEvent(
                    type="finish", finish_reason="tool_calls",
                    tool_calls=[ToolCall(id="read-1", type="function", function=FunctionCall(
                        name="read_data", arguments={},
                    ))],
                    usage=TokenUsage(prompt_tokens=4_500, completion_tokens=30, total_tokens=4_530),
                )
            else:
                yield StreamEvent(type="text", delta="done")
                yield StreamEvent(type="finish", finish_reason="stop")

    provider = Provider()
    events = [event async for event in run_agent_loop(
        llm=provider,
        messages=[Message(role="system", content="system"),
                  Message(role="user", content="请整理工作计划和待办。\n" * 800)],
        tools={"read_data": ReadTool()}, token_limit=17_100, max_steps=2,
    )]

    if change_schema:
        assert len(provider.main_requests) == 1
        assert any(isinstance(event, ErrorEvent) for event in events)
    else:
        assert len(provider.main_requests) == 2
        assert any(message.role == "tool" and "工具返回的结果" in message.content
                   for message in provider.main_requests[-1])
        assert not any(isinstance(event, (SummarizationEvent, ErrorEvent)) for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_usage", [False, True])
async def test_underestimated_unicode_user_input_is_blocked_without_provider_call(previous_usage):
    original = "龘 " * 10_000
    messages = [Message(role="system", content="system")]
    if previous_usage:
        messages.append(Message(role="assistant", content="old answer",
                                usage=TokenUsage(prompt_tokens=1_000, total_tokens=1_000)))
    messages.append(Message(role="user", content=original))
    async with wire_llm() as (llm, requests):
        events = [event async for event in run_agent_loop(
            llm=llm, messages=messages, tools={},
            token_limit=derive_context_token_limit(20_000, 1_000), max_steps=1,
        )]
    assert requests == []
    assert any(isinstance(event, SummarizationEvent) for event in events)
    assert any(isinstance(event, ErrorEvent) for event in events)
    assert any(isinstance(event, DoneEvent) and event.stop_reason == StopReason.ERROR for event in events)
    assert any(message.role == "user" and message.content == original for message in messages)


@pytest.mark.parametrize("change", ["same", "removed", "duplicate", "text", "role", "image"])
def test_measured_request_counts_only_new_occurrences_of_appended_content(change):
    llm = SimpleNamespace(
        model="model", provider="openai", api_base="https://budget.invalid/v1",
        estimate_text_tokens=lambda text: (len(text.encode("utf-8")) + 1) // 2,
    )
    history = [Message(role="system", content="system"), Message(role="user", content="task")]
    overlay = Message(role="user", source="runtime", content="memory " * 100)
    if change == "image":
        overlay.content = [{"type": "input_image", "data": "AAAA", "width": 64, "height": 64}]
    usage = TokenUsage(prompt_tokens=1_000, completion_tokens=10, total_tokens=1_010)
    usage._request_snapshot = capture_request_usage([*history, overlay], len(history), [], llm)
    response = Message(role="assistant", content="answer", usage=usage)
    current = [overlay.model_copy(deep=True)]
    added = []
    if change == "removed":
        current = []
    elif change == "duplicate":
        current.append(overlay.model_copy(deep=True))
        added = [current[-1]]
    elif change == "text":
        current[0].content = "changed memory " * 100
        added = current
    elif change == "role":
        current[0].role = "system"
        added = current
    elif change == "image":
        current[0].content[0]["data"] = "BBBB"
        added = current

    estimate = request_input_tokens([*history, response, *current], {}, llm=llm)
    assert estimate == 1_010 + (_fallback_context_estimate(added, {}, llm=llm) if added else 0)


@pytest.mark.parametrize("snapshot_mode", ["missing", "different-route"])
def test_unmatched_usage_still_checks_the_full_request_with_appended_content(snapshot_mode):
    llm = SimpleNamespace(
        model="model", provider="openai", api_base="https://budget.invalid/v1",
        estimate_text_tokens=lambda text: (len(text.encode("utf-8")) + 1) // 2,
    )
    history = [Message(role="system", content="system"), Message(role="user", content="龘 " * 5_000)]
    overlay = Message(role="user", source="runtime", content="memory " * 100)
    usage = TokenUsage(prompt_tokens=1_000, completion_tokens=10, total_tokens=1_010)
    if snapshot_mode == "different-route":
        usage._request_snapshot = capture_request_usage([*history, overlay], len(history), [], llm)
        llm.model = "other-model"
    messages = [*history, Message(role="assistant", content="answer", usage=usage), overlay]

    estimate = request_input_tokens(messages, {}, llm=llm)
    assert estimate == _fallback_context_estimate(messages, {}, llm=llm)
    assert estimate > 10_000


@pytest.mark.asyncio
@pytest.mark.parametrize("large_result", [False, True])
async def test_repeated_memory_does_not_compact_fitting_requests_but_new_overflow_does(large_result):
    class ReadTool(Tool):
        name = "read_data"
        description = "Read data."
        parameters = {"type": "object", "properties": {"page": {"type": "integer"}}, "required": ["page"]}

        async def execute(self, page):
            return ToolResult(success=True, content="龘 " * 5_000 if large_result else "read complete")

    class Provider:
        estimate_text_tokens = LLMClient.estimate_text_tokens

        def __init__(self):
            self.main_requests = []

        async def generate_stream(self, messages, **kwargs):
            if kwargs.get("call_kind") == "context_summary":
                yield StreamEvent(type="text", delta="<summary>Continue the task.</summary>")
                yield StreamEvent(type="finish", finish_reason="stop")
                return
            self.main_requests.append(list(messages))
            if len(self.main_requests) < 3:
                yield StreamEvent(
                    type="finish", finish_reason="tool_calls",
                    tool_calls=[ToolCall(id=f"read-{len(self.main_requests)}", type="function",
                                         function=FunctionCall(name="read_data", arguments={"page": len(self.main_requests)}))],
                    usage=TokenUsage(prompt_tokens=2_300, completion_tokens=30, total_tokens=2_330),
                )
            else:
                yield StreamEvent(type="text", delta="done")
                yield StreamEvent(type="finish", finish_reason="stop")

    provider = Provider()
    memory_text = "saved memory " * 230
    memory = SimpleNamespace(auto_match_context=lambda query: [{"text": memory_text}])
    events = [event async for event in run_agent_loop(
        llm=provider, tools={"read_data": ReadTool()}, memory_manager=memory,
        messages=[Message(role="system", content="system"), Message(role="user", content="task")],
        token_limit=3_000, max_steps=3,
    )]
    if large_result:
        assert any(isinstance(event, SummarizationEvent) for event in events)
        assert not any("龘 " * 5_000 in str(message.content)
                       for request in provider.main_requests[1:] for message in request)
    else:
        assert len(provider.main_requests) == 3, [
            event for event in events if isinstance(event, (ErrorEvent, DoneEvent))
        ]
        assert all(sum(memory_text in str(message.content) for message in request) == 1
                   for request in provider.main_requests)
        assert not any(isinstance(event, (SummarizationEvent, ErrorEvent)) for event in events)


@pytest.mark.asyncio
async def test_larger_run_budget_is_clamped_to_model_before_compacting_and_sending():
    messages = [
        Message(role="system", content="system"),
        Message(role="user", content="old request"),
        Message(role="assistant", content="龘 " * 10_000),
        *(Message(role="assistant", content=f"recent result {index}") for index in range(6)),
        Message(role="user", content="continue"),
    ]
    async with wire_llm() as (llm, requests):
        events = [event async for event in run_agent_loop(
            llm=llm, messages=messages, tools={}, token_limit=35_100, max_steps=1,
        )]
    compaction = next(event for event in events if isinstance(event, SummarizationEvent))
    assert compaction.token_limit == derive_context_token_limit(20_000, 1_000)
    assert compaction.mode == "fallback" and compaction.summary_calls == 0
    assert len(requests) == 1
    assert sum(len(message["content"].encode("utf-8"))
               for message in requests[0]["messages"]) <= compaction.token_limit
    assert requests[0]["messages"][-1]["content"] == "continue"
    assert not [event for event in events if isinstance(event, ErrorEvent)]


@pytest.mark.asyncio
async def test_new_tool_schema_after_usage_is_compacted_then_blocked_locally():
    class LargeTool(Tool):
        name = "large_tool"
        description = "schema " * 25_000
        parameters = {"type": "object", "properties": {}}

        async def execute(self):
            pytest.fail("Budget validation must not execute tools")

    messages = [
        Message(role="system", content="system"),
        Message(role="user", content="before tool activation"),
        Message(role="assistant", content="old answer",
                usage=TokenUsage(prompt_tokens=1_000, total_tokens=1_000)),
        Message(role="user", content="continue"),
    ]
    async with wire_llm() as (llm, requests):
        events = [event async for event in run_agent_loop(
            llm=llm, messages=messages, tools={"large_tool": LargeTool()},
            token_limit=17_100, max_steps=1,
        )]
    assert any(isinstance(event, SummarizationEvent) and event.mode == "blocked" for event in events)
    assert any(isinstance(event, ErrorEvent) for event in events)
    assert len(requests) == 1
    assert requests[0]["messages"][-1]["content"] == _SUMMARY_REQUEST
    assert not requests[0].get("tools")


@pytest.mark.parametrize("binding,client_window,expected", [
    ({"source": "builtin", "model": "other", "contextWindow": 40_000}, 8_000, 40_000),
    ({"source": "builtin", "model": "configured", "contextWindow": 80_000}, 40_000, 80_000),
    ({"source": "profile", "model": "other"}, 8_000, 8_000),
    ({"source": "builtin", "model": "other", "autoRouting": {"models": [
        {"model": "other", "contextWindow": 8_000},
    ]}}, None, 8_000),
    (None, None, 40_000),
    (None, 80_000, 40_000),
])
def test_acp_prefers_explicit_model_window_and_falls_back_to_known_capacity(binding, client_window, expected):
    host = SimpleNamespace(
        _config=SimpleNamespace(llm=SimpleNamespace(model="configured", context_window=40_000,
                                                   max_output_tokens=1_000)),
        _llm=SimpleNamespace(model="configured"),
    )
    window, output = BoxACPAgent._context_capabilities_for_binding(
        host, binding, llm=SimpleNamespace(context_window=client_window, max_output_tokens=2_000),
    )
    assert (window, output) == (expected, 2_000)


def test_switching_to_model_without_known_capacity_fails_locally():
    host = SimpleNamespace(
        _config=SimpleNamespace(llm=SimpleNamespace(model="configured", context_window=40_000,
                                                   max_output_tokens=1_000)),
        _llm=SimpleNamespace(model="configured"),
    )
    with pytest.raises(ValueError, match="contextWindow is required"):
        BoxACPAgent._context_capabilities_for_binding(
            host, {"source": "builtin", "model": "unknown-window"},
        )


@pytest.mark.asyncio
async def test_same_model_child_keeps_session_capacity_but_new_model_drops_it():
    async with wire_llm() as (llm, _requests):
        bound = SessionBoundLLM(llm)
        bound.context_window = 8_000
        child = bound.for_model(llm.model)
        changed = bound.for_model("another-model")
        assert child.context_window == 8_000
        assert changed.context_window is None
        assert llm.context_window == 20_000


@pytest.mark.asyncio
async def test_async_text_counter_uses_local_bound_instead_of_bypassing_budget():
    class Provider:
        estimate_text_tokens = AsyncMock()

        async def generate_stream(self, **kwargs):
            pytest.fail("Over-budget input must stay local")
            yield

    provider = Provider()
    events = [event async for event in run_agent_loop(
        llm=provider, tools={}, token_limit=1_000, max_steps=1,
        messages=[Message(role="system", content="system"), Message(role="user", content="龘" * 1_000)],
    )]
    provider.estimate_text_tokens.assert_not_called()
    assert any(isinstance(event, ErrorEvent) and event.error_category == "context_length" for event in events)

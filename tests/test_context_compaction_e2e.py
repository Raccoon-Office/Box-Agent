"""End-to-end coverage for the public Agent context-compaction path."""

from __future__ import annotations

import json
from contextlib import closing

import pytest

from box_agent.agent import Agent
from box_agent.events import DoneEvent, ErrorEvent, StopReason, SummarizationEvent
from box_agent.schema import FunctionCall, LLMResponse, Message, StreamEvent, TokenUsage, ToolCall
from box_agent.context_input import DefaultContextEngine
from box_agent.kernel.compact_engine import DefaultCompactEngine
from box_agent.kernel.context_engine import (
    REQUEST_INPUT_HEADROOM_TOKENS, _SUMMARY_MESSAGE_PREFIX, _SUMMARY_MESSAGE_SUFFIX,
    _create_summary, _fallback_context_estimate, _is_summary_marker, _summary_request,
)
from box_agent.llm.llm_wrapper import LLMClient
from box_agent.session_log import SessionLog
from box_agent.tools.base import Tool, ToolResult


class _PrefixTool(Tool):
    name = "second_by_name"
    description = "Load the first report."
    parameters = {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        return ToolResult(success=True, content="report")


class _AnotherPrefixTool(_PrefixTool):
    name = "first_by_name"
    description = "Load the second report."


@pytest.mark.asyncio
async def test_reactive_summary_reuses_main_request_wire_prefix_and_tool_order(tmp_path):
    import httpx
    from openai import AsyncOpenAI

    from box_agent.schema import LLMProvider

    requests = []

    def respond(request):
        payload = json.loads(request.content)
        if not payload.get("stream"):
            return httpx.Response(200, json={"choices": [{
                "index": 0, "message": {"role": "assistant", "content": '{"continue":false}'},
                "finish_reason": "stop",
            }]})
        requests.append(payload)
        if len(requests) == 1:
            return httpx.Response(400, json={"error": {
                "code": "context_length_exceeded", "message": "This model's maximum context length is 12000 tokens.",
            }})
        text = "<summary>Progress retained.</summary>" if len(requests) == 2 else "completed"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=(
            f"data: {json.dumps({'choices': [{'index': 0, 'delta': {'content': text}, 'finish_reason': 'stop'}]})}\n\n"
            "data: [DONE]\n\n"
        ))

    model = LLMClient(provider=LLMProvider.OPENAI, api_key="test", api_base="https://example.invalid/v1",
                      model="prefix-test", max_output_tokens=1000)
    await model._client.client.close()
    async with AsyncOpenAI(api_key="test", base_url=model.api_base, max_retries=0,
                           http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))) as client:
        model._client.client = client
        agent = Agent(llm_client=model, system_prompt="`<current_date>` is the session date.",
                      tools=[_PrefixTool(), _AnotherPrefixTool()], max_steps=1, token_limit=10_000,
                      workspace_dir=str(tmp_path), deferred_mcp_loading_enabled=False)
        agent.messages.extend([Message(role="user", content="First report"),
                               Message(role="assistant", content="Already checked the first report.")])
        agent.add_user_message("Summarize the checkpoints.")
        events = [event async for event in agent.run_events()]

    assert any(isinstance(event, SummarizationEvent) and event.mode == "summary" for event in events)
    assert len(requests) == 3
    initial, summary, _ = requests
    assert [tool["function"]["name"] for tool in initial["tools"][:2]] == ["second_by_name", "first_by_name"]
    assert summary["tools"] == initial["tools"]
    assert "tool_choice" not in summary
    assert any("<current_date>" in message["content"] for message in initial["messages"][1:])
    assert summary["messages"] == [
        *initial["messages"], {"role": "user", "content": _summary_request(summary["max_tokens"])},
    ]


@pytest.mark.asyncio
async def test_summary_preflight_counts_the_shared_tools():
    from box_agent.kernel.context_engine import _SummaryInputBudgetExceeded
    from box_agent.tools.engine.contracts import ToolDefinitionView

    llm = CompactionE2ELLM()
    history = [Message(role="system", content="system"), Message(role="user", content="request")]
    limit = _fallback_context_estimate([
        *history, Message(role="user", content=_summary_request(8_192)),
    ], None, llm=llm)
    with pytest.raises(_SummaryInputBudgetExceeded):
        await _create_summary(llm, history, 1, input_token_limit=limit,
                              tools=(ToolDefinitionView.from_tool(_PrefixTool()),))
    assert not llm.summary_messages
    assert await _create_summary(llm, history, 1, input_token_limit=limit)


class CompactionE2ELLM:
    """Exercise one non-streaming summary call followed by a normal stream."""

    def estimate_text_tokens(self, text):
        return len(text.encode("utf-8")) // 3

    def __init__(self) -> None:
        self.summary_messages: list[Message] = []
        self.normal_messages: list[Message] = []
        self.judge_messages: list[Message] = []

    async def generate(self, messages, tools=None, **_kwargs):
        assert tools is None
        if _kwargs.get("call_kind") == "turn_continuation_judge":
            self.judge_messages = list(messages)
            return LLMResponse(content='{"continue":false}', finish_reason="stop")
        self.summary_messages = list(messages)
        return LLMResponse(
            content=(
                "<summary>"
                "1. Primary Request and Intent:\nContinue the compaction E2E.\n\n"
                "6. All User Messages:\n"
                "- old user request\n"
                "- latest user request\n\n"
                "8. Current Work:\nContext compaction is being verified."
                "</summary>"
            ),
            finish_reason="stop",
        )

    async def generate_stream(self, messages, tools=None, **_kwargs):
        if _kwargs.get("call_kind") == "context_summary":
            self.summary_messages = list(messages)
            yield StreamEvent(
                type="text",
                delta=(
                    "<summary>1. Primary Request and Intent:\n"
                    "Continue the compaction E2E.\n\n"
                    "6. All User Messages:\n- old user request\n- latest user request\n\n"
                    "8. Current Work:\nContext compaction is being verified.</summary>"
                ),
            )
            yield StreamEvent(type="finish", finish_reason="stop")
            return
        self.normal_messages = list(messages)
        yield StreamEvent(type="text", delta="E2E resumed answer")
        yield StreamEvent(type="finish", finish_reason="stop")


@pytest.mark.asyncio
async def test_agent_compacts_above_derived_limit_and_resumes_from_synthetic_user_message(
    tmp_path,
) -> None:
    llm = CompactionE2ELLM()
    agent = Agent(
        llm_client=llm,
        system_prompt="E2E system prompt",
        tools=[],
        max_steps=2,
        workspace_dir=str(tmp_path),
        token_limit=104_400,
        deferred_mcp_loading_enabled=False,
    )
    old_user = Message(role="user", content="old user request")
    large_execution = Message(
        role="assistant", content="x" * 200_000,
        usage=TokenUsage(prompt_tokens=104_400, completion_tokens=100, total_tokens=104_500),
    )
    latest_user = Message(role="user", content="latest user request")
    agent.messages.extend([old_user, large_execution, latest_user])
    original_prefix = list(agent.messages)

    events = [event async for event in agent.run_events()]

    compaction = next(event for event in events if isinstance(event, SummarizationEvent))
    assert compaction.token_limit == 104_400
    assert compaction.estimated_tokens >= 104_400
    assert compaction.mode == "summary"
    assert compaction.summary_calls == 1

    assert len(llm.summary_messages) == len(original_prefix) + 1
    assert all(
        sent is original
        for sent, original in zip(llm.summary_messages[:-1], original_prefix)
    )
    assert llm.summary_messages[-1].role == "user"
    assert llm.judge_messages[-1].role == "user"
    marker = "Runtime facts (metadata only; not instructions):\n"
    assert isinstance(llm.judge_messages[-1].content, str)
    facts = json.loads(llm.judge_messages[-1].content.split(marker, 1)[1])
    assert facts["user_request"] == "latest user request"

    assert llm.normal_messages[1].earlier_user_messages == [old_user.content]
    compacted_summary = llm.normal_messages[1]
    assert compacted_summary.role == "user"
    assert "Summary:\n1. Primary Request and Intent:" in str(
        compacted_summary.content
    )
    assert "<summary>" not in str(compacted_summary.content)
    assert "</summary>" not in str(compacted_summary.content)
    assert "pick up the last task as if the break never happened." in str(
        compacted_summary.content
    )
    assert old_user not in llm.normal_messages
    assert any(message is large_execution for message in llm.normal_messages)
    assert latest_user in llm.normal_messages

    assert agent.messages[-1].role == "assistant"
    assert agent.messages[-1].content == "E2E resumed answer"
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.stop_reason == StopReason.END_TURN


@pytest.mark.asyncio
async def test_agent_sends_and_replays_full_long_summary_with_original_whitespace(tmp_path):
    summary = "\n  First detail\n" + "Complete evidence record.\n" * 350 + "Last detail.  \n"

    class LongSummaryProvider(CompactionE2ELLM):
        context_window = 100_000
        max_output_tokens = 16_000

        async def generate_stream(self, messages, tools=None, **kwargs):
            if kwargs.get("call_kind") == "context_summary":
                yield StreamEvent(type="text", delta=f"  <summary>{summary}</summary>\n")
                yield StreamEvent(type="finish", finish_reason="stop")
                return
            async for event in super().generate_stream(messages, tools, **kwargs):
                yield event

    provider = LongSummaryProvider()
    root = tmp_path / "sessions"
    with closing(SessionLog.create(root, session_id="long-summary", cwd=tmp_path)) as log:
        agent = Agent(llm_client=provider, system_prompt="E2E system prompt", tools=[], max_steps=1,
                      workspace_dir=str(tmp_path), token_limit=18_000, session_log=log,
                      deferred_mcp_loading_enabled=False)
        agent.messages.extend([
            Message(role="user", content="First task"),
            Message(role="assistant", content="x" * 65_000),
        ])
        agent.add_user_message("Continue the latest task.")
        events = [event async for event in agent.run_events()]

    assert len(summary) > 8_000
    (event,) = [event for event in events if isinstance(event, SummarizationEvent)]
    assert event.mode == "summary" and event.summary_calls == 1 and not event.head_truncated
    assert next(event for event in events if isinstance(event, DoneEvent)).stop_reason == StopReason.END_TURN
    sent = next(message.content for message in provider.normal_messages if _is_summary_marker(message))
    assert sent.startswith(_SUMMARY_MESSAGE_PREFIX + summary + _SUMMARY_MESSAGE_SUFFIX)
    assert sent.index("[Earlier User Messages]") < sent.index("If you need specific details")
    with closing(SessionLog.open(root, session_id="long-summary", cwd=tmp_path)) as log:
        replayed = next(message.content for message in log.replay().messages if _is_summary_marker(message))
    assert replayed == sent


@pytest.mark.asyncio
@pytest.mark.parametrize("history_chars", [100_000, 45_000], ids=["history-overflow", "summary-instruction-overflow"])
async def test_agent_trims_an_over_budget_summary_and_resumes(tmp_path, history_chars):
    llm = CompactionE2ELLM()
    token_limit = 16_000
    agent = Agent(
        llm_client=llm, system_prompt="system", tools=[], max_steps=1,
        workspace_dir=str(tmp_path), token_limit=token_limit,
        deferred_mcp_loading_enabled=False,
    )
    latest_user = Message(role="user", content="latest user request")
    agent.messages.extend([
        Message(role="user", content="old user request"),
        Message(
            role="assistant", content="x" * history_chars,
            usage=TokenUsage(prompt_tokens=token_limit, completion_tokens=100,
                             total_tokens=token_limit + 100),
        ),
        latest_user,
    ])
    history_tokens = _fallback_context_estimate(agent.messages, {}, llm=llm)
    summary_tokens = _fallback_context_estimate([
        *agent.messages, Message(role="user", source="runtime", content=_summary_request(8_192)),
    ], {}, llm=llm)
    if history_chars == 45_000:
        assert history_tokens < token_limit
    else:
        assert history_tokens > token_limit
    assert summary_tokens > token_limit

    events = [event async for event in agent.run_events()]

    compaction = next(event for event in events if isinstance(event, SummarizationEvent))
    assert compaction.mode == "summary"
    assert compaction.summary_calls == 1 and compaction.summary_retries == 1
    assert compaction.head_truncated
    assert llm.summary_messages[1].content == "[earlier conversation truncated for compaction retry]"
    assert "NOT covered by this summary" in str(llm.normal_messages)
    assert latest_user in llm.normal_messages
    assert _fallback_context_estimate(llm.normal_messages, {}, llm=llm) <= token_limit
    assert not [event.message for event in events if isinstance(event, ErrorEvent)]
    assert agent.messages[-1].content == "E2E resumed answer"
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.stop_reason == StopReason.END_TURN


@pytest.mark.asyncio
@pytest.mark.parametrize("summary_window, history_chars, request_shape", [
    (130_000, 98_000, "full"),
    (100_000, 98_000, "tight-output"),
    (80_000, 99_000, "summary-instruction-overflow"),
    (60_000, 98_000, "summary-history-overflow"),
], ids=["full", "tight-output", "summary-instruction-overflow", "summary-history-overflow"])
async def test_summary_uses_its_own_window_and_output_reservation(summary_window, history_chars, request_shape):
    from box_agent.config import derive_context_token_limit
    from box_agent.runtime import run_agent_loop
    from tests.test_main_input_budget import wire_llm

    history = [
        Message(role="system", content="system"),
        Message(role="user", content="old request"),
        Message(role="assistant", content="x" * history_chars),
        *(Message(role="assistant", content=f"recent result {index}") for index in range(6)),
        Message(role="user", content="continue"),
    ]
    configured_output = 50_000
    async with wire_llm(context_window=80_000, max_output_tokens=configured_output) as (llm, requests):
        summary_llm = llm.for_model("summary-model", max_output_tokens=configured_output)
        summary_llm.context_window = summary_window
        main_limit = derive_context_token_limit(80_000, configured_output)
        summary_limit = summary_window - 8_192 - REQUEST_INPUT_HEADROOM_TOKENS
        history_tokens = _fallback_context_estimate(history, {}, llm=llm)
        summary_tokens = _fallback_context_estimate([
            *history, Message(role="user", source="runtime", content=_summary_request(configured_output)),
        ], {}, llm=summary_llm)
        assert history_tokens > main_limit
        if request_shape == "summary-instruction-overflow":
            assert history_tokens <= summary_limit < summary_tokens
        elif request_shape == "summary-history-overflow":
            assert summary_limit < history_tokens
        else:
            assert summary_tokens <= summary_limit

        events = [event async for event in run_agent_loop(
            llm=llm, summary_llm=summary_llm, messages=history, tools={},
            token_limit=main_limit, max_steps=1,
        )]

        compaction = next(event for event in events if isinstance(event, SummarizationEvent))
        summary_requests = [request for request in requests if request["model"] == "summary-model"]
        assert compaction.mode == "summary" and compaction.summary_calls == 1
        assert compaction.summary_retries == int(summary_tokens > summary_limit)
        assert compaction.head_truncated == (summary_tokens > summary_limit)
        assert len(summary_requests) == 1
        summary_request = summary_requests[0]
        output = summary_request.get("max_tokens", summary_request.get("max_completion_tokens"))
        assert summary_request["messages"][-1]["content"] == _summary_request(output)
        sent_input = _fallback_context_estimate(
            [Message(role=message["role"], content=message["content"]) for message in summary_request["messages"]],
            {}, llm=summary_llm,
        )
        if request_shape == "tight-output":
            assert output == summary_window - sent_input - REQUEST_INPUT_HEADROOM_TOKENS
            assert 8_192 <= output < configured_output
        else:
            assert output == configured_output
        assert sent_input <= summary_limit
        assert sent_input + output + REQUEST_INPUT_HEADROOM_TOKENS <= summary_window
        main_requests = [request for request in requests if request["model"] == llm.model]
        assert len(main_requests) == 1
        assert compaction.estimated_after <= main_limit
        assert llm.max_output_tokens == summary_llm.max_output_tokens == configured_output
        assert not any(isinstance(event, ErrorEvent) for event in events)


@pytest.mark.asyncio
async def test_loop_prepares_before_compacting_and_reprepares_before_provider(monkeypatch):
    """The first projection is the budget decision for the exact request."""

    order: list[str] = []

    class Provider:
        def estimate_text_tokens(self, text):
            return len(text.encode("utf-8")) // 3

        async def generate_stream(self, messages, tools=None, **kwargs):
            if kwargs.get("call_kind") == "context_summary":
                yield StreamEvent(type="text", delta="<summary>kept</summary>")
            else:
                order.append("llm")
                yield StreamEvent(type="text", delta="done")
            yield StreamEvent(type="finish", finish_reason="stop")

    original_prepare = DefaultContextEngine.prepare_request
    original_compact = DefaultCompactEngine.compact_if_needed

    def prepare(self, *args, **kwargs):
        order.append("prepare")
        return original_prepare(self, *args, **kwargs)

    async def compact(self, inputs):
        order.append("compact")
        return await original_compact(self, inputs)

    monkeypatch.setattr(DefaultContextEngine, "prepare_request", prepare)
    monkeypatch.setattr(DefaultCompactEngine, "compact_if_needed", compact)

    from box_agent.runtime import run_agent_loop

    history = [
        Message(role="system", content="system"),
        Message(role="user", content="old request"),
        Message(role="assistant", content="x" * 20_000),
        Message(role="user", content="latest request"),
    ]
    events = [
        event
        async for event in run_agent_loop(
            llm=Provider(), messages=history, tools={}, token_limit=2_000, max_steps=1
        )
    ]

    assert order == ["prepare", "compact", "prepare", "llm"]
    assert not any(isinstance(event, ErrorEvent) for event in events)


@pytest.mark.asyncio
async def test_compacted_request_recounts_retained_tool_history_before_provider():
    """Usage of a retained response measured the old, uncompressed request."""
    from box_agent.runtime import run_agent_loop

    llm = CompactionE2ELLM()
    retained_response = Message(
        role="assistant", content="Checking the result",
        tool_calls=[ToolCall(id="check", type="function", function=FunctionCall(
            name="check_result", arguments={},
        ))],
        usage=TokenUsage(prompt_tokens=5_000, completion_tokens=100, total_tokens=5_100),
    )
    history = [
        Message(role="system", content="system"),
        Message(role="user", content="latest request"),
        retained_response,
        Message(role="tool", name="check_result", tool_call_id="check", content="passed"),
    ]
    events = [event async for event in run_agent_loop(
        llm=llm, messages=history, tools={}, token_limit=2_000, max_steps=1,
    )]

    assert any(isinstance(event, SummarizationEvent) for event in events)
    assert not [event.message for event in events if isinstance(event, ErrorEvent)]
    assert llm.normal_messages
    assert any(message.tool_calls == retained_response.tool_calls
               for message in llm.normal_messages if message.role == "assistant")
    assert retained_response.usage.prompt_tokens == 5_000


@pytest.mark.asyncio
async def test_compaction_keeps_full_summary_when_no_headroom_was_requested(monkeypatch):
    from box_agent.kernel import context_engine

    summary = ("Task progress. " * 400).rstrip()

    async def summarize(*args, **kwargs):
        return summary

    monkeypatch.setattr(context_engine, "_create_summary", summarize)
    messages = [
        Message(role="system", content="system"),
        Message(role="user", content="original request"),
        Message(role="assistant", content="old execution " * 1_000),
        *(Message(role="assistant", content=f"recent result {i}") for i in range(5)),
        Message(role="user", content="continue"),
    ]
    limit = 2_900
    outcome = await context_engine._maybe_summarize(
        None, messages, token_limit=limit, api_total_tokens=0, skip_check=False,
    )
    assert outcome.mode == "summary" and outcome.summary_calls == 1
    assert limit - context_engine.REQUEST_INPUT_HEADROOM_TOKENS < outcome.estimated_after <= limit
    assert outcome.degradation is None
    message = next(message for message in outcome.messages if context_engine._is_summary_marker(message))
    assert message.content.startswith(context_engine._SUMMARY_MESSAGE_PREFIX + summary + context_engine._SUMMARY_MESSAGE_SUFFIX)
    assert message.earlier_user_messages == ["original request"]


@pytest.mark.asyncio
async def test_compaction_rebases_usage_before_rebuilding_request():
    from box_agent.kernel.context_engine import _fallback_context_estimate, _maybe_summarize, request_input_tokens

    old_response = Message(
        role="assistant", content="old answer",
        usage=TokenUsage(prompt_tokens=5_000, completion_tokens=100, total_tokens=5_100),
    )
    messages = [
        Message(role="system", content="system"),
        Message(role="user", content="request"),
        old_response,
    ]
    from tests.test_context_compaction_recovery import _Summary

    outcome = await _maybe_summarize(
        _Summary([("invalid", "stop")] * 5), messages, token_limit=1_000, api_total_tokens=0,
        skip_check=False,
    )

    assert outcome.messages is not None
    assert any(message is old_response for message in outcome.messages)
    assert old_response.usage.prompt_tokens == 5_000
    assert request_input_tokens(outcome.messages, {}) == _fallback_context_estimate(
        outcome.messages, {}
    )
    assert outcome.estimated_after <= 1_000


@pytest.mark.asyncio
@pytest.mark.parametrize("user_chars,overlay_chars", [
    (57_000, 0), (57_000, 2_400), (59_800, 0),
], ids=["fits-original-budget", "full-request-overflow", "history-overflow"])
async def test_forced_compaction_headroom_does_not_replace_the_final_request_budget(
    monkeypatch, user_chars, overlay_chars,
):
    from dataclasses import replace

    from box_agent.kernel.context_engine import REQUEST_INPUT_HEADROOM_TOKENS
    from box_agent.kernel.loop import MAX_CONTEXT_COMPACTION_ATTEMPTS
    from box_agent.runtime import run_agent_loop

    class Provider:
        def __init__(self):
            self.normal_requests = []

        def estimate_text_tokens(self, text):
            return len(text.encode("utf-8")) // 2

        async def generate_stream(self, messages, **kwargs):
            if kwargs.get("call_kind") == "context_summary":
                yield StreamEvent(type="text", delta="<summary>" + "kept " * 160 + "</summary>")
            else:
                self.normal_requests.append(messages)
                yield StreamEvent(type="text", delta="resumed")
            yield StreamEvent(type="finish", finish_reason="stop")

    class SummaryProvider(Provider):
        model = "wide-summary-model"
        context_window = 60_000
        max_output_tokens = 4_000

        async def generate(self, messages, **kwargs):
            return LLMResponse(content="<summary>kept</summary>", finish_reason="stop")

    preparations = []
    original_prepare = DefaultContextEngine.prepare_request

    def prepare(self, messages, **kwargs):
        preparations.append(kwargs["token_limit"])
        if len(preparations) > 1 and overlay_chars:
            kwargs["extra_messages"] = (*kwargs.get("extra_messages", ()),
                                        Message(role="user", source="runtime", content="z" * overlay_chars))
        projection = original_prepare(self, messages, **kwargs)
        if len(preparations) == 1:
            assert projection.blocked_reason is None
            # A one-shot capability recovery uses the shared compaction path.
            return replace(projection, blocked_reason="Recover context once", budget_blocked=True)
        return projection

    monkeypatch.setattr(DefaultContextEngine, "prepare_request", prepare)
    provider = Provider()
    events = [event async for event in run_agent_loop(
        llm=provider, summary_llm=SummaryProvider(), tools={}, token_limit=30_000, max_steps=1,
        messages=[Message(role="system", content="BASE"), Message(role="user", content="x" * user_chars)],
    )]
    compactions = [event for event in events if isinstance(event, SummarizationEvent)]
    assert compactions[0].summary_path == "shared"
    assert compactions[0].estimated_after > 30_000 - REQUEST_INPUT_HEADROOM_TOKENS
    assert set(preparations) == {30_000}
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    done = next(event for event in events if isinstance(event, DoneEvent))
    if user_chars == 59_800:
        assert len(compactions) == 1
        assert compactions[0].estimated_after > 30_000
        assert preparations == [30_000]
    else:
        assert compactions[0].estimated_after <= 30_000
        assert len(preparations) >= 2
    if overlay_chars or user_chars == 59_800:
        assert 1 <= len(compactions) <= MAX_CONTEXT_COMPACTION_ATTEMPTS
        assert errors and errors[0].error_code == "CONTEXT_INPUT_BUDGET_EXCEEDED"
        assert not provider.normal_requests
        assert done.stop_reason == StopReason.ERROR
    else:
        assert len(compactions) == 1
        assert preparations == [30_000, 30_000]
        assert not errors and len(provider.normal_requests) == 1
        assert next(m for m in provider.normal_requests[0] if _is_summary_marker(m)).earlier_user_messages == [
            "x" * user_chars,
        ]
        assert done.stop_reason == StopReason.END_TURN


@pytest.mark.parametrize("summary_cap", [8_192, 50_000])
async def test_same_window_threshold_keeps_full_summary_prefix_on_wire(summary_cap):
    from box_agent.config import derive_context_token_limit
    from box_agent.runtime import run_agent_loop
    from tests.test_main_input_budget import wire_llm

    history = [Message(role="system", content="sys"), Message(role="user", content="old"),
               Message(role="assistant", content="x" * 77_000), Message(role="user", content="continue")]
    old_payload = history[2].content
    async with wire_llm(context_window=80_000, max_output_tokens=50_000) as (llm, requests):
        summary = llm.for_model(llm.model, max_output_tokens=summary_cap)
        events = [event async for event in run_agent_loop(
            llm=llm, summary_llm=summary, messages=history, tools={}, max_steps=1,
            token_limit=derive_context_token_limit(80_000, 50_000),
        )]
    (compaction,) = [event for event in events if isinstance(event, SummarizationEvent)]
    assert compaction.mode == "summary" and compaction.summary_calls == 1 and not compaction.head_truncated
    assert old_payload in [m["content"] for m in requests[0]["messages"]]
    cap = requests[0].get("max_tokens", requests[0].get("max_completion_tokens"))
    assert 8_192 <= cap <= summary_cap and (cap < 50_000)
    assert requests[0]["messages"][-1]["content"] == _summary_request(cap)
    assert events[-1].stop_reason == StopReason.END_TURN


async def test_summary_preflight_uses_summary_route_and_legacy_clients_need_full_output_room():
    from box_agent.kernel.context_engine import ContextTokenCalibration, _SummaryInputBudgetExceeded
    from tests.test_context_compaction_recovery import _Summary

    class Legacy(_Summary):
        context_window = 20_000
        max_output_tokens = 15_000

        async def generate_stream(self, messages, tools, thinking_enabled, session_id, turn_id, title, call_kind):
            async for event in super().generate_stream(messages):
                yield event

    provider = Legacy()
    history = [Message(role="user", content="漢" * 1850)]
    calibration = ContextTokenCalibration()
    calibration.record(_Summary(), 2, 1)
    assert await _create_summary(provider, history, 1, calibration=calibration)
    calibration.record(provider, 2, 1)
    with pytest.raises(_SummaryInputBudgetExceeded):
        await _create_summary(provider, history, 1, calibration=calibration)
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_persisted_compaction_schedules_memory_on_owner_loop_and_resumes(tmp_path):
    import asyncio
    import threading
    from box_agent.session_log import SessionLog

    owner_loop = asyncio.get_running_loop()
    owner_thread = threading.get_ident()
    extracted = asyncio.Event()
    snapshots = []

    class Extractor:
        async def maybe_extract(self, messages, trigger, **kwargs):
            assert asyncio.get_running_loop() is owner_loop
            assert threading.get_ident() == owner_thread
            snapshots.append((list(messages), trigger))
            extracted.set()

    log = SessionLog.create(tmp_path, session_id="compaction-memory", cwd=tmp_path)
    try:
        agent = Agent(
            llm_client=CompactionE2ELLM(), system_prompt="Test system", tools=[],
            max_steps=2, workspace_dir=str(tmp_path), token_limit=104_400,
            deferred_mcp_loading_enabled=False, session_log=log,
        )
        agent.set_memory_extractor(Extractor())
        agent.messages.extend([
            Message(role="user", content="old user request"),
            Message(role="assistant", content="x" * 200_000,
                    usage=TokenUsage(prompt_tokens=104_400, completion_tokens=100, total_tokens=104_500)),
            Message(role="user", content="latest user request"),
        ])
        events = [event async for event in agent.run_events()]
        await asyncio.wait_for(extracted.wait(), 2)
        assert any(isinstance(event, SummarizationEvent) for event in events)
        assert not any(isinstance(event, ErrorEvent) for event in events)
        assert any(isinstance(event, DoneEvent) for event in events)
        assert snapshots[0][1] == "pre_summarize"
        assert any(message.content == "x" * 200_000 for message in snapshots[0][0])
        assert any(event["type"] == "compaction/end" for event in log.events)
    finally:
        log.close()

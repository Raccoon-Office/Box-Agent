"""End-to-end coverage for the public Agent context-compaction path."""

from __future__ import annotations

import json

import pytest

from box_agent.agent import Agent
from box_agent.events import DoneEvent, ErrorEvent, StopReason, SummarizationEvent
from box_agent.schema import FunctionCall, LLMResponse, Message, StreamEvent, TokenUsage, ToolCall
from box_agent.context_input import DefaultContextEngine
from box_agent.kernel.compact_engine import DefaultCompactEngine


class CompactionE2ELLM:
    """Exercise one non-streaming summary call followed by a normal stream."""

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
    large_execution = Message(role="assistant", content="x" * 420_000)
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
    assert json.loads(llm.judge_messages[-1].content)["user_request"] == "latest user request"

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
    assert large_execution not in llm.normal_messages
    assert latest_user in llm.normal_messages

    assert agent.messages[-1].role == "assistant"
    assert agent.messages[-1].content == "E2E resumed answer"
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.stop_reason == StopReason.END_TURN


@pytest.mark.asyncio
async def test_loop_prepares_before_compacting_and_reprepares_before_provider(monkeypatch):
    """The first projection is the budget decision for the exact request."""

    order: list[str] = []

    class Provider:
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
    outcome = await _maybe_summarize(
        None, messages, token_limit=1_000, api_total_tokens=0,
        skip_check=False, allow_llm_summary=False,
    )

    assert outcome.messages is not None
    assert all(message.usage is None for message in outcome.messages)
    assert request_input_tokens(outcome.messages, {}) == _fallback_context_estimate(
        outcome.messages, {}
    )
    assert outcome.estimated_after <= 1_000

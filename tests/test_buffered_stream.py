"""Regression tests for buffered use of the streaming LLM transport."""

from __future__ import annotations

import asyncio

import pytest

from box_agent.llm.buffered_stream import generate_buffered_stream
from box_agent.schema import LLMResponse, StreamEvent, TokenUsage


class _StreamingLLM:
    def __init__(self, events, *, delay: float = 0.0):
        self.events = events
        self.delay = delay
        self.stream_calls = 0
        self.generate_calls = 0

    async def generate_stream(self, messages, tools=None, **kwargs):
        self.stream_calls += 1
        for event in self.events:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield event

    async def generate(self, messages, tools=None, **kwargs):
        self.generate_calls += 1
        return LLMResponse(content="non-stream", finish_reason="stop")


@pytest.mark.asyncio
async def test_buffered_stream_prefers_streaming_and_preserves_response_metadata():
    usage = TokenUsage(prompt_tokens=4, completion_tokens=2, total_tokens=6)
    llm = _StreamingLLM(
        [
            StreamEvent(type="thinking", delta="plan"),
            StreamEvent(type="text", delta="hel"),
            StreamEvent(type="activity"),
            StreamEvent(type="text", delta="lo"),
            StreamEvent(
                type="finish",
                finish_reason="stop",
                usage=usage,
                provider_response_id="response-1",
            ),
        ]
    )

    response = await generate_buffered_stream(llm, messages=[])

    assert response.content == "hello"
    assert response.thinking == "plan"
    assert response.usage == usage
    assert response.provider_response_id == "response-1"
    assert llm.stream_calls == 1
    assert llm.generate_calls == 0


@pytest.mark.asyncio
async def test_buffered_stream_idle_timeout_resets_after_each_event():
    llm = _StreamingLLM(
        [
            StreamEvent(type="text", delta="a"),
            StreamEvent(type="text", delta="b"),
            StreamEvent(type="finish", finish_reason="stop"),
        ],
        delay=0.02,
    )

    response = await generate_buffered_stream(
        llm,
        messages=[],
        idle_timeout=0.03,
    )

    assert response.content == "ab"


@pytest.mark.asyncio
async def test_buffered_stream_times_out_when_provider_goes_idle():
    llm = _StreamingLLM(
        [StreamEvent(type="finish", finish_reason="stop")],
        delay=0.05,
    )

    with pytest.raises(asyncio.TimeoutError):
        await generate_buffered_stream(llm, messages=[], idle_timeout=0.01)


@pytest.mark.asyncio
async def test_buffered_stream_requires_finish_event():
    llm = _StreamingLLM([StreamEvent(type="text", delta="partial")])

    with pytest.raises(RuntimeError, match="without a finish event"):
        await generate_buffered_stream(llm, messages=[])


@pytest.mark.asyncio
async def test_buffered_stream_falls_back_for_legacy_test_double():
    class _LegacyLLM:
        async def generate(self, messages, tools=None, **kwargs):
            return LLMResponse(content="legacy", finish_reason="stop")

    response = await generate_buffered_stream(_LegacyLLM(), messages=[])

    assert response.content == "legacy"

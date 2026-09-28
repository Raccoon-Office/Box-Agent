"""Tests for per-turn token accounting (``box_agent.llm.token_meter``).

Covers the accumulator arithmetic, context scoping (including
``asyncio.create_task`` children sharing the same accumulator), and the
``LLMClient`` choke-point recording for both ``generate`` and
``generate_stream``.
"""

from __future__ import annotations

import asyncio

import pytest

from box_agent.llm.llm_wrapper import LLMClient
from box_agent.llm.token_meter import (
    TokenAccumulator,
    get_token_meter,
    record_usage,
    reset_token_meter,
    start_token_meter,
)
from box_agent.schema import LLMProvider, LLMResponse, StreamEvent, TokenUsage


# ── Accumulator + scoping ───────────────────────────────────────


def test_accumulator_preserves_legacy_positional_call_count():
    meter = TokenAccumulator(10, 5, 15, 2)

    assert meter.calls == 2
    assert meter.cached_tokens == 0
    assert meter.cache_usage_reported_calls == 0


def test_accumulator_folds_usage():
    token = start_token_meter()
    try:
        record_usage(TokenUsage(prompt_tokens=10, completion_tokens=4, total_tokens=14))
        record_usage(TokenUsage(prompt_tokens=5, completion_tokens=2, total_tokens=7))
        record_usage(None)  # ignored, no-op
        meter = get_token_meter()
        assert meter is not None
        assert meter.total_tokens == 21
        assert meter.prompt_tokens == 15
        assert meter.completion_tokens == 6
        assert meter.calls == 2
    finally:
        reset_token_meter(token)


def test_accumulator_tracks_cache_reads_as_part_of_prompt_tokens():
    token = start_token_meter()
    try:
        record_usage(TokenUsage(
            prompt_tokens=100, completion_tokens=10, total_tokens=110,
            input_tokens=60, cache_read_input_tokens=40,
            cache_read_input_tokens_reported=True,
        ))
        meter = get_token_meter()
        assert meter.prompt_tokens == 100
        assert meter.total_tokens == 110
        assert meter.cached_tokens == 40
        assert meter.cache_usage_reported_calls == 1
    finally:
        reset_token_meter(token)


def test_accumulator_merges_separately_scoped_usage():
    outer = start_token_meter()
    try:
        record_usage(TokenUsage(prompt_tokens=3, completion_tokens=2, total_tokens=5))
        first = get_token_meter()
        inner = start_token_meter()
        try:
            record_usage(TokenUsage(prompt_tokens=7, completion_tokens=4, total_tokens=11))
            second = get_token_meter()
        finally:
            reset_token_meter(inner)

        first.merge(second)
        assert first.prompt_tokens == 10
        assert first.completion_tokens == 6
        assert first.total_tokens == 16
        assert first.calls == 2
    finally:
        reset_token_meter(outer)


def test_record_usage_without_active_meter_is_noop():
    # No meter started in this context.
    assert get_token_meter() is None
    record_usage(TokenUsage(total_tokens=99))  # must not raise


def test_reset_restores_previous_meter():
    outer = start_token_meter()
    try:
        record_usage(TokenUsage(total_tokens=3))
        inner = start_token_meter()
        record_usage(TokenUsage(total_tokens=100))
        assert get_token_meter().total_tokens == 100
        reset_token_meter(inner)
        # Outer accumulator is untouched by the inner scope.
        assert get_token_meter().total_tokens == 3
    finally:
        reset_token_meter(outer)


async def test_background_task_shares_accumulator():
    """A create_task child copies the context and mutates the same meter."""

    token = start_token_meter()
    try:

        async def child():
            record_usage(TokenUsage(total_tokens=50))

        await asyncio.create_task(child())
        assert get_token_meter().total_tokens == 50
    finally:
        reset_token_meter(token)


# ── LLMClient choke point ───────────────────────────────────────


def _make_client() -> LLMClient:
    return LLMClient(api_key="test-key", provider=LLMProvider.ANTHROPIC)


class _FakeUnderlying:
    """Stands in for the provider-specific client behind LLMClient."""

    def __init__(self, response: LLMResponse, stream_events: list[StreamEvent]):
        self._response = response
        self._stream_events = stream_events
        self.retry_callback = None

    async def generate(self, messages, tools=None, **_):
        return self._response

    async def generate_stream(self, messages, tools=None, **_):
        for event in self._stream_events:
            yield event


async def test_generate_records_usage():
    client = _make_client()
    client._client = _FakeUnderlying(
        LLMResponse(
            content="hi",
            finish_reason="end_turn",
            usage=TokenUsage(prompt_tokens=8, completion_tokens=2, total_tokens=10),
        ),
        [],
    )
    token = start_token_meter()
    try:
        await client.generate([])
        assert get_token_meter().total_tokens == 10
    finally:
        reset_token_meter(token)


async def test_generate_stream_records_usage_on_finish():
    client = _make_client()
    client._client = _FakeUnderlying(
        LLMResponse(content="", finish_reason="end_turn"),
        [
            StreamEvent(type="text", delta="hello"),
            StreamEvent(
                type="finish",
                finish_reason="end_turn",
                usage=TokenUsage(prompt_tokens=20, completion_tokens=5, total_tokens=25),
            ),
        ],
    )
    token = start_token_meter()
    try:
        async for _ in client.generate_stream([]):
            pass
        assert get_token_meter().total_tokens == 25
    finally:
        reset_token_meter(token)


async def test_multi_call_total_accumulates():
    """Multi-step + lite calls in one scope sum into a single total."""

    client = _make_client()
    client._client = _FakeUnderlying(
        LLMResponse(
            content="x",
            finish_reason="end_turn",
            usage=TokenUsage(total_tokens=7),
        ),
        [
            StreamEvent(
                type="finish",
                finish_reason="end_turn",
                usage=TokenUsage(total_tokens=11),
            ),
        ],
    )
    token = start_token_meter()
    try:
        async for _ in client.generate_stream([]):  # multi-turn step
            pass
        await client.generate([])  # lite / memory call
        assert get_token_meter().total_tokens == 18
        assert get_token_meter().calls == 2
    finally:
        reset_token_meter(token)

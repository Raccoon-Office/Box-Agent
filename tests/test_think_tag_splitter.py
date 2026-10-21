"""Tests for inline `<think>...</think>` unwrapping."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from box_agent.llm.think_tag_splitter import split_inline_think, unwrap_think_tags
from box_agent.schema import StreamEvent


async def _drain(stream: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    return [event async for event in stream]


async def _from_deltas(deltas: list[tuple[str, str]]) -> AsyncIterator[StreamEvent]:
    for kind, payload in deltas:
        yield StreamEvent(type=kind, delta=payload)


def _combine(events: list[StreamEvent], kind: str) -> str:
    return "".join(e.delta or "" for e in events if e.type == kind)


class TestSplitInlineThink:
    def test_no_tags_returns_original(self) -> None:
        text, thinking = split_inline_think("plain text without tags")
        assert text == "plain text without tags"
        assert thinking == ""

    def test_single_block(self) -> None:
        text, thinking = split_inline_think("<think>reasoning</think>answer")
        assert text == "answer"
        assert thinking == "reasoning"

    def test_block_in_middle(self) -> None:
        text, thinking = split_inline_think("prefix <think>r</think> suffix")
        assert text == "prefix  suffix"
        assert thinking == "r"

    def test_multiple_blocks(self) -> None:
        text, thinking = split_inline_think("<think>a</think>X<think>b</think>Y")
        assert text == "XY"
        assert thinking == "ab"

    def test_unterminated_trailing_think(self) -> None:
        text, thinking = split_inline_think("answer<think>cut off here")
        assert text == "answer"
        assert thinking == "cut off here"

    def test_dotall_multiline(self) -> None:
        text, thinking = split_inline_think("<think>line1\nline2</think>out")
        assert text == "out"
        assert thinking == "line1\nline2"


class TestUnwrapThinkTags:
    async def test_iterator_without_aclose_remains_supported(self) -> None:
        class Stream:
            def __init__(self):
                self.events = iter([
                    StreamEvent(type="text", delta="<think>reason</think>answer"),
                ])

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return next(self.events)
                except StopIteration:
                    raise StopAsyncIteration

        events = await _drain(unwrap_think_tags(Stream()))
        assert _combine(events, "text") == "answer"
        assert _combine(events, "thinking") == "reason"

    async def test_text_without_tags_passes_through(self) -> None:
        events = await _drain(unwrap_think_tags(_from_deltas([("text", "hello world")])))
        assert _combine(events, "text") == "hello world"
        assert _combine(events, "thinking") == ""

    async def test_existing_thinking_events_pass_through(self) -> None:
        events = await _drain(
            unwrap_think_tags(
                _from_deltas([("thinking", "deep"), ("text", "answer")])
            )
        )
        assert _combine(events, "thinking") == "deep"
        assert _combine(events, "text") == "answer"

    async def test_single_block_in_one_chunk(self) -> None:
        events = await _drain(
            unwrap_think_tags(_from_deltas([("text", "<think>reason</think>answer")]))
        )
        assert _combine(events, "thinking") == "reason"
        assert _combine(events, "text") == "answer"

    async def test_tag_split_across_chunks(self) -> None:
        # "<think>" arrives across two chunks: "<thi" + "nk>reason</think>done"
        events = await _drain(
            unwrap_think_tags(
                _from_deltas(
                    [
                        ("text", "prelude<thi"),
                        ("text", "nk>reason</think>done"),
                    ]
                )
            )
        )
        assert _combine(events, "thinking") == "reason"
        assert _combine(events, "text") == "preludedone"

    async def test_close_tag_split_across_chunks(self) -> None:
        events = await _drain(
            unwrap_think_tags(
                _from_deltas(
                    [
                        ("text", "<think>reason</"),
                        ("text", "think>tail"),
                    ]
                )
            )
        )
        assert _combine(events, "thinking") == "reason"
        assert _combine(events, "text") == "tail"

    async def test_thinking_spans_many_text_chunks(self) -> None:
        events = await _drain(
            unwrap_think_tags(
                _from_deltas(
                    [
                        ("text", "<think>part1 "),
                        ("text", "part2 "),
                        ("text", "part3</think>final"),
                    ]
                )
            )
        )
        assert _combine(events, "thinking") == "part1 part2 part3"
        assert _combine(events, "text") == "final"

    async def test_unterminated_think_emits_buffered_thinking_on_end(self) -> None:
        events = await _drain(
            unwrap_think_tags(_from_deltas([("text", "<think>no close tag")]))
        )
        assert _combine(events, "thinking") == "no close tag"
        assert _combine(events, "text") == ""

    async def test_finish_event_passes_through(self) -> None:
        events = await _drain(
            unwrap_think_tags(
                _from_deltas([("text", "<think>r</think>ok"), ("finish", "")])
            )
        )
        types = [e.type for e in events]
        assert "finish" in types
        assert _combine(events, "thinking") == "r"
        assert _combine(events, "text") == "ok"


class TestLLMClientIntegration:
    """End-to-end via LLMClient — both streaming and non-streaming paths."""

    async def test_generate_strips_think_block(self, monkeypatch) -> None:
        from box_agent.llm import LLMClient
        from box_agent.schema import LLMResponse

        client = LLMClient.__new__(LLMClient)

        async def fake_generate(messages, tools, *, thinking_enabled=False, session_id="", **_):
            return LLMResponse(
                content="<think>private reasoning</think>visible answer",
                thinking=None,
                tool_calls=None,
                finish_reason="stop",
                usage=None,
            )

        class _FakeUnderlying:
            generate = staticmethod(fake_generate)

        client._client = _FakeUnderlying()
        result = await client.generate([])
        assert result.content == "visible answer"
        assert result.thinking == "private reasoning"

    async def test_generate_preserves_existing_thinking(self, monkeypatch) -> None:
        from box_agent.llm import LLMClient
        from box_agent.schema import LLMResponse

        client = LLMClient.__new__(LLMClient)

        async def fake_generate(messages, tools, *, thinking_enabled=False, session_id="", **_):
            return LLMResponse(
                content="<think>extra</think>answer",
                thinking="provider-native",
                tool_calls=None,
                finish_reason="stop",
                usage=None,
            )

        class _FakeUnderlying:
            generate = staticmethod(fake_generate)

        client._client = _FakeUnderlying()
        result = await client.generate([])
        assert result.content == "answer"
        assert result.thinking == "provider-native" + "extra"

    async def test_generate_stream_unwraps_inline_think(self) -> None:
        from box_agent.llm import LLMClient

        client = LLMClient.__new__(LLMClient)

        async def fake_stream(messages, tools, *, thinking_enabled=False, session_id="", **_):
            yield StreamEvent(type="text", delta="<think>x</think>hi")
            yield StreamEvent(type="finish", finish_reason="stop")

        class _FakeUnderlying:
            generate_stream = staticmethod(fake_stream)

        client._client = _FakeUnderlying()
        events = [e async for e in client.generate_stream([])]
        assert _combine(events, "thinking") == "x"
        assert _combine(events, "text") == "hi"

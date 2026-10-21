"""Regression tests for streaming retry + StreamInterrupted on transient httpx errors.

These cover the production bug where the upstream LLM gateway closed the chunked HTTP
connection mid-response, producing ``LLM call failed: peer closed connection without
sending complete message body (incomplete chunked read)`` as a fatal error and losing
all partial output.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from box_agent.llm.openai_client import OpenAIClient
from box_agent.retry import RetryConfig, StreamInterrupted, is_retryable_stream_error
from box_agent.schema import Message


def _delta(content=None, tool_calls=None, reasoning=None, reasoning_content=None):
    return SimpleNamespace(
        content=content,
        tool_calls=tool_calls,
        reasoning=reasoning,
        reasoning_content=reasoning_content,
    )


def _chunk(
    content=None,
    finish_reason=None,
    usage=None,
    tool_calls=None,
    reasoning=None,
    reasoning_content=None,
):
    choice = SimpleNamespace(
        delta=_delta(
            content=content,
            tool_calls=tool_calls,
            reasoning=reasoning,
            reasoning_content=reasoning_content,
        ),
        finish_reason=finish_reason,
    )
    return SimpleNamespace(choices=[choice], usage=usage)


class _AsyncIter:
    """Drive arbitrary items (or exceptions) through an async-for loop."""

    def __init__(self, items):
        self._items = list(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._items:
            raise StopAsyncIteration
        nxt = self._items.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


def _build_client(raw_response_factory, retries=2):
    """Return an OpenAIClient whose `with_raw_response.create` is fully mocked."""
    client = OpenAIClient.__new__(OpenAIClient)
    client.api_key = "test"
    client.api_base = "http://test"
    client.model = "test-model"
    client.retry_config = RetryConfig(
        enabled=True,
        max_retries=retries,
        initial_delay=0.0,
        max_delay=0.0,
    )
    client.retry_callback = None
    client.auth_token = ""
    client.auth_file = ""
    client.max_output_tokens = 1024
    create = AsyncMock(side_effect=raw_response_factory)
    inner = SimpleNamespace(
        with_raw_response=SimpleNamespace(create=create),
        create=AsyncMock(side_effect=RuntimeError("fallback path not expected")),
    )
    client.client = SimpleNamespace(chat=SimpleNamespace(completions=inner))
    return client, create


def _raw_response(stream):
    raw = MagicMock()
    raw.request_id = "req-1"
    raw.headers = {}
    raw.parse = MagicMock(return_value=stream)
    return raw


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "retry", "error"])
async def test_response_cleanup_error_does_not_replace_provider_outcome(outcome):
    error = ValueError("fixture provider failure")
    items = {
        "success": [_chunk(content="ok", finish_reason="stop")],
        "retry": [httpx.RemoteProtocolError("fixture retryable read failure")],
        "error": [error],
    }
    first = _AsyncIter(items[outcome])
    first.close = AsyncMock(side_effect=OSError("fixture response close failure"))
    second = _AsyncIter([_chunk(content="ok", finish_reason="stop")])
    responses = iter([first, second])

    async def factory(**kwargs):
        return _raw_response(next(responses))

    client, create = _build_client(factory)
    stream = client.generate_stream([Message(role="user", content="hello")])
    if outcome == "error":
        with pytest.raises(ValueError) as raised:
            _ = [event async for event in stream]
        assert raised.value is error
    else:
        events = [event async for event in stream]
        assert "".join(event.delta for event in events if event.type == "text") == "ok"
        assert events[-1].finish_reason == "stop"
    assert create.await_count == (2 if outcome == "retry" else 1)


def _tool_delta(name: str, arguments: str, *, index: int = 0):
    return SimpleNamespace(
        index=index,
        id=f"call-{index}",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


@pytest.mark.asyncio
async def test_stream_stops_oversized_tool_arguments_before_json_parse(monkeypatch):
    monkeypatch.setattr(
        "box_agent.llm.openai_client.streamed_argument_limit", lambda _name: 12
    )
    stream = _AsyncIter(
        [
            _chunk(tool_calls=[_tool_delta("bash", '{"command":"')]),
            _chunk(tool_calls=[_tool_delta("", "x" * 20)]),
            _chunk(tool_calls=[_tool_delta("", '"}')], finish_reason="stop"),
        ]
    )

    async def factory(**kwargs):
        return _raw_response(stream)

    client, _ = _build_client(factory, retries=0)
    events = [
        event
        async for event in client.generate_stream([Message(role="user", content="hi")])
    ]

    finish = events[-1]
    assert finish.type == "finish"
    assert finish.finish_reason == "tool_argument_limit"
    assert finish.tool_calls is None
    assert finish.oversized_tool_calls == [
        {"name": "bash", "arguments_len": 32, "limit": 12}
    ]
    assert any(event.type == "activity" for event in events)


@pytest.mark.asyncio
async def test_stream_does_not_apply_a_local_argument_cap_to_write_file():
    content = "x" * 20_000
    arguments = json.dumps({"path": "large.txt", "content": content})
    stream = _AsyncIter(
        [
            _chunk(
                tool_calls=[_tool_delta("write_file", arguments)],
                finish_reason="tool_calls",
            ),
        ]
    )

    async def factory(**kwargs):
        return _raw_response(stream)

    client, _ = _build_client(factory, retries=0)
    events = [
        event
        async for event in client.generate_stream([Message(role="user", content="hi")])
    ]

    finish = events[-1]
    assert finish.finish_reason == "tool_calls"
    assert finish.oversized_tool_calls is None
    assert finish.tool_calls[0].function.arguments["content"] == content


@pytest.mark.asyncio
async def test_stream_emits_bounded_provider_liveness_during_tool_arguments(
    monkeypatch,
):
    clock = iter([0.0, 1.0, 6.0, 7.0])
    monkeypatch.setattr(
        "box_agent.llm.openai_client.monotonic", lambda: next(clock)
    )
    stream = _AsyncIter(
        [
            _chunk(tool_calls=[_tool_delta("staged_file_write", '{"content":"')]),
            _chunk(tool_calls=[_tool_delta("", "a")]),
            _chunk(tool_calls=[_tool_delta("", "b")]),
            _chunk(tool_calls=[_tool_delta("", '"}')], finish_reason="stop"),
        ]
    )

    async def factory(**kwargs):
        return _raw_response(stream)

    client, _ = _build_client(factory, retries=0)
    events = [
        event
        async for event in client.generate_stream([Message(role="user", content="hi")])
    ]

    provider_liveness = [
        event
        for event in events
        if event.type == "activity"
        and event.activity
        and event.activity.get("phase") == "provider_stream"
    ]
    assert len(provider_liveness) == 2


def test_is_retryable_stream_error_matches_production_string():
    exc = httpx.RemoteProtocolError(
        "peer closed connection without sending complete message body (incomplete chunked read)"
    )
    assert is_retryable_stream_error(exc) is True


def test_is_retryable_stream_error_ignores_value_error():
    assert is_retryable_stream_error(ValueError("bad input")) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("reasoning_field", ["reasoning", "reasoning_content"])
async def test_stream_emits_reasoning_alias_as_thinking(reasoning_field):
    """Provider-specific reasoning deltas are emitted as thinking events."""
    stream = _AsyncIter([
        _chunk(**{reasoning_field: "private "}),
        _chunk(
            content="answer",
            finish_reason="stop",
            **{reasoning_field: "reasoning"},
        ),
    ])

    async def factory(**kwargs):
        return _raw_response(stream)

    client, _ = _build_client(factory)
    events = [
        event
        async for event in client.generate_stream([Message(role="user", content="hi")])
    ]

    assert "".join(event.delta for event in events if event.type == "thinking") == (
        "private reasoning"
    )
    assert "".join(event.delta for event in events if event.type == "text") == "answer"


@pytest.mark.asyncio
async def test_stream_retries_when_open_fails_with_transient_error():
    """First open() fails with RemoteProtocolError; second succeeds — output is complete."""
    good_stream = _AsyncIter([
        _chunk(content="hello "),
        _chunk(content="world", finish_reason="stop"),
    ])
    attempts = {"n": 0}

    async def factory(**kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.RemoteProtocolError("peer closed connection during open")
        return _raw_response(good_stream)

    client, create = _build_client(factory, retries=2)
    out = []
    async for ev in client.generate_stream([Message(role="user", content="hi")]):
        out.append(ev)
    assert attempts["n"] == 2
    text = "".join(e.delta for e in out if e.type == "text")
    assert text == "hello world"
    finish = [e for e in out if e.type == "finish"]
    assert len(finish) == 1


@pytest.mark.asyncio
async def test_stream_gives_up_after_max_retries_on_open():
    async def factory(**kwargs):
        raise httpx.RemoteProtocolError("peer closed connection during open")

    client, create = _build_client(factory, retries=2)
    with pytest.raises(httpx.RemoteProtocolError):
        async for _ in client.generate_stream([Message(role="user", content="hi")]):
            pass
    # 1 initial + 2 retries = 3 calls
    assert create.await_count == 3


@pytest.mark.asyncio
async def test_stream_does_not_retry_on_non_transient_open_error():
    async def factory(**kwargs):
        raise ValueError("bad request")

    client, create = _build_client(factory, retries=2)
    with pytest.raises(ValueError):
        async for _ in client.generate_stream([Message(role="user", content="hi")]):
            pass
    assert create.await_count == 1


@pytest.mark.asyncio
async def test_stream_raises_StreamInterrupted_when_dropped_after_partial_yield():
    """Mid-stream drop with partial content → StreamInterrupted carrying the partial text."""
    bad_stream = _AsyncIter([
        _chunk(content="第一篇"),
        _chunk(content="中国乘用车"),
        httpx.RemoteProtocolError(
            "peer closed connection without sending complete message body (incomplete chunked read)"
        ),
    ])

    async def factory(**kwargs):
        return _raw_response(bad_stream)

    client, _ = _build_client(factory, retries=2)
    yielded = []
    with pytest.raises(StreamInterrupted) as excinfo:
        async for ev in client.generate_stream([Message(role="user", content="hi")]):
            yielded.append(ev)
    assert excinfo.value.partial_text == "第一篇中国乘用车"
    assert "peer closed connection" in str(excinfo.value.last_exception)
    # The partial chunks were emitted to the caller before the interruption.
    text = "".join(e.delta for e in yielded if e.type == "text")
    assert text == "第一篇中国乘用车"


@pytest.mark.asyncio
async def test_stream_propagates_non_transient_mid_stream_error_unchanged():
    """A non-network error mid-stream should NOT be wrapped as StreamInterrupted."""
    bad_stream = _AsyncIter([
        _chunk(content="abc"),
        RuntimeError("unrelated bug"),
    ])

    async def factory(**kwargs):
        return _raw_response(bad_stream)

    client, _ = _build_client(factory, retries=2)
    with pytest.raises(RuntimeError):
        async for _ in client.generate_stream([Message(role="user", content="hi")]):
            pass


@pytest.mark.asyncio
async def test_stream_retries_when_dropped_before_any_yield():
    """Mid-stream drop with NO content delivered → silently retry from scratch."""
    bad_stream = _AsyncIter([
        httpx.RemoteProtocolError(
            "peer closed connection without sending complete message body (incomplete chunked read)"
        ),
    ])
    good_stream = _AsyncIter([
        _chunk(content="recovered "),
        _chunk(content="output", finish_reason="stop"),
    ])
    attempts = {"n": 0}

    async def factory(**kwargs):
        attempts["n"] += 1
        return _raw_response(bad_stream if attempts["n"] == 1 else good_stream)

    client, _ = _build_client(factory, retries=2)
    out = []
    async for ev in client.generate_stream([Message(role="user", content="hi")]):
        out.append(ev)
    assert attempts["n"] == 2
    text = "".join(e.delta for e in out if e.type == "text")
    assert text == "recovered output"
    finish = [e for e in out if e.type == "finish"]
    assert len(finish) == 1


@pytest.mark.asyncio
async def test_stream_gives_up_no_yield_retry_after_max_attempts():
    """All attempts drop before any yield → propagate the underlying error."""

    def _new_bad():
        return _AsyncIter([
            httpx.RemoteProtocolError("peer closed connection (incomplete chunked read)"),
        ])

    async def factory(**kwargs):
        return _raw_response(_new_bad())

    client, create = _build_client(factory, retries=2)
    with pytest.raises(httpx.RemoteProtocolError):
        async for _ in client.generate_stream([Message(role="user", content="hi")]):
            pass
    # 1 initial + 2 retries = 3 attempts; each opens then drops during consume.
    assert create.await_count == 3


@pytest.mark.asyncio
async def test_stream_does_not_retry_no_yield_drop_for_non_transient_error():
    """Non-retryable mid-stream error with no yield → propagate, no retry."""
    bad_stream = _AsyncIter([RuntimeError("bad")])

    async def factory(**kwargs):
        return _raw_response(bad_stream)

    client, create = _build_client(factory, retries=2)
    with pytest.raises(RuntimeError):
        async for _ in client.generate_stream([Message(role="user", content="hi")]):
            pass
    assert create.await_count == 1

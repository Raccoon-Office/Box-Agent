"""Collect provider streaming events into the legacy ``LLMResponse`` shape."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator
from typing import Any

from ..schema import LLMResponse, StreamEvent


async def _next_event(
    iterator: AsyncIterator[StreamEvent],
    idle_timeout: float | None,
) -> StreamEvent:
    if idle_timeout is None:
        return await anext(iterator)
    return await asyncio.wait_for(anext(iterator), timeout=idle_timeout)


async def generate_buffered_stream(
    llm: Any,
    *,
    messages: list[Any],
    tools: list[Any] | None = None,
    idle_timeout: float | None = None,
    **kwargs: Any,
) -> LLMResponse:
    """Use streaming transport while returning one fully buffered response.

    ``idle_timeout`` applies independently to each wait for provider activity,
    so a healthy long-running response is not cancelled by a fixed total
    runtime limit. Minimal test doubles and third-party clients that do not yet
    expose ``generate_stream`` retain compatibility through ``generate``.
    """
    generate_stream = getattr(llm, "generate_stream", None)
    if not inspect.isasyncgenfunction(generate_stream):
        call = llm.generate(messages=messages, tools=tools, **kwargs)
        if idle_timeout is None:
            return await call
        return await asyncio.wait_for(call, timeout=idle_timeout)

    stream = generate_stream(messages=messages, tools=tools, **kwargs)
    iterator = stream.__aiter__()
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    finish: StreamEvent | None = None

    try:
        while True:
            try:
                event = await _next_event(iterator, idle_timeout)
            except StopAsyncIteration:
                break
            if event.type == "text" and event.delta:
                text_parts.append(event.delta)
            elif event.type == "thinking" and event.delta:
                thinking_parts.append(event.delta)
            elif event.type == "finish":
                finish = event
    finally:
        close = getattr(iterator, "aclose", None)
        if callable(close):
            await close()

    if finish is None:
        raise RuntimeError("LLM stream ended without a finish event")

    thinking = "".join(thinking_parts)
    return LLMResponse(
        content="".join(text_parts),
        thinking=thinking or None,
        tool_calls=finish.tool_calls,
        finish_reason=finish.finish_reason or "stop",
        usage=finish.usage,
        provider_response_id=finish.provider_response_id,
        truncated_tool_calls=finish.truncated_tool_calls,
        raw_finish_reason=finish.raw_finish_reason,
        stream_dropped_mid_tool=finish.stream_dropped_mid_tool,
        oversized_tool_calls=finish.oversized_tool_calls,
    )

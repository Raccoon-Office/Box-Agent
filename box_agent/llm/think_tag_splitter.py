"""Unwrap inline `<think>...</think>` blocks from LLM text output.

Some providers (notably MiniMax M2.x and several GLM/Qwen variants) embed
chain-of-thought directly in the text/content stream as `<think>...</think>`
instead of using a dedicated `thinking` channel. Box-Agent treats those text
deltas as user-facing content, so the front-end never receives
`agent_thought_chunk` events and the raw tags pollute the rendered Markdown.

This module provides a streaming state machine and a non-streaming helper that
relocate the tagged regions back to the `thinking` field, leaving the visible
content clean. It is a no-op for providers that already use the standard
thinking channel — `<think>` simply never appears in their text deltas.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass

from ..schema import StreamEvent
from .base import closing_llm_stream

OPEN_TAG = "<think>"
CLOSE_TAG = "</think>"
_MAX_TAG_LEN = max(len(OPEN_TAG), len(CLOSE_TAG))


@dataclass
class _SplitterState:
    """Mutable state for incremental tag splitting."""

    inside_think: bool = False
    buffer: str = ""  # holds at most _MAX_TAG_LEN-1 chars of the pending tail


def _process_text(state: _SplitterState, text: str) -> tuple[str, str]:
    """Feed `text` to the state machine; return ``(text_out, thinking_out)``.

    The buffer at the end may withhold up to ``_MAX_TAG_LEN - 1`` chars when
    they could be the prefix of a tag. Caller flushes the buffer at end-of-
    stream via :func:`_flush`.
    """
    pending = state.buffer + text
    state.buffer = ""
    text_out: list[str] = []
    thinking_out: list[str] = []

    i = 0
    n = len(pending)
    while i < n:
        if not state.inside_think:
            idx = pending.find(OPEN_TAG, i)
            if idx == -1:
                # Hold back a tail that could be the start of "<think>".
                tail_start = max(i, n - (_MAX_TAG_LEN - 1))
                # Only hold back if the tail actually looks like a prefix of "<".
                lt = pending.rfind("<", tail_start, n)
                if lt != -1 and lt >= i:
                    text_out.append(pending[i:lt])
                    state.buffer = pending[lt:]
                else:
                    text_out.append(pending[i:n])
                break
            text_out.append(pending[i:idx])
            i = idx + len(OPEN_TAG)
            state.inside_think = True
        else:
            idx = pending.find(CLOSE_TAG, i)
            if idx == -1:
                tail_start = max(i, n - (_MAX_TAG_LEN - 1))
                lt = pending.rfind("<", tail_start, n)
                if lt != -1 and lt >= i:
                    thinking_out.append(pending[i:lt])
                    state.buffer = pending[lt:]
                else:
                    thinking_out.append(pending[i:n])
                break
            thinking_out.append(pending[i:idx])
            i = idx + len(CLOSE_TAG)
            state.inside_think = False

    return "".join(text_out), "".join(thinking_out)


def _flush(state: _SplitterState) -> tuple[str, str]:
    """Emit any buffered tail at end of stream as text or thinking."""
    if not state.buffer:
        return "", ""
    tail = state.buffer
    state.buffer = ""
    if state.inside_think:
        return "", tail
    return tail, ""


async def unwrap_think_tags(
    stream: AsyncIterator[StreamEvent],
) -> AsyncIterator[StreamEvent]:
    """Wrap an LLM stream and convert inline `<think>...</think>` to thinking events.

    Provider-emitted ``thinking``-typed events pass through untouched. ``text``
    events are fed to a state machine; segments inside `<think>` tags are
    re-emitted as ``thinking`` events. Tags themselves are stripped.
    """
    state = _SplitterState()
    async with closing_llm_stream(stream):
        async for event in stream:
            if event.type != "text":
                yield event
                continue

            delta = event.delta or ""
            text_out, thinking_out = _process_text(state, delta)
            if thinking_out:
                yield StreamEvent(type="thinking", delta=thinking_out)
            if text_out:
                yield StreamEvent(type="text", delta=text_out)

    text_out, thinking_out = _flush(state)
    if thinking_out:
        yield StreamEvent(type="thinking", delta=thinking_out)
    if text_out:
        yield StreamEvent(type="text", delta=text_out)


_THINK_BLOCK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)
_OPEN_TRAILING_RE = re.compile(r"<think>(.*)$", re.DOTALL)


def split_inline_think(content: str) -> tuple[str, str]:
    """Remove `<think>...</think>` blocks from `content`; return ``(text, thinking)``.

    Used for non-streaming responses where the full content is materialized.
    Handles unterminated trailing `<think>` (no close tag) by treating the
    remainder as thinking. Returns the original string and empty thinking when
    no tags are present.
    """
    if OPEN_TAG not in content:
        return content, ""

    thinking_parts: list[str] = []

    def _capture(match: re.Match[str]) -> str:
        thinking_parts.append(match.group(1))
        return ""

    cleaned = _THINK_BLOCK_RE.sub(_capture, content)

    # Handle unterminated trailing <think> with no closing tag.
    trailing = _OPEN_TRAILING_RE.search(cleaned)
    if trailing is not None:
        thinking_parts.append(trailing.group(1))
        cleaned = cleaned[: trailing.start()]

    return cleaned, "".join(thinking_parts)

"""Task-local correlation headers for tool requests."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from box_agent.llm.base import LLMClientBase

_REQUEST_HEADERS: ContextVar[dict[str, str | bytes] | None] = ContextVar(
    "box_agent_request_headers", default=None,
)


@contextmanager
def scoped_request_context(
    *, session_id: str = "", turn_id: str = "", title: str = "",
) -> Iterator[None]:
    token = _REQUEST_HEADERS.set(LLMClientBase._agent_headers(session_id, turn_id, title))
    try:
        yield
    finally:
        _REQUEST_HEADERS.reset(token)


def current_request_headers() -> dict[str, str | bytes]:
    return dict(_REQUEST_HEADERS.get() or {})

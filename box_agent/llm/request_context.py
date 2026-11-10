"""Task-local correlation headers shared with hosted tool requests."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from box_agent.client_info import should_attach_client_headers
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


def current_request_headers(url: str) -> dict[str, str | bytes]:
    if not should_attach_client_headers(url):
        return {}
    return dict(_REQUEST_HEADERS.get() or {})

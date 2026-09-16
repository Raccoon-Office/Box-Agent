"""Turn-scoped model capabilities available to model-invoked tools."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import TypeVar


@dataclass(frozen=True, slots=True)
class ModelToolContext:
    model: str
    max_output_tokens: int
    session_id: str


_CURRENT_MODEL_TOOL_CONTEXT: ContextVar[ModelToolContext | None] = ContextVar(
    "box_agent_model_tool_context",
    default=None,
)

_EventT = TypeVar("_EventT")


def current_model_tool_context() -> ModelToolContext | None:
    return _CURRENT_MODEL_TOOL_CONTEXT.get()


def set_model_tool_context(
    *,
    model: object,
    max_output_tokens: object,
    session_id: object = "",
) -> Token[ModelToolContext | None]:
    normalized_model = model.strip() if isinstance(model, str) else ""
    normalized_max_tokens = (
        max_output_tokens
        if isinstance(max_output_tokens, int)
        and not isinstance(max_output_tokens, bool)
        and max_output_tokens > 0
        else 0
    )
    context = (
        ModelToolContext(
            model=normalized_model,
            max_output_tokens=normalized_max_tokens,
            session_id=session_id.strip() if isinstance(session_id, str) else "",
        )
        if (
            (normalized_model and normalized_max_tokens)
            or (isinstance(session_id, str) and bool(session_id.strip()))
        )
        else None
    )
    return _CURRENT_MODEL_TOOL_CONTEXT.set(context)


def reset_model_tool_context(token: Token[ModelToolContext | None]) -> None:
    _CURRENT_MODEL_TOOL_CONTEXT.reset(token)


class _ScopedModelToolIterator(AsyncIterator[_EventT]):
    def __init__(
        self,
        events: AsyncIterator[_EventT],
        *,
        model: object,
        max_output_tokens: object,
        session_id: object = "",
    ) -> None:
        self._events = events.__aiter__()
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._session_id = session_id

    def __aiter__(self) -> "_ScopedModelToolIterator[_EventT]":
        return self

    async def __anext__(self) -> _EventT:
        token = set_model_tool_context(
            model=self._model,
            max_output_tokens=self._max_output_tokens,
            session_id=self._session_id,
        )
        try:
            return await self._events.__anext__()
        finally:
            reset_model_tool_context(token)

    async def aclose(self) -> None:
        close = getattr(self._events, "aclose", None)
        if callable(close):
            await close()


def scoped_model_tool_context(
    events: AsyncIterator[_EventT],
    *,
    model: object,
    max_output_tokens: object,
    session_id: object = "",
) -> AsyncIterator[_EventT]:
    return _ScopedModelToolIterator(
        events,
        model=model,
        max_output_tokens=max_output_tokens,
        session_id=session_id,
    )


__all__ = [
    "ModelToolContext",
    "current_model_tool_context",
    "reset_model_tool_context",
    "scoped_model_tool_context",
    "set_model_tool_context",
]

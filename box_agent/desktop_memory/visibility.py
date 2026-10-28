"""D024 qualification derived from actual model input and successful delivery."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Callable
from uuid import uuid4

from ..context_input import DefaultContextEngine, ModelInputGuard, PreparedContext
from ..kernel.context_engine import request_input_tokens
from ..schema import Message
from .transport import MemoryProxyError


@dataclass(frozen=True, slots=True)
class CoreRead:
    path: str
    read_id: str
    content_sha256: str
    model_content: str


VisibilityCallback = Callable[[CoreRead, bool, str], asyncio.Task | None]


class CoreVisibilityGuard(ModelInputGuard):
    """HTTP reads remain pending until the exact full tool reply reached a model."""

    def __init__(self, notify: VisibilityCallback) -> None:
        self._context: DefaultContextEngine | None = None
        self._notify = notify
        self._reads: dict[str, CoreRead] = {}
        self._active: dict[str, asyncio.Task | None] = {}
        self._request = ""

    def bind_context(self, context: DefaultContextEngine) -> None:
        self.reset()
        self._context = context

    def reset(self) -> None:
        self._request = ""
        for path in tuple(self._reads):
            self.forget(path)

    def forget(self, path: str) -> None:
        self._active.pop(path, None)
        read = self._reads.pop(path, None)
        if read is not None:
            self._notify(read, False, self._request)

    def admit(self, read: CoreRead) -> None:
        """Reserve a complete tool result using current schemas and pending calls."""
        self.forget(read.path)
        context = self._context
        if context is None or context.prepared_tools is None:
            self._notify(read, False, self._request)
            raise MemoryProxyError("CORE_READ_REQUIRED")
        history = context._history
        calls = next((message.tool_calls for message in reversed(history)
                      if message.role == "assistant" and message.tool_calls), ())
        committed = {message.tool_call_id for message in history if message.role == "tool"}
        envelopes = [Message(role="tool", name=call.function.name, tool_call_id=call.id, content="")
                     for call in calls if call.id not in committed]
        existing = {message.content for message in history if message.role == "tool" and isinstance(message.content, str)}
        pending = [Message(role="tool", name="memory_read", tool_call_id=item.read_id, content=item.model_content)
                   for item in (*self._reads.values(), read) if item.model_content not in existing]
        followup = ([Message(role="user", source="runtime", content=list(context._pending_followup_blocks))]
                    if context._pending_followup_blocks else [])
        messages = context.project_history([*history, *envelopes, *context._extra_messages, *followup, *pending])
        estimated = request_input_tokens(messages, context.prepared_tools.definitions)
        if estimated + context._transient_reserve_tokens + 1024 > context._token_limit:
            self._notify(read, False, self._request)
            raise MemoryProxyError("CORE_CONTEXT_TOO_LARGE")
        self._reads[read.path] = read

    async def require(self, path: str) -> CoreRead:
        if path not in self._active or path not in self._reads:
            raise MemoryProxyError("CORE_READ_REQUIRED")
        read = self._reads[path]
        pending = self._active[path]
        if pending is not None:
            try:
                await asyncio.shield(pending)
            except Exception:
                self.forget(path)
                raise MemoryProxyError("CORE_READ_REQUIRED") from None
        if path not in self._active or self._reads.get(path) is not read:
            raise MemoryProxyError("CORE_READ_REQUIRED")
        return read

    def prepare_context(self, projection: PreparedContext) -> PreparedContext:
        request_id = self._request = str(uuid4())
        # Revoke previous delivery on every new request, including requests
        # whose compaction or model failure has removed the original full read.
        for path in tuple(self._active):
            self._active.pop(path, None)
            read = self._reads.get(path)
            if read is not None:
                self._notify(read, False, request_id)
        exact = {message.content for message in projection.messages
                 if message.role == "tool" and message.name == "memory_read" and isinstance(message.content, str)}
        visible = tuple(read for read in self._reads.values() if read.model_content in exact)
        if projection.blocked_reason:
            return projection
        committed = False

        def on_committed() -> None:
            nonlocal committed
            if projection.on_committed is not None:
                projection.on_committed()
            committed = self._request == request_id

        def on_response() -> None:
            if projection.on_response is not None:
                projection.on_response()
            if not committed or self._request != request_id:
                return
            for read in visible:
                if self._reads.get(read.path) is read:
                    self._active[read.path] = self._notify(read, True, request_id)

        return replace(projection, on_committed=on_committed, on_response=on_response)

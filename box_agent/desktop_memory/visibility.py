"""D024 qualification derived from actual model input and successful delivery."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import json
import logging
from typing import Callable
from uuid import uuid4

from ..context_input import DefaultContextEngine, ModelInputGuard, PreparedContext
from ..kernel.context_engine import _budget_content_json, _budget_image_tokens, request_input_tokens
from ..memory import MemoryHistory, external_memory_block, is_memory_history_message, project_memory_system
from ..schema import Message
from .transport import MemoryProxyError

_log = logging.getLogger(__name__)


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
            self._notify(read, False, "")

    def consume(self, path: str) -> CoreRead:
        """Consume locally before mutation; Host consumes its grant atomically."""
        if path not in self._active or path not in self._reads:
            raise MemoryProxyError("CORE_READ_REQUIRED")
        self._active.pop(path)
        return self._reads.pop(path)

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
        messages = self.project_messages(messages, tools=context.prepared_tools, token_limit=context._token_limit,
                                         reserve_tokens=context._transient_reserve_tokens)
        estimated = self._input_tokens(messages, context.prepared_tools.definitions)
        if estimated + context._transient_reserve_tokens + 1024 > context._token_limit:
            self._notify(read, False, self._request)
            _log.warning("memory operation=core_read reason=CORE_CONTEXT_TOO_LARGE estimated=%d limit=%d reserve=%d",
                         estimated, context._token_limit, context._transient_reserve_tokens + 1024)
            raise MemoryProxyError("CORE_CONTEXT_TOO_LARGE")
        self._reads[read.path] = read

    def _input_tokens(self, messages, definitions) -> int:
        return request_input_tokens(messages, definitions)

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
        for path, read in tuple(self._reads.items()):
            if read.model_content not in exact:
                self.forget(path)
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


class DesktopInputGuard(CoreVisibilityGuard):
    """Bound memory input and request recovery only after a failed core read."""

    def __init__(self, notify: VisibilityCallback, content_builder: Callable[[int], str], *,
                 history: MemoryHistory | None = None, can_read: Callable[[], bool] = lambda: True) -> None:
        super().__init__(notify)
        self._content_builder = content_builder
        self._memory_history = history
        self._can_read = can_read
        self._read_recovery_pending = False
        self._read_recovery_attempted = False

    def reset(self) -> None:
        super().reset()
        self._read_recovery_pending = self._read_recovery_attempted = False

    def admit(self, read: CoreRead) -> None:
        try:
            super().admit(read)
        except MemoryProxyError as exc:
            if exc.code == "CORE_CONTEXT_TOO_LARGE" and not self._read_recovery_attempted:
                self._read_recovery_pending = True
            raise

    def _input_tokens(self, messages, definitions) -> int:
        # Nova and other hosted aliases do not expose a verified tokenizer.
        # Byte fallback is conservative for UTF-8/byte-fallback tokenizers and
        # avoids treating Chinese or emoji as chars/4. Never claim exact counts.
        # Connector recovery already projects the visible snapshot into content;
        # its per-turn persistence metadata is never sent to the provider.
        serialized = [message.model_dump(exclude_none=True, exclude={"host_connector_status"})
                      for message in messages]
        for message, data in zip(messages, serialized):
            if isinstance(message.content, list):
                data["content"] = json.loads(_budget_content_json(message.content))
        payload = {"messages": serialized,
                   "tools": [tool.to_openai_schema() for tool in definitions]}
        byte_bound = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        byte_bound += sum(_budget_image_tokens(message.content) for message in messages)
        return max(request_input_tokens(messages, definitions), byte_bound)

    def prepare_context(self, projection: PreparedContext) -> PreparedContext:
        if self._memory_history is not None:
            # 投影在工作线程，宿主可能已在事件循环撤销这次授权。
            def visible(message: Message) -> bool:
                return not is_memory_history_message(message) or self._memory_history.accepts(message)

            projection = replace(projection, messages=list(filter(visible, projection.messages)),
                                 context_messages=list(filter(visible, projection.context_messages)),
                                 history_updates=tuple(filter(visible, projection.history_updates)))
            if self._memory_history.system_day is not None and not self._memory_history.system_block:
                projection = replace(projection, messages=project_memory_system(projection.messages, ""),
                                     context_messages=project_memory_system(projection.context_messages, ""))
        if self._read_recovery_pending:
            # Consume before the shared loop compacts/reprojects. A byte-bound
            # failure must not become a permanent ban on model-valid input.
            self._read_recovery_pending = False
            self._read_recovery_attempted = True
            if not projection.blocked_reason and self._can_read():
                _log.info("memory operation=core_read reason=compaction_requested")
                projection = replace(
                    projection, budget_blocked=True, on_committed=None, on_response=None,
                    blocked_reason="Core memory read exceeded its admission budget. Compact context before retrying memory_read.",
                )
        return super().prepare_context(projection)

    def project_messages(self, messages, *, tools, token_limit, tail=(), reserve_tokens=0, memory_update=None):
        history = self._memory_history
        if history is None:
            return messages

        def fits(candidate):
            return self._input_tokens([*candidate, *tail], tools.definitions) + reserve_tokens + 1024 <= token_limit

        if history.system_day is not None:
            if history.system_block and not fits(messages):
                _log.info("memory operation=preload reason=system_snapshot_over_budget")
                return project_memory_system(messages, "")
            return messages

        latest = next((message for message in reversed(messages) if history.accepts(message)), None)
        if latest is None:
            return messages
        if fits(messages):
            return messages
        # 预算不足时，只能临时省略已发快照。新快照裁剪后才进入持久历史。
        base = [message for message in messages if not is_memory_history_message(message) or message is memory_update]
        index = next((i for i, message in enumerate(base) if message is memory_update), None)
        best = [message for message in base if message is not memory_update]
        if not fits(best):
            _log.warning("memory operation=preload reason=history_over_budget budget=utf8_upper_bound estimated=%d limit=%d reserve=%d",
                         self._input_tokens([*best, *tail], tools.definitions), token_limit, reserve_tokens + 1024)
            return best
        if index is None:
            latest_only = [message for message in messages if not is_memory_history_message(message) or message is latest]
            if fits(latest_only):
                best = latest_only
        else:
            if fits(base):
                best = base
            elif memory_update.content != history.empty:
                low, high = 1, 15400
                while low <= high:
                    middle = (low + high) // 2
                    block = external_memory_block(self._content_builder(middle), middle)
                    candidate = ([*base[:index], history.message(block, name=memory_update.name), *base[index + 1:]]
                                 if block else None)
                    if candidate is None or fits(candidate):
                        low = middle + 1
                        if candidate is not None:
                            best = candidate
                    else:
                        high = middle - 1
        _log.info("memory operation=preload reason=budget_reduced budget=utf8_upper_bound estimated=%d limit=%d reserve=%d",
                  self._input_tokens([*best, *tail], tools.definitions), token_limit, reserve_tokens + 1024)
        return best

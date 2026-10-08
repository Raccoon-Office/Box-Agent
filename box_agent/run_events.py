"""Bounded host-neutral delivery of ordered run events."""
from __future__ import annotations

import asyncio
from collections import deque
import json
from typing import Any

from .api import EventEnvelope, RunDeliveryOptions


class RunDeliveryError(RuntimeError):
    code = "RUN_EVENT_DELIVERY_FAILED"


class EventTooLargeError(RunDeliveryError):
    code = "RUN_EVENT_TOO_LARGE"


class EventConsumerTimeoutError(RunDeliveryError):
    code = "RUN_EVENT_CONSUMER_TIMEOUT"


class RunEventChannel:
    END = object()

    def __init__(self, options: RunDeliveryOptions | None = None) -> None:
        self.options = options or RunDeliveryOptions()
        self._queue: deque[tuple[EventEnvelope, int]] = deque()
        self._changed = asyncio.Event()
        self._publish_lock = asyncio.Lock()
        self._closed = False
        self._interrupted = False
        self._sequence = 0
        self._pending_bytes = 0

    @property
    def pending_count(self) -> int:
        return len(self._queue)

    @property
    def pending_bytes(self) -> int:
        return self._pending_bytes

    def _check_open(self) -> None:
        if self._closed:
            raise RunDeliveryError("run event channel is closed")

    async def publish(self, run_id: str, payload: Any) -> None:
        async with self._publish_lock:
            self._check_open()
            sequence = self._sequence + 1
            envelope = EventEnvelope(run_id, f"{run_id}:{sequence}", sequence, payload)
            try:
                size = len(json.dumps(envelope.to_dict(), ensure_ascii=False,
                                      separators=(",", ":"), allow_nan=False).encode("utf-8"))
            except (TypeError, ValueError, OverflowError) as exc:
                raise RunDeliveryError("run event cannot be serialized") from exc
            if size > self.options.max_bytes:
                raise EventTooLargeError(
                    f"event payload is {size} bytes; limit is {self.options.max_bytes}"
                )
            if (len(self._queue) >= self.options.max_events
                    or self._pending_bytes + size > self.options.max_bytes):
                try:
                    await asyncio.wait_for(self._wait_for_capacity(size),
                                           self.options.congestion_timeout_seconds)
                except asyncio.TimeoutError as exc:
                    raise EventConsumerTimeoutError("run event consumer remained congested") from exc
            self._check_open()
            self._queue.append((envelope, size))
            self._pending_bytes += size
            self._sequence = sequence
            self._changed.set()

    async def _wait_for_capacity(self, size: int) -> None:
        while True:
            if self._interrupted:
                raise asyncio.CancelledError
            self._check_open()
            if (len(self._queue) <= self.options.max_events // 2
                    and self._pending_bytes <= self.options.max_bytes // 2
                    and len(self._queue) < self.options.max_events
                    and self._pending_bytes + size <= self.options.max_bytes):
                return
            self._changed.clear()
            await self._changed.wait()

    async def get(self) -> Any:
        while not self._queue:
            if self._closed:
                return self.END
            self._changed.clear()
            await self._changed.wait()
        envelope, size = self._queue.popleft()
        self._pending_bytes -= size
        self._changed.set()
        return envelope

    def interrupt(self) -> None:
        """Wake blocked publishers without spending queue capacity."""
        self._interrupted = True
        self._changed.set()

    def close(self) -> None:
        self._closed = True
        self._changed.set()

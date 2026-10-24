"""Keep synchronous desktop work serialized even when its waiter disappears."""
from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
import threading
from typing import Any, Callable


class SerialCoordinator:
    """One worker owns calls, invalidation and cleanup for one CUA service.

    Cancelling a queued future removes that call. Cancelling a running call
    cannot stop desktop effects; the worker retains ownership until it settles.
    Its observations are invalidated before another call can use them. This
    serializes this service only, not other Driver clients or the real user.
    """

    def __init__(self, call: Callable[..., Any], *, invalidate: Callable[[], None],
                 close: Callable[[], None]) -> None:
        self._call = call
        self._invalidate = invalidate
        self._close = close
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cua-owner")
        self._abandoned = threading.Event()
        self._pending: set[Future] = set()
        self._pending_lock = threading.Lock()
        self._close_future: Future | None = None

    def _recover(self) -> None:
        if self._abandoned.is_set():
            self._abandoned.clear()
            try:
                self._invalidate()
            except BaseException:
                # A failed cleanup must not turn the next request into a write.
                self._abandoned.set()
                raise

    def _execute(self, name: str, arguments: dict[str, Any]) -> Any:
        self._recover()
        try:
            return self._call(name, arguments)
        finally:
            self._recover()

    def _forget(self, future: Future) -> None:
        with self._pending_lock:
            self._pending.discard(future)

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        if self._close_future is not None:
            raise RuntimeError("CUA coordinator is closing")
        future = self._pool.submit(self._execute, name, arguments)
        with self._pending_lock:
            self._pending.add(future)
        future.add_done_callback(self._forget)
        # Shield the wrapper so we, rather than asyncio's delayed cancellation
        # callback, decide whether the queued operation can still be withdrawn.
        wrapped = asyncio.wrap_future(future)
        wrapped.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        try:
            return await asyncio.shield(wrapped)
        except asyncio.CancelledError:
            if not future.cancel():
                self._abandoned.set()
            raise

    def _begin_close(self) -> Future:
        if self._close_future is None:
            with self._pending_lock:
                pending = list(self._pending)
            for future in pending:
                future.cancel()
            # Runs after any in-flight operation. No close/call race on service.
            self._close_future = self._pool.submit(self._close)
            self._pool.shutdown(wait=False)
        return self._close_future

    async def aclose(self) -> None:
        await asyncio.shield(asyncio.wrap_future(self._begin_close()))

    def close(self) -> None:
        """Drain outside the event loop, including after loop shutdown."""
        try:
            self._begin_close().result()
        finally:
            self._pool.shutdown(wait=True)

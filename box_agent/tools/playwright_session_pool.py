"""Per-session MCP clients for the managed Playwright browser.

``@playwright/mcp`` in HTTP mode with ``--isolated`` hands every connected MCP
client its own ``BrowserContext`` on one shared Chromium process. This pool
keeps one such client per *browser session key* (an ACP session id, or
``"cli"``), so concurrent local-agent sessions stop fighting over tabs and
cookies.

Lifecycle:

* clients are created lazily on the first Playwright tool call of a session
  (``initialize`` / ``list_tools`` do not launch a browser upstream);
* at most ``max_clients`` live at once — the least recently used idle client
  is closed to make room, and if none is idle the caller gets
  :class:`BrowserSessionLimitError` after a short wait;
* clients idle for ``idle_timeout`` seconds are closed by a background reaper;
* ``invalidate()`` bumps the generation so that clients bound to a previous
  server process are dropped and rebuilt on next use (hot reconnect).

Closing a client first closes its tabs: upstream ``factory.disposed`` returns
early while other clients are alive and never calls ``BrowserContext.close()``
(``program.js`` 88-98), so without this a headed session would leave orphan
windows behind.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from .browser_runtime_scope import current_browser_session_key

DEFAULT_SESSION_KEY = "__default__"

# ``session_factory(exit_stack)`` opens the transport + ClientSession on the
# given stack, runs ``initialize()`` and returns the ready session.
SessionFactory = Callable[[AsyncExitStack], Awaitable[Any]]

_MAX_TABS_TO_CLOSE = 32
_TAB_CLEANUP_TIMEOUT = 15.0
_WARM_UP_TIMEOUT = 60.0  # first call may launch Chromium


async def close_all_tabs(session: Any, *, timeout: float = _TAB_CLEANUP_TIMEOUT) -> int:
    """Close the calling client's tabs one by one via ``browser_tabs``.

    Upstream never calls ``BrowserContext.close()`` for a client while other
    clients share the browser, and ``Context.dispose`` does not close pages, so
    ``browser_close`` / disconnect would otherwise leave orphan windows. Stops
    at the first ``isError`` (no tab left). Returns the number of tabs closed.
    """
    closed = 0

    async def _close_until_empty() -> None:
        nonlocal closed
        for _ in range(_MAX_TABS_TO_CLOSE):
            result = await session.call_tool("browser_tabs", arguments={"action": "close"})
            if getattr(result, "isError", False):
                break
            closed += 1

    await asyncio.wait_for(_close_until_empty(), timeout=timeout)
    return closed


class BrowserSessionLimitError(RuntimeError):
    """Raised when every pooled browser context is busy and none can be evicted."""

    def __init__(self, max_clients: int):
        self.max_clients = max_clients
        super().__init__(
            f"BROWSER_SESSION_LIMIT: at most {max_clients} agent sessions may use the "
            "managed browser at the same time. Wait for another session to finish "
            "or use a non-browser fallback."
        )


@dataclass
class _PooledClient:
    key: str
    session: Any
    generation: int
    created_at: float
    last_used: float
    # The transport context (anyio task group / cancel scope inside the MCP
    # SDK) must be exited by the same task that entered it. Each client is
    # therefore owned by a dedicated runner task that holds the AsyncExitStack
    # open until ``close_requested`` is set, then unwinds it in place.
    runner: asyncio.Task | None = None
    close_requested: asyncio.Event = field(default_factory=asyncio.Event)
    in_use: int = 0
    calls: int = 0
    closing: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


_CLIENT_CLOSE_TIMEOUT = 10.0


class _SuppressClosedStreamNoise(logging.Filter):
    """Drop the SDK's 'Error parsing SSE message' traceback emitted on client close.

    ``mcp.client.streamable_http`` logs at ERROR with a full traceback when the
    server's GET/SSE stream delivers a message after our read stream was closed
    (``anyio.BrokenResourceError``). For a client we are deliberately closing
    that is expected, not an error; everything else passes through untouched.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info and record.exc_info[0] is not None:
            exc_type = record.exc_info[0]
            if exc_type.__name__ == "BrokenResourceError" and "SSE" in record.getMessage():
                return False
        return True


def _install_sdk_log_filter() -> None:
    sdk_logger = logging.getLogger("mcp.client.streamable_http")
    if not any(isinstance(f, _SuppressClosedStreamNoise) for f in sdk_logger.filters):
        sdk_logger.addFilter(_SuppressClosedStreamNoise())


async def _run_client(
    factory: SessionFactory,
    ready: "asyncio.Future[Any]",
    close_requested: asyncio.Event,
) -> None:
    """Own one MCP client for its whole lifetime inside a single task."""
    exit_stack = AsyncExitStack()
    try:
        async with exit_stack:
            session = await factory(exit_stack)
            if not ready.done():
                ready.set_result(session)
            await close_requested.wait()
    except BaseException as error:  # noqa: BLE001
        if not ready.done():
            ready.set_exception(error)
            return
        # Teardown noise (BrokenResourceError, closed SSE streams, cancel
        # scope groups) is not actionable once the client is being closed.
        if isinstance(error, asyncio.CancelledError):
            raise


class PlaywrightSessionPool:
    """One MCP client (and therefore one BrowserContext) per browser session key."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        max_clients: int = 4,
        idle_timeout: float = 1800.0,
        wait_timeout: float = 5.0,
        time_fn: Callable[[], float] = time.monotonic,
    ):
        if max_clients < 1:
            raise ValueError("max_clients must be >= 1")
        self._factory = session_factory
        self._max_clients = max_clients
        self._idle_timeout = idle_timeout
        self._wait_timeout = wait_timeout
        self._time = time_fn
        self._clients: dict[str, _PooledClient] = {}
        self._generation = 0
        self._lock: asyncio.Lock | None = None
        self._condition: asyncio.Condition | None = None
        self._first_call_lock_obj: asyncio.Lock | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._reaper: asyncio.Task | None = None
        self._closed = False
        _install_sdk_log_filter()

    # ------------------------------------------------------------------ props
    @property
    def max_clients(self) -> int:
        return self._max_clients

    @property
    def generation(self) -> int:
        return self._generation

    def active_keys(self) -> list[str]:
        return [key for key, client in self._clients.items() if not client.closing]

    def snapshot(self) -> dict[str, Any]:
        now = self._time()
        return {
            "mode": "per_session_context",
            "maxClients": self._max_clients,
            "idleTimeout": self._idle_timeout,
            "generation": self._generation,
            "activeClients": len(self.active_keys()),
            "clients": [
                {
                    "key": client.key,
                    "inUse": client.in_use,
                    "calls": client.calls,
                    "idleSeconds": round(max(0.0, now - client.last_used), 1),
                }
                for client in self._clients.values()
                if not client.closing
            ],
        }

    # --------------------------------------------------------------- internal
    def _sync(self) -> asyncio.Condition:
        loop = asyncio.get_running_loop()
        if self._condition is None or self._loop is not loop:
            self._lock = asyncio.Lock()
            self._condition = asyncio.Condition(self._lock)
            self._first_call_lock_obj = asyncio.Lock()
            self._loop = loop
        return self._condition

    @property
    def _first_call_lock(self) -> asyncio.Lock:
        self._sync()
        return self._first_call_lock_obj

    @staticmethod
    def _resolve_key() -> str:
        return current_browser_session_key() or DEFAULT_SESSION_KEY

    def _live(self, key: str) -> _PooledClient | None:
        client = self._clients.get(key)
        if client is None or client.closing or client.generation != self._generation:
            return None
        return client

    def _evictable(self) -> _PooledClient | None:
        candidates = [
            c for c in self._clients.values() if c.in_use == 0 and not c.closing
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda c: c.last_used)

    async def _create(self, key: str) -> _PooledClient:
        condition = self._sync()
        async with condition:
            # Another task may have created it while we waited for the lock.
            live = self._live(key)
            if live is not None:
                return live

            # Drop a stale-generation client under the same key.
            stale = self._clients.get(key)
            if stale is not None and not stale.closing:
                await self._close_client(stale, reason="stale-generation")

            # Make room.
            deadline = self._time() + self._wait_timeout
            while len(self.active_keys()) >= self._max_clients:
                victim = self._evictable()
                if victim is not None:
                    await self._close_client(victim, reason="lru-evict")
                    continue
                remaining = deadline - self._time()
                if remaining <= 0:
                    raise BrowserSessionLimitError(self._max_clients)
                try:
                    await asyncio.wait_for(condition.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    raise BrowserSessionLimitError(self._max_clients) from None

            generation = self._generation
            loop = asyncio.get_running_loop()
            ready: asyncio.Future[Any] = loop.create_future()
            close_requested = asyncio.Event()
            runner = asyncio.create_task(
                _run_client(self._factory, ready, close_requested),
                name=f"playwright-session-client-{key}",
            )
            try:
                session = await ready
            except BaseException:
                close_requested.set()
                runner.cancel()
                raise
            now = self._time()
            client = _PooledClient(
                key=key,
                session=session,
                generation=generation,
                created_at=now,
                last_used=now,
                runner=runner,
                close_requested=close_requested,
            )
            self._clients[key] = client
            self._ensure_reaper()
            return client

    async def _close_tabs(self, client: _PooledClient) -> None:
        """Best-effort: close every tab so the upstream context leaves no windows."""
        if client.calls == 0:
            # No tool call happened → upstream never created a backend/browser
            # for this client. Calling browser_tabs now would *launch* one.
            return
        try:
            await close_all_tabs(client.session)
        except Exception as error:  # noqa: BLE001
            sys.stderr.write(
                f"[browser] tab cleanup skipped for session {client.key!r}: {error}\n"
            )

    async def _close_client(self, client: _PooledClient, *, reason: str) -> None:
        if client.closing:
            return
        client.closing = True
        self._clients.pop(client.key, None)
        await self._close_tabs(client)
        client.close_requested.set()
        if client.runner is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(client.runner), timeout=_CLIENT_CLOSE_TIMEOUT
                )
            except asyncio.TimeoutError:
                client.runner.cancel()
            except BaseException as error:  # noqa: BLE001
                if isinstance(error, asyncio.CancelledError):
                    raise
                # Transport teardown noise; the runner already swallowed what
                # it could and the process-level cleanup will reap the rest.
        sys.stderr.write(
            f"[browser] closed managed browser context for session {client.key!r} "
            f"({reason})\n"
        )
        if self._condition is not None:
            self._condition.notify_all()

    def _ensure_reaper(self) -> None:
        if self._idle_timeout <= 0 or self._closed:
            return
        if self._reaper is not None and not self._reaper.done():
            return
        self._reaper = asyncio.create_task(
            self._reap_idle_loop(), name="playwright-session-pool-reaper"
        )

    async def _reap_idle_loop(self) -> None:
        interval = max(1.0, min(60.0, self._idle_timeout / 4))
        try:
            while not self._closed and self._clients:
                await asyncio.sleep(interval)
                await self.reap_idle()
        except asyncio.CancelledError:
            pass

    async def _warm_up(self, key: str, client: _PooledClient) -> _PooledClient:
        """Create the client's browser backend under a lock, one client at a time.

        Upstream builds a client's backend lazily on its FIRST tool call, and
        its shared-browser bookkeeping races: ``create()`` awaits
        ``createBrowser()`` before incrementing ``clientCount``, so N new
        clients whose first calls overlap launch N Chromium processes instead
        of sharing one. A cheap ``browser_tabs list`` issued here is that first
        call; serializing only this warm-up (not the caller's real call) keeps
        concurrent sessions on one browser without making them wait on each
        other's navigations. Failures are ignored — the real call reports them.
        """
        async with self._first_call_lock:
            live = self._live(key)
            if live is None:
                client = await self._create(key)
            else:
                client = live
            if client.calls > 0:
                return client
            client.in_use += 1
            try:
                await asyncio.wait_for(
                    client.session.call_tool("browser_tabs", arguments={"action": "list"}),
                    timeout=_WARM_UP_TIMEOUT,
                )
                client.calls += 1
            except Exception as error:  # noqa: BLE001
                sys.stderr.write(
                    f"[browser] warm-up for session {client.key!r} failed: {error}\n"
                )
            finally:
                client.in_use -= 1
                client.last_used = self._time()
            return client

    # ----------------------------------------------------------------- public
    async def reap_idle(self) -> list[str]:
        """Close clients idle for longer than ``idle_timeout``. Returns closed keys."""
        if self._idle_timeout <= 0:
            return []
        now = self._time()
        closed: list[str] = []
        condition = self._sync()
        async with condition:
            for client in list(self._clients.values()):
                if client.in_use or client.closing:
                    continue
                if now - client.last_used >= self._idle_timeout:
                    await self._close_client(client, reason="idle-timeout")
                    closed.append(client.key)
        return closed

    async def resolve(self, key: str | None = None) -> Any:
        """Return the ClientSession for ``key`` (default: current session key)."""
        key = key or self._resolve_key()
        live = self._live(key)
        if live is not None:
            return live.session
        return (await self._create(key)).session

    @asynccontextmanager
    async def lease(self, key: str | None = None) -> AsyncIterator[Any]:
        """Yield the session for the current browser session key, counting it in-use."""
        if self._closed:
            raise RuntimeError("PlaywrightSessionPool is closed")
        key = key or self._resolve_key()
        client = self._live(key)
        if client is None:
            client = await self._create(key)
        if client.calls == 0:
            client = await self._warm_up(key, client)
        # No await between the lookup above and this increment, so an eviction
        # cannot slip in between (eviction only picks in_use == 0).
        client.in_use += 1
        client.calls += 1
        client.last_used = self._time()
        try:
            yield client.session
        finally:
            client.in_use -= 1
            client.last_used = self._time()
            if client.in_use == 0 and self._condition is not None:
                async with self._condition:
                    self._condition.notify_all()

    async def close(self, key: str) -> bool:
        """Close the client for ``key`` if present. Returns True when one was closed."""
        client = self._clients.get(key)
        if client is None or client.closing:
            return False
        condition = self._sync()
        async with condition:
            client = self._clients.get(key)
            if client is None or client.closing:
                return False
            await self._close_client(client, reason="session-closed")
        return True

    def invalidate(self) -> None:
        """Mark all current clients stale; they are rebuilt lazily on next use."""
        self._generation += 1

    async def close_all(self, *, final: bool = False) -> None:
        """Close every client (and stop the reaper when ``final``)."""
        if final:
            self._closed = True
        if self._reaper is not None:
            self._reaper.cancel()
            self._reaper = None
        condition = self._sync()
        async with condition:
            for client in list(self._clients.values()):
                await self._close_client(client, reason="pool-shutdown")
        self._generation += 1

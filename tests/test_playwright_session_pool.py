from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass, field

import pytest

from box_agent.tools.browser_runtime_scope import (
    reset_browser_session_key,
    set_browser_session_key,
)
from box_agent.tools.playwright_session_pool import (
    DEFAULT_SESSION_KEY,
    BrowserSessionLimitError,
    PlaywrightSessionPool,
)


@dataclass
class _Result:
    isError: bool = False
    content: list = field(default_factory=list)


class FakeSession:
    def __init__(self, name: str, tabs: int = 0):
        self.name = name
        self.tabs = tabs
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    async def call_tool(self, name: str, arguments: dict | None = None):
        self.calls.append((name, dict(arguments or {})))
        if name == "browser_tabs" and (arguments or {}).get("action") == "close":
            if self.tabs <= 0:
                return _Result(isError=True)
            self.tabs -= 1
            return _Result()
        return _Result()


class FakeFactory:
    def __init__(self, tabs_per_session: int = 0):
        self.created: list[FakeSession] = []
        self.tabs_per_session = tabs_per_session

    async def __call__(self, exit_stack: AsyncExitStack) -> FakeSession:
        session = FakeSession(f"s{len(self.created)}", tabs=self.tabs_per_session)
        self.created.append(session)

        async def _close() -> None:
            session.closed = True

        exit_stack.push_async_callback(_close)
        return session


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_distinct_session_keys_get_distinct_clients_and_same_key_reuses():
    factory = FakeFactory()
    pool = PlaywrightSessionPool(factory, max_clients=4)

    a1 = await pool.resolve("A")
    b1 = await pool.resolve("B")
    a2 = await pool.resolve("A")

    assert a1 is a2
    assert a1 is not b1
    assert len(factory.created) == 2
    assert sorted(pool.active_keys()) == ["A", "B"]


async def test_lease_reads_current_browser_session_key_from_contextvar():
    factory = FakeFactory()
    pool = PlaywrightSessionPool(factory, max_clients=4)

    token = set_browser_session_key("sess-1")
    try:
        async with pool.lease() as session:
            assert session is factory.created[0]
    finally:
        reset_browser_session_key(token)

    async with pool.lease() as default_session:
        assert default_session is factory.created[1]
    assert sorted(pool.active_keys()) == [DEFAULT_SESSION_KEY, "sess-1"]


async def test_lru_eviction_only_targets_idle_clients():
    factory = FakeFactory()
    clock = Clock()
    pool = PlaywrightSessionPool(factory, max_clients=2, time_fn=clock)

    await pool.resolve("old")
    clock.now += 10
    await pool.resolve("busy")

    # "old" is idle and least recently used; "busy" is in use → "old" is evicted.
    async with pool.lease("busy"):
        clock.now += 10
        async with pool.lease("new"):
            pass

    assert sorted(pool.active_keys()) == ["busy", "new"]
    assert factory.created[0].closed is True  # old
    assert factory.created[1].closed is False  # busy


async def test_limit_raises_when_every_client_is_in_use():
    factory = FakeFactory()
    pool = PlaywrightSessionPool(factory, max_clients=1, wait_timeout=0.05)

    async with pool.lease("A"):
        with pytest.raises(BrowserSessionLimitError) as excinfo:
            async with pool.lease("B"):
                pass
    assert "BROWSER_SESSION_LIMIT" in str(excinfo.value)
    assert pool.active_keys() == ["A"]


async def test_limit_waits_for_a_release_before_giving_up():
    factory = FakeFactory()
    pool = PlaywrightSessionPool(factory, max_clients=1, wait_timeout=1.0)

    release = asyncio.Event()

    async def hold_a() -> None:
        async with pool.lease("A"):
            await release.wait()

    holder = asyncio.create_task(hold_a())
    await asyncio.sleep(0)

    async def want_b() -> str:
        async with pool.lease("B") as session:
            return session.name

    waiter = asyncio.create_task(want_b())
    await asyncio.sleep(0.02)
    assert waiter.done() is False
    release.set()
    assert await asyncio.wait_for(waiter, timeout=1.0) == "s1"
    await holder


async def test_close_closes_tabs_before_disconnecting_when_client_was_used():
    factory = FakeFactory(tabs_per_session=2)
    pool = PlaywrightSessionPool(factory, max_clients=4)

    async with pool.lease("A") as session:
        pass
    assert await pool.close("A") is True

    close_calls = [
        c for c in session.calls if c == ("browser_tabs", {"action": "close"})
    ]
    # two tabs closed + one final call that reports "no tab" (isError)
    assert len(close_calls) == 3
    assert session.tabs == 0
    assert session.closed is True
    assert pool.active_keys() == []
    assert await pool.close("A") is False


async def test_close_skips_tab_cleanup_for_never_used_client():
    factory = FakeFactory(tabs_per_session=2)
    pool = PlaywrightSessionPool(factory, max_clients=4)

    session = await pool.resolve("A")  # created but no tool call yet
    await pool.close("A")

    assert session.calls == []  # browser_tabs would launch a browser upstream
    assert session.closed is True


async def test_invalidate_rebuilds_client_on_next_use():
    factory = FakeFactory()
    pool = PlaywrightSessionPool(factory, max_clients=4)

    first = await pool.resolve("A")
    pool.invalidate()
    second = await pool.resolve("A")

    assert first is not second
    assert first.closed is True
    assert len(factory.created) == 2


async def test_reap_idle_closes_only_idle_clients_past_timeout():
    factory = FakeFactory()
    clock = Clock()
    pool = PlaywrightSessionPool(factory, max_clients=4, idle_timeout=100, time_fn=clock)

    await pool.resolve("stale")
    await pool.resolve("fresh")
    clock.now += 150
    async with pool.lease("fresh"):
        closed = await pool.reap_idle()

    assert closed == ["stale"]
    assert pool.active_keys() == ["fresh"]
    await pool.close_all(final=True)


async def test_first_call_warm_ups_are_serialized_but_real_calls_are_not():
    """Upstream forks a Chromium per client when first calls overlap; the pool
    issues one serialized ``browser_tabs list`` warm-up per new client and lets
    the callers' real calls run concurrently afterwards."""
    active_warmups = 0
    max_concurrent_warmups = 0

    class SlowWarmupSession(FakeSession):
        async def call_tool(self, name, arguments=None):
            nonlocal active_warmups, max_concurrent_warmups
            if name == "browser_tabs" and (arguments or {}).get("action") == "list":
                active_warmups += 1
                max_concurrent_warmups = max(max_concurrent_warmups, active_warmups)
                await asyncio.sleep(0.02)
                active_warmups -= 1
            return await super().call_tool(name, arguments)

    class SlowFactory(FakeFactory):
        async def __call__(self, exit_stack):
            session = SlowWarmupSession(f"s{len(self.created)}")
            self.created.append(session)
            return session

    pool = PlaywrightSessionPool(SlowFactory(), max_clients=4)
    active_real = 0
    max_concurrent_real = 0

    async def use(key: str) -> None:
        nonlocal active_real, max_concurrent_real
        async with pool.lease(key) as session:
            active_real += 1
            max_concurrent_real = max(max_concurrent_real, active_real)
            await asyncio.sleep(0.15)  # longer than all three warm-ups combined
            await session.call_tool("browser_navigate", {"url": "https://x"})
            active_real -= 1

    await asyncio.gather(use("A"), use("B"), use("C"))

    assert max_concurrent_warmups == 1
    assert max_concurrent_real >= 2
    for key in ("A", "B", "C"):
        session = await pool.resolve(key)
        assert [c[0] for c in session.calls] == ["browser_tabs", "browser_navigate"]

    # A second lease on a warmed client issues no further warm-up.
    async with pool.lease("A") as session:
        pass
    assert [c[0] for c in session.calls] == ["browser_tabs", "browser_navigate"]


async def test_close_all_closes_everything_and_reports_snapshot():
    factory = FakeFactory()
    pool = PlaywrightSessionPool(factory, max_clients=4)
    await pool.resolve("A")
    await pool.resolve("B")

    snapshot = pool.snapshot()
    assert snapshot["mode"] == "per_session_context"
    assert snapshot["activeClients"] == 2
    assert sorted(c["key"] for c in snapshot["clients"]) == ["A", "B"]

    await pool.close_all(final=True)
    assert pool.active_keys() == []
    assert all(s.closed for s in factory.created)
    with pytest.raises(RuntimeError):
        async with pool.lease("C"):
            pass


async def test_cancelling_tab_cleanup_still_settles_the_client_runner(monkeypatch):
    factory = FakeFactory(tabs_per_session=1)
    pool = PlaywrightSessionPool(factory, idle_timeout=0)
    async with pool.lease("A") as session:
        pass
    cleaning = asyncio.Event()

    async def stalled_cleanup(client):
        cleaning.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(pool, "_close_tabs", stalled_cleanup)
    runner = pool._clients["A"].runner
    closer = asyncio.create_task(pool.close("A"))
    await cleaning.wait()
    closer.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await closer
        assert session.closed
        assert runner.done()
        assert pool.active_keys() == []
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


async def test_final_shutdown_prevents_resolve_and_waiting_leases_from_reopening():
    factory = FakeFactory()
    pool = PlaywrightSessionPool(factory, max_clients=1, idle_timeout=0)
    async with pool.lease("A"):
        waiter = asyncio.create_task(pool.resolve("B"))
        await asyncio.sleep(0)
        await pool.close_all(final=True)
        try:
            with pytest.raises(RuntimeError, match="closed"):
                await waiter
            with pytest.raises(RuntimeError, match="closed"):
                await pool.resolve("C")
        finally:
            await pool.close_all(final=True)


async def test_cancelled_warmup_still_cleans_tabs_on_close(monkeypatch):
    factory = FakeFactory(tabs_per_session=1)
    pool = PlaywrightSessionPool(factory, idle_timeout=0)
    session = await pool.resolve("A")
    warmup = asyncio.Event()
    original = session.call_tool

    async def stall(name, arguments):
        if arguments.get("action") == "list":
            warmup.set()
            await asyncio.Event().wait()
        return await original(name, arguments)

    monkeypatch.setattr(session, "call_tool", stall)

    async def use():
        async with pool.lease("A"):
            pass

    task = asyncio.create_task(use())
    await warmup.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await pool.close_all(final=True)
    assert session.closed
    assert session.tabs == 0


async def test_cancelled_shutdown_closes_all_clients(monkeypatch):
    factory = FakeFactory()
    pool = PlaywrightSessionPool(factory, idle_timeout=0)
    await pool.resolve("A")
    await pool.resolve("B")
    closing = asyncio.Event()
    release = asyncio.Event()

    async def delayed_tabs(client):
        closing.set()
        await release.wait()

    monkeypatch.setattr(pool, "_close_tabs", delayed_tabs)
    task = asyncio.create_task(pool.close_all(final=True))
    await closing.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(session.closed for session in factory.created)
    assert pool.active_keys() == []

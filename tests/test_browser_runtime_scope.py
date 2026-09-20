from __future__ import annotations

import asyncio

import pytest

from box_agent.tools.browser_runtime_scope import (
    BrowserRuntimeCoordinator,
    current_browser_session_key,
    reset_browser_session_key,
    set_browser_session_key,
)


@pytest.mark.asyncio
async def test_browser_session_key_is_inherited_by_child_tasks_and_reset_after():
    assert current_browser_session_key() is None
    token = set_browser_session_key("session-a")
    try:
        assert current_browser_session_key() == "session-a"

        async def child() -> str | None:
            # Sub-agents run in child tasks; they must land in the parent's context.
            return current_browser_session_key()

        assert await asyncio.create_task(child()) == "session-a"
    finally:
        reset_browser_session_key(token)
    assert current_browser_session_key() is None


@pytest.mark.asyncio
async def test_browser_session_keys_are_isolated_between_concurrent_tasks():
    seen: dict[str, str | None] = {}

    async def turn(key: str) -> None:
        token = set_browser_session_key(key)
        try:
            await asyncio.sleep(0)
            seen[key] = current_browser_session_key()
        finally:
            reset_browser_session_key(token)

    await asyncio.gather(turn("A"), turn("B"))
    assert seen == {"A": "A", "B": "B"}


@pytest.mark.asyncio
async def test_browser_runtime_serializes_distinct_turns_but_reenters_same_turn():
    await BrowserRuntimeCoordinator.acquire("session-a:turn-1")

    # Parallel browser calls from one turn share the lease instead of
    # deadlocking behind themselves.
    await asyncio.wait_for(
        BrowserRuntimeCoordinator.acquire("session-a:turn-1"), timeout=0.1
    )

    waiting = asyncio.create_task(
        BrowserRuntimeCoordinator.acquire("session-b:turn-1")
    )
    await asyncio.sleep(0)
    assert waiting.done() is False

    await BrowserRuntimeCoordinator.release("session-a:turn-1")
    await asyncio.wait_for(waiting, timeout=0.5)
    await BrowserRuntimeCoordinator.release("session-b:turn-1")

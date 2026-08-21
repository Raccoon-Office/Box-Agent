import asyncio

import pytest

import box_agent.core as core
from box_agent.schema import StreamEvent


@pytest.mark.asyncio
async def test_stream_wrapper_emits_activity_while_provider_waits(monkeypatch):
    monkeypatch.setattr(core, "LLM_ACTIVITY_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(core, "LLM_PROVIDER_STALE_SECONDS", 1.0)

    async def slow_stream():
        await asyncio.sleep(0.025)
        yield StreamEvent(type="text", delta="ok")
        yield StreamEvent(type="finish", finish_reason="stop")

    events = [event async for event in core._stream_with_activity(slow_stream())]

    activity = [event for event in events if event.type == "activity"]
    assert activity
    assert activity[0].activity["protocol"] == "agent_activity_v1"
    assert activity[0].activity["phase"] == "provider_wait"
    assert any(event.type == "text" for event in events)


@pytest.mark.asyncio
async def test_stream_wrapper_stops_stale_provider(monkeypatch):
    monkeypatch.setattr(core, "LLM_ACTIVITY_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(core, "LLM_PROVIDER_STALE_SECONDS", 0.025)

    async def stuck_stream():
        await asyncio.sleep(10)
        yield StreamEvent(type="text", delta="late")

    events = [event async for event in core._stream_with_activity(stuck_stream())]

    assert events[-1].type == "finish"
    assert events[-1].finish_reason == "provider_stale"
    assert not any(event.type == "text" for event in events)


@pytest.mark.asyncio
async def test_provider_stream_liveness_prevents_false_stale(monkeypatch):
    monkeypatch.setattr(core, "LLM_ACTIVITY_INTERVAL_SECONDS", 0.005)
    monkeypatch.setattr(core, "LLM_PROVIDER_STALE_SECONDS", 0.025)

    async def slow_tool_arguments():
        for _ in range(4):
            await asyncio.sleep(0.015)
            yield StreamEvent(
                type="activity",
                activity={
                    "protocol": "agent_activity_v1",
                    "phase": "provider_stream",
                },
            )
        yield StreamEvent(type="finish", finish_reason="stop")

    events = [
        event async for event in core._stream_with_activity(slow_tool_arguments())
    ]

    assert events[-1].finish_reason == "stop"
    assert not any(event.finish_reason == "provider_stale" for event in events)


def test_resolve_provider_stale_seconds_precedence(monkeypatch):
    monkeypatch.delenv("BOX_AGENT_PROVIDER_STALE_SECONDS", raising=False)
    # default
    assert core._resolve_provider_stale_seconds() == core.LLM_PROVIDER_STALE_SECONDS
    # configured value used when no env
    assert core._resolve_provider_stale_seconds(350) == 350.0
    # non-positive / bad configured value falls back to default
    assert core._resolve_provider_stale_seconds(0) == core.LLM_PROVIDER_STALE_SECONDS
    assert core._resolve_provider_stale_seconds(-5) == core.LLM_PROVIDER_STALE_SECONDS
    # env overrides configured value
    monkeypatch.setenv("BOX_AGENT_PROVIDER_STALE_SECONDS", "500")
    assert core._resolve_provider_stale_seconds(350) == 500.0
    # unparseable env ignored -> falls back to configured value
    monkeypatch.setenv("BOX_AGENT_PROVIDER_STALE_SECONDS", "junk")
    assert core._resolve_provider_stale_seconds(350) == 350.0
    # non-positive env ignored
    monkeypatch.setenv("BOX_AGENT_PROVIDER_STALE_SECONDS", "0")
    assert core._resolve_provider_stale_seconds(350) == 350.0


@pytest.mark.asyncio
async def test_stream_wrapper_honors_explicit_stale_seconds(monkeypatch):
    monkeypatch.setattr(core, "LLM_ACTIVITY_INTERVAL_SECONDS", 0.01)
    # Module default is generous; the explicit per-turn value is what bites.
    monkeypatch.setattr(core, "LLM_PROVIDER_STALE_SECONDS", 100.0)

    async def stuck_stream():
        await asyncio.sleep(10)
        yield StreamEvent(type="text", delta="late")

    events = [
        event
        async for event in core._stream_with_activity(
            stuck_stream(), stale_seconds=0.025
        )
    ]

    assert events[-1].type == "finish"
    assert events[-1].finish_reason == "provider_stale"
    assert not any(event.type == "text" for event in events)

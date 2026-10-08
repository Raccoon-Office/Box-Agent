import asyncio
import json
from types import SimpleNamespace

import pytest

from box_agent.api import ControlCommand, EventEnvelope, RunDeliveryOptions
from box_agent.agent_run import AgentRunHandle
from box_agent.events import ContentEvent, DoneEvent, StopReason
from box_agent.run_events import (
    EventConsumerTimeoutError, EventTooLargeError, RunDeliveryError, RunEventChannel,
)


@pytest.mark.asyncio
async def test_count_backpressure_waits_until_low_watermark():
    channel = RunEventChannel(RunDeliveryOptions(max_events=4))
    for _ in range(4):
        await channel.publish("r", ContentEvent("a"))
    blocked = asyncio.create_task(channel.publish("r", ContentEvent("b")))
    await asyncio.sleep(0)
    await channel.get()
    await asyncio.sleep(0)
    assert not blocked.done()
    await channel.get()
    await asyncio.wait_for(blocked, 1)
    assert channel.pending_count == 3


@pytest.mark.asyncio
async def test_byte_backpressure_and_single_event_rejection():
    payload = ContentEvent("中文" * 10)
    size = len(json.dumps(EventEnvelope("r", "r:1", 1, payload).to_dict(),
                          ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    channel = RunEventChannel(RunDeliveryOptions(max_bytes=size * 2))
    await channel.publish("r", payload)
    await channel.publish("r", payload)
    assert channel.pending_bytes == size * 2
    blocked = asyncio.create_task(channel.publish("r", payload))
    await asyncio.sleep(0)
    assert not blocked.done()
    await channel.get()
    await asyncio.wait_for(blocked, 1)
    tiny = RunEventChannel(RunDeliveryOptions(max_bytes=size - 1))
    with pytest.raises(EventTooLargeError):
        await tiny.publish("r", payload)
    assert tiny.pending_count == 0


@pytest.mark.asyncio
async def test_timeout_is_structured_without_real_wait(monkeypatch):
    import box_agent.run_events as module

    async def timeout(awaitable, seconds):
        awaitable.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(module.asyncio, "wait_for", timeout)
    channel = RunEventChannel(RunDeliveryOptions(max_events=1))
    await channel.publish("r", ContentEvent("one"))
    with pytest.raises(EventConsumerTimeoutError) as error:
        await channel.publish("r", ContentEvent("two"))
    assert error.value.code == "RUN_EVENT_CONSUMER_TIMEOUT"


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [True, False])
async def test_full_channel_publish_wakes_on_cancel_or_close(cancel):
    channel = RunEventChannel(RunDeliveryOptions(max_events=1))
    await channel.publish("r", ContentEvent("one"))
    task = asyncio.create_task(channel.publish("r", ContentEvent("two")))
    await asyncio.sleep(0)
    channel.interrupt() if cancel else channel.close()
    with pytest.raises(asyncio.CancelledError if cancel else RunDeliveryError):
        await asyncio.wait_for(task, 1)


@pytest.mark.asyncio
async def test_concurrent_publishers_have_contiguous_delivery_order():
    channel = RunEventChannel(RunDeliveryOptions(max_events=2))

    async def consume():
        return [await channel.get() for _ in range(20)]

    consumer = asyncio.create_task(consume())
    await asyncio.gather(*(channel.publish("r", ContentEvent(str(n))) for n in range(20)))
    events = await consumer
    assert [event.sequence for event in events] == list(range(1, 21))
    assert channel.pending_bytes == 0


@pytest.mark.asyncio
async def test_result_reports_delivery_failure_even_after_done():
    async def events():
        yield DoneEvent(stop_reason=StopReason.END_TURN, final_content="x" * 1000)

    handle = AgentRunHandle.for_run(
        state=SimpleNamespace(), run_id="r", events_factory=events,
        delivery_options=RunDeliveryOptions(max_bytes=200),
    )
    result = await handle.result()
    assert result.status == "failed"
    assert result.error["code"] == "RUN_EVENT_TOO_LARGE"
    await handle.aclose()


@pytest.mark.asyncio
async def test_broker_delivery_failure_cannot_be_swallowed_by_event_producer():
    async def events():
        try:
            await handle.publish(ContentEvent("x" * 2000))
        except RunDeliveryError:
            pass
        yield DoneEvent(stop_reason=StopReason.END_TURN, final_content="done")

    handle = AgentRunHandle.for_run(
        state=SimpleNamespace(), run_id="r", events_factory=events,
        delivery_options=RunDeliveryOptions(max_bytes=512),
    )
    result = await handle.result()
    assert result.status == "failed"
    assert result.error["code"] == "RUN_EVENT_TOO_LARGE"
    await handle.aclose()


@pytest.mark.asyncio
async def test_full_run_can_be_cancelled_without_consumer():
    async def events():
        for _ in range(100):
            yield ContentEvent("x")

    handle = AgentRunHandle.for_run(
        state=SimpleNamespace(cancelled=False), run_id="r", events_factory=events,
        delivery_options=RunDeliveryOptions(max_events=1),
    )
    handle._start()
    await asyncio.sleep(0)
    await handle.send(ControlCommand.cancel())
    result = await asyncio.wait_for(handle.result(), 1)
    assert result.status == "cancelled"
    await handle.aclose()


@pytest.mark.asyncio
async def test_cancel_is_allowed_while_terminal_event_waits_for_capacity():
    terminal = asyncio.Event()

    async def events():
        yield ContentEvent("pending")
        terminal.set()
        yield DoneEvent(stop_reason=StopReason.END_TURN, final_content="done")

    handle = AgentRunHandle.for_run(
        state=SimpleNamespace(cancelled=False), run_id="terminal", events_factory=events,
        delivery_options=RunDeliveryOptions(max_events=1),
    )
    handle._start()
    await terminal.wait()
    await handle.cancel()
    assert (await asyncio.wait_for(handle.result(), 1)).status == "cancelled"
    await handle.aclose()


@pytest.mark.parametrize("kwargs", [
    {"max_events": 0}, {"max_bytes": -1}, {"max_events": True},
    {"congestion_timeout_seconds": float("nan")}, {"congestion_timeout_seconds": 0},
])
def test_invalid_delivery_limits_rejected(kwargs):
    with pytest.raises(ValueError):
        RunDeliveryOptions(**kwargs)


@pytest.mark.asyncio
async def test_buffer_memory_stabilizes_across_repeated_bounded_backlogs():
    import gc
    import tracemalloc

    channel = RunEventChannel(RunDeliveryOptions(max_events=4, max_bytes=64 * 1024))

    async def cycles(count):
        for cycle in range(count):
            for index in range(4):
                await channel.publish("r", ContentEvent(f"{cycle}:{index}:" + "x" * 4096))
            assert channel.pending_count == 4
            assert channel.pending_bytes <= 64 * 1024
            for _ in range(4):
                await channel.get()
            assert channel.pending_bytes == 0

    owned_trace = not tracemalloc.is_tracing()
    if owned_trace:
        tracemalloc.start()
    try:
        await cycles(20)
        gc.collect()
        warmed = tracemalloc.get_traced_memory()[0]
        await cycles(400)
        gc.collect()
        assert tracemalloc.get_traced_memory()[0] - warmed < 256 * 1024
    finally:
        if owned_trace:
            tracemalloc.stop()

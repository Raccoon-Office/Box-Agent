"""Provider read tasks remain owned when completion races with cancellation."""

import asyncio
import gc

import pytest

from box_agent.kernel import stream_controller
from box_agent.schema import StreamEvent


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["exhausted", "provider_error", "chunk"])
async def test_cancel_after_provider_read_finishes_retrieves_its_outcome(monkeypatch, outcome):
    loop = asyncio.get_running_loop()
    unhandled = []
    previous_handler = loop.get_exception_handler()
    closed = asyncio.Event()
    real_wait = asyncio.wait

    async def provider():
        try:
            if outcome == "provider_error":
                raise ValueError("fixture provider failure")
            if outcome == "chunk":
                yield StreamEvent(type="text", delta="ready")
        finally:
            closed.set()

    async def cancel_before_consuming_result(tasks, **kwargs):
        # asyncio.wait observes completion without retrieving the result. Cancel
        # the consumer at precisely that boundary, independently of scheduling.
        done, pending = await real_wait(tasks, **kwargs)
        assert done and not pending
        asyncio.current_task().cancel()
        await asyncio.sleep(0)
        raise AssertionError("consumer cancellation was not delivered")

    monkeypatch.setattr(stream_controller.asyncio, "wait", cancel_before_consuming_result)
    stream = stream_controller.stream_with_activity(
        provider(), stale_seconds=60, activity_interval_seconds=1,
    )
    loop.set_exception_handler(lambda loop, context: unhandled.append(context))
    try:
        consumer = asyncio.create_task(anext(stream))
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(consumer, 2)
        await stream.aclose()
        del consumer
        # Let wait_for's completion callbacks release their references before
        # checking the loop's report for an unobserved provider task exception.
        await asyncio.sleep(0)
        gc.collect()
        assert closed.is_set()
        assert not unhandled, [
            (context.get("message"), repr(context.get("exception"))) for context in unhandled
        ]
    finally:
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_normal_provider_error_reaches_the_consumer():
    closed = asyncio.Event()

    async def provider():
        try:
            yield StreamEvent(type="text", delta="before failure")
            raise ValueError("fixture provider failure")
        finally:
            closed.set()

    stream = stream_controller.stream_with_activity(
        provider(), stale_seconds=60, activity_interval_seconds=1,
    )
    assert (await anext(stream)).delta == "before failure"
    with pytest.raises(ValueError, match="fixture provider failure"):
        await anext(stream)
    assert closed.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_error", [False, True])
async def test_cancelling_pending_read_awaits_provider_cleanup(cleanup_error):
    started = asyncio.Event()
    closed = asyncio.Event()

    async def provider():
        try:
            started.set()
            await asyncio.Event().wait()
            yield StreamEvent(type="text", delta="unreachable")
        finally:
            closed.set()
            if cleanup_error:
                raise ValueError("fixture cleanup failure")

    stream = stream_controller.stream_with_activity(
        provider(), stale_seconds=60, activity_interval_seconds=1,
    )
    consumer = asyncio.create_task(anext(stream))
    await asyncio.wait_for(started.wait(), 2)
    consumer.cancel()
    expected = ValueError if cleanup_error else asyncio.CancelledError
    with pytest.raises(expected):
        await asyncio.wait_for(consumer, 2)
    await stream.aclose()
    assert closed.is_set()

"""Provider read tasks remain owned when completion races with cancellation."""

import asyncio
from contextlib import aclosing
import gc
import weakref

import pytest

from box_agent.kernel import stream_controller
from box_agent.schema import StreamEvent


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["empty", "chunks", "provider_error", "close_after_chunk"])
async def test_finished_stream_releases_request_and_read_tasks_without_gc(outcome):
    closed = asyncio.Event()
    requests = []
    reads = []

    class Request:
        pass

    async def provider():
        try:
            if outcome != "empty":
                for text in ("first", "second"):
                    reads.append(weakref.ref(asyncio.current_task()))
                    yield StreamEvent(type="text", delta=text)
            reads.append(weakref.ref(asyncio.current_task()))
            if outcome == "provider_error":
                raise ValueError("fixture provider failure")
        finally:
            closed.set()

    async def consume():
        # Model the caller's request/session state. An exhausted read must not
        # keep this caller frame alive through its exception traceback.
        request = Request()
        requests.append(weakref.ref(request))
        received = []
        try:
            async with aclosing(stream_controller.stream_with_activity(
                provider(), stale_seconds=60, activity_interval_seconds=1,
            )) as stream:
                async for event in stream:
                    received.append(event.delta)
                    if outcome == "close_after_chunk":
                        break
        except ValueError as error:
            assert outcome == "provider_error"
            assert str(error) == "fixture provider failure"
        else:
            assert outcome != "provider_error"
        return received

    was_enabled = gc.isenabled()
    gc.disable()
    try:
        expected = {
            "empty": [], "close_after_chunk": ["first"],
            "chunks": ["first", "second"], "provider_error": ["first", "second"],
        }
        assert await consume() == expected[outcome]
        # Allow asyncio's completed-task callbacks to release their references;
        # no cyclic collection is allowed to hide a retained request here.
        await asyncio.sleep(0)
        assert closed.is_set()
        assert reads and all(ref() is None for ref in reads)
        assert all(ref() is None for ref in requests)
    finally:
        if was_enabled:
            gc.enable()


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", ["close_after_activity", "provider_stale"])
async def test_closing_waiting_stream_releases_read_task_without_gc(shutdown):
    closed = asyncio.Event()
    reads = []

    async def provider():
        try:
            reads.append(weakref.ref(asyncio.current_task()))
            await asyncio.Event().wait()
            yield StreamEvent(type="text", delta="unreachable")
        finally:
            closed.set()

    was_enabled = gc.isenabled()
    gc.disable()
    try:
        async with aclosing(stream_controller.stream_with_activity(
            provider(), stale_seconds=0 if shutdown == "provider_stale" else 60,
            activity_interval_seconds=0.001,
        )) as stream:
            event = await asyncio.wait_for(anext(stream), 2)
            if shutdown == "provider_stale":
                assert event.finish_reason == "provider_stale"
            else:
                assert event.type == "activity"
        await asyncio.sleep(0)
        assert closed.is_set()
        assert len(reads) == 1 and reads[0]() is None
    finally:
        if was_enabled:
            gc.enable()


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


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_error", [False, True])
async def test_provider_completion_cleans_up_cancellation_waiter(provider_error):
    waiter_closed = asyncio.Event()

    async def wait_cancelled():
        try:
            await asyncio.Event().wait()
        finally:
            waiter_closed.set()

    async def provider():
        if provider_error:
            raise ValueError("fixture provider failure")
        yield StreamEvent(type="text", delta="ready")

    stream = stream_controller.stream_with_activity(
        provider(), stale_seconds=60, activity_interval_seconds=15,
        wait_cancelled=wait_cancelled,
    )
    if provider_error:
        with pytest.raises(ValueError, match="fixture provider failure"):
            await anext(stream)
    else:
        assert [event.delta async for event in stream] == ["ready"]
    assert waiter_closed.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["exhausted", "provider_error", "chunk"])
async def test_control_cancel_wins_when_provider_read_also_finishes(monkeypatch, outcome):
    cancelled, closed = asyncio.Event(), asyncio.Event()
    real_wait = asyncio.wait

    async def provider():
        try:
            if outcome == "provider_error":
                raise ValueError("fixture provider failure")
            if outcome == "chunk":
                yield StreamEvent(type="text", delta="must not be delivered")
        finally:
            closed.set()

    async def complete_both(tasks, **kwargs):
        await real_wait(tasks, **kwargs)
        cancelled.set()
        return await real_wait(tasks)

    monkeypatch.setattr(stream_controller.asyncio, "wait", complete_both)
    stream = stream_controller.stream_with_activity(
        provider(), stale_seconds=60, activity_interval_seconds=15,
        wait_cancelled=cancelled.wait,
    )
    assert [event async for event in stream] == []
    assert closed.is_set()

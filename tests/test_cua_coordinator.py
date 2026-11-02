"""Cancellation and shutdown tests with blocked fake work; never desktop input."""
import asyncio
import threading

import pytest

from box_agent.plugins.cua.runtime.coordinator import SerialCoordinator


class BlockingService:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.events = []

    def call(self, name, arguments):
        self.events.append(name + ":start")
        if name == "first":
            self.started.set()
            if not self.release.wait(5):
                raise TimeoutError("test did not release worker")
        self.events.append(name + ":end")
        if arguments.get("raise"):
            raise RuntimeError("fake failure after dispatch")
        return name

    def invalidate(self):
        self.events.append("invalidate")

    def close(self):
        self.events.append("close")


async def started(service):
    assert await asyncio.to_thread(service.started.wait, 2)


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_release_execution_ownership():
    service = BlockingService()
    owner = SerialCoordinator(service.call, invalidate=service.invalidate, close=service.close)
    try:
        first = asyncio.create_task(owner.call("first", {}))
        await started(service)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(owner.call("second", {}))
        await asyncio.sleep(0)
        assert not second.done()
        assert service.events == ["first:start"]
        service.release.set()
        assert await asyncio.wait_for(second, 2) == "second"
        assert service.events == ["first:start", "first:end", "invalidate", "second:start", "second:end"]
    finally:
        service.release.set()
        await owner.aclose()


@pytest.mark.asyncio
async def test_queued_input_waits_for_cancellation_even_when_worker_finishes_first():
    service = BlockingService()
    finished = threading.Event()
    def call(name, arguments):
        result = service.call(name, arguments)
        if name == "first":
            finished.set()
        return result
    owner = SerialCoordinator(call, invalidate=service.invalidate, close=service.close)
    try:
        first = asyncio.create_task(owner.call("first", {}))
        await started(service)
        second = asyncio.create_task(owner.call("second", {}))
        await asyncio.sleep(0)
        first.cancel()
        service.release.set()
        # Deliberately keep the event loop here while the worker completes A.
        # B was already queued when cancellation was requested.
        assert finished.wait(2)
        owner._pool.submit(lambda: None).result(timeout=2)
        with pytest.raises(asyncio.CancelledError):
            await first
        assert await asyncio.wait_for(second, 2) == "second"
        assert service.events == ["first:start", "first:end", "invalidate", "second:start", "second:end"]
    finally:
        service.release.set()
        await owner.aclose()


@pytest.mark.asyncio
async def test_cancelled_queued_call_is_never_dispatched_or_invalidated():
    service = BlockingService()
    owner = SerialCoordinator(service.call, invalidate=service.invalidate, close=service.close)
    try:
        first = asyncio.create_task(owner.call("first", {}))
        await started(service)
        queued = asyncio.create_task(owner.call("queued", {}))
        await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        service.release.set()
        assert await asyncio.wait_for(first, 2) == "first"
        assert await owner.call("last", {}) == "last"
        assert service.events == ["first:start", "first:end", "last:start", "last:end"]
    finally:
        service.release.set()
        await owner.aclose()


@pytest.mark.asyncio
async def test_shutdown_waits_for_dispatched_work_and_cancels_queued_work():
    service = BlockingService()
    owner = SerialCoordinator(service.call, invalidate=service.invalidate, close=service.close)
    first = asyncio.create_task(owner.call("first", {}))
    try:
        await started(service)
        queued = asyncio.create_task(owner.call("queued", {}))
        await asyncio.sleep(0)
        closing = asyncio.create_task(owner.aclose())
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="closing"):
            await owner.call("late", {})
        assert "close" not in service.events
        with pytest.raises(asyncio.CancelledError):
            await queued
        service.release.set()
        await asyncio.wait_for(first, 2)
        await asyncio.wait_for(closing, 2)
        await owner.aclose()
        assert service.events == ["first:start", "first:end", "close"]
    finally:
        service.release.set()
        await owner.aclose()


@pytest.mark.asyncio
async def test_abandoned_failure_is_not_replayed_and_next_call_waits_for_cleanup():
    service = BlockingService()
    owner = SerialCoordinator(service.call, invalidate=service.invalidate, close=service.close)
    try:
        first = asyncio.create_task(owner.call("first", {"raise": True}))
        await started(service)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        service.release.set()
        assert await asyncio.wait_for(owner.call("last", {}), 2) == "last"
        assert service.events == ["first:start", "first:end", "invalidate", "last:start", "last:end"]
    finally:
        service.release.set()
        await owner.aclose()


@pytest.mark.asyncio
async def test_cleanup_failure_keeps_subsequent_calls_from_dispatching():
    service = BlockingService()
    def broken_cleanup():
        raise RuntimeError("cleanup pending")
    owner = SerialCoordinator(service.call, invalidate=broken_cleanup, close=service.close)
    try:
        first = asyncio.create_task(owner.call("first", {}))
        await started(service)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        service.release.set()
        for _ in range(2):
            with pytest.raises(RuntimeError, match="cleanup pending"):
                await owner.call("must-not-run", {})
        assert service.events == ["first:start", "first:end"]
    finally:
        service.release.set()
        await owner.aclose()


@pytest.mark.asyncio
async def test_cancelled_call_holds_admission_until_cleanup_finishes():
    service = BlockingService()
    cleaning = threading.Event()
    release_cleanup = threading.Event()
    def invalidate():
        cleaning.set()
        if not release_cleanup.wait(2):
            raise TimeoutError('test did not release cleanup')
        service.invalidate()
    owner = SerialCoordinator(service.call, invalidate=invalidate, close=service.close)
    try:
        first = asyncio.create_task(owner.call('first', {}))
        await started(service)
        second = asyncio.create_task(owner.call('second', {}))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        service.release.set()
        assert await asyncio.to_thread(cleaning.wait, 2)
        assert not second.done()
        assert 'second:start' not in service.events
        release_cleanup.set()
        assert await asyncio.wait_for(second, 2) == 'second'
        assert service.events == ['first:start', 'first:end', 'invalidate', 'second:start', 'second:end']
    finally:
        release_cleanup.set()
        service.release.set()
        await owner.aclose()

"""Finished runs release execution inputs while preserving host-facing state."""

import asyncio
from contextlib import aclosing
import gc
from types import SimpleNamespace
import weakref

import pytest

from box_agent.agent_run import AgentRunHandle
from box_agent.agent_service import AgentService
from box_agent.api import ControlCommand, RunRequest
from box_agent.events import ContentEvent, DoneEvent, StopReason


@pytest.fixture
def without_cyclic_gc():
    enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if enabled:
            gc.enable()


class _Payload:
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_abandoned_active_run_releases_inputs_and_keeps_outcome(
    without_cyclic_gc, cleanup_fails,
):
    references = []
    waiting, closed = asyncio.Event(), asyncio.Event()

    async def provider():
        local = _Payload()
        references.append(weakref.ref(local))
        try:
            yield ContentEvent(content="started")
            waiting.set()
            await asyncio.Event().wait()
        finally:
            closed.set()
            if cleanup_fails:
                raise ValueError("cleanup failed")

    state = object()
    handle = AgentRunHandle.for_run(state=state, run_id="abandoned", events_factory=provider)
    async with aclosing(handle.events()) as events:
        assert (await anext(events)).payload.content == "started"
        await asyncio.wait_for(waiting.wait(), 1)
    result = await handle.result()
    await asyncio.sleep(0)

    assert closed.is_set()
    assert references[0]() is None
    assert handle.state is state
    assert await handle.result() is result
    assert result.status == ("failed" if cleanup_fails else "cancelled")
    assert result.error["type"] == ("ValueError" if cleanup_fails else "CancelledError")
    if cleanup_fails:
        assert result.error["message"] == "cleanup failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("close_before_start", [False, True])
async def test_finished_handle_releases_factory_but_keeps_result_and_state(
    without_cyclic_gc, close_before_start,
):
    calls = []

    class Factory:
        async def __call__(self):
            calls.append(True)
            yield DoneEvent(stop_reason=StopReason.END_TURN, final_content="saved answer")

    state = SimpleNamespace(agent=object())
    factory = Factory()
    reference = weakref.ref(factory)
    handle = AgentRunHandle.for_run(state=state, run_id="first", events_factory=factory)
    del factory
    if close_before_start:
        async with handle:
            pass
    result = await handle.result()
    await handle.aclose()
    await asyncio.sleep(0)

    assert reference() is None
    assert calls == ([] if close_before_start else [True])
    assert result.status == ("cancelled" if close_before_start else "completed")
    assert await handle.result() is result
    assert handle.state is state and handle.agent is state.agent
    assert not handle.is_active


@pytest.mark.asyncio
async def test_close_after_done_releases_run_inputs_and_allows_next_turn(without_cyclic_gc):
    references = []
    cleaning = asyncio.Event()

    class Session:
        cancelled = False
        agent = SimpleNamespace(add_user_message=lambda text: None)

        def build_run_options(self, **kwargs):
            return kwargs

        async def run_events(self, *, options):
            local = _Payload()
            references.append(weakref.ref(local))
            try:
                yield DoneEvent(stop_reason=StopReason.END_TURN, final_content=options['turn_id'])
            finally:
                cleaning.set()
                await asyncio.Event().wait()

    session = Session()
    service = AgentService()
    first = await service.start(RunRequest("first", "session"), session=session)
    async with aclosing(first.events()) as events:
        assert (await anext(events)).payload.final_content == "first"
        await cleaning.wait()
    result = await first.result()
    await asyncio.sleep(0)
    assert references[0]() is None
    assert result.status == "completed" and result.final_content == "first"
    assert first.state is session and session._run_handle is first

    cleaning.clear()
    second = await service.start(RunRequest("second", "session"), session=session)
    async with aclosing(second.events()) as events:
        assert (await anext(events)).payload.final_content == "second"
        await cleaning.wait()
        with pytest.raises(RuntimeError, match="active run"):
            await first.send(ControlCommand.cancel())
    assert (await second.result()).final_content == "second"
    assert await first.result() is result
    await asyncio.sleep(0)
    assert all(ref() is None for ref in references)


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [ValueError, asyncio.CancelledError])
async def test_failure_keeps_original_exception_and_traceback(error_type):
    original_error = error_type("provider failed")

    async def provider():
        diagnostic = "provider diagnostic"
        raise original_error
        yield  # Make this a provider event stream.

    handle = AgentRunHandle.for_run(state=object(), run_id="failed", events_factory=provider)
    async with aclosing(handle.events()) as events:
        try:
            await anext(events)
        except error_type as caught:
            assert caught is original_error
        else:
            pytest.fail("provider failure was lost")
    result = await handle.result()
    await asyncio.sleep(0)

    assert result.status == ("cancelled" if error_type is asyncio.CancelledError else "failed")
    assert result.error == {"type": error_type.__name__, "message": "provider failed"}
    names = []
    tb = original_error.__traceback__
    while tb is not None:
        names.append(tb.tb_frame.f_code.co_name)
        if tb.tb_frame.f_code.co_name == "provider":
            assert tb.tb_frame.f_locals["diagnostic"] == "provider diagnostic"
        tb = tb.tb_next
    assert "provider" in names
    assert await handle.result() is result


@pytest.mark.asyncio
@pytest.mark.parametrize("consumption", ["events", "result", "late_result"])
@pytest.mark.parametrize("error_type", [ValueError, asyncio.CancelledError])
async def test_consumed_failure_releases_run_inputs(without_cyclic_gc, consumption, error_type):
    references = []

    async def provider():
        local = _Payload()
        references.append(weakref.ref(local))
        raise error_type("failed run")
        yield

    handle = AgentRunHandle.for_run(state=object(), run_id="failed", events_factory=provider)
    async with handle:
        if consumption == "events":
            try:
                async for _ in handle.events():
                    pass
            except error_type as error:
                assert str(error) == "failed run"
            else:
                pytest.fail("provider failure was lost")
        elif consumption == "late_result":
            # The producer has already saved its error before result-only
            # consumption is chosen; it must still release that error.
            await asyncio.sleep(0)
        result = await handle.result()
    await asyncio.sleep(0)
    assert references[0]() is None
    assert result.status == ("cancelled" if error_type is asyncio.CancelledError else "failed")
    assert result.error == {"type": error_type.__name__, "message": "failed run"}
    assert await handle.result() is result


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [ValueError, asyncio.CancelledError])
async def test_late_event_consumer_receives_error_after_runner_is_closed(error_type):
    original_error = error_type("saved failure")

    async def provider():
        raise original_error
        yield

    handle = AgentRunHandle.for_run(state=object(), run_id="late", events_factory=provider)
    async with handle:
        await asyncio.sleep(0)
    try:
        async for _ in handle.events():
            pass
    except error_type as caught:
        assert caught is original_error
    else:
        pytest.fail("late event consumer lost the failure")
    assert (await handle.result()).error["message"] == "saved failure"


@pytest.mark.asyncio
@pytest.mark.parametrize("relationship", ["direct", "cause", "cleanup_group"])
async def test_run_cleanup_does_not_clear_a_suspended_exception_owner(relationship):
    ready = asyncio.Event()
    resume = asyncio.Event()
    shared = []

    async def owner():
        try:
            raise ValueError("shared failure")
        except ValueError as error:
            shared.append(error)
        ready.set()
        await resume.wait()
        return "owner continued"

    task = asyncio.create_task(owner())
    await ready.wait()

    async def provider():
        if relationship == "cause":
            raise RuntimeError("provider failed") from shared[0]
        if relationship == "cleanup_group":
            from box_agent.plugins.host import PluginCleanupError

            raise PluginCleanupError(shared)
        raise shared[0]
        yield

    handle = AgentRunHandle.for_run(state=object(), run_id="shared", events_factory=provider)
    try:
        assert (await handle.result()).status == "failed"
        await handle.aclose()
        await asyncio.sleep(0)
    finally:
        resume.set()
    assert await asyncio.wait_for(task, 1) == "owner continued"


@pytest.mark.asyncio
async def test_late_event_subscription_preserves_completed_run_after_factory_release(without_cyclic_gc):
    ready = asyncio.Event()

    class Factory:
        async def __call__(self):
            yield DoneEvent(stop_reason=StopReason.END_TURN, final_content="ready before subscription")
            ready.set()

    factory = Factory()
    reference = weakref.ref(factory)
    handle = AgentRunHandle.for_run(state=object(), run_id="late", events_factory=factory)
    del factory
    async with handle:
        await asyncio.wait_for(ready.wait(), 1)
        assert reference() is None
        events = [event async for event in handle.events()]
    assert len(events) == 1
    assert events[0].payload.final_content == "ready before subscription"
    assert (await handle.result()).final_content == "ready before subscription"


@pytest.mark.asyncio
async def test_delivered_exception_does_not_close_consumer_waiting_for_next_action():
    received = asyncio.Event()
    resume = asyncio.Event()

    async def provider():
        raise ValueError("failed run")
        yield

    handle = AgentRunHandle.for_run(state=object(), run_id="consumer", events_factory=provider)

    async def consumer():
        try:
            async for _ in handle.events():
                pass
        except ValueError as error:
            assert str(error) == "failed run"
        received.set()
        await resume.wait()
        return "next action"

    task = asyncio.create_task(consumer())
    try:
        await asyncio.wait_for(received.wait(), 1)
        await asyncio.sleep(0)
    finally:
        resume.set()
    assert await asyncio.wait_for(task, 1) == "next action"


@pytest.mark.asyncio
@pytest.mark.skipif(not hasattr(asyncio, "eager_task_factory"), reason="requires Python 3.12")
@pytest.mark.parametrize("failure", [False, True])
async def test_run_can_finish_during_eager_task_creation(failure):
    async def provider():
        if failure:
            raise ValueError("eager failure")
        yield DoneEvent(stop_reason=StopReason.END_TURN, final_content="eager answer")

    loop = asyncio.get_running_loop()
    previous = loop.get_task_factory()
    loop.set_task_factory(asyncio.eager_task_factory)
    try:
        handle = AgentRunHandle.for_run(state=object(), run_id="eager", events_factory=provider)
        result = await handle.result()
        await handle.aclose()
        assert result.status == ("failed" if failure else "completed")
        if failure:
            assert result.error == {"type": "ValueError", "message": "eager failure"}
        else:
            assert result.final_content == "eager answer"
    finally:
        loop.set_task_factory(previous)


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [ValueError, asyncio.CancelledError])
async def test_session_releases_propagated_failure_locals(without_cyclic_gc, error_type):
    from box_agent.agent_session import AgentSession

    references = []

    class Agent:
        async def run_events(self, *, options):
            local = _Payload()
            references.append(weakref.ref(local))
            raise error_type("session failure")
            yield

    session = AgentSession(agent=Agent())
    try:
        async for _ in session.run_events(options=object()):
            pass
    except error_type as error:
        assert str(error) == "session failure"
    else:
        pytest.fail("session failure was lost")
    assert references[0]() is None
    assert not session.turn_active
    await session.aclose()


@pytest.mark.asyncio
async def test_cancelled_hook_wait_releases_cancellation_inputs(without_cyclic_gc):
    from box_agent.composition import _wait_for_hook_cleanup

    release = asyncio.Event()
    cleanup = asyncio.create_task(release.wait())

    # Hook cleanup returns None on success, like the real cleanup task.
    async def clean():
        await cleanup

    cleanup_task = asyncio.create_task(clean())

    async def wait():
        try:
            await _wait_for_hook_cleanup(cleanup_task, settle=True)
        except asyncio.CancelledError:
            return "cancelled after cleanup"

    waiter = asyncio.create_task(wait())
    await asyncio.sleep(0)
    payload = _Payload()
    reference = weakref.ref(payload)
    waiter.cancel(payload)
    del payload
    await asyncio.sleep(0)
    assert not waiter.done()
    release.set()
    assert await asyncio.wait_for(waiter, 1) == "cancelled after cleanup"
    assert reference() is None


@pytest.mark.asyncio
async def test_managed_done_cleanup_releases_run_context_without_gc(
    tmp_path, monkeypatch, without_cyclic_gc,
):
    import box_agent.composition as composition
    from tests.test_acp import DoneLLM
    from tests.test_plugin_runtime_lifecycle import open_session

    references = []
    original_capabilities = composition._default_capabilities
    original_cleanup = composition._cleanup_hook_run
    entered, release = asyncio.Event(), asyncio.Event()

    def capabilities(arguments):
        references.append(weakref.ref(arguments["context_engine"]))
        return original_capabilities(arguments)

    async def cleanup(*args):
        entered.set()
        await release.wait()
        return await original_cleanup(*args)

    monkeypatch.setattr(composition, "_default_capabilities", capabilities)
    monkeypatch.setattr(composition, "_cleanup_hook_run", cleanup)
    session = await open_session(tmp_path, llm=DoneLLM())
    try:
        handle = await AgentService().start(RunRequest("first", "session", "hello"), session=session)
        events = handle.events()
        async for envelope in events:
            if isinstance(envelope.payload, DoneEvent):
                break
        await asyncio.wait_for(entered.wait(), 1)
        closing = asyncio.create_task(events.aclose())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        await asyncio.wait_for(closing, 1)
        assert (await handle.result()).status == "completed"
        await asyncio.sleep(0)
        assert references and all(ref() is None for ref in references)
    finally:
        release.set()
        await session.aclose()

"""Cancellation and failure paths of managed session ownership."""

import asyncio
from pathlib import Path
from typing import Protocol, runtime_checkable

import pytest

from box_agent.agent_session import AgentSession
from box_agent.plugins.builtins import SessionInitializerPort
from box_agent.plugins.descriptors import PluginDescriptor, PluginScope
from box_agent.plugins.registries import CapabilityBinding, CapabilityPolicy
from box_agent.plugins.host import PluginScopeError
from box_agent.plugins.runtime import PluginRuntime
from box_agent.session_context import HostBindings
from tests.test_agent_session import session_config
from tests.test_session_plugins import RecordingLLM


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))


async def open_session(tmp_path, runtime=None, llm=None):
    return await AgentSession.open(
        config=session_config(tmp_path), runtime=runtime,
        host=HostBindings(llm_client=llm or RecordingLLM(), tools=[], system_prompt="system"),
    )


@pytest.mark.asyncio
async def test_runtime_rejects_duplicate_session_key_without_disposing_owner(tmp_path):
    runtime = PluginRuntime()
    owner = await open_session(tmp_path, runtime)
    try:
        with pytest.raises(ValueError, match="session key"):
            await runtime.open_session(owner.plugin_session.context)
        owner.agent.add_user_message("still available")
        events = [event async for event in owner.run_events(
            options=owner.build_run_options(logger=None),
        )]
        assert events
        assert not owner.plugin_session._closed
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_runtime_retains_failed_preparation_for_cancelled_cleanup_retry(tmp_path):
    attempts = []

    class FailingInitializer:
        async def prepare(self):
            raise ValueError("preparation failed")

    def dispose(resource):
        attempts.append(resource)
        if len(attempts) == 1:
            raise asyncio.CancelledError("retry rollback")

    runtime = PluginRuntime(plugins=(PluginDescriptor(
        "test.failing", "1.0.0", (SessionInitializerPort,), factory=FailingInitializer,
        scope=PluginScope.SESSION, disposer=dispose,
    ),))
    with pytest.raises(ValueError, match="preparation failed"):
        await open_session(tmp_path, runtime)
    assert len(runtime._sessions) == 1
    await runtime.aclose()
    assert attempts[0] is attempts[1]
    assert not runtime._sessions


@pytest.mark.asyncio
async def test_runtime_close_during_prepare_does_not_wait_for_callers_finally(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowInitializer:
        async def prepare(self):
            entered.set()
            await release.wait()

    runtime = PluginRuntime(plugins=(PluginDescriptor(
        "test.slow", "1.0.0", (SessionInitializerPort,), factory=SlowInitializer,
        scope=PluginScope.SESSION,
    ),))

    async def caller():
        try:
            await open_session(tmp_path, runtime)
        finally:
            await runtime.aclose()

    opening = asyncio.create_task(caller())
    await entered.wait()
    closing = asyncio.create_task(runtime.aclose())
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.wait_for(
        asyncio.gather(opening, closing, return_exceptions=True), timeout=2,
    )
    assert isinstance(results[0], RuntimeError)
    assert "closing" in str(results[0])
    assert results[1] is None


@pytest.mark.asyncio
async def test_cancelled_run_disposal_is_retried_before_session_resources_close(tmp_path):
    class RunResource:
        pass

    attempts = []

    def dispose(resource):
        attempts.append(resource)
        if len(attempts) == 1:
            raise asyncio.CancelledError("interrupted close")

    runtime = PluginRuntime(
        plugins=(PluginDescriptor("test.run", "1.0.0", (RunResource,),
                                  factory=RunResource, disposer=dispose),),
        bindings=(CapabilityBinding(RunResource, CapabilityPolicy.REQUIRED_SINGLE),),
    )
    session = await open_session(tmp_path, runtime)
    session.agent.add_user_message("hello")
    with pytest.raises(asyncio.CancelledError, match="interrupted close"):
        _ = [event async for event in session.run_events(
            options=session.build_run_options(logger=None),
        )]
    await session.aclose()
    assert len(attempts) == 2
    assert attempts[0] is attempts[1]
    await runtime.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_site", ["factory", "port_validation"])
@pytest.mark.parametrize("cancelled_attempts", [1, 2])
async def test_failed_run_rollback_stays_with_its_session_until_cleanup_finishes(
    tmp_path, failure_site, cancelled_attempts,
):
    class ProcessResource:
        pass

    class SessionResource:
        def __init__(self, owner):
            self.owner = owner

    class RunResource:
        def __init__(self, owner):
            self.owner = owner

    @runtime_checkable
    class CheckedPort(Protocol):
        def required(self) -> None: ...

    class CheckedResource(RunResource):
        def required(self):
            pass

    target = None
    attempts = {}
    order = []

    def create_run(context):
        return RunResource(context.context.session.session_key)

    def create_checked(context):
        owner = context.context.session.session_key
        if owner == target:
            if failure_site == "factory":
                raise ValueError("run factory failed")
            return RunResource(owner)  # Deliberately lacks CheckedPort.required.
        return CheckedResource(owner)

    async def interrupted_dispose(resource):
        attempts[resource.owner] = attempts.get(resource.owner, 0) + 1
        if resource.owner == target and attempts[resource.owner] <= cancelled_attempts:
            raise asyncio.CancelledError("run rollback interrupted")
        order.append(("run-finished", resource.owner))

    runtime = PluginRuntime(
        plugins=(
            PluginDescriptor("test.process", "1.0.0", (ProcessResource,),
                             factory=ProcessResource, scope=PluginScope.PROCESS,
                             disposer=lambda resource: order.append(("process", None))),
            PluginDescriptor("test.session", "1.0.0", (SessionResource,),
                             context_factory=lambda context: SessionResource(context.context.session_key),
                             scope=PluginScope.SESSION, dependencies=("test.process",),
                             disposer=lambda resource: order.append(("session", resource.owner))),
            PluginDescriptor("test.run", "1.0.0", (RunResource,),
                             context_factory=create_run, dependencies=("test.session",),
                             disposer=interrupted_dispose if failure_site == "factory" else None),
            PluginDescriptor("test.checked", "1.0.0", (CheckedPort,),
                             context_factory=create_checked, dependencies=("test.run",),
                             disposer=interrupted_dispose if failure_site == "port_validation" else None),
        ),
        bindings=tuple(CapabilityBinding(port, CapabilityPolicy.REQUIRED_SINGLE)
                       for port in (ProcessResource, SessionResource, RunResource, CheckedPort)),
    )
    first = await open_session(tmp_path / "first", runtime)
    second = await open_session(tmp_path / "second", runtime)
    target = first.plugin_session.context.session_key
    try:
        with pytest.raises(asyncio.CancelledError, match="rollback interrupted"):
            _ = [event async for event in first.run_events()]
        assert attempts[target] == 1
        with pytest.raises(PluginScopeError, match="cleanup is incomplete"):
            _ = [event async for event in first.run_events()]
        if cancelled_attempts > 1:
            with pytest.raises(asyncio.CancelledError, match="rollback interrupted"):
                await first.aclose()
            assert not first._closed and ("session", target) not in order

        second.agent.add_user_message("other session remains usable")
        assert [event async for event in second.run_events()]
        assert ("process", None) not in order
        assert not second._closed

        await first.aclose()
        assert first._closed and attempts[target] == cancelled_attempts + 1
        assert order.index(("run-finished", target)) < order.index(("session", target))
        assert ("session", second.plugin_session.context.session_key) not in order
        assert ("process", None) not in order
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_session_close_cancels_active_model_stream_and_finishes_cleanup(tmp_path):
    entered, closed = asyncio.Event(), asyncio.Event()

    class WaitingLLM(RecordingLLM):
        async def generate_stream(self, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
                yield
            finally:
                closed.set()

    session = await open_session(tmp_path, llm=WaitingLLM())
    session.agent.add_user_message("wait")

    async def consume():
        return [event async for event in session.run_events(
            options=session.build_run_options(logger=None),
        )]

    running = asyncio.create_task(consume())
    await entered.wait()
    await asyncio.wait_for(session.aclose(), timeout=3)
    assert running.done()
    assert closed.is_set()


@pytest.mark.asyncio
async def test_early_closed_run_leaves_session_available(tmp_path):
    session = await open_session(tmp_path)
    session.agent.add_user_message("first")
    stream = session.run_events(options=session.build_run_options(logger=None))
    await anext(stream)
    await stream.aclose()
    session.agent.add_user_message("second")
    events = [event async for event in session.run_events(
        options=session.build_run_options(logger=None),
    )]
    assert events
    await session.aclose()


@pytest.mark.asyncio
async def test_closing_session_while_consumer_is_suspended_closes_stream(tmp_path):
    session = await open_session(tmp_path)
    session.agent.add_user_message("first")
    stream = session.run_events(options=session.build_run_options(logger=None))
    await anext(stream)
    await session.aclose()
    await stream.aclose()
    with pytest.raises(RuntimeError, match="closed"):
        await anext(session.run_events())


@pytest.mark.asyncio
async def test_external_close_does_not_wait_for_consumer_finally_close(tmp_path):
    entered = asyncio.Event()

    class WaitingLLM(RecordingLLM):
        async def generate_stream(self, **kwargs):
            entered.set()
            await asyncio.Event().wait()
            yield

    session = await open_session(tmp_path, llm=WaitingLLM())
    session.agent.add_user_message("wait")

    async def consume():
        try:
            return [event async for event in session.run_events(
                options=session.build_run_options(logger=None),
            )]
        finally:
            await session.aclose()

    running = asyncio.create_task(consume())
    await entered.wait()
    closing = asyncio.create_task(session.aclose())
    done, pending = await asyncio.wait({running, closing}, timeout=1)
    try:
        assert not pending, "close waited for caller finally while holding close lock"
    finally:
        for task in pending:
            task.cancel()
        await asyncio.gather(running, closing, return_exceptions=True)


@pytest.mark.asyncio
async def test_external_close_of_suspended_stream_does_not_cancel_consumer(tmp_path):
    session = await open_session(tmp_path)
    session.agent.add_user_message("hello")
    paused, release = asyncio.Event(), asyncio.Event()
    cancelled = []

    async def consume():
        stream = session.run_events(options=session.build_run_options(logger=None))
        try:
            await anext(stream)
            paused.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
        finally:
            await stream.aclose()

    running = asyncio.create_task(consume())
    await paused.wait()
    await session.aclose()
    release.set()
    await asyncio.gather(running, return_exceptions=True)
    assert not cancelled


@pytest.mark.asyncio
async def test_cancelled_owned_cleanup_callback_is_retained_for_retry(tmp_path):
    session = await open_session(tmp_path)
    attempts = []

    async def cleanup():
        attempts.append(True)
        if len(attempts) == 1:
            raise asyncio.CancelledError("retry cleanup")

    session.plugin_session.resources.cleanup.push_async_callback(cleanup)
    with pytest.raises(asyncio.CancelledError, match="retry cleanup"):
        await session.aclose()
    await session.aclose()
    assert attempts == [True, True]


def test_agent_run_options_preserve_positional_summary_model():
    from box_agent.agent import AgentRunOptions

    llm, summary = object(), object()
    options = AgentRunOptions(llm, summary)
    assert options.summary_llm is summary
    assert options.kernel_services is None


@pytest.mark.asyncio
async def test_runtime_close_waits_for_session_finalization_then_rolls_back(tmp_path, monkeypatch):
    import box_agent.session_assembly as assembly

    entered, release = asyncio.Event(), asyncio.Event()
    original = assembly.finish_session

    async def finish(session, resources):
        entered.set()
        await release.wait()
        await original(session, resources)

    monkeypatch.setattr(assembly, "finish_session", finish)
    runtime = PluginRuntime()
    opening = asyncio.create_task(open_session(tmp_path, runtime))
    closing = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        closing = asyncio.create_task(runtime.aclose())
        await asyncio.sleep(0)
        assert not closing.done(), "runtime disposed an initializing session"
        release.set()
        results = await asyncio.wait_for(
            asyncio.gather(opening, closing, return_exceptions=True), timeout=2,
        )
        assert isinstance(results[0], RuntimeError)
        assert "closing" in str(results[0])
        assert results[1] is None
    finally:
        release.set()
        await asyncio.gather(opening, return_exceptions=True)
        if closing is not None:
            await closing
        await runtime.aclose()


@pytest.mark.asyncio
async def test_consumer_resuming_during_external_close_joins_single_stream_cleanup(tmp_path):
    from box_agent.events import StepStart

    session = await open_session(tmp_path)
    paused, resume, closing, release = (asyncio.Event() for _ in range(4))

    async def stream(**kwargs):
        try:
            yield StepStart(step=1, max_steps=2)
        finally:
            closing.set()
            await release.wait()

    session.agent.run_events = stream

    async def consume():
        events = session.run_events(options=session.build_run_options(logger=None))
        await anext(events)
        paused.set()
        await resume.wait()
        return [event async for event in events]

    consumer = asyncio.create_task(consume())
    await asyncio.wait_for(paused.wait(), timeout=2)
    closer = asyncio.create_task(session.aclose())
    await asyncio.wait_for(closing.wait(), timeout=2)
    resume.set()
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.wait_for(
        asyncio.gather(consumer, closer, return_exceptions=True), timeout=2,
    )
    assert results == [[], None]


@pytest.mark.asyncio
async def test_close_cancels_current_driver_after_stream_moves_between_tasks(tmp_path):
    from box_agent.events import StepStart

    session = await open_session(tmp_path)
    entered, closed = asyncio.Event(), asyncio.Event()

    async def stream(**kwargs):
        try:
            yield StepStart(step=1, max_steps=2)
            entered.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    session.agent.run_events = stream
    events = session.run_events(options=session.build_run_options(logger=None))
    await asyncio.wait_for(anext(events), timeout=1)
    driver = asyncio.create_task(anext(events))
    await asyncio.wait_for(entered.wait(), timeout=1)
    try:
        await asyncio.wait_for(session.aclose(), timeout=2)
        assert driver.done()
        assert closed.is_set()
        assert session._closed
    finally:
        driver.cancel()
        await asyncio.gather(driver, return_exceptions=True)
        await events.aclose()
        await session.aclose()

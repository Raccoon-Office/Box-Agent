"""Context-aware selection and lifetime behavior of the static plugin host."""

from dataclasses import dataclass
import asyncio
from typing import Protocol, runtime_checkable

import pytest

from box_agent.plugins import (
    CapabilityBinding, CapabilityPolicy, CapabilitySchema,
    PluginDescriptor, PluginHost, PluginScope, PluginScopeError,
    PluginValidationError,
)


class ProcessPort:
    pass


class SessionPort:
    pass


class RunPort:
    pass


def schema(*ports):
    return CapabilitySchema(tuple(
        CapabilityBinding(port, CapabilityPolicy.REQUIRED_SINGLE) for port in ports
    ))


@dataclass
class Config:
    name: str


@pytest.mark.asyncio
async def test_scope_context_and_dependencies_reuse_session_but_dispose_only_run():
    calls, disposed = [], []

    def factory(label):
        def create(ctx):
            calls.append((label, ctx))
            return object()
        return create

    descriptors = (
        PluginDescriptor("process", "1.0.0", (ProcessPort,),
                         scope=PluginScope.PROCESS, context_factory=factory("process"),
                         disposer=disposed.append),
        PluginDescriptor("session", "1.0.0", (SessionPort,),
                         dependencies=("process",), scope=PluginScope.SESSION,
                         context_factory=factory("session"), disposer=disposed.append),
        PluginDescriptor("run", "1.0.0", (RunPort,), dependencies=("session",),
                         context_factory=factory("run"), disposer=disposed.append),
    )
    host = PluginHost(descriptors, schema=schema(ProcessPort, SessionPort, RunPort))
    process_context, config_a, config_b = object(), Config("a"), Config("b")
    contexts = {PluginScope.PROCESS: process_context, PluginScope.SESSION: config_a}
    session = await host.activate(session_key="a", scopes=(PluginScope.PROCESS, PluginScope.SESSION), contexts=contexts)
    assert [label for label, _ in calls] == ["process", "session"]
    assert RunPort not in session.registry
    first = await host.activate(session_key="a", contexts=contexts)
    second = await host.activate(session_key="a", contexts=contexts)
    other = await host.activate(session_key="b", contexts={PluginScope.SESSION: config_b})
    assert first.registry[SessionPort] is second.registry[SessionPort]
    assert first.registry[SessionPort] is not other.registry[SessionPort]
    assert first.registry[RunPort] is not second.registry[RunPort]
    assert calls[0][1].context is process_context
    assert calls[1][1].context is config_a
    assert calls[1][1].dependencies == {"process": first.registry[ProcessPort]}
    assert calls[2][1].dependencies == {"session": first.registry[SessionPort]}
    with pytest.raises(TypeError):
        calls[2][1].dependencies["undeclared"] = object()
    assert calls[4][1].context is config_b
    await first.dispose()
    assert disposed == [first.registry[RunPort]]
    await host.dispose_session("a")
    assert disposed[-1] is first.registry[SessionPort]
    assert other.registry[SessionPort] not in disposed
    await host.close()


@pytest.mark.asyncio
async def test_selected_dependency_closure_uses_catalog_order_without_mutating_catalog():
    made = []
    descriptors = (
        PluginDescriptor("first", "1.0.0", (RunPort,), lambda: made.append("first") or object()),
        PluginDescriptor("dependency", "1.0.0", (SessionPort,), object),
        PluginDescriptor("second", "1.0.0", (RunPort,), lambda: made.append("second") or object(), dependencies=("dependency",)),
    )
    host = PluginHost(descriptors, schema=schema(RunPort, SessionPort))
    assert [d.plugin_id for d in host.resolve_dependencies(["second"])] == ["dependency", "second"]
    await host.activate(plugin_ids=["second"])
    assert made == ["second"]
    assert host.discover() == descriptors
    with pytest.raises(PluginValidationError, match="duplicate capability"):
        host.resolve_dependencies()
    with pytest.raises(PluginValidationError, match="unknown plugin"):
        host.resolve_dependencies(["missing"])
    with pytest.raises(PluginValidationError, match="missing required"):
        host.resolve_dependencies([])


@pytest.mark.asyncio
@pytest.mark.parametrize("parent,dependency", [(PluginScope.PROCESS, PluginScope.SESSION), (PluginScope.PROCESS, PluginScope.RUN), (PluginScope.SESSION, PluginScope.RUN)])
async def test_shorter_lived_dependencies_fail_before_any_factory(parent, dependency):
    made = []
    host = PluginHost((
        PluginDescriptor("dependency", "1.0.0", (SessionPort,), lambda: made.append(1), scope=dependency),
        PluginDescriptor("parent", "1.0.0", (RunPort,), object, dependencies=("dependency",), scope=parent),
    ), schema=schema(SessionPort, RunPort))
    with pytest.raises(PluginValidationError, match="shorter-lived"):
        await host.activate(session_key="a")
    assert made == []


@pytest.mark.asyncio
async def test_factory_conflict_and_unknown_scope_fail_before_factories():
    made = []
    host = PluginHost((PluginDescriptor("bad", "1.0.0", (RunPort,), lambda: made.append(1), context_factory=lambda ctx: object()),), schema=schema(RunPort))
    with pytest.raises(PluginValidationError, match="exactly one"):
        await host.activate()
    assert made == []
    host = PluginHost((PluginDescriptor("good", "1.0.0", (RunPort,), lambda: made.append(1)),), schema=schema(RunPort))
    with pytest.raises(PluginScopeError, match="scope"):
        await host.activate(scopes=("nonsense",))
    assert made == []


@pytest.mark.asyncio
async def test_partial_activation_still_validates_full_required_bindings():
    made = []
    host = PluginHost((PluginDescriptor("process", "1.0.0", (ProcessPort,), lambda: made.append(1), scope=PluginScope.PROCESS),), schema=schema(ProcessPort, RunPort))
    with pytest.raises(PluginValidationError, match="missing required"):
        await host.activate(scopes=(PluginScope.PROCESS,))
    assert made == []


@pytest.mark.asyncio
async def test_failure_rollback_does_not_dispose_another_session():
    disposed = []
    def run(ctx):
        if ctx.context == "fail":
            raise TypeError("factory failed once")
        return object()
    host = PluginHost((
        PluginDescriptor("session", "1.0.0", (SessionPort,), object, scope=PluginScope.SESSION, disposer=disposed.append),
        PluginDescriptor("run", "1.0.0", (RunPort,), dependencies=("session",), context_factory=run),
    ), schema=schema(SessionPort, RunPort))
    first = await host.activate(session_key="a")
    with pytest.raises(TypeError, match="factory failed once"):
        await host.activate(session_key="b", contexts={PluginScope.RUN: "fail"})
    assert len(disposed) == 1
    assert first.registry[SessionPort] not in disposed
    again = await host.activate(session_key="a")
    assert again.registry[SessionPort] is first.registry[SessionPort]
    await host.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("close_method", ["dispose_session", "close"])
async def test_nested_interrupted_rollback_retries_dependents_before_dependencies(close_method):
    @runtime_checkable
    class CheckedPort(Protocol):
        def required(self) -> None: ...

    class Resource:
        def __init__(self, name, parent=None):
            self.name = name
            self.parent = parent
            self.closed = False

    parent = Resource("parent")
    child = Resource("child", parent)
    attempts, closed = {}, []

    async def dispose(resource):
        attempts[resource.name] = attempts.get(resource.name, 0) + 1
        if attempts[resource.name] == 1:
            raise asyncio.CancelledError("rollback interrupted")
        if resource.parent is not None:
            assert not resource.parent.closed, "dependency closed before dependent cleanup"
        resource.closed = True
        closed.append(resource.name)

    host = PluginHost((
        PluginDescriptor("parent", "1.0.0", (RunPort,),
                         factory=lambda: parent, disposer=dispose),
        PluginDescriptor("child", "1.0.0", (CheckedPort,),
                         factory=lambda: child, dependencies=("parent",), disposer=dispose),
    ), schema=schema(RunPort, CheckedPort))
    try:
        # The invalid child is retained by Port validation before the outer
        # activation retains its parent; both require a later cleanup retry.
        with pytest.raises(asyncio.CancelledError, match="rollback interrupted"):
            await host.activate(session_key="session")
        if close_method == "dispose_session":
            await host.dispose_session("session")
        else:
            await host.close()
        assert closed == ["child", "parent"]
        assert child.closed and parent.closed
        assert attempts == {"child": 2, "parent": 2}
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_run_only_activation_requires_cached_dependencies_before_factories():
    made = []
    session = PluginDescriptor(
        "session", "1.0.0", (SessionPort,), object,
        scope=PluginScope.SESSION,
    )
    run = PluginDescriptor(
        "run", "1.0.0", (RunPort,), dependencies=("session",),
        context_factory=lambda ctx: made.append(ctx.dependencies["session"]) or object(),
    )
    host = PluginHost((session, run), schema=schema(SessionPort, RunPort))
    with pytest.raises(PluginScopeError, match="no cached instance"):
        await host.activate(session_key="a", scopes=(PluginScope.RUN,))
    assert made == []
    activated_session = await host.activate(
        session_key="a", scopes=(PluginScope.SESSION,),
    )
    activated_run = await host.activate(session_key="a", scopes=(PluginScope.RUN,))
    assert made == [activated_session.registry[SessionPort]]
    assert SessionPort not in activated_run.registry
    await host.close()


@pytest.mark.asyncio
async def test_process_factory_without_process_context_never_receives_session_context():
    seen = []
    host = PluginHost((PluginDescriptor(
        "process", "1.0.0", (ProcessPort,), scope=PluginScope.PROCESS,
        context_factory=lambda ctx: seen.append(ctx.context) or object(),
    ),), schema=schema(ProcessPort))
    await host.activate(contexts={PluginScope.SESSION: Config("private")})
    assert seen == [None]
    await host.close()


@pytest.mark.asyncio
async def test_empty_selection_is_valid_without_required_capabilities():
    host = PluginHost((), schema=CapabilitySchema(()))
    assert host.resolve_dependencies([]) == ()
    activation = await host.activate(plugin_ids=[])
    assert len(activation.registry) == 0
    await host.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("context_factory", [None, 123])
async def test_invalid_context_factory_is_rejected_before_side_effects(context_factory):
    made = []
    host = PluginHost((
        PluginDescriptor("first", "1.0.0", (SessionPort,), lambda: made.append(1)),
        PluginDescriptor("bad", "1.0.0", (RunPort,), context_factory=context_factory),
    ), schema=schema(SessionPort, RunPort))
    with pytest.raises(PluginValidationError, match="factory"):
        await host.activate()
    assert made == []

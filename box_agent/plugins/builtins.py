"""Config-driven factories wrapping the existing shared capability modules."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

from ..hooks import HookManager
from ..kernel.ports import KernelServices
from ..session_context import RunContext, SessionContext
from .descriptors import PluginDescriptor, PluginFactoryContext, PluginScope
from .registries import CapabilityBinding, CapabilityPolicy, CapabilitySchema


@dataclass(eq=False)
class _CleanupCallback:
    callback: Callable
    args: tuple
    kwargs: dict
    completed: bool = False


class SessionCleanup:
    """Reverse-order cleanup retaining callbacks interrupted by cancellation.

    AsyncExitStack removes a callback before awaiting it, which would defeat
    the Host's retryable cancellation semantics. Ordinary failures remain
    terminal, matching PluginHost; only interrupted callbacks are retained.
    """

    def __init__(self) -> None:
        self._callbacks: list[_CleanupCallback] = []

    def callback(self, callback: Callable, *args: Any, **kwargs: Any) -> Callable:
        self._callbacks.append(_CleanupCallback(callback, args, kwargs))
        return callback

    def push_async_callback(self, callback: Callable, *args: Any, **kwargs: Any) -> Callable:
        return self.callback(callback, *args, **kwargs)

    async def aclose(self) -> None:
        from ..composition import _combined_cleanup_error

        errors = []
        for entry in reversed(tuple(self._callbacks)):
            try:
                result = entry.callback(*entry.args, **entry.kwargs)
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError as error:
                errors.append(error)
            except BaseException as error:
                entry.completed = True
                errors.append(error)
            else:
                entry.completed = True
        self._callbacks[:] = [entry for entry in self._callbacks if not entry.completed]
        if errors:
            raise _combined_cleanup_error(errors)


@dataclass
class SessionResources:
    """Prepared constructor inputs and the resources created for this session."""

    context: SessionContext
    llm_client: Any = None
    system_prompt: str = ""
    tools: list[Any] = field(default_factory=list)
    hooks: list[Any] | None = None
    memory_manager: Any = None
    skill_loader: Any = None
    mcp_task: Any = None
    skill_task: Any = None
    state: dict[str, Any] = field(default_factory=dict)
    cleanup: SessionCleanup = field(default_factory=SessionCleanup, repr=False)

    def __post_init__(self) -> None:
        self.state = dict(self.context.options.state)

    async def aclose(self) -> None:
        await self.cleanup.aclose()


@runtime_checkable
class SessionInitializerPort(Protocol):
    async def prepare(self) -> None: ...


@dataclass
class _Initializer:
    plugin_id: str
    resources: SessionResources
    prepare_callback: Callable

    async def prepare(self) -> None:
        context = self.resources.context
        context.emit("plugin.prepare", plugin_id=self.plugin_id, scope="session")
        await self.prepare_callback(self.resources)
        context.emit("plugin.ready", plugin_id=self.plugin_id, scope="session")


def _resource_factory(context: PluginFactoryContext) -> SessionResources:
    return SessionResources(context.context)


def _initializer_factory(plugin_id: str, function_name: str) -> Callable:
    def create(context: PluginFactoryContext) -> _Initializer:
        # Import lazily to keep the plugin catalog free of initialization I/O.
        from .. import session_assembly

        return _Initializer(
            plugin_id,
            context.dependencies["session.resources"],
            getattr(session_assembly, function_name),
        )
    return create


def _run_services(context: PluginFactoryContext) -> KernelServices:
    run: RunContext = context.context
    agent, options = run.agent, run.options
    return KernelServices(
        llm=options.llm,
        summary_llm=options.summary_llm,
        permission_gateway=options.permission_negotiator,
        memory_lookup=options.memory_manager,
        memory_extraction=options.memory_extractor,
        memory_promotion=options.memory_manager if agent.memory_promotion_enabled else None,
        session_store=agent.session_log,
        hook_bus=HookManager(options.hooks),
        tool_catalog=agent.tools,
        tool_exposure=agent.mcp_tool_exposure,
        tool_result_store=agent.tool_result_storage,
    )


SESSION_CAPABILITY_SCHEMA = CapabilitySchema((
    CapabilityBinding(SessionResources, CapabilityPolicy.REQUIRED_SINGLE),
    CapabilityBinding(SessionInitializerPort, CapabilityPolicy.MULTI),
    CapabilityBinding(KernelServices, CapabilityPolicy.REQUIRED_SINGLE),
))


def builtin_plugin_descriptors() -> tuple[PluginDescriptor, ...]:
    """Keep preparation order explicit and independent of tool exposure order."""
    descriptors = [PluginDescriptor(
        "session.resources", "1.0.0", (SessionResources,),
        scope=PluginScope.SESSION, context_factory=_resource_factory,
        disposer=lambda resource: resource.aclose(),
    )]
    previous = "session.resources"
    for name in ("model", "memory", "tools", "prompt", "hooks"):
        plugin_id = f"session.{name}"
        dependencies = tuple(dict.fromkeys(("session.resources", previous)))
        descriptors.append(PluginDescriptor(
            plugin_id, "1.0.0", (SessionInitializerPort,),
            scope=PluginScope.SESSION, dependencies=dependencies,
            context_factory=_initializer_factory(plugin_id, f"prepare_{name}"),
        ))
        previous = plugin_id
    descriptors.append(PluginDescriptor(
        "run.services", "1.0.0", (KernelServices,),
        scope=PluginScope.RUN, dependencies=(previous,), context_factory=_run_services,
    ))
    return tuple(descriptors)


__all__ = ["SessionResources", "SessionInitializerPort", "SESSION_CAPABILITY_SCHEMA",
           "builtin_plugin_descriptors"]

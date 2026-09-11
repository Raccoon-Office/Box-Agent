"""Application and session orchestration over the existing PluginHost."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ..composition import _attach_cleanup_error, _combined_cleanup_error
from ..session_context import RunContext, SessionContext
from .builtins import (SESSION_CAPABILITY_SCHEMA, SessionInitializerPort,
                       SessionResources, builtin_plugin_descriptors)
from .descriptors import PluginDescriptor, PluginScope
from .host import PluginActivation, PluginHost
from .registries import CapabilityBinding, CapabilitySchema


@dataclass(frozen=True, slots=True)
class ActivationPlan:
    plugin_ids: tuple[str, ...]


class PluginSession:
    def __init__(self, runtime: PluginRuntime, context: SessionContext,
                 plan: ActivationPlan, activation: PluginActivation) -> None:
        self.runtime = runtime
        self.context = context
        self.plan = plan
        self.activation = activation
        self.resources = activation.registry.require(SessionResources)
        self._closed = False
        self._closing = False
        self._run: PluginActivation | None = None
        self._run_opening = False
        self._close_lock = asyncio.Lock()
        self.owner: Any = None

    async def open_run(self, context: RunContext) -> PluginActivation:
        if self._closed or self._closing or self.runtime._closing:
            raise RuntimeError("plugin session is closed")
        if self._run is not None or self._run_opening:
            raise RuntimeError("session already has an active run")
        if context.session is not self.context:
            raise ValueError("run context belongs to another session")
        self._run_opening = True
        try:
            self._run = await self.runtime.host.activate(
                session_key=self.context.session_key,
                plugin_ids=self.plan.plugin_ids,
                contexts={PluginScope.PROCESS: None, PluginScope.SESSION: self.context,
                          PluginScope.RUN: context},
            )
        finally:
            self._run_opening = False
        self.context.emit("plugin.run.start", run_id=context.run_id)
        return self._run

    async def close_run(self, activation: PluginActivation) -> None:
        await activation.dispose()
        if self._run is activation:
            self._run = None
        self.context.emit("plugin.run.end")

    async def aclose(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closing = True
            if self._run_opening or (
                self.owner is not None and self.owner._active_run_task is not None
            ):
                raise RuntimeError("close the active run before its plugin session")
            if self._run is not None:
                await self.close_run(self._run)
            await self.runtime.host.dispose_session(self.context.session_key)
            await self.activation.dispose()
            self._closed = True
            self.runtime._sessions.pop(self.context.session_key, None)
            self.context.emit("plugin.session.closed")


class PluginRuntime:
    """One stable catalog and Host, shared across sessions and runs.

    Built-in factories read existing Config gates. Config-dependent resources
    are session-scoped; custom PROCESS factories receive application context
    only (currently None), never a session's Config.
    """

    def __init__(self, *, plugins: Iterable[PluginDescriptor] = (),
                 bindings: Iterable[CapabilityBinding] = ()) -> None:
        descriptors = builtin_plugin_descriptors() + tuple(plugins)
        self.host = PluginHost(descriptors, schema=CapabilitySchema(
            SESSION_CAPABILITY_SCHEMA.bindings + tuple(bindings),
        ))
        self._sessions: dict[str, PluginSession] = {}
        self._opening_keys: set[str] = set()
        self._opening: set[asyncio.Future] = set()
        self._closing = False
        self._closed = False
        self._close_lock = asyncio.Lock()

    def begin_open(self) -> asyncio.Future:
        """Reserve initialization through the caller's final construction step."""
        if self._closing or self._closed:
            raise RuntimeError("plugin runtime is closing or closed")
        opening = asyncio.get_running_loop().create_future()
        self._opening.add(opening)
        return opening

    def finish_open(self, opening: asyncio.Future) -> None:
        self._opening.discard(opening)
        if not opening.done():
            opening.set_result(None)

    async def open_session(self, context: SessionContext) -> PluginSession:
        if self._closing or self._closed:
            raise RuntimeError("plugin runtime is closed")
        if context.session_key in self._sessions or context.session_key in self._opening_keys:
            raise ValueError("session key is already active or opening")
        opening = self.begin_open()
        self._opening_keys.add(context.session_key)
        session = None
        try:
            plan = ActivationPlan(tuple(
                descriptor.plugin_id for descriptor in self.host.resolve_dependencies()
            ))
            context.emit("plugin.session.plan", plugin_ids=list(plan.plugin_ids))
            activation = await self.host.activate(
                session_key=context.session_key, plugin_ids=plan.plugin_ids,
                scopes=(PluginScope.PROCESS, PluginScope.SESSION),
                contexts={PluginScope.PROCESS: None, PluginScope.SESSION: context},
            )
            session = PluginSession(self, context, plan, activation)
            # Retain ownership even if preparation and its rollback both fail.
            # Runtime shutdown can then retry interrupted cleanup.
            self._sessions[context.session_key] = session
            # Preparation may perform I/O. It runs OUTSIDE Host lifecycle reservations.
            for initializer in activation.registry.get_all(SessionInitializerPort):
                await initializer.prepare()
            if self._closing:
                raise RuntimeError("plugin runtime is closing")
            return session
        except BaseException as error:
            if session is not None:
                try:
                    await session.aclose()
                except BaseException as cleanup_error:
                    _attach_cleanup_error(error, cleanup_error)
            raise
        finally:
            self._opening_keys.discard(context.session_key)
            self.finish_open(opening)

    async def aclose(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closing = True
            pending = tuple(self._opening)
            if pending:
                await asyncio.gather(*(asyncio.shield(item) for item in pending))
            errors = []
            for session in tuple(self._sessions.values()):
                try:
                    if session.owner is not None:
                        await session.owner.aclose()
                    else:
                        await session.aclose()
                except BaseException as error:
                    errors.append(error)
            if errors:
                raise _combined_cleanup_error(errors)
            await self.host.close()
            self._closed = True


__all__ = ["ActivationPlan", "PluginRuntime", "PluginSession"]

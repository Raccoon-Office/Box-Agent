"""Outer composition boundary for the default kernel capability bundle."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import fields, replace
from typing import Any

from .events import AgentEvent
from .hooks import HookBus, HookManager, HookRegistration, LegacyHookAdapter, HookConfigError
from .kernel.loop import AgentLoopKernel
from .kernel.ports import KernelServices
from .plugins.defaults import (
    compose_default_services,
    create_default_plugin_host,
    kernel_services_from_registry,
    skill_loader_from_catalog,
)
from .plugins.host import PluginActivation, PluginCleanupError, PluginHost
from .plugins.hooks import HookOwner, HookProviderPort


_SERVICE_OWNED_RUN_ARGUMENTS = frozenset(
    {
        "llm",
        "summary_llm",
        "tools",
        "permission_negotiator",
        "hooks",
        "plugins",
        "memory_manager",
        "memory_extractor",
        "session_log",
        "tool_exposure_manager",
        "tool_result_storage",
        "kernel_services",
        "skill_engine",
        "context_engine",
        "compact_engine",
    }
)


def _default_capabilities(run_arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Translate legacy run arguments without copying capability instances."""

    memory_manager = run_arguments.get("memory_manager")
    skill_engine = run_arguments.get("skill_engine")
    if skill_engine is None:
        from .skill_runtime import SkillRuntime
        loader = skill_loader_from_catalog(run_arguments["tools"])
        if loader is not None:
            # Bind persistence only after SessionStorePort replacement.
            skill_engine = SkillRuntime(loader)
    return {
        "llm": run_arguments["llm"],
        "summary_llm": run_arguments.get("summary_llm"),
        "permission_gateway": run_arguments.get("permission_negotiator"),
        "memory_lookup": memory_manager,
        "memory_extraction": run_arguments.get("memory_extractor"),
        "memory_promotion": (
            memory_manager
            if run_arguments.get("memory_promotion_enabled", False)
            else None
        ),
        "session_store": run_arguments.get("session_log"),
        "hook_bus": HookBus(
            session_id=run_arguments.get("session_id", ""),
            is_cancelled=run_arguments.get("is_cancelled"),
        ),
        "tool_catalog": run_arguments["tools"],
        "tool_exposure": run_arguments.get("tool_exposure_manager"),
        "tool_result_store": run_arguments.get("tool_result_storage"),
        "skill_engine": skill_engine,
        "context_engine": run_arguments.get("context_engine"),
        "compact_engine": run_arguments.get("compact_engine"),
    }


def _kernel_run_arguments(run_arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Keep capability objects behind the resolved ``KernelServices`` bundle."""

    return {
        name: value
        for name, value in run_arguments.items()
        if name not in _SERVICE_OWNED_RUN_ARGUMENTS
    }


def _register_legacy_hooks(bus: HookBus, hooks: Sequence[object]) -> list[HookRegistration]:
    """保留旧列表顺序，归属由装配层生成。"""
    tokens = []
    for index, hook in enumerate(hooks):
        owner = HookOwner(f"legacy.hook-{index}", "0.0.0", f"legacy:{index}", bus.context.run_id)
        for spec in LegacyHookAdapter(hook).get_hooks():
            tokens.append(bus.register(spec, owner))
    return tokens


async def register_run_hooks(
    *, bus: HookBus, activation: PluginActivation, legacy_hooks: Sequence[object],
) -> tuple[HookRegistration, ...]:
    """取得插件声明，绑定来源，全部注册成功后冻结当前 Run。"""
    tokens: list[HookRegistration] = []
    try:
        tokens.extend(_register_legacy_hooks(bus, legacy_hooks))
        for contribution in activation.contributions(HookProviderPort):
            descriptor = contribution.descriptor
            owner = HookOwner(
                descriptor.plugin_id, descriptor.version, f"plugin:{descriptor.plugin_id}",
                bus.context.run_id, descriptor.scope.value,
            )
            for spec in contribution.instance.get_hooks():
                tokens.append(bus.register(spec, owner))
        bus.freeze()
        return tuple(tokens)
    except BaseException:
        await bus.close()
        raise


def compose_default_kernel_services(
    run_arguments: Mapping[str, Any],
) -> KernelServices:
    """Resolve one immutable bundle from the existing call arguments."""

    from .tools.engine.engine import DefaultToolEngine

    if run_arguments.get("plugins"):
        raise HookConfigError("静态插件需要通过异步运行入口激活")
    capabilities = _default_capabilities(run_arguments)
    bus = capabilities["hook_bus"]
    _register_legacy_hooks(bus, run_arguments.get("hooks") or ())
    bus.freeze()
    services = compose_default_services(**capabilities)
    if services.context_engine is not None:
        services.context_engine.bind_history(run_arguments.get("messages") or [])
    return replace(
        services,
        hook_context=bus.context,
        tool_engine=DefaultToolEngine(
            tools=services.tool_catalog,
            tool_exposure=services.tool_exposure,
            tool_result_store=services.tool_result_store,
        ),
    )


def _validate_managed_services(
    services: KernelServices,
    run_arguments: Mapping[str, Any],
) -> None:
    """Reject contradictory legacy capabilities before kernel execution."""

    memory_manager = run_arguments.get("memory_manager")
    expected = {
        "llm": ("llm", run_arguments["llm"]),
        "summary_llm": ("summary_llm", run_arguments.get("summary_llm")),
        "permission_gateway": (
            "permission_negotiator",
            run_arguments.get("permission_negotiator"),
        ),
        "memory_lookup": ("memory_manager", memory_manager),
        "memory_extraction": (
            "memory_extractor",
            run_arguments.get("memory_extractor"),
        ),
        "memory_promotion": (
            "memory promotion gate",
            memory_manager
            if run_arguments.get("memory_promotion_enabled", False)
            else None,
        ),
        "session_store": ("session_log", run_arguments.get("session_log")),
        "tool_catalog": ("tools", run_arguments["tools"]),
        "tool_exposure": (
            "tool_exposure_manager",
            run_arguments.get("tool_exposure_manager"),
        ),
        "tool_result_store": (
            "tool_result_storage",
            run_arguments.get("tool_result_storage"),
        ),
    }
    mismatches = [
        public_name
        for field_name, (public_name, value) in expected.items()
        if getattr(services, field_name) is not value
    ]
    effective_hooks = list(run_arguments.get("hooks") or ())
    if not isinstance(services.hook_bus, HookManager):
        mismatches.append("hooks")
    elif len(services.hook_bus.hooks) != len(effective_hooks) or any(
        actual is not expected
        for actual, expected in zip(services.hook_bus.hooks, effective_hooks)
    ):
        mismatches.append("hooks")
    if mismatches:
        names = ", ".join(mismatches)
        raise ValueError(
            f"kernel_services contradict effective run capabilities: {names}"
        )


def _add_cleanup_note(error: BaseException, note: str) -> None:
    """Keep cleanup diagnostics inspectable on Python 3.10 as well."""

    add_note = getattr(error, "add_note", None)
    if callable(add_note):
        add_note(note)
    else:
        if not hasattr(error, "__notes__"):
            error.__notes__ = []
        error.__notes__.append(note)


def _combined_cleanup_error(errors: list[BaseException]) -> BaseException:
    flattened: list[BaseException] = []
    for error in errors:
        if isinstance(error, PluginCleanupError):
            flattened.extend(error.errors)
        else:
            flattened.append(error)
    cancellations = [
        error for error in flattened if isinstance(error, asyncio.CancelledError)
    ]
    if cancellations:
        cancellation = cancellations[0]
        ordinary_errors = [
            error
            for error in flattened
            if not isinstance(error, asyncio.CancelledError)
        ]
        if ordinary_errors:
            ordinary_failure: BaseException
            if len(ordinary_errors) == 1:
                ordinary_failure = ordinary_errors[0]
            else:
                ordinary_failure = PluginCleanupError(ordinary_errors)
            _attach_cleanup_error(cancellation, ordinary_failure)
        for additional in cancellations[1:]:
            _add_cleanup_note(
                cancellation,
                f"Additional cleanup cancellation: {additional!r}"
            )
        return cancellation
    if len(flattened) == 1:
        return flattened[0]
    return PluginCleanupError(flattened)


async def _cleanup_plugin_run(
    *,
    activation: PluginActivation | None,
    host: PluginHost,
) -> None:
    """Release one activation and host while attempting every cleanup step."""

    errors: list[BaseException] = []
    if activation is not None:
        try:
            await activation.dispose()
        except BaseException as error:
            errors.append(error)
    try:
        await host.close()
    except BaseException as error:
        errors.append(error)
    if errors:
        raise _combined_cleanup_error(errors)


def _attach_cleanup_error(
    primary_error: BaseException,
    cleanup_error: BaseException,
) -> None:
    """Keep execution failure primary while making cleanup failure inspectable."""

    if primary_error.__cause__ is None:
        primary_error.__cause__ = cleanup_error
    else:
        _add_cleanup_note(primary_error, f"Additional cleanup failure: {cleanup_error!r}")


_HOOK_CLEANUP_TASKS: set[asyncio.Task] = set()


async def _cleanup_hook_run(
    bus: HookBus, activation: PluginActivation | None, host: PluginHost | None,
) -> BaseException | None:
    """Drain handlers before releasing providers; carry failures across Task boundaries."""
    errors: list[BaseException] = []
    try:
        await bus.close()
    except BaseException as error:
        if bus.state != "Closed":
            return error
        errors.append(error)
    if host is not None:
        try:
            await _cleanup_plugin_run(activation=activation, host=host)
        except BaseException as error:
            if isinstance(error, asyncio.CancelledError):
                # This private Run Host has no later Session owner to retry it.
                # Keep its cleanup lease until all interrupted records settle.
                while host.has_live_instances:
                    try:
                        await host.close()
                    except asyncio.CancelledError:
                        await asyncio.sleep(0)
                    except BaseException as retry_error:
                        _attach_cleanup_error(error, retry_error)
                        break
            # Python 3.10 wraps cancellation raised by Task.result(), losing its
            # top-level cause. A task result preserves the exact error object.
            errors.append(error)
    return _combined_cleanup_error(errors) if errors else None


async def _wait_for_hook_cleanup(task: asyncio.Task, *, settle: bool) -> None:
    """Managed runs cannot release their session resources before hooks finish."""
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            cleanup_error = await asyncio.shield(task)
            break
        except asyncio.CancelledError as error:
            if not settle or task.cancelled():
                if task.done() and not task.cancelled():
                    completed_error = task.result()
                    if completed_error is not None:
                        raise _combined_cleanup_error([completed_error, error])
                raise
            if cancellation is None:
                cancellation = error
    if cancellation is not None:
        if cleanup_error is not None:
            raise _combined_cleanup_error([cleanup_error, cancellation])
        raise cancellation
    if cleanup_error is not None:
        raise cleanup_error


def _cleanup_task_finished(task: asyncio.Task) -> None:
    _HOOK_CLEANUP_TASKS.discard(task)
    if not task.cancelled():
        task.exception()


async def run_agent_loop_with_default_services(
    *,
    run_arguments: Mapping[str, Any],
    runtime_defaults: Any,
) -> AsyncIterator[AgentEvent]:
    """Bind run hooks to default or borrowed managed capabilities, then run the kernel."""
    managed_services = run_arguments.get("kernel_services")
    if managed_services is not None:
        if not isinstance(managed_services, KernelServices):
            raise TypeError("kernel_services must be a KernelServices instance")
        _validate_managed_services(managed_services, run_arguments)

    capabilities = _default_capabilities(run_arguments)
    if managed_services is not None:
        for name in ("skill_engine", "context_engine"):
            supplied = getattr(managed_services, name)
            if supplied is not None and supplied is not capabilities[name]:
                raise ValueError(f"kernel_services contradict effective run capabilities: {name}")
        supplied_compact = managed_services.compact_engine
        requested_compact = capabilities["compact_engine"]
        if (supplied_compact is not None and requested_compact is not None
                and supplied_compact is not requested_compact):
            raise ValueError("kernel_services contradict effective run capabilities: compact_engine")
        capabilities["compact_engine"] = (
            supplied_compact if supplied_compact is not None else requested_compact
        )
        bound = compose_default_services(**capabilities)
        managed_services = replace(
            managed_services, skill_engine=bound.skill_engine,
            context_engine=bound.context_engine, compact_engine=bound.compact_engine,
        )
        capabilities["skill_engine"] = bound.skill_engine
        capabilities["context_engine"] = bound.context_engine
        capabilities["compact_engine"] = bound.compact_engine
    bus = capabilities["hook_bus"]
    plugins = tuple(run_arguments.get("plugins") or ())
    host: PluginHost | None = None
    if managed_services is None or plugins:
        if managed_services is not None:
            # Captured descriptors borrow all existing capabilities. Only this
            # separate RUN-only extension host owns the supplied Hook providers.
            capabilities["tool_engine"] = managed_services.tool_engine
        host = create_default_plugin_host(
            **capabilities, **({"plugins": plugins} if plugins else {}),
        )
    activation: PluginActivation | None = None
    events: AsyncIterator[AgentEvent] | None = None
    primary_error: BaseException | None = None
    try:
        if host is not None:
            activation = await host.activate()
            resolved = kernel_services_from_registry(activation.registry)
            if managed_services is not None:
                mismatches = [
                    field.name for field in fields(KernelServices)
                    if field.name not in {"hook_bus", "hook_dispatch", "hook_context"}
                    and getattr(resolved, field.name) is not getattr(managed_services, field.name)
                ]
                if mismatches:
                    raise ValueError(
                        "run plugins contradict managed capabilities: " + ", ".join(mismatches)
                    )
            await register_run_hooks(
                bus=bus, activation=activation,
                legacy_hooks=run_arguments.get("hooks") or (),
            )
        else:
            _register_legacy_hooks(bus, run_arguments.get("hooks") or ())
            bus.freeze()
        services = replace(
            managed_services if managed_services is not None else resolved,
            hook_bus=bus, hook_dispatch=bus, hook_context=bus.context,
        )
        if services.context_engine is not None:
            services.context_engine.bind_history(run_arguments.get("messages") or [])
        kernel = AgentLoopKernel(
            _services=services,
            _runtime_defaults=runtime_defaults,
            **_kernel_run_arguments(run_arguments),
        )
        events = kernel.run()
        async for event in events:
            yield event
    except GeneratorExit:
        raise
    except BaseException as error:
        primary_error = error
        raise
    finally:
        cleanup_errors: list[BaseException] = []
        if events is not None:
            try:
                await events.aclose()
            except BaseException as error:
                cleanup_errors.append(error)
        try:
            cleanup_task = asyncio.create_task(_cleanup_hook_run(bus, activation, host))
            _HOOK_CLEANUP_TASKS.add(cleanup_task)
            cleanup_task.add_done_callback(_cleanup_task_finished)
            await _wait_for_hook_cleanup(
                cleanup_task, settle=managed_services is not None,
            )
        except BaseException as error:
            cleanup_errors.append(error)
        if cleanup_errors:
            cleanup_error = _combined_cleanup_error(cleanup_errors)
            if primary_error is None:
                raise cleanup_error
            _attach_cleanup_error(primary_error, cleanup_error)


__all__ = [
    "compose_default_kernel_services",
    "run_agent_loop_with_default_services",
]

"""Outer composition boundary for the default kernel capability bundle."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from typing import Any

from .events import AgentEvent
from .hooks import HookManager
from .kernel.loop import AgentLoopKernel
from .kernel.ports import KernelServices
from .plugins.defaults import (
    compose_default_services,
    create_default_plugin_host,
    kernel_services_from_registry,
)
from .plugins.host import PluginActivation, PluginCleanupError, PluginHost


_SERVICE_OWNED_RUN_ARGUMENTS = frozenset(
    {
        "llm",
        "summary_llm",
        "tools",
        "permission_negotiator",
        "hooks",
        "memory_manager",
        "memory_extractor",
        "session_log",
        "tool_exposure_manager",
        "tool_result_storage",
    }
)


def _default_capabilities(run_arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Translate legacy run arguments without copying capability instances."""

    memory_manager = run_arguments.get("memory_manager")
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
        "hook_bus": HookManager(run_arguments.get("hooks")),
        "tool_catalog": run_arguments["tools"],
        "tool_exposure": run_arguments.get("tool_exposure_manager"),
        "tool_result_store": run_arguments.get("tool_result_storage"),
    }


def _kernel_run_arguments(run_arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Keep capability objects behind the resolved ``KernelServices`` bundle."""

    return {
        name: value
        for name, value in run_arguments.items()
        if name not in _SERVICE_OWNED_RUN_ARGUMENTS
    }


def compose_default_kernel_services(
    run_arguments: Mapping[str, Any],
) -> KernelServices:
    """Resolve one immutable bundle from the existing call arguments."""

    return compose_default_services(**_default_capabilities(run_arguments))


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


async def run_agent_loop_with_default_services(
    *,
    run_arguments: Mapping[str, Any],
    runtime_defaults: Any,
) -> AsyncIterator[AgentEvent]:
    """Resolve defaults lazily, then delegate one run to the pure kernel."""

    host = create_default_plugin_host(**_default_capabilities(run_arguments))
    activation: PluginActivation | None = None
    events: AsyncIterator[AgentEvent] | None = None
    primary_error: BaseException | None = None
    try:
        activation = await host.activate()
        services = kernel_services_from_registry(activation.registry)
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
            await _cleanup_plugin_run(activation=activation, host=host)
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

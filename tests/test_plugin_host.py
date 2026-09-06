"""Default composition checks for kernel-owned capability ports."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError, fields, replace
import inspect
from typing import Any

import pytest

from box_agent.events import ContentEvent
from box_agent.schema import LLMResponse, Message, StreamEvent
from box_agent.tools.base import Tool


SERVICE_OWNED_RUN_ARGUMENTS = frozenset(
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


class _LLM:
    async def generate(self, **_kwargs: Any) -> LLMResponse:
        return LLMResponse(content="summary")

    async def generate_stream(self, **_kwargs: Any):
        yield StreamEvent(type="text", delta="done")
        yield StreamEvent(type="finish", finish_reason="stop")


class _PermissionGateway:
    async def negotiate(self, _request: dict[str, Any]) -> bool:
        return True


class _MemoryLookup:
    def auto_match_context(
        self,
        _query: str,
        *,
        limit: int = 3,
    ) -> list[dict[str, str]]:
        return []


class _MemoryExtraction:
    async def maybe_extract(
        self,
        _messages: list[Message],
        _trigger: str,
        *,
        turn_id: str | None = None,
    ) -> bool:
        return False


class _MemoryPromotion:
    def list_promotion_candidates(
        self,
        *,
        hit_threshold: int,
        cooldown_days: int,
    ) -> list[Any]:
        return []

    def mark_proposed(self, _candidate_ids: list[str]) -> None:
        return None

    def read_all_context_entries(self) -> list[Any]:
        return []

    async def plan_promotion(
        self,
        _candidates: list[Any],
        _llm: Any,
    ) -> Any | None:
        return None


class _SessionStore:
    def append(self, _event_type: str, _data: dict[str, Any], **_kwargs: Any):
        return {}

    def append_unlogged_messages(
        self,
        _messages: list[Message],
        *,
        turn: int,
        step: int | None,
        tool_result_metadata: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        return []

    def replace_surface(
        self,
        _messages: list[Message],
        *,
        turn: int,
        step: int,
    ) -> list[dict[str, Any]]:
        return []

    def flush(self) -> None:
        return None


class _HookBus:
    hooks: list[Any] = []

    async def fire_agent_start(self, **_kwargs: Any) -> None:
        return None

    async def fire_step_start(self, **_kwargs: Any) -> None:
        return None

    async def fire_llm_response(self, **_kwargs: Any) -> None:
        return None

    async def fire_step_end(self, **_kwargs: Any) -> None:
        return None

    async def fire_done(self, **_kwargs: Any) -> None:
        return None

    async def fire_error(self, **_kwargs: Any) -> None:
        return None

    async def fire_tool_start(self, **kwargs: Any) -> dict[str, Any]:
        return kwargs["arguments"]

    async def fire_tool_result(
        self,
        **kwargs: Any,
    ) -> tuple[str, str | None]:
        return kwargs["content"], kwargs["error"]


class _ToolCatalog(dict[str, Tool]):
    pass


class _ExposureOutcome:
    def __init__(self, tools: list[Tool]) -> None:
        self.tools = tools
        self.offered_names = frozenset(tool.name for tool in tools)
        self.mcp_generations: dict[str, int] = {}


class _ToolExposure:
    def prepare_tools(self, candidates: list[Tool]) -> _ExposureOutcome:
        return _ExposureOutcome(candidates)

    def validate_call(
        self,
        name: str,
        offered_generation: int | None,
        target_tool: Tool | None = None,
    ) -> str | None:
        return None


class _ToolResultStore:
    aggregate_budget = 50_000

    def set_context_token_limit(self, _token_limit: int) -> None:
        return None

    def initialize_history(self, _messages: list[Message]) -> None:
        return None

    def process_message(self, message: Message, **_kwargs: Any) -> Message:
        return message

    def enforce_fresh_budget(
        self,
        _messages: list[Message],
        **_kwargs: Any,
    ) -> "_BudgetOutcome":
        return _BudgetOutcome()


class _BudgetOutcome:
    persisted_count = 0
    fresh_count = 0
    original_chars = 0
    remaining_chars = 0


def _call_shape(method: Any) -> tuple[tuple[str, inspect._ParameterKind, Any], ...]:
    empty = inspect.Parameter.empty
    return tuple(
        (
            parameter.name,
            parameter.kind,
            empty if parameter.default is empty else parameter.default,
        )
        for parameter in inspect.signature(method).parameters.values()
    )


def test_current_capability_shapes_satisfy_kernel_ports() -> None:
    from box_agent.kernel.ports import (
        HookBusPort,
        LLMPort,
        MemoryExtractionPort,
        MemoryLookupPort,
        MemoryPromotionPort,
        PermissionGatewayPort,
        SessionStorePort,
        SummaryLLMPort,
        ToolCatalogPort,
        ToolExposureResultPort,
        ToolExposurePort,
        ToolResultBudgetOutcomePort,
        ToolResultStorePort,
    )

    assert isinstance(_LLM(), LLMPort)
    assert isinstance(_LLM(), SummaryLLMPort)
    assert isinstance(_PermissionGateway(), PermissionGatewayPort)
    assert isinstance(_MemoryLookup(), MemoryLookupPort)
    assert isinstance(_MemoryExtraction(), MemoryExtractionPort)
    assert isinstance(_MemoryPromotion(), MemoryPromotionPort)
    assert isinstance(_SessionStore(), SessionStorePort)
    assert isinstance(_HookBus(), HookBusPort)
    assert isinstance(_ToolCatalog(), ToolCatalogPort)
    assert isinstance(_ToolExposure(), ToolExposurePort)
    assert isinstance(_ExposureOutcome([]), ToolExposureResultPort)
    assert isinstance(_ToolResultStore(), ToolResultStorePort)
    assert isinstance(_BudgetOutcome(), ToolResultBudgetOutcomePort)


def test_production_defaults_match_port_call_shapes() -> None:
    from box_agent.cli_permissions import CLIPermissionNegotiator
    from box_agent.hooks import HookManager
    from box_agent.kernel.ports import (
        HookBusPort,
        LLMPort,
        MemoryExtractionPort,
        MemoryLookupPort,
        MemoryPromotionPort,
        PermissionGatewayPort,
        SessionStorePort,
        SummaryLLMPort,
        ToolExposureResultPort,
        ToolExposurePort,
        ToolResultBudgetOutcomePort,
        ToolResultStorePort,
    )
    from box_agent.llm.base import LLMClientBase
    from box_agent.memory import MemoryExtractor, MemoryManager
    from box_agent.session_log import SessionLog
    from box_agent.tool_result_storage import (
        ToolResultBudgetOutcome,
        ToolResultStorage,
    )
    from box_agent.tools.mcp_tool_search import MCPToolExposureManager, ToolExposure

    method_pairs = (
        (SummaryLLMPort.generate, LLMClientBase.generate),
        (LLMPort.generate_stream, LLMClientBase.generate_stream),
        (PermissionGatewayPort.negotiate, CLIPermissionNegotiator.negotiate),
        (MemoryLookupPort.auto_match_context, MemoryManager.auto_match_context),
        (MemoryExtractionPort.maybe_extract, MemoryExtractor.maybe_extract),
        (
            MemoryPromotionPort.list_promotion_candidates,
            MemoryManager.list_promotion_candidates,
        ),
        (MemoryPromotionPort.mark_proposed, MemoryManager.mark_proposed),
        (
            MemoryPromotionPort.read_all_context_entries,
            MemoryManager.read_all_context_entries,
        ),
        (MemoryPromotionPort.plan_promotion, MemoryManager.plan_promotion),
        (SessionStorePort.append, SessionLog.append),
        (SessionStorePort.append_unlogged_messages, SessionLog.append_unlogged_messages),
        (SessionStorePort.replace_surface, SessionLog.replace_surface),
        (SessionStorePort.flush, SessionLog.flush),
        (HookBusPort.fire_agent_start, HookManager.fire_agent_start),
        (HookBusPort.fire_step_start, HookManager.fire_step_start),
        (HookBusPort.fire_llm_response, HookManager.fire_llm_response),
        (HookBusPort.fire_step_end, HookManager.fire_step_end),
        (HookBusPort.fire_done, HookManager.fire_done),
        (HookBusPort.fire_error, HookManager.fire_error),
        (HookBusPort.fire_tool_start, HookManager.fire_tool_start),
        (HookBusPort.fire_tool_result, HookManager.fire_tool_result),
        (ToolExposurePort.prepare_tools, MCPToolExposureManager.prepare_tools),
        (ToolExposurePort.validate_call, MCPToolExposureManager.validate_call),
        (
            ToolResultStorePort.set_context_token_limit,
            ToolResultStorage.set_context_token_limit,
        ),
        (ToolResultStorePort.initialize_history, ToolResultStorage.initialize_history),
        (ToolResultStorePort.process_message, ToolResultStorage.process_message),
        (ToolResultStorePort.enforce_fresh_budget, ToolResultStorage.enforce_fresh_budget),
    )

    for port_method, concrete_method in method_pairs:
        assert _call_shape(port_method) == _call_shape(concrete_method)

    assert isinstance(
        ToolExposure(tools=[], offered_names=frozenset(), mcp_generations={}),
        ToolExposureResultPort,
    )
    assert isinstance(
        ToolResultBudgetOutcome(),
        ToolResultBudgetOutcomePort,
    )


def test_default_composition_preserves_identity_and_is_frozen() -> None:
    from box_agent.plugins.defaults import compose_default_services

    capabilities = {
        "llm": _LLM(),
        "summary_llm": _LLM(),
        "permission_gateway": _PermissionGateway(),
        "memory_lookup": _MemoryLookup(),
        "memory_extraction": _MemoryExtraction(),
        "memory_promotion": _MemoryPromotion(),
        "session_store": _SessionStore(),
        "hook_bus": _HookBus(),
        "tool_catalog": _ToolCatalog(),
        "tool_exposure": _ToolExposure(),
        "tool_result_store": _ToolResultStore(),
    }

    services = compose_default_services(**capabilities)

    for field_name, capability in capabilities.items():
        assert getattr(services, field_name) is capability
    with pytest.raises(FrozenInstanceError):
        services.llm = _LLM()  # type: ignore[misc]


def test_default_composition_preserves_optional_none_values() -> None:
    from box_agent.plugins.defaults import compose_default_services

    services = compose_default_services(
        llm=_LLM(),
        summary_llm=None,
        permission_gateway=None,
        memory_lookup=None,
        memory_extraction=None,
        memory_promotion=None,
        session_store=None,
        hook_bus=_HookBus(),
        tool_catalog=_ToolCatalog(),
        tool_exposure=None,
        tool_result_store=None,
    )

    assert services.summary_llm is None
    assert services.permission_gateway is None
    assert services.memory_lookup is None
    assert services.memory_extraction is None
    assert services.memory_promotion is None
    assert services.session_store is None
    assert services.tool_exposure is None
    assert services.tool_result_store is None


def test_disabled_memory_promotion_is_absent_from_default_services() -> None:
    from box_agent.composition import compose_default_kernel_services

    class MemoryManager(_MemoryPromotion, _MemoryLookup):
        pass

    memory = MemoryManager()
    run_arguments = {
        "llm": _LLM(),
        "messages": [Message(role="user", content="respond")],
        "tools": {},
        "memory_manager": memory,
        "memory_promotion_enabled": False,
    }

    disabled = compose_default_kernel_services(run_arguments)
    enabled = compose_default_kernel_services(
        {**run_arguments, "memory_promotion_enabled": True}
    )

    assert disabled.memory_lookup is memory
    assert disabled.memory_promotion is None
    assert enabled.memory_promotion is memory


def test_default_capability_schema_covers_kernel_services_in_field_order() -> None:
    from box_agent.kernel.ports import (
        HookBusPort,
        KernelServices,
        LLMPort,
        MemoryExtractionPort,
        MemoryLookupPort,
        MemoryPromotionPort,
        PermissionGatewayPort,
        SessionStorePort,
        SummaryLLMPort,
        ToolCatalogPort,
        ToolExposurePort,
        ToolResultStorePort,
    )
    from box_agent.plugins.defaults import DEFAULT_CAPABILITY_SCHEMA
    from box_agent.plugins.registries import CapabilityPolicy

    bindings = DEFAULT_CAPABILITY_SCHEMA.bindings

    ports_by_field = {
        "llm": LLMPort,
        "summary_llm": SummaryLLMPort,
        "permission_gateway": PermissionGatewayPort,
        "memory_lookup": MemoryLookupPort,
        "memory_extraction": MemoryExtractionPort,
        "memory_promotion": MemoryPromotionPort,
        "session_store": SessionStorePort,
        "hook_bus": HookBusPort,
        "tool_catalog": ToolCatalogPort,
        "tool_exposure": ToolExposurePort,
        "tool_result_store": ToolResultStorePort,
    }
    assert tuple(binding.port_type for binding in bindings) == tuple(
        ports_by_field[field.name] for field in fields(KernelServices)
    )
    policies = {binding.port_type: binding.policy for binding in bindings}
    assert policies[LLMPort] is CapabilityPolicy.REQUIRED_SINGLE
    assert policies[HookBusPort] is CapabilityPolicy.REQUIRED_SINGLE
    assert policies[ToolCatalogPort] is CapabilityPolicy.REQUIRED_SINGLE
    assert sum(
        policy is CapabilityPolicy.REQUIRED_SINGLE for policy in policies.values()
    ) == 3
    assert all(policy is not CapabilityPolicy.MULTI for policy in policies.values())


def test_default_descriptors_are_deterministic_and_preserve_exact_instances() -> None:
    from box_agent.kernel.ports import (
        MemoryLookupPort,
        MemoryPromotionPort,
        PermissionGatewayPort,
        ToolCatalogPort,
    )
    from box_agent.plugins.defaults import default_plugin_descriptors
    from box_agent.plugins.descriptors import PluginScope

    class FalseyPermission(_PermissionGateway):
        def __bool__(self) -> bool:
            return False

    llm = _LLM()
    falsey_permission = FalseyPermission()
    memory = _MemoryPromotion()
    memory.auto_match_context = lambda *_args, **_kwargs: []  # type: ignore[attr-defined]
    tools: dict[str, Tool] = {}
    capabilities = dict(
        llm=llm,
        summary_llm=None,
        permission_gateway=falsey_permission,
        memory_lookup=memory,
        memory_extraction=None,
        memory_promotion=memory,
        session_store=None,
        hook_bus=_HookBus(),
        tool_catalog=tools,
        tool_exposure=None,
        tool_result_store=None,
    )

    first = default_plugin_descriptors(**capabilities)
    second = default_plugin_descriptors(**capabilities)

    assert tuple(descriptor.plugin_id for descriptor in first) == tuple(
        descriptor.plugin_id for descriptor in second
    )
    assert tuple(descriptor.capabilities for descriptor in first) == tuple(
        descriptor.capabilities for descriptor in second
    )
    assert all(descriptor.scope is PluginScope.RUN for descriptor in first)
    assert all(descriptor.disposer is None for descriptor in first)
    by_port = {
        descriptor.capabilities[0]: descriptor.factory() for descriptor in first
    }
    assert by_port[PermissionGatewayPort] is falsey_permission
    assert by_port[MemoryLookupPort] is memory
    assert by_port[MemoryPromotionPort] is memory
    assert by_port[ToolCatalogPort] is tools
    assert len(first) == 6


@pytest.mark.asyncio
async def test_default_host_registry_maps_to_services_and_allows_static_replacement() -> None:
    from box_agent.kernel.ports import LLMPort
    from box_agent.plugins.defaults import (
        DEFAULT_CAPABILITY_SCHEMA,
        default_plugin_descriptors,
        kernel_services_from_registry,
    )
    from box_agent.plugins.host import PluginHost

    original_llm = _LLM()
    replacement_llm = _LLM()
    hook_bus = _HookBus()
    tools: dict[str, Tool] = {}
    descriptors = default_plugin_descriptors(
        llm=original_llm,
        summary_llm=None,
        permission_gateway=None,
        memory_lookup=None,
        memory_extraction=None,
        memory_promotion=None,
        session_store=None,
        hook_bus=hook_bus,
        tool_catalog=tools,
        tool_exposure=None,
        tool_result_store=None,
    )
    replaced = tuple(
        replace(descriptor, factory=lambda: replacement_llm)
        if descriptor.capabilities == (LLMPort,)
        else descriptor
        for descriptor in descriptors
    )
    host = PluginHost(replaced, schema=DEFAULT_CAPABILITY_SCHEMA)

    activation = await host.activate()
    try:
        services = kernel_services_from_registry(activation.registry)
        assert services.llm is replacement_llm
        assert services.hook_bus is hook_bus
        assert services.tool_catalog is tools
        assert services.summary_llm is None
    finally:
        await activation.dispose()
        await host.close()


@pytest.mark.asyncio
async def test_hooks_are_snapshotted_when_core_iteration_begins() -> None:
    import box_agent.core as core

    starts: list[str] = []

    class LateHook:
        async def on_agent_start(self, **_kwargs: Any) -> None:
            starts.append("started")

    hooks: list[Any] = []
    run_arguments = dict(
        llm=_LLM(),
        messages=[Message(role="user", content="respond")],
        tools={},
        hooks=hooks,
        max_steps=1,
    )
    events = core.run_agent_loop(**run_arguments)
    hooks.append(LateHook())

    try:
        await anext(events)
        assert starts == ["started"]
    finally:
        await events.aclose()


@pytest.mark.asyncio
async def test_outer_composition_passes_only_services_and_closes_host(
    monkeypatch,
) -> None:
    import box_agent.composition as composition
    import box_agent.core as core
    from box_agent.kernel.ports import KernelServices
    from box_agent.plugins.defaults import (
        DEFAULT_CAPABILITY_SCHEMA,
        create_default_plugin_host,
    )
    from box_agent.plugins.host import PluginHost

    lifecycle: list[str] = []
    hosts: list[PluginHost] = []
    captured: dict[str, Any] = {}

    class RecordingHost(PluginHost):
        async def activate(self, **kwargs: Any):
            lifecycle.append("activate")
            return await super().activate(**kwargs)

        async def _dispose_activation(self, activation):
            lifecycle.append("dispose")
            await super()._dispose_activation(activation)

        async def close(self) -> None:
            lifecycle.append("close")
            await super().close()

    def build_host(**capabilities: Any) -> PluginHost:
        source = create_default_plugin_host(**capabilities)
        host = RecordingHost(
            source.discover(),
            schema=DEFAULT_CAPABILITY_SCHEMA,
        )
        hosts.append(host)
        return host

    class CapturingKernel:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def run(self):
            yield ContentEvent(content="done")

    monkeypatch.setattr(composition, "create_default_plugin_host", build_host)
    monkeypatch.setattr(composition, "AgentLoopKernel", CapturingKernel)

    events = [
        event
        async for event in core.run_agent_loop(
            llm=_LLM(),
            messages=[Message(role="user", content="respond")],
            tools={},
            max_steps=1,
        )
    ]

    assert [event.content for event in events] == ["done"]
    assert isinstance(captured.pop("_services"), KernelServices)
    assert SERVICE_OWNED_RUN_ARGUMENTS.isdisjoint(captured)
    assert not any(isinstance(value, PluginHost) for value in captured.values())
    assert lifecycle == ["activate", "dispose", "close"]
    assert len(hosts) == 1
    assert hosts[0]._live_records == []


@pytest.mark.asyncio
async def test_outer_composition_closes_host_on_early_stream_close(
    monkeypatch,
) -> None:
    import box_agent.composition as composition
    import box_agent.core as core
    from box_agent.plugins.defaults import (
        DEFAULT_CAPABILITY_SCHEMA,
        create_default_plugin_host,
    )
    from box_agent.plugins.host import PluginHost

    lifecycle: list[str] = []
    hosts: list[PluginHost] = []

    class RecordingHost(PluginHost):
        async def activate(self, **kwargs: Any):
            lifecycle.append("activate")
            return await super().activate(**kwargs)

        async def _dispose_activation(self, activation):
            lifecycle.append("dispose")
            await super()._dispose_activation(activation)

        async def close(self) -> None:
            lifecycle.append("close")
            await super().close()

    def build_host(**capabilities: Any) -> PluginHost:
        source = create_default_plugin_host(**capabilities)
        host = RecordingHost(
            source.discover(),
            schema=DEFAULT_CAPABILITY_SCHEMA,
        )
        hosts.append(host)
        return host

    monkeypatch.setattr(composition, "create_default_plugin_host", build_host)
    events = core.run_agent_loop(
        llm=_LLM(),
        messages=[Message(role="user", content="respond")],
        tools={},
        max_steps=1,
    )

    await anext(events)
    await events.aclose()

    assert lifecycle == ["activate", "dispose", "close"]
    assert hosts[0]._live_records == []


@pytest.mark.asyncio
async def test_outer_composition_propagates_cleanup_failure_on_early_close(
    monkeypatch,
) -> None:
    import box_agent.composition as composition
    import box_agent.core as core
    from box_agent.plugins.defaults import (
        DEFAULT_CAPABILITY_SCHEMA,
        create_default_plugin_host,
    )
    from box_agent.plugins.host import PluginHost

    cleanup_error = RuntimeError("activation disposal failed")

    class CleanupFailingHost(PluginHost):
        async def _dispose_activation(self, activation):
            await super()._dispose_activation(activation)
            raise cleanup_error

    def build_host(**capabilities: Any) -> PluginHost:
        source = create_default_plugin_host(**capabilities)
        return CleanupFailingHost(
            source.discover(),
            schema=DEFAULT_CAPABILITY_SCHEMA,
        )

    monkeypatch.setattr(composition, "create_default_plugin_host", build_host)
    events = core.run_agent_loop(
        llm=_LLM(),
        messages=[Message(role="user", content="respond")],
        tools={},
        max_steps=1,
    )

    await anext(events)
    with pytest.raises(RuntimeError) as caught:
        await events.aclose()

    assert caught.value is cleanup_error


@pytest.mark.asyncio
async def test_outer_cleanup_never_aggregates_cancellation_with_ordinary_failure(
    monkeypatch,
) -> None:
    import box_agent.composition as composition
    import box_agent.core as core
    from box_agent.plugins.defaults import (
        DEFAULT_CAPABILITY_SCHEMA,
        create_default_plugin_host,
    )
    from box_agent.plugins.host import PluginHost

    close_error = RuntimeError("host close failed")

    class CancellationThenFailureHost(PluginHost):
        async def _dispose_activation(self, activation):
            await super()._dispose_activation(activation)
            raise asyncio.CancelledError

        async def close(self) -> None:
            await super().close()
            raise close_error

    def build_host(**capabilities: Any) -> PluginHost:
        source = create_default_plugin_host(**capabilities)
        return CancellationThenFailureHost(
            source.discover(),
            schema=DEFAULT_CAPABILITY_SCHEMA,
        )

    monkeypatch.setattr(composition, "create_default_plugin_host", build_host)
    events = core.run_agent_loop(
        llm=_LLM(),
        messages=[Message(role="user", content="respond")],
        tools={},
        max_steps=1,
    )

    await anext(events)
    with pytest.raises(asyncio.CancelledError) as caught:
        await events.aclose()

    assert caught.value.__cause__ is close_error


@pytest.mark.asyncio
async def test_outer_composition_closes_host_when_iteration_is_cancelled(
    monkeypatch,
) -> None:
    import asyncio

    import box_agent.composition as composition
    import box_agent.core as core
    from box_agent.plugins.defaults import (
        DEFAULT_CAPABILITY_SCHEMA,
        create_default_plugin_host,
    )
    from box_agent.plugins.host import PluginHost

    lifecycle: list[str] = []
    kernel_started = asyncio.Event()
    hosts: list[PluginHost] = []

    class RecordingHost(PluginHost):
        async def activate(self, **kwargs: Any):
            lifecycle.append("activate")
            return await super().activate(**kwargs)

        async def _dispose_activation(self, activation):
            lifecycle.append("dispose")
            await super()._dispose_activation(activation)

        async def close(self) -> None:
            lifecycle.append("close")
            await super().close()

    def build_host(**capabilities: Any) -> PluginHost:
        source = create_default_plugin_host(**capabilities)
        host = RecordingHost(
            source.discover(),
            schema=DEFAULT_CAPABILITY_SCHEMA,
        )
        hosts.append(host)
        return host

    class SuspendedKernel:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def run(self):
            kernel_started.set()
            await asyncio.Event().wait()
            if False:
                yield ContentEvent(content="unreachable")

    monkeypatch.setattr(composition, "create_default_plugin_host", build_host)
    monkeypatch.setattr(composition, "AgentLoopKernel", SuspendedKernel)
    events = core.run_agent_loop(
        llm=_LLM(),
        messages=[Message(role="user", content="respond")],
        tools={},
        max_steps=1,
    )
    pending = asyncio.create_task(anext(events))
    await kernel_started.wait()
    pending.cancel()

    with pytest.raises(asyncio.CancelledError):
        await pending

    assert lifecycle == ["activate", "dispose", "close"]
    assert hosts[0]._live_records == []


@pytest.mark.asyncio
async def test_outer_composition_preserves_kernel_error_when_cleanup_fails(
    monkeypatch,
) -> None:
    import box_agent.composition as composition
    import box_agent.core as core
    from box_agent.plugins.defaults import (
        DEFAULT_CAPABILITY_SCHEMA,
        create_default_plugin_host,
    )
    from box_agent.plugins.host import PluginHost

    lifecycle: list[str] = []
    primary_error = RuntimeError("kernel failed")
    cleanup_error = ValueError("cleanup failed")

    class CleanupFailingHost(PluginHost):
        async def _dispose_activation(self, activation):
            lifecycle.append("dispose")
            await super()._dispose_activation(activation)
            raise cleanup_error

        async def close(self) -> None:
            lifecycle.append("close")
            await super().close()

    def build_host(**capabilities: Any) -> PluginHost:
        source = create_default_plugin_host(**capabilities)
        return CleanupFailingHost(
            source.discover(),
            schema=DEFAULT_CAPABILITY_SCHEMA,
        )

    class FailingKernel:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def run(self):
            if False:
                yield ContentEvent(content="unreachable")
            raise primary_error

    monkeypatch.setattr(composition, "create_default_plugin_host", build_host)
    monkeypatch.setattr(composition, "AgentLoopKernel", FailingKernel)
    events = core.run_agent_loop(
        llm=_LLM(),
        messages=[Message(role="user", content="respond")],
        tools={},
        max_steps=1,
    )

    with pytest.raises(RuntimeError) as caught:
        await anext(events)

    assert caught.value is primary_error
    assert caught.value.__cause__ is cleanup_error
    assert lifecycle == ["dispose", "close"]


@pytest.mark.parametrize("native_notes", (True, False))
@pytest.mark.asyncio
async def test_outer_cleanup_preserves_repeated_cancellation(native_notes) -> None:
    from box_agent.composition import _cleanup_plugin_run

    class CleanupCancelled(asyncio.CancelledError):
        if not native_notes:
            add_note = None

    first = CleanupCancelled("activation cleanup cancelled")
    second = CleanupCancelled("host cleanup cancelled")

    class Activation:
        async def dispose(self):
            raise first

    class Host:
        async def close(self):
            raise second

    with pytest.raises(CleanupCancelled) as caught:
        await _cleanup_plugin_run(activation=Activation(), host=Host())

    assert caught.value is first
    assert "host cleanup cancelled" in " ".join(caught.value.__notes__)


@pytest.mark.parametrize("native_notes", (True, False))
def test_cleanup_failure_keeps_existing_exception_cause(native_notes) -> None:
    from box_agent.composition import _attach_cleanup_error

    class KernelError(RuntimeError):
        if not native_notes:
            add_note = None

    primary = KernelError("kernel failed")
    cause = OSError("provider failed")
    primary.__cause__ = cause

    _attach_cleanup_error(primary, ValueError("cleanup failed"))

    assert primary.__cause__ is cause
    assert "cleanup failed" in " ".join(primary.__notes__)


@pytest.mark.asyncio
async def test_kernel_receives_resolved_services_without_composing() -> None:
    from box_agent.composition import compose_default_kernel_services
    from box_agent.kernel.loop import AgentLoopKernel

    class UnusedRawLLM(_LLM):
        async def generate_stream(self, **_kwargs: Any):
            raise AssertionError("raw LLM must not reach the kernel")
            yield

    run_arguments = {
        "llm": UnusedRawLLM(),
        "messages": [Message(role="user", content="respond")],
        "tools": {},
        "max_steps": 1,
    }
    services = replace(
        compose_default_kernel_services(run_arguments),
        llm=_LLM(),
    )

    kernel = AgentLoopKernel(
        _services=services,
        messages=run_arguments["messages"],
        max_steps=run_arguments["max_steps"],
    )

    events = [event async for event in kernel.run()]

    assert kernel._services is services
    assert SERVICE_OWNED_RUN_ARGUMENTS.isdisjoint(kernel._run_arguments)
    assert "".join(
        event.content for event in events if isinstance(event, ContentEvent)
    ) == "done"


@pytest.mark.asyncio
async def test_each_outer_run_resolves_fresh_services_and_hook_snapshot(
    monkeypatch,
) -> None:
    import box_agent.composition as composition
    import box_agent.core as core

    original_build_host = composition.create_default_plugin_host
    composition_calls = 0
    starts: list[str] = []

    def build_host_spy(**kwargs: Any):
        nonlocal composition_calls
        composition_calls += 1
        return original_build_host(**kwargs)

    class NamedHook:
        def __init__(self, name: str) -> None:
            self.name = name

        async def on_agent_start(self, **_kwargs: Any) -> None:
            starts.append(self.name)

    monkeypatch.setattr(composition, "create_default_plugin_host", build_host_spy)
    hooks: list[Any] = [NamedHook("first")]

    first = core.run_agent_loop(
        llm=_LLM(),
        messages=[Message(role="user", content="first")],
        tools={},
        hooks=hooks,
        max_steps=1,
    )
    _ = [event async for event in first]
    hooks.append(NamedHook("second"))
    second = core.run_agent_loop(
        llm=_LLM(),
        messages=[Message(role="user", content="second")],
        tools={},
        hooks=hooks,
        max_steps=1,
    )
    _ = [event async for event in second]

    assert composition_calls == 2
    assert starts == ["first", "first", "second"]


def _plugin_descriptor(
    plugin_id: str,
    port_type: type[Any],
    factory: Any,
    *,
    dependencies: tuple[str, ...] = (),
    scope: Any = None,
    disposer: Any = None,
):
    from box_agent.plugins.descriptors import PluginDescriptor, PluginScope

    return PluginDescriptor(
        plugin_id=plugin_id,
        version="1.0.0",
        dependencies=dependencies,
        capabilities=(port_type,),
        factory=factory,
        scope=PluginScope.RUN if scope is None else scope,
        disposer=disposer,
    )


def _plugin_host(
    descriptors: Any,
    *,
    required: tuple[type[Any], ...] = (),
    optional: tuple[type[Any], ...] = (),
    multi: tuple[type[Any], ...] = (),
):
    from box_agent.plugins.host import PluginHost
    from box_agent.plugins.registries import (
        CapabilityBinding,
        CapabilityPolicy,
        CapabilitySchema,
    )

    bindings = tuple(
        CapabilityBinding(port_type, CapabilityPolicy.REQUIRED_SINGLE)
        for port_type in required
    ) + tuple(
        CapabilityBinding(port_type, CapabilityPolicy.OPTIONAL_SINGLE)
        for port_type in optional
    ) + tuple(
        CapabilityBinding(port_type, CapabilityPolicy.MULTI)
        for port_type in multi
    )
    return PluginHost(tuple(descriptors), schema=CapabilitySchema(bindings))


def test_plugin_discovery_is_explicit_static_and_side_effect_free() -> None:
    from box_agent.plugins.host import PluginHost

    class Port:
        pass

    factory_calls = 0

    def factory() -> object:
        nonlocal factory_calls
        factory_calls += 1
        return object()

    supplied = [_plugin_descriptor("explicit.plugin", Port, factory)]
    host = _plugin_host(supplied, required=(Port,))
    supplied.clear()

    assert [item.plugin_id for item in host.discover()] == ["explicit.plugin"]
    assert factory_calls == 0
    assert not hasattr(host, "reload")
    assert not hasattr(host, "watch")


@pytest.mark.asyncio
async def test_plugin_activation_uses_stable_dependency_order_and_exact_lookup() -> None:
    from box_agent.plugins.host import PluginHost

    class FirstPort:
        pass

    class SecondPort:
        pass

    class ThirdPort:
        pass

    class DerivedFirstPort(FirstPort):
        pass

    order: list[str] = []
    first = object()
    second = object()
    third = object()
    descriptors = (
        _plugin_descriptor(
            "third.plugin",
            ThirdPort,
            lambda: (order.append("third"), third)[1],
            dependencies=("first.plugin",),
        ),
        _plugin_descriptor(
            "second.plugin",
            SecondPort,
            lambda: (order.append("second"), second)[1],
        ),
        _plugin_descriptor(
            "first.plugin",
            FirstPort,
            lambda: (order.append("first"), first)[1],
        ),
    )
    host = _plugin_host(
        descriptors,
        required=(FirstPort, SecondPort, ThirdPort),
    )

    assert [item.plugin_id for item in host.resolve_dependencies()] == [
        "second.plugin",
        "first.plugin",
        "third.plugin",
    ]
    activation = await host.activate()

    assert order == ["second", "first", "third"]
    assert activation.registry.require(FirstPort) is first
    assert activation.registry.get(SecondPort) is second
    with pytest.raises(KeyError):
        activation.registry.require(DerivedFirstPort)
    with pytest.raises(TypeError):
        activation.registry.entries[FirstPort] = object()  # type: ignore[index]


@pytest.mark.asyncio
async def test_capability_schema_resolves_required_optional_and_ordered_multi() -> None:
    from box_agent.plugins.host import PluginHost

    class RequiredPort:
        pass

    class OptionalPort:
        pass

    class MultiPort:
        pass

    required = object()
    first_multi = object()
    second_multi = object()
    host = _plugin_host(
        (
            _plugin_descriptor(
                "multi.second",
                MultiPort,
                lambda: second_multi,
                dependencies=("multi.first",),
            ),
            _plugin_descriptor("required.plugin", RequiredPort, lambda: required),
            _plugin_descriptor("multi.first", MultiPort, lambda: first_multi),
        ),
        required=(RequiredPort,),
        optional=(OptionalPort,),
        multi=(MultiPort,),
    )

    activation = await host.activate()

    assert activation.registry.require(RequiredPort) is required
    assert activation.registry.get(OptionalPort) is None
    assert activation.registry.get_all(MultiPort) == (first_multi, second_multi)
    with pytest.raises(KeyError):
        activation.registry.require(MultiPort)
    with pytest.raises(TypeError):
        activation.registry.multi_entries[MultiPort][0] = object()  # type: ignore[index]


def test_capability_schema_rejects_missing_required_before_factories() -> None:
    from box_agent.plugins.host import PluginValidationError

    class RequiredPort:
        pass

    class OptionalPort:
        pass

    calls: list[str] = []
    host = _plugin_host(
        (
            _plugin_descriptor(
                "optional.plugin",
                OptionalPort,
                lambda: calls.append("activated"),
            ),
        ),
        required=(RequiredPort,),
        optional=(OptionalPort,),
    )

    with pytest.raises(PluginValidationError, match="missing required capability"):
        host.validate()
    assert calls == []


@pytest.mark.parametrize(
    "malformation",
    ["missing", "bindings-list", "binding", "port", "policy", "overlap"],
)
def test_capability_schema_validation_is_consistent_and_precedes_factories(
    malformation: str,
) -> None:
    from box_agent.plugins.host import PluginHost, PluginValidationError
    from box_agent.plugins.registries import (
        CapabilityBinding,
        CapabilityPolicy,
        CapabilitySchema,
    )

    class Port:
        pass

    calls: list[str] = []
    descriptor = _plugin_descriptor(
        "valid.plugin",
        Port,
        lambda: calls.append("activated"),
    )
    binding = CapabilityBinding(Port, CapabilityPolicy.REQUIRED_SINGLE)
    schemas: dict[str, Any] = {
        "missing": None,
        "bindings-list": CapabilitySchema([binding]),  # type: ignore[arg-type]
        "binding": CapabilitySchema((object(),)),  # type: ignore[arg-type]
        "port": CapabilitySchema(
            (CapabilityBinding([], CapabilityPolicy.REQUIRED_SINGLE),)  # type: ignore[arg-type]
        ),
        "policy": CapabilitySchema(
            (CapabilityBinding(Port, "required-single"),)  # type: ignore[arg-type]
        ),
        "overlap": CapabilitySchema(
            (
                binding,
                CapabilityBinding(Port, CapabilityPolicy.OPTIONAL_SINGLE),
            )
        ),
    }
    host = PluginHost((descriptor,), schema=schemas[malformation])

    with pytest.raises(PluginValidationError, match="capability schema"):
        host.validate()
    assert calls == []


def test_capability_schema_rejects_undeclared_descriptor_capability() -> None:
    from box_agent.plugins.host import PluginValidationError

    class DeclaredPort:
        pass

    class UndeclaredPort:
        pass

    host = _plugin_host(
        (_plugin_descriptor("unknown.plugin", UndeclaredPort, object),),
        optional=(DeclaredPort,),
    )

    with pytest.raises(PluginValidationError, match="undeclared capability"):
        host.validate()


@pytest.mark.asyncio
async def test_tool_catalog_without_indexed_lookup_is_rejected_before_kernel_use() -> None:
    from box_agent.kernel.ports import ToolCatalogPort
    from box_agent.plugins.host import PluginValidationError

    class MissingIndexedLookup:
        def __contains__(self, _name: object) -> bool:
            return False

        def __setitem__(self, _name: str, _tool: Tool) -> None:
            return None

        def __delitem__(self, _name: str) -> None:
            return None

        def __iter__(self):
            return iter(())

        def __len__(self) -> int:
            return 0

        def values(self):
            return ()

        def get(self, _name: str) -> Tool | None:
            return None

    catalog = MissingIndexedLookup()
    host = _plugin_host(
        (_plugin_descriptor("invalid.catalog", ToolCatalogPort, lambda: catalog),),
        required=(ToolCatalogPort,),
    )

    assert not isinstance(catalog, ToolCatalogPort)
    with pytest.raises(
        PluginValidationError,
        match=r"invalid\.catalog.*ToolCatalogPort",
    ):
        await host.activate()


@pytest.mark.asyncio
async def test_invalid_runtime_port_instance_is_disposed_before_publication() -> None:
    from box_agent.kernel.ports import LLMPort
    from box_agent.plugins.host import PluginValidationError

    disposed: list[object] = []
    instance = object()
    host = _plugin_host(
        (
            _plugin_descriptor(
                "invalid.llm",
                LLMPort,
                lambda: instance,
                disposer=disposed.append,
            ),
        ),
        required=(LLMPort,),
    )

    with pytest.raises(
        PluginValidationError,
        match=r"invalid\.llm.*LLMPort",
    ):
        await host.activate()

    assert disposed == [instance]
    assert host._live_records == []


@pytest.mark.parametrize("failure_type", (RuntimeError, asyncio.CancelledError))
@pytest.mark.asyncio
async def test_runtime_port_validation_exception_disposes_created_resource(
    failure_type,
) -> None:
    class CheckingPortMeta(type):
        def __instancecheck__(cls, instance):
            raise failure

    class CheckingPort(metaclass=CheckingPortMeta):
        _is_protocol = True
        _is_runtime_protocol = True

    failure = failure_type("resource validation failed")
    instance = object()
    disposed: list[object] = []
    host = _plugin_host(
        (_plugin_descriptor(
            "checked.resource", CheckingPort, lambda: instance,
            disposer=disposed.append,
        ),),
        required=(CheckingPort,),
    )

    with pytest.raises(failure_type) as caught:
        await host.activate()
    await host.close()

    assert caught.value is failure
    assert disposed == [instance]
    assert host._live_records == []


@pytest.mark.asyncio
async def test_default_llm_port_accepts_legacy_streaming_only_implementation() -> None:
    from box_agent.kernel.ports import LLMPort

    class StreamingOnlyLLM:
        async def generate_stream(self, **_kwargs):
            if False:
                yield None

    llm = StreamingOnlyLLM()
    host = _plugin_host(
        (_plugin_descriptor("streaming.llm", LLMPort, lambda: llm),),
        required=(LLMPort,),
    )

    activation = await host.activate()

    assert activation.registry.require(LLMPort) is llm
    await activation.dispose()
    await host.close()


def test_capability_schema_rejects_unhashable_port_type_before_factory() -> None:
    from box_agent.plugins.host import PluginHost, PluginValidationError
    from box_agent.plugins.registries import (
        CapabilityBinding,
        CapabilityPolicy,
        CapabilitySchema,
    )

    class UnhashableMeta(type):
        __hash__ = None  # type: ignore[assignment]

    class UnhashablePort(metaclass=UnhashableMeta):
        pass

    class RegularPort:
        pass

    calls: list[str] = []
    descriptor = _plugin_descriptor(
        "regular.plugin",
        RegularPort,
        lambda: calls.append("activated"),
    )
    schema = CapabilitySchema(
        (
            CapabilityBinding(RegularPort, CapabilityPolicy.OPTIONAL_SINGLE),
            CapabilityBinding(
                UnhashablePort,
                CapabilityPolicy.OPTIONAL_SINGLE,
            ),
        )
    )

    with pytest.raises(PluginValidationError, match="hashable Port type"):
        PluginHost((descriptor,), schema=schema).validate()
    assert calls == []


def test_plugin_descriptor_rejects_unhashable_port_type_before_factory() -> None:
    from box_agent.plugins.host import PluginHost, PluginValidationError
    from box_agent.plugins.registries import CapabilitySchema

    class UnhashableMeta(type):
        __hash__ = None  # type: ignore[assignment]

    class UnhashablePort(metaclass=UnhashableMeta):
        pass

    calls: list[str] = []
    descriptor = _plugin_descriptor(
        "unhashable.plugin",
        UnhashablePort,
        lambda: calls.append("activated"),
    )

    with pytest.raises(PluginValidationError, match="hashable Port types"):
        PluginHost((descriptor,), schema=CapabilitySchema(())).validate()
    assert calls == []


@pytest.mark.parametrize(
    "descriptors, message",
    [
        (
            lambda calls, port: (
                _plugin_descriptor(
                    "duplicate.plugin",
                    port,
                    lambda: calls.append("first"),
                ),
                _plugin_descriptor(
                    "duplicate.plugin",
                    type("OtherPort", (), {}),
                    lambda: calls.append("second"),
                ),
            ),
            "duplicate plugin id",
        ),
        (
            lambda calls, port: (
                _plugin_descriptor("first.plugin", port, lambda: calls.append("first")),
                _plugin_descriptor("second.plugin", port, lambda: calls.append("second")),
            ),
            "duplicate capability",
        ),
    ],
)
def test_plugin_validation_rejects_duplicates_before_activation(
    descriptors,
    message: str,
) -> None:
    from box_agent.plugins.host import PluginHost, PluginValidationError

    calls: list[str] = []

    class Port:
        pass

    descriptor_set = descriptors(calls, Port)
    declared_ports = tuple(
        dict.fromkeys(
            port_type
            for descriptor in descriptor_set
            for port_type in descriptor.capabilities
        )
    )
    host = _plugin_host(descriptor_set, required=declared_ports)

    with pytest.raises(PluginValidationError, match=message):
        host.validate()
    assert calls == []


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"plugin_id": "Bad Plugin"}, "plugin id"),
        ({"version": "latest"}, "version"),
        ({"version": "1.0.0-01"}, "version"),
        ({"version": "1.0.0-.."}, "version"),
        ({"version": "1.0.0-a..b"}, "version"),
        ({"version": "1.0.0+.."}, "version"),
        ({"scope": "run"}, "scope"),
        ({"dependencies": (["not-hashable"],)}, "dependency"),
        ({"capabilities": ()}, "capabilit"),
        ({"capabilities": (object(),)}, "capabilit"),
        ({"capabilities": (["not-hashable"],)}, "capabilit"),
        ({"factory": None}, "factory"),
        ({"disposer": object()}, "disposer"),
    ],
)
def test_plugin_validation_rejects_malformed_descriptors(
    overrides: dict[str, Any],
    message: str,
) -> None:
    from box_agent.plugins.descriptors import PluginDescriptor, PluginScope
    from box_agent.plugins.host import PluginHost, PluginValidationError

    class Port:
        pass

    values: dict[str, Any] = {
        "plugin_id": "valid.plugin",
        "version": "1.0.0",
        "dependencies": (),
        "capabilities": (Port,),
        "factory": object,
        "scope": PluginScope.RUN,
        "disposer": None,
    }
    values.update(overrides)

    with pytest.raises(PluginValidationError, match=message):
        _plugin_host(
            (PluginDescriptor(**values),),
            required=(Port,),
        ).validate()


def test_plugin_validation_rejects_missing_dependencies_and_cycles() -> None:
    from box_agent.plugins.host import (
        PluginDependencyCycleError,
        PluginHost,
        PluginValidationError,
    )

    class FirstPort:
        pass

    class SecondPort:
        pass

    missing = _plugin_host(
        (
            _plugin_descriptor(
                "first.plugin",
                FirstPort,
                object,
                dependencies=("missing.plugin",),
            ),
        ),
        required=(FirstPort,),
    )
    with pytest.raises(PluginValidationError, match="missing dependency"):
        missing.validate()

    cyclic = _plugin_host(
        (
            _plugin_descriptor(
                "first.plugin",
                FirstPort,
                object,
                dependencies=("second.plugin",),
            ),
            _plugin_descriptor(
                "second.plugin",
                SecondPort,
                object,
                dependencies=("first.plugin",),
            ),
        ),
        required=(FirstPort, SecondPort),
    )
    with pytest.raises(PluginDependencyCycleError, match="cycle"):
        cyclic.resolve_dependencies()


@pytest.mark.asyncio
async def test_activation_failure_rolls_back_in_reverse_order_and_keeps_error() -> None:
    from box_agent.plugins.host import PluginHost

    class FirstPort:
        pass

    class SecondPort:
        pass

    class FailingPort:
        pass

    events: list[str] = []
    failure = RuntimeError("activation failed")

    async def dispose_first(_instance: object) -> None:
        events.append("dispose:first")

    def dispose_second(_instance: object) -> None:
        events.append("dispose:second")

    def fail() -> object:
        events.append("activate:failing")
        raise failure

    host = _plugin_host(
        (
            _plugin_descriptor(
                "first.plugin",
                FirstPort,
                lambda: (events.append("activate:first"), object())[1],
                disposer=dispose_first,
            ),
            _plugin_descriptor(
                "second.plugin",
                SecondPort,
                lambda: (events.append("activate:second"), object())[1],
                dependencies=("first.plugin",),
                disposer=dispose_second,
            ),
            _plugin_descriptor(
                "failing.plugin",
                FailingPort,
                fail,
                dependencies=("second.plugin",),
            ),
        ),
        required=(FirstPort, SecondPort, FailingPort),
    )

    with pytest.raises(RuntimeError) as caught:
        await host.activate()

    assert caught.value is failure
    assert events == [
        "activate:first",
        "activate:second",
        "activate:failing",
        "dispose:second",
        "dispose:first",
    ]


@pytest.mark.asyncio
async def test_activation_failure_keeps_cleanup_errors_visible() -> None:
    from box_agent.plugins.host import PluginCleanupError, PluginHost

    class FirstPort:
        pass

    class FailingPort:
        pass

    failure = RuntimeError("factory failed")

    def dispose(_instance: object) -> None:
        raise ValueError("cleanup failed")

    def fail() -> object:
        raise failure

    host = _plugin_host(
        (
            _plugin_descriptor("first.plugin", FirstPort, object, disposer=dispose),
            _plugin_descriptor(
                "failing.plugin",
                FailingPort,
                fail,
                dependencies=("first.plugin",),
            ),
        ),
        required=(FirstPort, FailingPort),
    )

    with pytest.raises(RuntimeError) as caught:
        await host.activate()

    assert caught.value is failure
    assert isinstance(caught.value.__cause__, PluginCleanupError)
    assert "cleanup failed" in str(caught.value.__cause__)
    assert len(host._live_records) == 0


@pytest.mark.asyncio
async def test_activation_disposal_is_reverse_order_and_idempotent() -> None:
    from box_agent.plugins.host import PluginHost

    class FirstPort:
        pass

    class SecondPort:
        pass

    disposed: list[str] = []
    host = _plugin_host(
        (
            _plugin_descriptor(
                "first.plugin",
                FirstPort,
                object,
                disposer=lambda _instance: disposed.append("first"),
            ),
            _plugin_descriptor(
                "second.plugin",
                SecondPort,
                object,
                dependencies=("first.plugin",),
                disposer=lambda _instance: disposed.append("second"),
            ),
        ),
        required=(FirstPort, SecondPort),
    )
    activation = await host.activate()

    await activation.dispose()
    await activation.dispose()
    await host.close()
    await host.close()

    assert disposed == ["second", "first"]


@pytest.mark.asyncio
async def test_activation_disposal_preserves_cancellation_and_retries_interrupted_record() -> None:
    from box_agent.plugins.host import PluginHost

    class StablePort:
        pass

    class InterruptedPort:
        pass

    calls: list[str] = []
    interrupted_attempts = 0

    def dispose_stable(_instance: object) -> None:
        calls.append("stable")

    async def dispose_interrupted(_instance: object) -> None:
        nonlocal interrupted_attempts
        interrupted_attempts += 1
        calls.append("interrupted")
        if interrupted_attempts == 1:
            raise asyncio.CancelledError

    host = _plugin_host(
        (
            _plugin_descriptor(
                "stable.plugin",
                StablePort,
                object,
                disposer=dispose_stable,
            ),
            _plugin_descriptor(
                "interrupted.plugin",
                InterruptedPort,
                object,
                disposer=dispose_interrupted,
            ),
        ),
        required=(StablePort, InterruptedPort),
    )
    activation = await host.activate()

    with pytest.raises(asyncio.CancelledError):
        await activation.dispose()

    assert calls == ["interrupted", "stable"]
    assert len(host._live_records) == 1

    await activation.dispose()
    await host.close()

    assert calls == ["interrupted", "stable", "interrupted"]
    assert host._live_records == []


@pytest.mark.asyncio
async def test_session_disposal_preserves_cancellation_and_keeps_cache_retryable() -> None:
    from box_agent.plugins.descriptors import PluginScope

    class SessionPort:
        pass

    calls = 0

    async def dispose(_instance: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise asyncio.CancelledError

    host = _plugin_host(
        (
            _plugin_descriptor(
                "session.plugin",
                SessionPort,
                object,
                scope=PluginScope.SESSION,
                disposer=dispose,
            ),
        ),
        required=(SessionPort,),
    )
    await host.activate(session_key="session-a")

    with pytest.raises(asyncio.CancelledError):
        await host.dispose_session("session-a")

    assert "session-a" in host._session_instances
    assert len(host._live_records) == 1

    await host.dispose_session("session-a")
    await host.close()

    assert calls == 2
    assert "session-a" not in host._session_instances
    assert host._live_records == []


@pytest.mark.asyncio
async def test_host_close_preserves_cancellation_and_can_finish_on_retry() -> None:
    from box_agent.plugins.descriptors import PluginScope
    from box_agent.plugins.host import PluginError

    class ProcessPort:
        pass

    calls = 0

    async def dispose(_instance: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise asyncio.CancelledError

    host = _plugin_host(
        (
            _plugin_descriptor(
                "process.plugin",
                ProcessPort,
                object,
                scope=PluginScope.PROCESS,
                disposer=dispose,
            ),
        ),
        required=(ProcessPort,),
    )
    await host.activate()

    with pytest.raises(asyncio.CancelledError):
        await host.close()

    assert len(host._live_records) == 1
    with pytest.raises(PluginError, match="closed"):
        await host.activate()

    await host.close()

    assert calls == 2
    assert host._live_records == []


@pytest.mark.asyncio
async def test_factory_child_task_reentry_fails_fast_without_deadlock() -> None:
    from box_agent.plugins.host import PluginError

    class Port:
        pass

    host = None

    async def factory() -> object:
        assert host is not None
        return await asyncio.create_task(host.activate())

    host = _plugin_host(
        (_plugin_descriptor("reentrant.factory", Port, factory),),
        required=(Port,),
    )

    with pytest.raises(PluginError, match="callback re-entry"):
        await asyncio.wait_for(host.activate(), timeout=0.2)


@pytest.mark.asyncio
async def test_disposer_child_task_reentry_fails_fast_without_deadlock() -> None:
    from box_agent.plugins.host import PluginCleanupError, PluginError

    class Port:
        pass

    host = None

    async def disposer(_instance: object) -> None:
        assert host is not None
        await asyncio.create_task(host.close())

    host = _plugin_host(
        (
            _plugin_descriptor(
                "reentrant.disposer",
                Port,
                object,
                disposer=disposer,
            ),
        ),
        required=(Port,),
    )
    activation = await host.activate()

    with pytest.raises(PluginCleanupError) as caught:
        await asyncio.wait_for(activation.dispose(), timeout=0.2)

    assert len(caught.value.errors) == 1
    assert isinstance(caught.value.errors[0], PluginError)
    assert "callback re-entry" in str(caught.value.errors[0])


@pytest.mark.asyncio
async def test_concurrent_activations_reuse_process_and_session_instances_once() -> None:
    from box_agent.plugins.descriptors import PluginScope

    class ProcessPort:
        pass

    class SessionPort:
        pass

    process_started = asyncio.Event()
    release_process = asyncio.Event()
    counts = {"process": 0, "session": 0}

    async def create_process() -> object:
        counts["process"] += 1
        process_started.set()
        await release_process.wait()
        return object()

    def create_session() -> object:
        counts["session"] += 1
        return object()

    host = _plugin_host(
        (
            _plugin_descriptor(
                "process.plugin",
                ProcessPort,
                create_process,
                scope=PluginScope.PROCESS,
            ),
            _plugin_descriptor(
                "session.plugin",
                SessionPort,
                create_session,
                scope=PluginScope.SESSION,
            ),
        ),
        required=(ProcessPort, SessionPort),
    )
    pending = [
        asyncio.create_task(host.activate(session_key="session-a"))
        for _ in range(5)
    ]
    await asyncio.wait_for(process_started.wait(), timeout=0.2)
    release_process.set()
    activations = await asyncio.wait_for(asyncio.gather(*pending), timeout=0.5)

    assert counts == {"process": 1, "session": 1}
    assert len(
        {id(item.registry.require(ProcessPort)) for item in activations}
    ) == 1
    assert len(
        {id(item.registry.require(SessionPort)) for item in activations}
    ) == 1

    for activation in activations:
        await activation.dispose()
    await host.close()


@pytest.mark.asyncio
async def test_factories_and_disposers_run_outside_the_host_state_lock() -> None:
    class Port:
        pass

    host = None

    def factory() -> object:
        assert host is not None
        assert host._lock.locked() is False
        return object()

    def disposer(_instance: object) -> None:
        assert host is not None
        assert host._lock.locked() is False

    host = _plugin_host(
        (
            _plugin_descriptor(
                "lock.observer",
                Port,
                factory,
                disposer=disposer,
            ),
        ),
        required=(Port,),
    )

    activation = await host.activate()
    await activation.dispose()
    await host.close()


@pytest.mark.asyncio
async def test_async_factory_and_host_cleanup_follow_lifecycle_order() -> None:
    from box_agent.plugins.descriptors import PluginScope
    from box_agent.plugins.host import PluginHost

    class ProcessPort:
        pass

    class SessionPort:
        pass

    events: list[str] = []
    process_instance = object()
    session_instance = object()

    async def create_session() -> object:
        events.append("activate:session")
        return session_instance

    async def dispose_process(instance: object) -> None:
        assert instance is process_instance
        events.append("dispose:process")

    def dispose_session(instance: object) -> None:
        assert instance is session_instance
        events.append("dispose:session")

    host = _plugin_host(
        (
            _plugin_descriptor(
                "process.plugin",
                ProcessPort,
                lambda: (events.append("activate:process"), process_instance)[1],
                scope=PluginScope.PROCESS,
                disposer=dispose_process,
            ),
            _plugin_descriptor(
                "session.plugin",
                SessionPort,
                create_session,
                dependencies=("process.plugin",),
                scope=PluginScope.SESSION,
                disposer=dispose_session,
            ),
        ),
        required=(ProcessPort, SessionPort),
    )

    activation = await host.activate(session_key="session-a")
    assert activation.registry.require(SessionPort) is session_instance

    await host.close()
    await host.close()

    assert events == [
        "activate:process",
        "activate:session",
        "dispose:session",
        "dispose:process",
    ]


@pytest.mark.asyncio
async def test_plugin_scopes_reuse_only_their_declared_lifetime() -> None:
    from box_agent.plugins.descriptors import PluginScope
    from box_agent.plugins.host import PluginHost

    class ProcessPort:
        pass

    class SessionPort:
        pass

    class RunPort:
        pass

    counters = {"process": 0, "session": 0, "run": 0}

    def create(label: str) -> object:
        counters[label] += 1
        return object()

    host = _plugin_host(
        (
            _plugin_descriptor(
                "process.plugin",
                ProcessPort,
                lambda: create("process"),
                scope=PluginScope.PROCESS,
            ),
            _plugin_descriptor(
                "session.plugin",
                SessionPort,
                lambda: create("session"),
                scope=PluginScope.SESSION,
            ),
            _plugin_descriptor(
                "run.plugin",
                RunPort,
                lambda: create("run"),
                scope=PluginScope.RUN,
            ),
        ),
        required=(ProcessPort, SessionPort, RunPort),
    )

    first = await host.activate(session_key="session-a")
    second = await host.activate(session_key="session-a")
    third = await host.activate(session_key="session-b")

    assert first.registry.require(ProcessPort) is second.registry.require(ProcessPort)
    assert first.registry.require(ProcessPort) is third.registry.require(ProcessPort)
    assert first.registry.require(SessionPort) is second.registry.require(SessionPort)
    assert first.registry.require(SessionPort) is not third.registry.require(SessionPort)
    assert first.registry.require(RunPort) is not second.registry.require(RunPort)
    assert first.registry.require(RunPort) is not third.registry.require(RunPort)
    assert counters == {"process": 1, "session": 2, "run": 3}


@pytest.mark.asyncio
async def test_disposed_instances_are_released_but_live_scopes_stay_owned() -> None:
    import gc
    import weakref

    from box_agent.plugins.descriptors import PluginScope
    from box_agent.plugins.host import PluginCleanupError

    class ProcessPort:
        pass

    class SessionPort:
        pass

    class RunPort:
        pass

    class Instance:
        pass

    references: dict[str, list[Any]] = {"process": [], "session": [], "run": []}

    def create(scope: str) -> Instance:
        instance = Instance()
        references[scope].append(weakref.ref(instance))
        return instance

    def fail_disposal(_instance: object) -> None:
        raise RuntimeError("expected cleanup failure")

    host = _plugin_host(
        (
            _plugin_descriptor(
                "process.plugin",
                ProcessPort,
                lambda: create("process"),
                scope=PluginScope.PROCESS,
                disposer=fail_disposal,
            ),
            _plugin_descriptor(
                "session.plugin",
                SessionPort,
                lambda: create("session"),
                scope=PluginScope.SESSION,
                disposer=fail_disposal,
            ),
            _plugin_descriptor(
                "run.plugin",
                RunPort,
                lambda: create("run"),
                scope=PluginScope.RUN,
                disposer=fail_disposal,
            ),
        ),
        required=(ProcessPort, SessionPort, RunPort),
    )

    activation = await host.activate(session_key="session-a")
    assert len(host._live_records) == 3

    with pytest.raises(PluginCleanupError):
        await activation.dispose()
    assert len(host._live_records) == 2
    assert references["process"][0]() is not None
    assert references["session"][0]() is not None

    with pytest.raises(PluginCleanupError):
        await host.dispose_session("session-a")
    assert len(host._live_records) == 1

    with pytest.raises(PluginCleanupError):
        await host.close()
    assert len(host._live_records) == 0

    del activation
    gc.collect()
    assert references["run"][0]() is None
    assert references["session"][0]() is None
    assert references["process"][0]() is None


@pytest.mark.asyncio
async def test_repeated_run_disposal_keeps_host_live_state_bounded() -> None:
    import gc
    import weakref

    from box_agent.plugins.descriptors import PluginScope

    class ProcessPort:
        pass

    class SessionPort:
        pass

    class RunPort:
        pass

    class Instance:
        pass

    run_references: list[Any] = []

    def create_run() -> Instance:
        instance = Instance()
        run_references.append(weakref.ref(instance))
        return instance

    host = _plugin_host(
        (
            _plugin_descriptor(
                "process.plugin",
                ProcessPort,
                Instance,
                scope=PluginScope.PROCESS,
            ),
            _plugin_descriptor(
                "session.plugin",
                SessionPort,
                Instance,
                scope=PluginScope.SESSION,
            ),
            _plugin_descriptor("run.plugin", RunPort, create_run),
        ),
        required=(ProcessPort, SessionPort, RunPort),
    )

    for _ in range(5):
        activation = await host.activate(session_key="session-a")
        assert len(host._live_records) == 3
        await activation.dispose()
        del activation
        gc.collect()
        assert len(host._live_records) == 2

    assert all(reference() is None for reference in run_references)
    await host.dispose_session("session-a")
    assert len(host._live_records) == 1
    await host.close()
    assert len(host._live_records) == 0


@pytest.mark.asyncio
async def test_session_scope_requires_explicit_key_before_side_effects() -> None:
    from box_agent.plugins.descriptors import PluginScope
    from box_agent.plugins.host import PluginHost, PluginScopeError

    class SessionPort:
        pass

    calls: list[str] = []
    host = _plugin_host(
        (
            _plugin_descriptor(
                "session.plugin",
                SessionPort,
                lambda: calls.append("activated"),
                scope=PluginScope.SESSION,
            ),
        ),
        required=(SessionPort,),
    )

    with pytest.raises(PluginScopeError, match="session key"):
        await host.activate()
    assert calls == []


def test_plugin_descriptors_are_immutable() -> None:
    from box_agent.plugins.descriptors import PluginScope

    class Port:
        pass

    descriptor = _plugin_descriptor("immutable.plugin", Port, object)

    with pytest.raises(FrozenInstanceError):
        descriptor.scope = PluginScope.PROCESS  # type: ignore[misc]

"""Static descriptors and compatibility composition for default capabilities."""

from __future__ import annotations

from ..kernel.ports import (
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
from .descriptors import PluginDescriptor, PluginScope
from .host import PluginHost
from .registries import (
    ActivatedRegistry,
    CapabilityBinding,
    CapabilityPolicy,
    CapabilitySchema,
)


DEFAULT_CAPABILITY_SCHEMA = CapabilitySchema(
    (
        CapabilityBinding(LLMPort, CapabilityPolicy.REQUIRED_SINGLE),
        CapabilityBinding(SummaryLLMPort, CapabilityPolicy.OPTIONAL_SINGLE),
        CapabilityBinding(PermissionGatewayPort, CapabilityPolicy.OPTIONAL_SINGLE),
        CapabilityBinding(MemoryLookupPort, CapabilityPolicy.OPTIONAL_SINGLE),
        CapabilityBinding(MemoryExtractionPort, CapabilityPolicy.OPTIONAL_SINGLE),
        CapabilityBinding(MemoryPromotionPort, CapabilityPolicy.OPTIONAL_SINGLE),
        CapabilityBinding(SessionStorePort, CapabilityPolicy.OPTIONAL_SINGLE),
        CapabilityBinding(HookBusPort, CapabilityPolicy.REQUIRED_SINGLE),
        CapabilityBinding(ToolCatalogPort, CapabilityPolicy.REQUIRED_SINGLE),
        CapabilityBinding(ToolExposurePort, CapabilityPolicy.OPTIONAL_SINGLE),
        CapabilityBinding(ToolResultStorePort, CapabilityPolicy.OPTIONAL_SINGLE),
    )
)


def _captured_instance_descriptor(
    plugin_id: str,
    port_type: type,
    instance: object,
) -> PluginDescriptor:
    """Describe one caller-owned instance without transferring ownership."""

    return PluginDescriptor(
        plugin_id=plugin_id,
        version="1.0.0",
        capabilities=(port_type,),
        factory=lambda instance=instance: instance,
        scope=PluginScope.RUN,
        disposer=None,
    )


def default_plugin_descriptors(
    *,
    llm: LLMPort,
    summary_llm: SummaryLLMPort | None,
    permission_gateway: PermissionGatewayPort | None,
    memory_lookup: MemoryLookupPort | None,
    memory_extraction: MemoryExtractionPort | None,
    memory_promotion: MemoryPromotionPort | None,
    session_store: SessionStorePort | None,
    hook_bus: HookBusPort,
    tool_catalog: ToolCatalogPort,
    tool_exposure: ToolExposurePort | None,
    tool_result_store: ToolResultStorePort | None,
) -> tuple[PluginDescriptor, ...]:
    """Return deterministic descriptors for the supplied runtime instances."""

    capabilities = (
        ("default.llm", LLMPort, llm, False),
        ("default.summary-llm", SummaryLLMPort, summary_llm, True),
        (
            "default.permission-gateway",
            PermissionGatewayPort,
            permission_gateway,
            True,
        ),
        ("default.memory-lookup", MemoryLookupPort, memory_lookup, True),
        (
            "default.memory-extraction",
            MemoryExtractionPort,
            memory_extraction,
            True,
        ),
        ("default.memory-promotion", MemoryPromotionPort, memory_promotion, True),
        ("default.session-store", SessionStorePort, session_store, True),
        ("default.hook-bus", HookBusPort, hook_bus, False),
        ("default.tool-catalog", ToolCatalogPort, tool_catalog, False),
        ("default.tool-exposure", ToolExposurePort, tool_exposure, True),
        (
            "default.tool-result-store",
            ToolResultStorePort,
            tool_result_store,
            True,
        ),
    )
    return tuple(
        _captured_instance_descriptor(plugin_id, port_type, instance)
        for plugin_id, port_type, instance, optional in capabilities
        if not optional or instance is not None
    )


def create_default_plugin_host(
    *,
    llm: LLMPort,
    summary_llm: SummaryLLMPort | None,
    permission_gateway: PermissionGatewayPort | None,
    memory_lookup: MemoryLookupPort | None,
    memory_extraction: MemoryExtractionPort | None,
    memory_promotion: MemoryPromotionPort | None,
    session_store: SessionStorePort | None,
    hook_bus: HookBusPort,
    tool_catalog: ToolCatalogPort,
    tool_exposure: ToolExposurePort | None,
    tool_result_store: ToolResultStorePort | None,
) -> PluginHost:
    """Create a fresh static host for one outer agent-loop run."""

    return PluginHost(
        default_plugin_descriptors(
            llm=llm,
            summary_llm=summary_llm,
            permission_gateway=permission_gateway,
            memory_lookup=memory_lookup,
            memory_extraction=memory_extraction,
            memory_promotion=memory_promotion,
            session_store=session_store,
            hook_bus=hook_bus,
            tool_catalog=tool_catalog,
            tool_exposure=tool_exposure,
            tool_result_store=tool_result_store,
        ),
        schema=DEFAULT_CAPABILITY_SCHEMA,
    )


def kernel_services_from_registry(registry: ActivatedRegistry) -> KernelServices:
    """Map one immutable activated registry to the kernel's immutable bundle."""

    return KernelServices(
        llm=registry.require(LLMPort),
        summary_llm=registry.get(SummaryLLMPort),
        permission_gateway=registry.get(PermissionGatewayPort),
        memory_lookup=registry.get(MemoryLookupPort),
        memory_extraction=registry.get(MemoryExtractionPort),
        memory_promotion=registry.get(MemoryPromotionPort),
        session_store=registry.get(SessionStorePort),
        hook_bus=registry.require(HookBusPort),
        tool_catalog=registry.require(ToolCatalogPort),
        tool_exposure=registry.get(ToolExposurePort),
        tool_result_store=registry.get(ToolResultStorePort),
    )


def compose_default_services(
    *,
    llm: LLMPort,
    summary_llm: SummaryLLMPort | None,
    permission_gateway: PermissionGatewayPort | None,
    memory_lookup: MemoryLookupPort | None,
    memory_extraction: MemoryExtractionPort | None,
    memory_promotion: MemoryPromotionPort | None,
    session_store: SessionStorePort | None,
    hook_bus: HookBusPort,
    tool_catalog: ToolCatalogPort,
    tool_exposure: ToolExposurePort | None,
    tool_result_store: ToolResultStorePort | None,
) -> KernelServices:
    """Return one immutable bundle without discovery, I/O, or object creation."""

    return KernelServices(
        llm=llm,
        summary_llm=summary_llm,
        permission_gateway=permission_gateway,
        memory_lookup=memory_lookup,
        memory_extraction=memory_extraction,
        memory_promotion=memory_promotion,
        session_store=session_store,
        hook_bus=hook_bus,
        tool_catalog=tool_catalog,
        tool_exposure=tool_exposure,
        tool_result_store=tool_result_store,
    )


__all__ = [
    "DEFAULT_CAPABILITY_SCHEMA",
    "compose_default_services",
    "create_default_plugin_host",
    "default_plugin_descriptors",
    "kernel_services_from_registry",
]

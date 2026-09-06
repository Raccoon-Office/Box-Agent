"""Startup-static plugin composition and lifecycle APIs."""

from .defaults import (
    DEFAULT_CAPABILITY_SCHEMA,
    compose_default_services,
    create_default_plugin_host,
    default_plugin_descriptors,
    kernel_services_from_registry,
)
from .descriptors import PluginDescriptor, PluginScope
from .host import (
    PluginActivation,
    PluginCleanupError,
    PluginDependencyCycleError,
    PluginError,
    PluginHost,
    PluginScopeError,
    PluginValidationError,
)
from .registries import (
    ActivatedRegistry,
    CapabilityBinding,
    CapabilityPolicy,
    CapabilitySchema,
    DuplicateCapabilityError,
    TypedRegistry,
)

__all__ = [
    "ActivatedRegistry",
    "DEFAULT_CAPABILITY_SCHEMA",
    "CapabilityBinding",
    "CapabilityPolicy",
    "CapabilitySchema",
    "DuplicateCapabilityError",
    "PluginActivation",
    "PluginCleanupError",
    "PluginDependencyCycleError",
    "PluginDescriptor",
    "PluginError",
    "PluginHost",
    "PluginScope",
    "PluginScopeError",
    "PluginValidationError",
    "TypedRegistry",
    "compose_default_services",
    "create_default_plugin_host",
    "default_plugin_descriptors",
    "kernel_services_from_registry",
]

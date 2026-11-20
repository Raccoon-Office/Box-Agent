"""Application composition root for the explicitly bundled plugin catalog.

Only this registration list knows the concrete plugin packages. Configuration,
MCP loading, lifecycle orchestration and the kernel depend on generic contracts.
"""

from .descriptors import PluginDescriptor


def bundled_plugin_descriptors() -> tuple[PluginDescriptor, ...]:
    from .cua import plugin_descriptors
    from .repl import plugin_descriptors as repl_descriptors

    return plugin_descriptors() + repl_descriptors()

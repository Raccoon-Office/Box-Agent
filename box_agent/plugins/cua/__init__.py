"""Cua plugin registration and plugin-owned configuration parsing."""

from __future__ import annotations

from ..builtins import AgentRunBindingPort
from ..descriptors import PluginDescriptor, PluginFactoryContext, PluginScope


def _create_bindings(context: PluginFactoryContext):
    from pathlib import Path

    from .config import CuaConfig
    from .wiring import build_cua_bindings

    run = context.context
    raw_settings = run.session.config.plugins.get("cua")
    # The bundled descriptor is discoverable by the host, but CUA itself is
    # opt-in. An absent namespace must not wrap every ordinary Agent run.
    settings = (
        CuaConfig(feed_screenshots=False)
        if raw_settings is None
        else CuaConfig.model_validate(raw_settings)
    )
    session_log = getattr(run.agent, "session_log", None)
    log_path = getattr(session_log, "path", None)
    sidecar_dir = Path(log_path).parent / "images" if log_path else None
    return build_cua_bindings(
        llm=run.options.llm,
        config=settings,
        sidecar_dir=sidecar_dir,
        enabled=raw_settings is not None,
    )


def plugin_descriptors() -> tuple[PluginDescriptor, ...]:
    """Declare lifecycle ownership without changing core service assembly."""
    return (PluginDescriptor(
        "run.cua", "1.0.0", (AgentRunBindingPort,),
        scope=PluginScope.RUN,
        dependencies=("run.services",),
        context_factory=_create_bindings,
        disposer=lambda bindings: bindings.close(),
    ),)


__all__ = ["plugin_descriptors"]

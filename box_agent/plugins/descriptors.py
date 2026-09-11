"""Immutable declarations for startup-static runtime plugins."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Awaitable, Callable


class PluginScope(str, Enum):
    """Lifetime over which a plugin instance is reused."""

    PROCESS = "process"
    SINGLETON = "process"
    SESSION = "session"
    RUN = "run"


PluginFactory = Callable[[], object | Awaitable[object]]
PluginDisposer = Callable[[object], None | Awaitable[None]]


@dataclass(frozen=True, slots=True)
class PluginFactoryContext:
    """Scope context and declared dependency instances passed to a factory.

    PROCESS context must be application-invariant; session configuration belongs
    in SESSION context. The caller's context object is retained by identity.
    """

    context: object
    dependencies: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "dependencies", MappingProxyType(dict(self.dependencies)))


PluginContextFactory = Callable[[PluginFactoryContext], object | Awaitable[object]]


@dataclass(frozen=True, slots=True)
class PluginDescriptor:
    """Side-effect-free metadata used to validate and activate one plugin."""

    plugin_id: str
    version: str
    capabilities: tuple[type[Any], ...]
    factory: PluginFactory | None = None
    dependencies: tuple[str, ...] = ()
    scope: PluginScope = PluginScope.RUN
    disposer: PluginDisposer | None = None
    context_factory: PluginContextFactory | None = None


__all__ = [
    "PluginDescriptor",
    "PluginContextFactory",
    "PluginDisposer",
    "PluginFactory",
    "PluginFactoryContext",
    "PluginScope",
]

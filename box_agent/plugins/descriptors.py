"""Immutable declarations for startup-static runtime plugins."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
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
class PluginDescriptor:
    """Side-effect-free metadata used to validate and activate one plugin."""

    plugin_id: str
    version: str
    capabilities: tuple[type[Any], ...]
    factory: PluginFactory
    dependencies: tuple[str, ...] = ()
    scope: PluginScope = PluginScope.RUN
    disposer: PluginDisposer | None = None


__all__ = [
    "PluginDescriptor",
    "PluginDisposer",
    "PluginFactory",
    "PluginScope",
]

"""Process-only desktop launch overlay, applied before any memory manager exists."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping
from uuid import UUID

from ..config import AgentConfig, DesktopMemoryConfig

_native_environment: dict[str, str] | None = None
_NATIVE_KEYS = ("BOX_AGENT_MEMORY_MODE", "BOX_AGENT_MEMORY_BOOTSTRAP_FILE", "BOX_AGENT_MEMORY_IDENTITY_EPOCH")


def _consume_native_environment() -> Mapping[str, str]:
    global _native_environment
    if _native_environment is None and "BOX_AGENT_MEMORY_MODE" in os.environ:
        _native_environment = {key: os.environ[key] for key in _NATIVE_KEYS if key in os.environ}
    # Shells, MCP servers and tools must never inherit the private bootstrap location.
    for key in _NATIVE_KEYS:
        os.environ.pop(key, None)
    return _native_environment or {}


def desktop_launch_agent(
    agent: AgentConfig, environment: Mapping[str, str] | None = None,
) -> AgentConfig:
    """No bootstrap I/O here. A missing/invalid grant disables memory, never chat."""
    env = _consume_native_environment() if environment is None else environment
    if "BOX_AGENT_MEMORY_MODE" not in env:
        return agent
    file = env.get("BOX_AGENT_MEMORY_BOOTSTRAP_FILE", "")
    epoch = env.get("BOX_AGENT_MEMORY_IDENTITY_EPOCH", "")
    valid = (
        env.get("BOX_AGENT_MEMORY_MODE") == "desktop"
        and 0 < len(file) <= 4096 and "\0" not in file and Path(file).is_absolute()
        and len(epoch) == 36
    )
    try:
        valid = valid and str(UUID(epoch)) == epoch
    except (ValueError, AttributeError):
        valid = False
    return agent.model_copy(update={
        "enable_memory": bool(valid),
        "enable_memory_extraction": False,
        "memory_backend_type": "desktop",
        "memory_desktop": DesktopMemoryConfig(
            bootstrap_file=file if valid else "",
            identity_epoch=epoch if valid else "",
        ),
    })

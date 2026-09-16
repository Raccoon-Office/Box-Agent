"""Resolve the built-in Cua MCP runtime for standalone Box-Agent processes."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from .cua_runtime_daemon import STANDALONE_CUA_SOCKET
from .mcp_sources import ResolvedMcpServer


CUA_MCP_SERVER_NAME = "cua-computer-use"
CUA_RUNTIME_MODE_ENV = "BOX_AGENT_CUA_RUNTIME_MODE"
CUA_DRIVER_PATH_ENV = "BOX_AGENT_CUA_DRIVER_PATH"


def _resolve_cua_driver_command() -> str | None:
    """Find the Cua executable without requiring a user-authored MCP entry."""
    candidates: list[str] = []
    explicit = os.environ.get(CUA_DRIVER_PATH_ENV, "").strip()
    if explicit:
        candidates.append(explicit)
    runtime_root = os.environ.get("BOX_AGENT_RUNTIME_ROOT", "").strip()
    if runtime_root:
        root = Path(runtime_root).expanduser().resolve()
        executable = "cua-driver.exe" if os.name == "nt" else "cua-driver"
        candidates.extend(
            [
                str(root.parent / "cua-driver" / executable),
                str(root / "cua-driver" / executable),
                str(root / "runtimes" / "cua-driver" / executable),
            ]
        )

    candidates.append("cua-driver")
    for candidate in candidates:
        expanded = Path(candidate).expanduser()
        if expanded.is_absolute():
            if expanded.is_file():
                return str(expanded)
            continue
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


def _inherited_cua_environment() -> dict[str, str]:
    """Forward the host's complete Cua policy/runtime environment unchanged."""
    environment = {
        name: value
        for name, value in os.environ.items()
        if name.startswith("CUA_DRIVER_")
    }
    environment.setdefault("CUA_DRIVER_RS_UPDATE_CHECK", "false")
    return environment


def _is_app_hosted_config(definition: ResolvedMcpServer) -> bool:
    """Recognize an Electron-only definition that cannot run standalone."""
    args = definition.config.get("args")
    return (
        isinstance(args, list)
        and "--embedded" in args
        and "--socket" in args
    )


def with_builtin_standalone_cua(
    definitions: dict[str, ResolvedMcpServer],
) -> dict[str, ResolvedMcpServer]:
    """Supply a lazy daemon-backed Cua runtime unless an embedding host owns it."""
    if os.environ.get(CUA_RUNTIME_MODE_ENV, "").strip().lower() == "embedded":
        return definitions

    existing = definitions.get(CUA_MCP_SERVER_NAME)
    if existing is not None and existing.config.get("disabled") is True:
        return definitions
    if existing is not None and not _is_app_hosted_config(existing):
        # A standalone-capable explicit definition is an operator-owned
        # security boundary. Keep its command, arguments, environment,
        # permission mode, and policy intact.
        return definitions

    # Electron persists an app-hosted definition in the shared config. Its
    # private socket and TCC identity are invalid when the CLI runs without the
    # embedding host, so reserve the name for the standalone definition below.
    standalone_definitions = dict(definitions)
    standalone_definitions.pop(CUA_MCP_SERVER_NAME, None)

    command = _resolve_cua_driver_command()
    if command is None:
        return standalone_definitions

    owns_daemon = sys.platform != "darwin"
    args = (
        ["mcp", "--socket", STANDALONE_CUA_SOCKET]
        if owns_daemon
        else ["mcp"]
    )

    config: dict[str, Any] = {
        "description": "Cua Driver - standalone desktop computer use for Box-Agent",
        "command": command,
        "args": args,
        "env": _inherited_cua_environment(),
        "alwaysLoad": True,
        "startOnDemand": True,
        "boxAgentOwnedDaemon": owns_daemon,
        "disabled": False,
        "connect_timeout": 20,
        "execute_timeout": 120,
    }
    fingerprint = hashlib.sha256(
        json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    definition = ResolvedMcpServer(
        name=CUA_MCP_SERVER_NAME,
        config=config,
        owner="system",
        config_id=f"system:{CUA_MCP_SERVER_NAME}",
        connector_id=None,
        connector_name=None,
        source_path="<builtin:cua>",
        fingerprint=fingerprint,
    )
    return {**standalone_definitions, CUA_MCP_SERVER_NAME: definition}


__all__ = [
    "CUA_DRIVER_PATH_ENV",
    "CUA_MCP_SERVER_NAME",
    "CUA_RUNTIME_MODE_ENV",
    "with_builtin_standalone_cua",
]

"""Resolve isolated MCP configuration sources into one runtime registry."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


McpOwner = Literal["system", "connector", "user"]
_CONNECTOR_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class McpConfigSource:
    owner: McpOwner
    path: Path


@dataclass(frozen=True)
class ResolvedMcpServer:
    name: str
    config: dict
    owner: McpOwner
    config_id: str
    connector_id: str | None
    connector_name: str | None
    source_path: str
    fingerprint: str


@dataclass(frozen=True)
class ResolvedMcpSources:
    sources: tuple[McpConfigSource, ...]
    servers: dict[str, ResolvedMcpServer]
    conflicts: tuple[str, ...]


def configured_mcp_sources(primary_path: str) -> tuple[McpConfigSource, ...]:
    """Return source paths in protected-owner precedence order.

    Standalone Box-Agent keeps its historical single-file behavior. The Officev3
    host opts into physical source isolation through explicit environment paths.
    """

    user_path = os.environ.get("BOX_AGENT_USER_MCP_CONFIG_PATH", "").strip()
    system_path = os.environ.get("BOX_AGENT_SYSTEM_MCP_CONFIG_PATH", "").strip()
    connector_path = os.environ.get("BOX_AGENT_CONNECTOR_MCP_CONFIG_PATH", "").strip()

    if not any((user_path, system_path, connector_path)):
        return (McpConfigSource("user", Path(primary_path).expanduser()),)

    sources: list[McpConfigSource] = []
    if system_path:
        sources.append(McpConfigSource("system", Path(system_path).expanduser()))
    if connector_path:
        sources.append(McpConfigSource("connector", Path(connector_path).expanduser()))
    sources.append(McpConfigSource("user", Path(user_path or primary_path).expanduser()))
    return tuple(sources)


def _read_servers(source: McpConfigSource) -> dict[str, dict]:
    try:
        payload = json.loads(source.path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(payload, dict):
        raise ValueError(f"MCP config must be an object: {source.path}")
    raw_servers = payload.get("mcpServers", {})
    if not isinstance(raw_servers, dict):
        raise ValueError(f"mcpServers must be an object: {source.path}")
    servers: dict[str, dict] = {}
    for name, config in raw_servers.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(config, dict):
            raise ValueError(f"Invalid MCP server entry in {source.path}")
        servers[name] = dict(config)
    return servers


def _copy_servers(raw_servers: object, source_label: str) -> dict[str, dict]:
    if not isinstance(raw_servers, dict):
        raise ValueError(f"mcpServers must be an object: {source_label}")
    servers: dict[str, dict] = {}
    for name, config in raw_servers.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(config, dict):
            raise ValueError(f"Invalid MCP server entry in {source_label}")
        servers[name] = dict(config)
    return servers


def _server_identity(
    source: McpConfigSource,
    name: str,
    config: dict,
) -> tuple[str, str | None, str | None]:
    connector_id = config.get("_connectorId")
    if source.owner == "connector":
        if not isinstance(connector_id, str):
            raise ValueError(f"Connector MCP server {name} is missing _connectorId")
        normalized_connector_id = connector_id.strip().lower()
        if not _CONNECTOR_ID_PATTERN.fullmatch(normalized_connector_id):
            raise ValueError(f"Connector MCP server {name} has an invalid _connectorId")
        connector_name = config.get("_connectorName")
        normalized_connector_name = (
            connector_name.strip()
            if isinstance(connector_name, str) and connector_name.strip()
            else normalized_connector_id
        )
        return (
            f"connector:{normalized_connector_id}",
            normalized_connector_id,
            normalized_connector_name,
        )
    if source.owner == "system":
        return f"system:{name}", None, None
    return f"custom-mcp:{name}", None, None


def _fingerprint(
    source: McpConfigSource,
    config_id: str,
    config: dict,
    credential_version: int,
) -> str:
    serialized = json.dumps(
        {
            "owner": source.owner,
            "sourcePath": str(source.path),
            "configId": config_id,
            "config": config,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(f"{serialized}\0{credential_version}".encode("utf-8")).hexdigest()


def resolve_mcp_sources(
    sources: tuple[McpConfigSource, ...],
    credential_versions: dict[str, int] | None = None,
    reserved_names: set[str] | None = None,
    source_server_overrides: dict[McpOwner, dict[str, dict]] | None = None,
) -> ResolvedMcpSources:
    """Resolve sources without allowing lower-trust entries to shadow protected ones."""

    credential_versions = credential_versions or {}
    reserved_names = reserved_names or set()
    resolved: dict[str, ResolvedMcpServer] = {}
    conflicts: list[str] = []
    for source in sources:
        if source_server_overrides is not None and source.owner in source_server_overrides:
            servers = _copy_servers(
                source_server_overrides[source.owner],
                str(source.path),
            )
        else:
            servers = _read_servers(source)
        for name, config in servers.items():
            if source.owner == "user" and name in reserved_names:
                conflicts.append(f"user:{name} uses a protected server name")
                continue
            if name in resolved:
                conflicts.append(
                    f"{source.owner}:{name} conflicts with {resolved[name].owner}:{name}"
                )
                continue
            config_id, connector_id, connector_name = _server_identity(source, name, config)
            credential_ref = config.get("credentialRef")
            if credential_ref is not None and source.owner != "connector":
                raise ValueError(f"MCP server {name}: credentialRef requires a connector-owned source")
            credential_version = (
                credential_versions.get(credential_ref, 0)
                if isinstance(credential_ref, str)
                else 0
            )
            resolved[name] = ResolvedMcpServer(
                name=name,
                config=config,
                owner=source.owner,
                config_id=config_id,
                connector_id=connector_id,
                connector_name=connector_name,
                source_path=str(source.path),
                fingerprint=_fingerprint(source, config_id, config, credential_version),
            )
    return ResolvedMcpSources(sources=sources, servers=resolved, conflicts=tuple(conflicts))

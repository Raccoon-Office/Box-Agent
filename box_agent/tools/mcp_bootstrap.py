"""Bootstrap Box-Agent-managed MCP servers for CLI and ACP runtimes."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MANAGED_MCP_SCHEMA_KEY = "boxAgentManagedMcpVersion"
MANAGED_MCP_SCHEMA_VERSION = 1
HOSTED_SEARCH_SERVER_NAME = "mcp-server-askecho-search-infinity"
HOSTED_SEARCH_SERVER: dict[str, Any] = {
    "description": "Office Raccoon hosted web search",
    "url": "https://xiaohuanxiong.com/api/web/mcp/web_search/v1/mcp",
    "type": "streamable_http",
    "alwaysLoad": True,
    "disabled": False,
}
_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class McpBootstrapResult:
    """Result of reconciling Box-Agent-managed MCP entries."""

    path: Path
    changed: bool = False
    warning: str | None = None


def _runtime_root(explicit_root: Path | None = None) -> Path | None:
    if explicit_root is not None:
        return explicit_root.expanduser().resolve()
    configured = os.environ.get("BOX_AGENT_RUNTIME_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve().parent.parent


def _bundled_mcp_servers(runtime_root: Path | None) -> dict[str, dict[str, Any]]:
    """Return sanitized stdio MCP entries advertised by a runtime manifest."""
    if runtime_root is None:
        return {}
    manifest_path = runtime_root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    raw_servers = manifest.get("mcp_servers")
    if not isinstance(raw_servers, dict):
        return {}

    servers: dict[str, dict[str, Any]] = {}
    for name, raw in raw_servers.items():
        if not isinstance(name, str) or not _SERVER_NAME_RE.fullmatch(name):
            continue
        if not isinstance(raw, dict) or raw.get("transport") != "stdio":
            continue
        entry = raw.get("entry")
        args = raw.get("args", [])
        if not isinstance(entry, str) or not entry or Path(entry).is_absolute():
            continue
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            continue
        command = (runtime_root / entry).resolve()
        try:
            command.relative_to(runtime_root)
        except ValueError:
            continue
        if not command.is_file():
            continue
        servers[name] = {
            "description": f"Box-Agent bundled MCP server: {name}",
            "command": str(command),
            "args": list(args),
            "alwaysLoad": True,
            "disabled": False,
        }
        if name == "box-agent-web-extract":
            servers[name].update(
                {
                    "description": (
                        "Box-Agent Web Extract MCP - direct HTTP page extraction "
                        "with configured-model summarization"
                    ),
                    "connect_timeout": 15,
                    "execute_timeout": 240,
                }
            )
    return servers


def _web_extract_server(command: str | None) -> dict[str, Any]:
    return {
        "description": (
            "Box-Agent Web Extract MCP - direct HTTP page extraction "
            "with configured-model summarization"
        ),
        "command": command,
        "args": [],
        "alwaysLoad": True,
        "disabled": False,
        "connect_timeout": 15,
        "execute_timeout": 240,
    }


def _merge_managed_entry(
    existing: object,
    managed: dict[str, Any],
    *,
    migrate_legacy_disabled: bool,
) -> dict[str, Any]:
    current = dict(existing) if isinstance(existing, dict) else {}
    disabled = current.get("disabled")
    current.update(managed)
    if not migrate_legacy_disabled and isinstance(disabled, bool):
        current["disabled"] = disabled
    return current


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    previous_mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temp_path = Path(handle.name)
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temp_path, previous_mode)
    os.replace(temp_path, path)


def bootstrap_managed_mcp_config(
    path: Path,
    *,
    runtime_root: Path | None = None,
    web_extract_command: str | None = None,
) -> McpBootstrapResult:
    """Add hosted and runtime-bundled MCP servers to the user configuration.

    The first migration enables legacy bundled entries that shipped disabled.
    Later runs preserve an explicit user ``disabled`` choice while refreshing
    managed endpoints and runtime-relative executable paths.
    """
    path = path.expanduser()
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return McpBootstrapResult(
                path=path,
                warning=f"Cannot bootstrap managed MCP servers from {path}: {exc}",
            )
        if not isinstance(payload, dict):
            return McpBootstrapResult(
                path=path,
                warning=f"Cannot bootstrap managed MCP servers: {path} is not an object",
            )
    else:
        payload = {}

    raw_servers = payload.get("mcpServers")
    if raw_servers is None:
        servers: dict[str, Any] = {}
    elif isinstance(raw_servers, dict):
        servers = dict(raw_servers)
    else:
        return McpBootstrapResult(
            path=path,
            warning=f"Cannot bootstrap managed MCP servers: {path} mcpServers is not an object",
        )

    already_migrated = payload.get(MANAGED_MCP_SCHEMA_KEY) == MANAGED_MCP_SCHEMA_VERSION
    managed_servers = {HOSTED_SEARCH_SERVER_NAME: HOSTED_SEARCH_SERVER}
    bundled_servers = _bundled_mcp_servers(_runtime_root(runtime_root))
    managed_servers.update(bundled_servers)
    if "box-agent-web-extract" not in bundled_servers:
        command = web_extract_command or shutil.which("box-agent-web-extract-mcp")
        if command:
            managed_servers["box-agent-web-extract"] = _web_extract_server(command)
    for name, managed in managed_servers.items():
        servers[name] = _merge_managed_entry(
            servers.get(name),
            managed,
            migrate_legacy_disabled=not already_migrated,
        )

    updated = dict(payload)
    updated["mcpServers"] = servers
    updated[MANAGED_MCP_SCHEMA_KEY] = MANAGED_MCP_SCHEMA_VERSION
    changed = updated != payload
    if changed:
        _atomic_write_json(path, updated)
    return McpBootstrapResult(path=path, changed=changed)

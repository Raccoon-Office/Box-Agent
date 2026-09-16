"""Idempotent bootstrap for the configured Cua Driver MCP runtime."""

from __future__ import annotations

import asyncio
import os
from typing import Any, Literal

from .base import Tool, ToolResult
from .cua_runtime_daemon import ensure_standalone_cua_daemon
from .mcp_loader import (
    get_mcp_server_config,
    get_mcp_status,
    get_mcp_tool_catalog,
    get_mcp_tools_for_server,
    is_mcp_server_connection_current,
    reconnect_mcp_server,
)


CUA_MCP_SERVER_NAME = "cua-computer-use"
_READY_LOCK = asyncio.Lock()


def _server_status() -> dict[str, Any] | None:
    return next(
        (status for status in get_mcp_status() if status.get("name") == CUA_MCP_SERVER_NAME),
        None,
    )


def _configured_mode() -> tuple[Literal["app-hosted", "standalone", "unknown"], str | None]:
    try:
        server = get_mcp_server_config(CUA_MCP_SERVER_NAME)
    except (OSError, ValueError) as exc:
        return "unknown", f"Cua MCP configuration is invalid: {exc}"
    if not isinstance(server, dict):
        return "unknown", "Cua MCP server is not configured"
    if server.get("disabled") is True:
        return "unknown", "Cua MCP server is disabled"

    args = server.get("args", [])
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        return "unknown", "Cua MCP server has invalid arguments"
    if os.environ.get("BOX_AGENT_CUA_RUNTIME_MODE", "").strip().lower() == "embedded":
        return "app-hosted", None
    return "standalone", None


def _standalone_runtime(server: dict[str, Any]) -> tuple[str, str] | None:
    if server.get("boxAgentOwnedDaemon") is not True:
        return None
    command = server.get("command")
    args = server.get("args", [])
    if not isinstance(command, str) or not command.strip() or not isinstance(args, list):
        return None
    try:
        socket_index = args.index("--socket") + 1
        socket_path = args[socket_index]
    except (ValueError, IndexError):
        return None
    if not isinstance(socket_path, str) or not socket_path:
        return None
    return command, socket_path


def _result(
    status: str,
    *,
    mode: str,
    tool_count: int = 0,
    error: str | None = None,
) -> ToolResult:
    payload = {
        "status": status,
        "mode": mode,
        "daemon_running": status == "ready",
        "mcp_connected": status == "ready",
        "tool_count": tool_count,
        "next_action": (
            "Use tool_search with server_name='cua-computer-use' to activate the "
            "desktop tools needed for the task."
            if status == "ready"
            else None
        ),
    }
    content = "\n".join(
        f"{key}={value}"
        for key, value in payload.items()
        if value is not None
    )
    return ToolResult(
        success=status == "ready",
        content=content,
        error=error,
        raw_output=payload | ({"error": error} if error else {}),
    )


def _failure_status(mode: str, error: str) -> str:
    normalized = error.casefold()
    if any(
        marker in normalized
        for marker in ("permission", "accessibility", "screen recording", "授权")
    ):
        return "needs_permission"
    if mode == "app-hosted" and any(
        marker in normalized
        for marker in ("daemon", "embedded", "socket", "pipe", "listening", "connect")
    ):
        return "host_unavailable"
    return "failed"


class EnsureCuaReadyTool(Tool):
    """Ensure the trusted Cua MCP configuration has a live connection."""

    @property
    def name(self) -> str:
        return "ensure_cua_ready"

    @property
    def description(self) -> str:
        return (
            "Ensure desktop computer-use is ready before operating native applications. "
            "This idempotent tool checks the configured Cua Driver connection and starts "
            "or reconnects it when needed. In app-hosted mode it only attaches to the "
            "Electron-managed daemon; in standalone mode Box-Agent prepares the platform's "
            "supported Cua runtime and connects its MCP proxy. Call it once when a desktop "
            "task begins or after a connection failure, then use tool_search for the "
            "required Cua tools. It never downloads or updates the executable, widens "
            "the permission policy, or changes the selected runtime mode."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }

    async def execute(self) -> ToolResult:
        catalog = get_mcp_tool_catalog()
        if catalog.loading:
            await catalog.wait_until_ready(timeout=20.0)

        mode, config_error = _configured_mode()
        if config_error:
            return _result("not_configured", mode=mode, error=config_error)

        status = _server_status()
        if status and status.get("state") == "connecting":
            await catalog.wait_until_ready(timeout=20.0)

        tools = get_mcp_tools_for_server(CUA_MCP_SERVER_NAME)
        status = _server_status()
        if (
            status
            and status.get("state") == "connected"
            and tools
            and is_mcp_server_connection_current(CUA_MCP_SERVER_NAME)
        ):
            return _result("ready", mode=mode, tool_count=len(tools))

        async with _READY_LOCK:
            tools = get_mcp_tools_for_server(CUA_MCP_SERVER_NAME)
            status = _server_status()
            if (
                status
                and status.get("state") == "connected"
                and tools
                and is_mcp_server_connection_current(CUA_MCP_SERVER_NAME)
            ):
                return _result("ready", mode=mode, tool_count=len(tools))

            if mode == "standalone":
                server = get_mcp_server_config(CUA_MCP_SERVER_NAME)
                runtime = _standalone_runtime(server or {})
                if runtime is not None:
                    command, socket_path = runtime
                    daemon_ready, daemon_error = await ensure_standalone_cua_daemon(
                        command,
                        socket_path=socket_path,
                    )
                    if not daemon_ready:
                        return _result("failed", mode=mode, error=daemon_error)

            reconnect = await reconnect_mcp_server(CUA_MCP_SERVER_NAME)
            if reconnect.get("success"):
                tools = get_mcp_tools_for_server(CUA_MCP_SERVER_NAME)
                if tools:
                    return _result("ready", mode=mode, tool_count=len(tools))
                return _result(
                    "failed",
                    mode=mode,
                    error="Cua MCP connected without exposing any desktop tools",
                )

            error = str(reconnect.get("error") or "Cua Driver did not become ready")
            return _result(
                _failure_status(mode, error),
                mode=mode,
                error=error,
            )


__all__ = ["CUA_MCP_SERVER_NAME", "EnsureCuaReadyTool"]

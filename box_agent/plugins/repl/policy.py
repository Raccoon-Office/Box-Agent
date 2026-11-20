"""REPL-only model entrypoints; internal Browser resolution stays independent."""
from __future__ import annotations

from box_agent.tools.base import Tool
from box_agent.tools.browser_tool_names import is_browser_tool_name


def exclude_browser_backends(tool: Tool) -> bool:
    server = getattr(tool, "server_name", "") or getattr(tool, "_server_name", "")
    return server not in {"playwright", "browser-gateway"} and not is_browser_tool_name(tool.name)


def model_tool_filter(cua_server: str = "computer-use"):
    servers = {"computer-use", cua_server}

    def allowed(tool: Tool) -> bool:
        server = getattr(tool, "server_name", "") or getattr(tool, "_server_name", "")
        return not (
            server in servers
            or tool.name == "ensure_cua_ready"
            or not exclude_browser_backends(tool)
        )

    return allowed

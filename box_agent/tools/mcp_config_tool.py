"""MCP configuration tool — inspect and manage mcp.json servers."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from box_agent.config import Config
from box_agent.tools.base import Tool, ToolResult
from box_agent.user_paths import configured_box_agent_home, state_path


_ALLOWED_SERVER_FIELDS = frozenset(
    {
        "command",
        "args",
        "env",
        "url",
        "type",
        "transport",
        "headers",
        "connect_timeout",
        "execute_timeout",
        "sse_read_timeout",
        "disabled",
        "alwaysLoad",
    }
)
_UPDATE_OPERATORS = frozenset({"args_add", "args_remove", "remove_fields"})


def _updated_server_config(
    existing: dict[str, Any],
    changes: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Apply a shallow server patch plus safe list/remove operators."""
    unknown = set(changes) - _ALLOWED_SERVER_FIELDS - _UPDATE_OPERATORS
    if unknown:
        return None, f"Unsupported update field(s): {', '.join(sorted(unknown))}"

    updated = dict(existing)
    for field in _ALLOWED_SERVER_FIELDS:
        if field in changes:
            updated[field] = changes[field]

    remove_fields = changes.get("remove_fields", [])
    if not isinstance(remove_fields, list) or not all(
        isinstance(field, str) for field in remove_fields
    ):
        return None, "'remove_fields' must be a list of strings"
    invalid_removals = set(remove_fields) - _ALLOWED_SERVER_FIELDS
    if invalid_removals:
        return None, (
            "Unsupported remove field(s): "
            + ", ".join(sorted(invalid_removals))
        )
    for field in remove_fields:
        updated.pop(field, None)

    args_add = changes.get("args_add", [])
    args_remove = changes.get("args_remove", [])
    if not isinstance(args_add, list) or not all(isinstance(arg, str) for arg in args_add):
        return None, "'args_add' must be a list of strings"
    if not isinstance(args_remove, list) or not all(
        isinstance(arg, str) for arg in args_remove
    ):
        return None, "'args_remove' must be a list of strings"
    if args_add or args_remove:
        raw_args = updated.get("args")
        if not isinstance(raw_args, list) or not all(
            isinstance(arg, str) for arg in raw_args
        ):
            return None, "Server has no valid string args list"
        next_args = [arg for arg in raw_args if arg not in set(args_remove)]
        for arg in args_add:
            if arg not in next_args:
                next_args.append(arg)
        updated["args"] = next_args

    return updated, None


def _browser_config_summary(config: Any) -> list[str]:
    """Return a secret-free summary of the configured browser launch mode."""
    if not isinstance(config, dict):
        return [
            "mode=unknown",
            "isolated=unknown",
            "profile=unknown",
            "executable=configured=false",
        ]

    raw_args = config.get("args")
    if not isinstance(raw_args, list) or not all(isinstance(arg, str) for arg in raw_args):
        return [
            "mode=unknown",
            "isolated=unknown",
            "profile=unknown",
            "executable=configured=false",
        ]

    args = list(raw_args)
    headless = "--headless" in args
    isolated = "--isolated" in args
    has_user_data_dir = any(
        arg == "--user-data-dir" or arg.startswith("--user-data-dir=")
        for arg in args
    )
    shared_context = "--shared-browser-context" in args
    has_executable = any(
        arg == "--executable-path" or arg.startswith("--executable-path=")
        for arg in args
    )

    if isolated:
        profile = "ephemeral-isolated"
    elif has_user_data_dir or shared_context:
        profile = "persistent-or-shared"
    else:
        profile = "runtime-default"

    # Per-session BrowserContext isolation is only possible for an isolated
    # (ephemeral) profile; keep the reason visible so the model does not try
    # to "fix" concurrency by launching duplicate playwright entries.
    from box_agent.tools.mcp_loader import playwright_multiplex_blocker

    blocker = playwright_multiplex_blocker("playwright", config)
    per_session_context = (
        "true" if blocker is None else f"false ({blocker})"
    )

    return [
        f"mode={'headless' if headless else 'headed'}",
        f"isolated={str(isolated).lower()}",
        f"profile={profile}",
        f"per_session_context={per_session_context}",
        f"executable=configured={str(has_executable).lower()}",
    ]


def _resolve_write_target() -> Path:
    host_user_path = os.environ.get("BOX_AGENT_USER_MCP_CONFIG_PATH", "").strip()
    if host_user_path:
        target = state_path("config/mcp.json", Path(host_user_path).expanduser())
        target.parent.mkdir(parents=True, exist_ok=True)
        return target
    if configured_box_agent_home() is not None:
        from box_agent.tools.mcp_loader import get_mcp_config_path
        target = state_path("config/mcp.json", get_mcp_config_path() or None)
        target.parent.mkdir(parents=True, exist_ok=True)
        return target
    # Priority: loader's actual runtime path > user config dir > packaged config.
    # In dev, box-agent may boot before ~/.box-agent/config/mcp.json exists and
    # end up loading ./box_agent/config/mcp.json. Writing to user dir here would
    # split the two views: loader keeps reading dev copy, watcher sees the user
    # copy, reconnects come up empty. Following the loader's resolved path keeps
    # tool + loader + host watcher pointed at the same file.
    try:
        from box_agent.tools.mcp_loader import get_mcp_config_path
        loader_path = get_mcp_config_path()
        if loader_path:
            p = Path(loader_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            return p
    except Exception:
        pass

    user_path = state_path("config/mcp.json")
    if user_path.exists():
        return user_path
    try:
        p = Config.find_config_file("mcp.json")
        if p and p.exists():
            return p
    except Exception:
        pass
    user_path.parent.mkdir(parents=True, exist_ok=True)
    return user_path


class McpConfigTool(Tool):
    """Manage MCP server entries in mcp.json."""

    @property
    def name(self) -> str:
        return "mcp_config"

    @property
    def description(self) -> str:
        return (
            "Read or modify the MCP server configuration (mcp.json). "
            "Actions: list — show current servers; "
            "inspect_browser — safely report the configured Playwright browser mode "
            "without exposing unrelated MCP credentials; "
            "update — patch an existing server while preserving unspecified settings; "
            "add — add or replace a server entry; "
            "remove — delete a server entry; "
            "enable / disable — toggle a server without deleting it. "
            "The list action reports configuration entries, not connected tools or "
            "callable schemas; use tool_search for capability discovery. "
            "This tool only confirms the configuration write, not a live connection. "
            "The host watches mcp.json and applies hot reload automatically; when "
            "registration finishes during an active turn, the runtime supplies an "
            "internal connection-state update (or load after box-agent restart)."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "list",
                        "inspect_browser",
                        "update",
                        "add",
                        "remove",
                        "enable",
                        "disable",
                    ],
                    "description": "Operation to perform.",
                },
                "name": {
                    "type": "string",
                    "description": "Server name key in mcpServers (required for add/remove/enable/disable).",
                },
                "config": {
                    "type": "object",
                    "description": (
                        "Server config object for 'add', or changes for 'update'. "
                        "For stdio: {command, args?, env?}. "
                        "For URL-based: {url, type?: 'sse'|'http'|'streamable_http', headers?}. "
                        "Optional: connect_timeout, execute_timeout, sse_read_timeout, disabled, "
                        "alwaysLoad. Set alwaysLoad=true only for a small core tool set that must "
                        "remain directly visible; other MCP tools stay deferred. "
                        "Update also supports args_add, args_remove, and remove_fields string lists."
                    ),
                },
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str,
        name: str = "",
        config: dict | None = None,
    ) -> ToolResult:
        target = _resolve_write_target()

        if target.exists():
            try:
                with open(target, encoding="utf-8") as f:
                    data: dict[str, Any] = json.load(f)
            except Exception as e:
                return ToolResult(success=False, content="", error=f"Failed to read {target}: {e}")
        else:
            data = {"mcpServers": {}}

        servers: dict[str, Any] = data.setdefault("mcpServers", {})

        if action == "list":
            if not servers:
                return ToolResult(
                    success=True,
                    content=(
                        f"No MCP servers configured in {target}. This is configuration "
                        "state, not a connected tool inventory; use tool_search for "
                        "capability discovery."
                    ),
                )
            lines = [
                f"MCP config: {target}",
                "note=Configuration entries only; not proof of connection and not a tool inventory.",
                "note=Use tool_search to discover connected MCP capabilities.",
                "",
            ]
            for sname, scfg in servers.items():
                disabled = scfg.get("disabled", False)
                status = "disabled" if disabled else "enabled"
                transport = (
                    "url"
                    if scfg.get("url")
                    else "stdio"
                    if scfg.get("command")
                    else "unknown"
                )
                lines.append(f"  {sname} [{status}] transport={transport}")
            return ToolResult(success=True, content="\n".join(lines))

        if action == "inspect_browser":
            playwright = servers.get("playwright")
            lines = [
                f"Browser automation config: {target}",
                "source=config_file",
            ]
            if not isinstance(playwright, dict):
                lines.extend(_browser_config_summary(None))
                lines.append("enabled=false")
                lines.append(
                    "note=No playwright server entry is configured; current process mode cannot be inferred."
                )
                return ToolResult(success=True, content="\n".join(lines))

            lines.extend(_browser_config_summary(playwright))
            lines.append(
                f"enabled={str(playwright.get('disabled') is not True).lower()}"
            )
            lines.append(
                "note=This is the current configured launch mode. It becomes effective "
                "after the host reconnect succeeds; it is not proof of the running process."
            )
            return ToolResult(success=True, content="\n".join(lines))

        if not name:
            return ToolResult(success=False, content="", error="'name' is required for this action")

        if action == "remove":
            if name not in servers:
                return ToolResult(success=False, content="", error=f"Server '{name}' not found")
            del servers[name]

        elif action in ("enable", "disable"):
            if name not in servers:
                return ToolResult(success=False, content="", error=f"Server '{name}' not found")
            servers[name]["disabled"] = (action == "disable")

        elif action == "update":
            if name not in servers:
                return ToolResult(
                    success=False,
                    content="",
                    error=f"Server '{name}' not found",
                )
            if config is None:
                return ToolResult(
                    success=False,
                    content="",
                    error="'config' object is required for update",
                )
            existing = servers[name]
            if not isinstance(existing, dict):
                return ToolResult(
                    success=False,
                    content="",
                    error=f"Server '{name}' has an invalid configuration",
                )
            updated, error = _updated_server_config(existing, config)
            if error:
                return ToolResult(success=False, content="", error=error)
            servers[name] = updated

        elif action == "add":
            if config is None:
                return ToolResult(success=False, content="", error="'config' object is required for add")
            # Only fields that mcp_loader actually consumes are kept; legacy
            # lazy / keywords are silently dropped because the runtime never
            # honored them. alwaysLoad is the explicit deferred-loading escape
            # hatch for a deliberately small core tool set.
            entry = {k: v for k, v in config.items() if k in _ALLOWED_SERVER_FIELDS}
            servers[name] = entry

        else:
            return ToolResult(success=False, content="", error=f"Unknown action: {action}")

        try:
            with open(target, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
                f.write("\n")
        except Exception as e:
            return ToolResult(success=False, content="", error=f"Failed to write {target}: {e}")

        return ToolResult(
            success=True,
            content=(
                f"Done. {target} updated. "
                "This confirms only the configuration write, not an MCP connection. "
                "The host watches this file and applies hot reload automatically. "
                "If registration completes during this turn, the runtime will inject "
                "an internal readiness update; otherwise report the server as pending. "
                "If no host is driving reconnects, restart box-agent to load."
            ),
        )

"""CUA readiness seam registered alongside the existing MCP runtime owner."""
from __future__ import annotations

import json
import os
import sys

from box_agent.tools.base import Tool, ToolResult
from .config import CuaConfig, CuaRuntimeConfig
from .driver import DriverAdapter, installed_driver
from .lifecycle import CuaRuntimeManager
from .runtime.connection import DriverConnection, HOST_RUNTIME_ENV


class CuaService:
    def __init__(self, settings: CuaConfig):
        self.settings = settings
        runtime = settings.runtime
        assert runtime is not None
        driver = runtime.driver_path or installed_driver()
        self.adapter = DriverAdapter(driver or "cua-driver", runtime.endpoint)
        self.config = runtime.model_copy(update={"driver_path": driver})
        self.manager = CuaRuntimeManager(self.config, probe=self.adapter.ready, start=self.adapter.start)

    @property
    def name(self):
        return self.settings.server_name

    @property
    def driver_server_config(self):
        command = DriverConnection(self.config.driver_path or "cua-driver", self.adapter.endpoint,
                                   self.config.mode == "external").command()
        return {"command": command[0], "args": command[1:],
                "disabled": not self.config.enabled}

    @property
    def server_config(self):
        if self.settings.tool_interface == "driver":
            return self.driver_server_config
        command = ([sys.executable, "--cua-mcp"] if getattr(sys, "frozen", False)
                   else [sys.executable, "-m", "box_agent.mcp_servers.cua_server"])
        runtime = self.config.model_copy(update={
            "driver_path": self.config.driver_path or "cua-driver",
            "endpoint": self.adapter.endpoint,
        })
        return {"command": command[0], "args": command[1:],
                "env": {HOST_RUNTIME_ENV: runtime.model_dump_json()},
                "disabled": not self.config.enabled}

    async def prepare(self):
        result = await self.manager.ensure_ready()
        if result.state != "ready":
            raise RuntimeError(result.detail or f"CUA runtime is {result.state}")

    async def close(self):
        await self.manager.close()
        await self.adapter.close_pending()
        self.adapter.cleanup()


class EnsureCuaReadyTool(Tool):
    name = "ensure_cua_ready"
    description = "Prepare desktop computer-use, then connect its MCP tools. Call before desktop actions; use tool_search to discover the returned tools. Never downloads drivers or grants OS permissions."
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}

    def __init__(self, service):
        self.service = service

    async def execute(self, **kwargs):
        from box_agent.tools.mcp_loader import reconnect_mcp_server
        from box_agent.tools.mcp_tool_catalog import get_mcp_tool_catalog
        if kwargs:
            return ToolResult(success=False, error="ensure_cua_ready accepts no arguments")
        if not await get_mcp_tool_catalog().wait_until_ready(timeout=30):
            return ToolResult(success=False, error="MCP discovery is still loading; retry after readiness")
        result = await reconnect_mcp_server(self.service.name)
        content = json.dumps(result)
        if result.get("success"):
            instructions = (
                "CUA ready. Use get_app to bind the application and get_state to observe it. "
                "Element IDs come from the latest app observation; observe again after each action.\n"
                if self.service.settings.tool_interface == "app" else
                "CUA ready. Read the computer-use skill before acting. Use tool_search to discover actions. "
                "Copy only exact window_id, snapshot_id and element_token values from CUA structured results; "
                "never guess or synthesize identifiers. Pass session only if the specific tool schema declares it.\n"
            )
            content = instructions + content
        return ToolResult(success=result.get("success", False), content=content, error=result.get("error"))


def create_cua_service(config):
    raw = config.plugins.get("cua")
    host = os.environ.get("BOX_AGENT_CUA_RUNTIME_CONFIG", "").strip()
    if host:
        runtime = json.loads(host)
        user_runtime = (raw or {}).get("runtime") or {}
        user_enabled = CuaRuntimeConfig(enabled=user_runtime.get("enabled", True)).enabled
        if not user_enabled:
            runtime["enabled"] = False
        settings = CuaConfig.model_validate({**(raw or {}), "runtime": runtime})
    elif raw is not None:
        settings = CuaConfig.model_validate(raw)
        if settings.runtime is None:
            settings.runtime = CuaRuntimeConfig(enabled=True)
    else:
        settings = CuaConfig(runtime=CuaRuntimeConfig(enabled=True))
    config.plugins["cua"] = settings.model_dump()
    return CuaService(settings)

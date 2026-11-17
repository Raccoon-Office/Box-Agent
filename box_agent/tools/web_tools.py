"""Stable first-party web interfaces backed by the current MCP connections."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from box_agent.mcp_servers.web_extract import WebExtractTool

from . import mcp_loader
from .base import (
    Tool,
    ToolInvocationContext,
    ToolInvocationRejected,
    ToolResult,
    check_tool_cancellation,
)
from .mcp_result_hooks import allowed_mcp_result_adapters
from .mcp_tool_catalog import get_mcp_tool_catalog


_WEB_SERVERS = {
    "web_search": mcp_loader.HOSTED_WEB_SEARCH_SERVER_NAME,
    "web_extract": "box-agent-web-extract",
}


class FirstPartyWebTool(Tool):
    """Expose a local contract and resolve its MCP implementation when called."""

    reserved_mcp_entrypoint = True
    uses_invocation_context = True

    def __init__(self, name: str) -> None:
        self._name = name
        self._server_name = _WEB_SERVERS[name]
        if name == "web_search":
            self._description = mcp_loader.HOSTED_WEB_SEARCH_DESCRIPTION
            self._parameters = deepcopy(mcp_loader.HOSTED_WEB_SEARCH_PARAMETERS)
        else:
            extractor = WebExtractTool(llm=None)
            self._description = extractor.description
            self._parameters = extractor.parameters

    @property
    def name(self) -> str:
        return self._name

    @property
    def server_name(self) -> str:
        return self._server_name

    @property
    def remote_name(self) -> str:
        return self._name

    @property
    def execution_tool_id(self) -> str:
        return f"mcp:{self.server_name}/{self.remote_name}"

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return deepcopy(self._parameters)

    @property
    def transient_followup_allowed(self) -> bool:
        return bool(allowed_mcp_result_adapters(
            server_name=self.server_name, remote_name=self.remote_name,
        ))

    async def invoke(
        self,
        arguments: dict[str, Any],
        *,
        context: ToolInvocationContext | None = None,
    ) -> ToolResult:
        try:
            await mcp_loader.wait_for_mcp_server(self.server_name)
            check_tool_cancellation()
        except ToolInvocationRejected as error:
            return error.to_result()
        except Exception as error:
            return ToolResult(
                success=False, error=f"WEB_TOOL_UNAVAILABLE: {self.name}: {error}",
            )

        entry = get_mcp_tool_catalog().get_by_model_name(self.name)
        if (entry is None or entry.name_conflict
                or entry.server_name != self.server_name
                or entry.remote_name != self.remote_name
                or entry.connector_id is not None):
            return ToolResult(
                success=False,
                error=f"WEB_TOOL_UNAVAILABLE: {self.name} has no ready first-party MCP connection.",
            )
        return await entry.tool.invoke(arguments, context=context)

    async def execute(self, **arguments: Any) -> ToolResult:
        return await self.invoke(arguments)


def create_web_tools(config_path: str) -> list[Tool]:
    """Read local configuration without waiting for MCP transports to connect."""
    try:
        definitions = mcp_loader._resolve_registered_sources(config_path)
    except Exception:
        # The background loader owns configuration diagnostics.
        return []
    return [
        FirstPartyWebTool(name)
        for name, server in _WEB_SERVERS.items()
        if (definition := definitions.get(server)) is not None
        and definition.connector_id is None
        and not definition.config.get("disabled", False)
    ]

"""Expand state-test fixtures into the SDK's keyword-based adapter API.

Wire tests use actual JSON-RPC and schema validation; this helper only keeps
the existing reusable namespace fixtures useful for business-state assertions.
"""

from types import SimpleNamespace

from pydantic import BaseModel


def acp_params(params):
    values = params.model_dump() if isinstance(params, BaseModel) else vars(params).copy()
    meta = values.pop("field_meta", None) or {}
    if "prompt" in values:
        # Old unit fixtures used bare text dictionaries / namespace blocks.
        # Public methods now receive schema-validated ContentBlocks from SDK.
        blocks = [vars(block) if isinstance(block, SimpleNamespace) else block
                  for block in values["prompt"]]
        values["prompt"] = [dict(block, type="text")
                            if isinstance(block, dict) and "type" not in block and "text" in block
                            else block for block in blocks]
    names = {
        "sessionId": "session_id", "protocolVersion": "protocol_version",
        "mcpServers": "mcp_servers", "clientCapabilities": "client_capabilities",
        "clientInfo": "client_info",
    }
    return {names.get(key, key): value for key, value in values.items()} | meta

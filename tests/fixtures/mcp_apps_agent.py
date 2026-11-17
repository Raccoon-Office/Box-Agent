"""Offline ACP peer for desktop MCP Apps acceptance.

Run with the repository Python: python tests/fixtures/mcp_apps_agent.py
--state-dir /tmp/mcp-apps-acceptance. It uses the real BoxACPAgent and MCP stdio
transport. No model endpoint, user configuration, or login token is loaded.
"""
import argparse
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from acp.agent.connection import AgentSideConnection
from box_agent.acp import BoxACPAgent, _close_acp_connection
from box_agent.acp.request_state import release_received_frames
from box_agent.acp.stdio_compat import StdioTransport, stdio_streams_largebuf
from box_agent.config import Config, AgentConfig, LLMConfig, ToolsConfig, MCPConfig
from box_agent.schema import FunctionCall, StreamEvent, ToolCall
from box_agent.tools.mcp_loader import load_mcp_tools_async, cleanup_mcp_connections


class FixtureModel:
    model = "offline-mcp-apps-fixture"
    max_output_tokens = 2048

    def __init__(self):
        self.requests = []

    async def generate_stream(self, messages, tools=None, **kwargs):
        self.requests.append(deepcopy(messages))
        if len(self.requests) == 1:
            call = ToolCall(id="discover-flights", type="function", function=FunctionCall(
                name="tool_search", arguments={"tool_names": ["mcp__flight-fixture__list_flights"]}))
        elif len(self.requests) == 2:
            result = next(m for m in messages if m.role == "tool" and m.tool_call_id == "discover-flights")
            target = next(row for row in json.loads(result.content)["activated"] if row["name"] == "mcp__flight-fixture__list_flights")
            call = ToolCall(id="flights-original", type="function", function=FunctionCall(
                name="call_tool", arguments={"tool_ref": target["tool_ref"],
                    "arguments_json": json.dumps({"date": "2026-11-20"})}))
        else:
            yield StreamEvent(type="text", delta="Flight options are ready.")
            yield StreamEvent(type="finish", finish_reason="stop")
            return
        yield StreamEvent(type="finish", finish_reason="tool", tool_calls=[call])


async def run(state_dir):
    state_dir.mkdir(parents=True, exist_ok=True)
    workspace = state_dir / "workspace"
    workspace.mkdir(exist_ok=True)
    os.environ["BOX_AGENT_HOME"] = str(state_dir)
    os.environ["BOX_AGENT_SESSION_TRACE_ENABLED"] = "0"
    for key in ("BOX_AGENT_USER_MCP_CONFIG_PATH", "BOX_AGENT_SYSTEM_MCP_CONFIG_PATH", "BOX_AGENT_CONNECTOR_MCP_CONFIG_PATH"):
        os.environ.pop(key, None)
    config_path = state_dir / "mcp.json"
    config_path.write_text(json.dumps({"mcpServers": {"flight-fixture": {
        "command": sys.executable, "args": [str(Path(__file__).with_name("mcp_apps_server.py"))],
        "env": {"MCP_APPS_FIXTURE_LOG": os.environ.get("MCP_APPS_FIXTURE_LOG", str(state_dir / "calls.jsonl"))},
    }}}), encoding="utf-8")
    config = Config(llm=LLMConfig(api_key="fixture-unused"),
        agent=AgentConfig(workspace_dir=str(workspace), max_steps=4, enable_memory=False),
        tools=ToolsConfig(enable_sub_agent=False, mcp=MCPConfig(deferred_loading_enabled=True)))
    gate, shutdown = asyncio.Event(), asyncio.Event()

    async def load():
        await gate.wait()
        return await load_mcp_tools_async(str(config_path))

    task = asyncio.create_task(load(), name="fixture-mcp-discovery")
    reader, writer = await stdio_streams_largebuf(on_eof=shutdown.set)
    sys.stdout = sys.stderr  # All non-protocol diagnostics remain on stderr.
    adapter = None

    def create(conn):
        nonlocal adapter
        adapter = BoxACPAgent(conn, config, FixtureModel(), [], "You are a test flight assistant.",
                              mcp_task=task)
        return adapter

    connection = AgentSideConnection(create, StdioTransport(reader, writer, on_disconnect=shutdown.set),
                                     use_unstable_protocol=True)
    release_received_frames(connection._conn)
    gate.set()
    try:
        await shutdown.wait()
    finally:
        if adapter is not None:
            await adapter.aclose()
        await _close_acp_connection(connection, writer)
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await cleanup_mcp_connections()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path)
    args = parser.parse_args()
    if args.state_dir is not None:
        asyncio.run(run(args.state_dir.resolve()))
    else:
        with tempfile.TemporaryDirectory(prefix="mcp-apps-agent-") as directory:
            asyncio.run(run(Path(directory)))


if __name__ == "__main__":
    main()

"""Real, local stdio MCP fixture; tools/list waits for in-task discovery."""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server


async def serve(gate: Path, events: Path, receipt: str, timeout: float) -> None:
    server = Server("cache-probe-server")

    def record(event: str, **data) -> None:
        with events.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(),
                                     "event": event, "pid": os.getpid(), **data}) + "\n")

    @server.list_tools()
    async def list_tools():
        record("tools_list_requested", gate_exists=gate.exists())
        deadline = asyncio.get_running_loop().time() + timeout
        while not gate.exists():
            if asyncio.get_running_loop().time() >= deadline:
                record("gate_timeout")
                raise RuntimeError("Discovery gate did not open")
            await asyncio.sleep(0.05)
        record("tools_list_returned")
        return [types.Tool(
            name="cache_probe_echo",
            description="Read-only synthetic probe. Echo text and return a server-only receipt.",
            inputSchema={"type": "object", "properties": {"text": {"type": "string"}},
                         "required": ["text"], "additionalProperties": False},
        )]

    @server.call_tool()
    async def call_tool(name, arguments):
        if name != "cache_probe_echo" or set(arguments or {}) != {"text"} or not isinstance(arguments["text"], str):
            raise ValueError("Expected cache_probe_echo with string text")
        result = {"receipt": receipt, "echo": arguments["text"],
                  "sha256": hashlib.sha256(arguments["text"].encode()).hexdigest()}
        record("tool_executed", tool=name, arguments=arguments, result=result)
        return [types.TextContent(type="text", text=json.dumps(result))]

    record("server_started")
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        record("server_stopped")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate", type=Path, required=True)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args()
    asyncio.run(serve(args.gate, args.events, args.receipt, args.timeout))
